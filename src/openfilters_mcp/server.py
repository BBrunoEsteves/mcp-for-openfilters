"""OpenFilters MCP server.

Claude orchestrates; the server computes. Short calculations run in a small
interactive worker pool; campaigns run as background jobs across all cores,
write their results to disk, and return compact summaries.
"""

import asyncio
import concurrent.futures as cf
import csv
import datetime
import multiprocessing
import os
import random
import time
from typing import Annotated, Any

from pydantic import Field

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, engine, jobs, plotting, settings, tasks

INSTRUCTIONS = """\
OpenFilters thin-film optical coating design (Larouche & Martinu, Polytechnique Montréal).

Conventions: wavelengths and thicknesses in nm; angles in degrees; R, T, A are fractions 0-1;
phase in degrees, GD in fs, GDD in fs^2; complex index N = n - ik (k >= 0 absorbs).

A design is a JSON object:
  {"substrate": "FusedSilica", "incident_medium": "void", "exit_medium": "void",
   "center_wavelength": 550, "consider_backside": false, "substrate_thickness_mm": 1,
   "layers": [{"material": "TiO2", "thickness": 58.6}, {"material": "SiO2", "qwot": 1.0}, ...]}
Layers are listed FROM THE SUBSTRATE OUTWARD (first layer touches the substrate). A layer may give
"thickness" (nm) or "qwot" (quarter-wave optical thickness at center_wavelength); mixtures need "index";
optional flags refine_thickness / add_needles (default true). Instead of layers, a stack formula:
  "formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}  (letters = quarter waves).

A target is a JSON object (used by evaluate/optimize/tolerance):
  {"kind": "R"|"T"|"A"|"R_phase"|"T_phase"|"R_GD"|"T_GD"|"R_GDD"|"T_GDD"|"R_color"|"T_color",
   "angle": 0, "polarization": "unpolarized"|"s"|"p",
   single wavelength:  "wavelength": 1064, "value": 0.995, "tolerance": 0.001,
   or a band:          "from": 400, "to": 700, "step": 5, "value": 0.9 (or "points": [[wl, value, tol], ...]),
   "inequality": "equal" | ">=" | "<=",  "direction": "normal"|"reverse"}
Use ">=" / "<=" for specifications like "R > 99 %". Phase/GD/GDD targets need s or p polarization.
Color targets: {"kind": "R_color", "color_space": "L*a*b*", "values": [L, a, b], "tolerances": [1, 1, 1]}.

Workflow: list_materials -> describe_design / compute_spectrum -> evaluate_design -> optimize_design
(refine = thickness refinement, needle = adds layers, step = graded index) -> tolerance_analysis.
For many cases use run_batch (sweeps) and run_optimization_campaign (many starts, ranked, optional
yield analysis); they return a job_id: poll job_status, then get_results.
Plots are also saved as PNG files (paths in "plot_files"); some chat apps do not display tool images to
the user, so always give the user the plot file paths. Results, CSV tables and
.ofp projects (openable in the OpenFilters GUI) are written to the data folder shown by server_info.
"""

mcp = MCPServer("OpenFilters", instructions=INSTRUCTIONS, version=__version__)

_interactive = None
_manager = None


def interactive_pool():
    global _interactive
    if _interactive is None:
        _interactive = cf.ProcessPoolExecutor(2, mp_context=multiprocessing.get_context("spawn"),
                                              initializer=tasks.worker_init)
    return _interactive


def manager():
    global _manager
    if _manager is None:
        _manager = jobs.JobManager()
    return _manager


async def compute(op, **args):
    """Run an engine operation in the interactive worker pool."""
    try:
        return await asyncio.wrap_future(interactive_pool().submit(tasks.run, op, args))
    except engine.EngineError as error:
        raise ToolError(str(error)) from error
    except cf.process.BrokenProcessPool as error:
        global _interactive
        _interactive = None
        raise ToolError("the calculation process crashed; please retry") from error


def checked(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except engine.EngineError as error:
        raise ToolError(str(error)) from error


def outputs_dir():
    path = os.path.join(settings.home(), "outputs")
    os.makedirs(path, exist_ok=True)
    return path


def stamp(name):
    return os.path.join(outputs_dir(), datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + name)


def with_plots(out, pngs, name):
    """Return the result plus the plots, and save each plot as a PNG so the
    user can open it (some clients show tool images only to the model)."""
    if isinstance(pngs, bytes):
        pngs = [pngs]
    paths = []
    for i, png in enumerate(pngs):
        path = stamp(f"{name}{'' if len(pngs) == 1 else f'_{i + 1}'}.png")
        with open(path, "wb") as outfile:
            outfile.write(png)
        paths.append(path)
    out["plot_files"] = paths
    return [out] + [Image(data=png, format="png") for png in pngs]


Design = Annotated[dict[str, Any], Field(description="Design object (see server instructions): substrate, media, layers from the substrate outward or a formula.")]
Targets = Annotated[list[dict[str, Any]], Field(description="List of target objects (see server instructions).")]
Wavelengths = Annotated[dict[str, float] | list[float] | None,
                        Field(description='{"from": 380, "to": 1100, "step": 1} or an explicit list of wavelengths (nm).')]


# ---------------------------------------------------------------------------
# Server and materials
# ---------------------------------------------------------------------------

@mcp.tool()
async def server_info() -> dict:
    """Version, data folder, worker count and whether the compiled C++ core is in use."""
    accel = await compute("acceleration_status")
    return {"version": __version__, "data_folder": settings.home(), "workers": settings.workers(),
            **accel, "materials_folder": settings.materials_dir(), "jobs_folder": settings.jobs_dir()}


@mcp.tool()
async def list_materials(name_filter: str | None = None) -> dict:
    """List available materials (built-in OpenFilters library plus the user's own) with n,k at a few
    wavelengths. Mixtures (variable composition) report their index range at 550 nm."""
    return {"materials": await compute("list_materials", name_filter=name_filter)}


@mcp.tool()
async def get_material_index(name: str, wavelengths: Wavelengths = None, plot: bool = False):
    """Tabulate n and k of a regular material over wavelengths (default 300-2000 nm, 50 nm steps)."""
    wvls = engine._wavelength_list(wavelengths, default=(300.0, 2000.0, 50.0)) if wavelengths is not None \
        else engine._wavelength_list(None, default=(300.0, 2000.0, 50.0))
    N = await compute("material_N", name=name, wavelengths=wvls)
    table = [[w, round(z.real, 5), round(-z.imag, 6)] for w, z in zip(wvls, N)]
    out = {"material": name, "columns": ["wavelength_nm", "n", "k"], "rows": table}
    if plot:
        png = plotting.curves(wvls, {"n": [r[1] for r in table], "k": [r[2] for r in table]},
                              f"{name} optical constants", "Wavelength (nm)", "n, k")
        return with_plots(out, png, f"index_{name}")
    return out


@mcp.tool()
async def add_material(name: str, model: str, description: str = "",
                       n: float | None = None, k: float = 0.0,
                       coefficients: list[float] | None = None,
                       table: list[list[float]] | None = None,
                       table_file: str | None = None,
                       wavelength_unit: str = "nm", overwrite: bool = False) -> dict:
    """Add a material to the user's material folder.

    model = "constant" (n, k) | "cauchy" (coefficients [A, B, C] with n = A + B/l^2 + C/l^4, l in um,
    optionally + [Ak, exponent, edge_nm] Urbach absorption) | "sellmeier" ([B1, C1, B2, C2, B3, C3],
    C in um^2, optional same 3 absorption terms) | "table" (rows [wavelength, n, k] in `table`, or a
    CSV/whitespace text file `table_file` with columns wavelength, n, k; wavelength_unit nm|um|eV).
    k is the positive extinction coefficient."""
    if table_file:
        table = _read_table_file(table_file)
    return await compute("add_material", name=name, model=model, description=description, n=n, k=k,
                         coefficients=coefficients, table=table, wavelength_unit=wavelength_unit,
                         overwrite=overwrite)


def _read_table_file(path):
    rows = []
    try:
        with open(path, encoding="utf-8-sig") as infile:
            for line in infile:
                parts = line.replace(",", " ").replace(";", " ").replace("\t", " ").split()
                try:
                    values = [float(p) for p in parts]
                except ValueError:
                    continue  # header or comment
                if len(values) >= 2:
                    rows.append(values[:3])
    except OSError as error:
        raise ToolError(f"cannot read {path}: {error}") from error
    if len(rows) < 2:
        raise ToolError(f"no numeric rows found in {path}")
    return rows


# ---------------------------------------------------------------------------
# Single-design calculations
# ---------------------------------------------------------------------------

@mcp.tool()
async def describe_design(design: Design, plot_index_profile: bool = False):
    """Validate a design and list its layers (thickness, index and QWOT at the center wavelength),
    total thickness and the normalized design JSON (e.g. a formula expanded into layers)."""
    out = await compute("describe", design=design)
    if plot_index_profile:
        return with_plots(out, plotting.index_profile(out["layers"]), "index_profile")
    return out


def _summarize_series(wvls, values, n_points):
    import numpy as np
    i_min, i_max = int(np.argmin(values)), int(np.argmax(values))
    summary = {"min": round(float(values[i_min]), 6), "at_nm_min": float(wvls[i_min]),
               "max": round(float(values[i_max]), 6), "at_nm_max": float(wvls[i_max]),
               "mean": round(float(np.mean(values)), 6)}
    if n_points:
        idx = np.unique(np.linspace(0, len(wvls) - 1, min(n_points, len(wvls))).round().astype(int))
        summary["samples"] = [[float(wvls[i]), round(float(values[i]), 6)] for i in idx]
    return summary


@mcp.tool()
async def compute_spectrum(design: Design,
                           quantities: list[str] = ["R", "T"],
                           wavelengths: Wavelengths = None,
                           angles: list[float] = [0.0],
                           polarizations: list[str] = ["unpolarized"],
                           reverse: bool = False,
                           sample_points: int = 25,
                           plot: bool = True,
                           save_csv: bool = False):
    """Compute R, T, A, phase, GD or GDD spectra versus wavelength for one design at one or more
    angles and polarizations. Returns, per series, min/max/mean and `sample_points` evenly spaced
    samples (set sample_points=0 for statistics only); full data go to a CSV when save_csv=true.
    reverse=true illuminates from the substrate side (R, T, A only)."""
    result = await compute("spectra", design=design, quantities=quantities, wavelengths=wavelengths,
                           angles=angles, polarizations=polarizations, reverse=reverse)
    wvls = result["wavelengths"]
    out = {"wavelength_range_nm": [float(wvls[0]), float(wvls[-1])], "n_wavelengths": len(wvls), "series": []}
    for s in result["series"]:
        out["series"].append({"quantity": s["quantity"], "angle": s["angle"], "polarization": s["polarization"],
                              **_summarize_series(wvls, s["values"], sample_points)})
    if save_csv:
        path = stamp("spectrum.csv")
        with open(path, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.writer(outfile)
            writer.writerow(["wavelength_nm"] + [f"{s['quantity']}_{s['angle']:g}deg_{s['polarization']}"
                                                 for s in result["series"]])
            for i, w in enumerate(wvls):
                writer.writerow([w] + [s["values"][i] for s in result["series"]])
        out["csv"] = path
    if plot:
        return with_plots(out, plotting.spectra(result), "spectrum")
    return out


@mcp.tool()
async def compute_angle_scan(design: Design, wavelength: float, quantities: list[str] = ["R", "T"],
                             angles: Annotated[dict[str, float], Field(description='{"from": 0, "to": 85, "step": 1}')] = {"from": 0, "to": 85, "step": 1},
                             polarizations: list[str] = ["s", "p", "unpolarized"], plot: bool = True):
    """R/T/A (or phase) versus angle of incidence at a single wavelength."""
    angle_list = engine._wavelength_list(angles)
    if angle_list[-1] >= 90:
        raise ToolError("angles must stay below 90")
    result = await compute("spectra", design=design, quantities=quantities, wavelengths=[wavelength],
                           angles=angle_list, polarizations=polarizations)
    curves = {}
    for s in result["series"]:
        curves.setdefault(f"{s['quantity']} {s['polarization']}", []).append(float(s["values"][0]))
    out = {"wavelength_nm": wavelength, "angles": angle_list,
           "curves": {k: [round(v, 6) for v in vals] for k, vals in curves.items()}}
    if plot:
        png = plotting.curves(angle_list, curves, f"Angle scan at {wavelength:g} nm", "Angle of incidence (deg)", "Value")
        return with_plots(out, png, "angle_scan")
    return out


@mcp.tool()
async def compute_color(design: Design, angles: list[float] = [0.0], mode: str = "reflection",
                        polarization: str = "unpolarized", illuminant: str = "CIE-D65",
                        observer: str = "CIE-1931") -> dict:
    """Color of the coating in reflection or transmission: XYZ, xy, L*a*b*, LCh, sRGB and hex, at each
    angle (use several angles to see the color shift)."""
    return await compute("color", design=design, angles=angles, mode=mode, polarization=polarization,
                         illuminant=illuminant, observer=observer)


@mcp.tool()
async def compute_electric_field(design: Design, wavelength: float, angle: float = 0.0,
                                 polarization: str = "s", plot: bool = True):
    """Standing-wave |E|^2 inside the coating (relative to the incident wave), e.g. for laser-damage
    analysis. Returns the peak value and position and its value at each interface."""
    r = await compute("electric_field", design=design, wavelength=wavelength, angle=angle,
                      polarization=polarization)
    values = r["E_squared"]
    i_max = max(range(len(values)), key=values.__getitem__)
    boundary_values = []
    for b in r["layer_boundaries_nm"]:
        j = min(range(len(r["position_nm"])), key=lambda k: abs(r["position_nm"][k] - b))
        boundary_values.append(round(values[j], 5))
    out = {"peak_E_squared": round(values[i_max], 5), "peak_position_nm_from_substrate": r["position_nm"][i_max],
           "E_squared_at_layer_tops": boundary_values, "note": r["note"]}
    if plot:
        png = plotting.curves(r["position_nm"], {"|E|²": values}, f"Electric field at {wavelength:g} nm",
                              "Distance from substrate (nm)", "|E|² / |E_incident|²", r["layer_boundaries_nm"])
        return with_plots(out, png, "electric_field")
    return out


@mcp.tool()
async def compute_monitoring_curves(design: Design, wavelengths: list[float], angle: float = 0.0,
                                    polarization: str = "unpolarized", mode: str = "transmission",
                                    plot: bool = True):
    """Optical monitoring signal (R or T at fixed wavelengths) versus deposited thickness, as seen
    during deposition. Returns the signal at the end of each layer (turning-point/level-cut planning).
    The substrate backside is included unless the design sets "consider_backside_on_monitoring": false."""
    r = await compute("monitoring", design=design, wavelengths=wavelengths, angle=angle,
                      polarization=polarization, mode=mode)
    ends = {}
    for w, values in r["signal"].items():
        ends[w] = []
        for b in r["layer_boundaries_nm"]:
            j = min(range(len(r["deposited_thickness_nm"])), key=lambda k: abs(r["deposited_thickness_nm"][k] - b))
            ends[w].append(round(values[j], 5))
    out = {"mode": mode, "signal_at_layer_ends": ends, "layer_boundaries_nm": r["layer_boundaries_nm"],
           "includes_backside": r["includes_backside"]}
    if plot:
        png = plotting.curves(r["deposited_thickness_nm"], r["signal"], f"{mode.title()} monitoring",
                              "Deposited thickness (nm)", "Signal", r["layer_boundaries_nm"])
        return with_plots(out, png, "monitoring")
    return out


@mcp.tool()
async def evaluate_design(design: Design, targets: Targets) -> dict:
    """Merit (chi^2, OpenFilters' definition) of a design against targets, with per-target pass
    fraction and maximum deviation. all_targets_met is true when every point is within tolerance."""
    return await compute("evaluate", design=design, target_specs=targets)


@mcp.tool()
async def layer_sensitivity(design: Design, targets: Targets, relative_step: float = 0.01) -> dict:
    """Change in merit when each layer thickness is increased by relative_step (default 1 %), to find
    the layers that need the tightest deposition control."""
    return await compute("sensitivity", design=design, target_specs=targets, relative_step=relative_step)


# ---------------------------------------------------------------------------
# Optimization and campaigns (background jobs)
# ---------------------------------------------------------------------------

async def _wait(job_id, wait_seconds):
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        status = manager().status(job_id)
        if status["status"] != "running":
            break
        await asyncio.sleep(0.5)
    status = manager().status(job_id)
    if status["status"] == "running":
        return {**status, "note": "still running; call job_status / get_results later"}
    return manager().results(job_id, top_n=10)


@mcp.tool()
async def optimize_design(design: Design, targets: Targets, method: str = "needle",
                          options: Annotated[dict[str, Any] | None, Field(description=(
                              "time_limit (s, default 300), max_iterations, min_thickness (nm; guaranteed in the "
                              "result: thin layers are clamped or removed and the design re-refined with that bound), "
                              "max_layers, max_total_thickness (nm), needle_materials (list), nb_needles, nb_steps, "
                              "automatic (default true)"))] = None,
                          wait_seconds: float = 60.0) -> dict:
    """Optimize a design with OpenFilters: method "refine" (thicknesses only), "needle" (refine and
    insert new layers until no improvement) or "step" (graded/step index, needs a mixture material).
    Waits up to wait_seconds; if not finished, returns a job_id to poll. Returns the best design found
    (not merely the last), stop_reason (converged, time_limit, cycling, max_layers, ...), thinnest_layer_nm,
    and an .ofp project (start + optimized) openable in OpenFilters. One run finds a local optimum; for
    hard specifications use run_optimization_campaign (many starts)."""
    checked(engine.build_targets, targets)
    job_id = manager().submit("optimize", [{"op": "optimize", "params": {"method": method},
                                            "args": {"design": design, "target_specs": targets,
                                                     "method": method, "options": options}}],
                              meta={"design": design, "targets": targets}, label=f"optimize ({method})")
    out = await _wait(job_id, wait_seconds)
    if out.get("status") == "done":
        r = manager().result(job_id, 0)
        if not r.get("ok"):
            raise ToolError(r.get("error"))
        return {**{k: v for k, v in r["result"].items()}, "job_id": job_id, "outputs": out.get("outputs")}
    return out


def _expand(template, parameters, mode):
    import itertools
    names = list(parameters)
    if mode == "zip":
        lengths = {len(v) for v in parameters.values()}
        if len(lengths) > 1:
            raise ToolError("zip mode needs parameter lists of equal length")
        combos = list(zip(*parameters.values()))
    else:
        combos = list(itertools.product(*parameters.values()))
    if len(combos) > jobs.MAX_TASKS:
        raise ToolError(f"{len(combos)} combinations exceed the limit of {jobs.MAX_TASKS}")

    def substitute(obj, values):
        if isinstance(obj, str):
            if obj.startswith("$") and obj[1:] in values:
                return values[obj[1:]]
            for k, v in values.items():
                obj = obj.replace("${" + k + "}", str(v))
            return obj
        if isinstance(obj, list):
            return [substitute(x, values) for x in obj]
        if isinstance(obj, dict):
            return {k: substitute(v, values) for k, v in obj.items()}
        return obj

    return [(dict(zip(names, combo)), substitute(template, dict(zip(names, combo)))) for combo in combos]


@mcp.tool()
async def run_batch(operation: str,
                    template: Annotated[dict[str, Any], Field(description=(
                        'Arguments of the operation, where any value written "$name" (or "${name}" inside a '
                        'string) is replaced by the swept parameter `name`.'))],
                    parameters: Annotated[dict[str, list[Any]], Field(description='e.g. {"n_pairs": [4, 6, 8], "aoi": [0, 15, 30]}')],
                    mode: str = "grid", label: str = "") -> dict:
    """Parameter sweep run in parallel on all cores as a background job.

    operation: "spectrum" (args: design, quantities, wavelengths, angles, polarizations, metrics), "evaluate"
    (design, target_specs), "optimize" (design, target_specs, method, options), "tolerance", "color".
    For "spectrum", give `metrics` to reduce each spectrum to numbers, e.g.
    [{"name": "Rmin_vis", "quantity": "R", "from": 400, "to": 700, "stat": "min"}, {"name": "T633",
    "quantity": "T", "stat": "at", "wavelength": 633}] (stat: mean|min|max|std|at; optional angle/polarization).
    mode "grid" = all combinations, "zip" = parallel lists. Returns a job_id; then job_status/get_results
    (one row per case with parameters and results; full table in summary.csv)."""
    if operation not in tasks.OPS:
        raise ToolError(f"operation must be one of {', '.join(tasks.OPS)}")
    cases = _expand(template, parameters, mode)
    task_list = [{"op": operation, "params": params, "args": args} for params, args in cases]
    # Fail fast on the first case rather than after thousands of tasks.
    first = dict(task_list[0]["args"])
    if "design" in first:
        await compute("describe", design=first["design"])
    job_id = manager().submit("batch", task_list, meta={"operation": operation}, label=label or f"batch {operation}")
    return {"job_id": job_id, "tasks": len(task_list), "workers": manager().workers,
            "note": "poll job_status, then get_results"}


def _random_starts(spec, count, seed):
    rng = random.Random(seed)
    materials = spec["materials"]
    if len(materials) < 2:
        raise ToolError("random starts need at least 2 materials")
    lo, hi = spec.get("n_layers", [5, 20])
    t_lo, t_hi = spec.get("thickness", [20.0, 200.0])
    base = {k: v for k, v in spec.items() if k not in ("materials", "n_layers", "thickness")}
    starts = []
    for _ in range(count):
        n = rng.randint(int(lo), int(hi))
        offset = rng.randrange(len(materials))
        layers = [{"material": materials[(offset + i) % len(materials)], "thickness": round(rng.uniform(t_lo, t_hi), 2)}
                  for i in range(n)]
        starts.append({**base, "layers": layers})
    return starts


@mcp.tool()
async def run_optimization_campaign(targets: Targets,
                                    starts: Annotated[list[dict[str, Any]] | None, Field(description="Explicit starting designs.")] = None,
                                    random_starts: Annotated[dict[str, Any] | None, Field(description=(
                                        'Generate starts: {"count": 50, "materials": ["Ta2O5", "SiO2"], "n_layers": [5, 25], '
                                        '"thickness": [20, 200], "substrate": "BK7", "seed": 1, ...other design keys}'))] = None,
                                    methods: list[str] = ["needle"],
                                    options: dict[str, Any] | None = None,
                                    top_n: int = 10,
                                    tolerance: Annotated[dict[str, Any] | None, Field(description=(
                                        'Optional yield stage on the top_n designs: {"n_trials": 200, "thickness_error": '
                                        '{"type": "relative", "sigma": 0.01}, "index_error": {"sigma": 0.005}, "seed": 0}'))] = None,
                                    label: str = "") -> dict:
    """Many optimizations from different starting designs, in parallel; ranks results (targets met,
    then yield if a tolerance stage is requested, then merit, layer count, total thickness) and saves
    the top_n designs as an .ofp project. Returns a job_id."""
    checked(engine.build_targets, targets)
    designs = list(starts or [])
    if random_starts:
        spec = dict(random_starts)
        designs += _random_starts(spec, int(spec.pop("count", 20)), spec.pop("seed", None))
    if not designs:
        raise ToolError("give starts and/or random_starts")
    for m in methods:
        if m not in engine.METHODS:
            raise ToolError(f"unknown method {m}")
    await compute("describe", design=designs[0])
    task_list = [{"op": "optimize", "params": {"start": i, "method": m},
                  "args": {"design": d, "target_specs": targets, "method": m, "options": options}}
                 for i, d in enumerate(designs) for m in methods]
    meta = {"targets": targets, "top_n": int(top_n), "tolerance": tolerance}
    job_id = manager().submit("campaign", task_list, meta=meta, label=label or "optimization campaign")
    return {"job_id": job_id, "optimizations": len(task_list), "workers": manager().workers,
            "note": "poll job_status, then get_results"}


@mcp.tool()
async def tolerance_analysis(design: Design, targets: Targets, n_trials: int = 500,
                             thickness_error: Annotated[dict[str, Any] | None, Field(description=(
                                 '{"type": "relative"|"absolute", "sigma": 0.01 (fraction) or nm, '
                                 '"distribution": "normal"|"uniform", "systematic": 0.0 (common relative offset)}'))] = None,
                             index_error: Annotated[dict[str, Any] | None, Field(description='{"sigma": 0.005} relative index error, drawn per material per trial')] = None,
                             seed: int = 0, wait_seconds: float = 60.0) -> dict:
    """Monte Carlo yield: random thickness (and optional index) errors; a trial passes when every target
    is met. Runs in parallel; returns yield with 95 % interval, merit statistics and the failure rate
    of each target."""
    checked(engine.build_targets, targets)
    n_trials = int(n_trials)
    if not 1 <= n_trials <= 1000000:
        raise ToolError("n_trials must be between 1 and 1e6")
    chunks = max(1, min(manager().workers * 2, n_trials // 10 or 1))
    sizes = [n_trials // chunks + (1 if i < n_trials % chunks else 0) for i in range(chunks)]
    task_list = [{"op": "tolerance", "params": {"chunk": i},
                  "args": {"design": design, "target_specs": targets, "n_trials": size,
                           "thickness_error": thickness_error, "index_error": index_error, "seed": seed * 100003 + i}}
                 for i, size in enumerate(sizes) if size]
    job_id = manager().submit("tolerance", task_list, label="tolerance analysis")
    out = await _wait(job_id, wait_seconds)
    if out.get("status") == "done":
        return {"job_id": job_id, **out.get("summary", {})}
    return out


@mcp.tool()
async def job_status(job_id: str) -> dict:
    """Progress of a background job (tasks done/total, failures, ETA, output files when done)."""
    return checked(manager().status, job_id)


@mcp.tool()
async def list_jobs(limit: int = 20) -> dict:
    """Most recent background jobs, newest first."""
    return {"jobs": manager().list(limit)}


@mcp.tool()
async def get_results(job_id: str, top_n: int = 10, sort_by: str | None = None, descending: bool = False,
                      include_designs: bool = False) -> dict:
    """Result rows of a job (campaigns are ranked best first; batches in task order unless sort_by is
    given). Works on partial results while the job is running. include_designs adds the optimized
    design JSON of the returned rows."""
    out = checked(manager().results, job_id, top_n, sort_by, descending)
    if include_designs:
        for row in out["rows"]:
            r = manager().result(job_id, row["task"])
            if r.get("ok") and isinstance(r["result"], dict) and "design" in r["result"]:
                row["design"] = r["result"]["design"]
    return out


@mcp.tool()
async def cancel_job(job_id: str) -> dict:
    """Cancel the pending tasks of a background job."""
    return checked(manager().cancel, job_id)


# ---------------------------------------------------------------------------
# OpenFilters project files
# ---------------------------------------------------------------------------

@mcp.tool()
async def load_project(path: str) -> dict:
    """Read an OpenFilters .ofp project: returns its designs (as design JSON) and targets."""
    return await compute("load_project", path=path)


@mcp.tool()
async def save_project(path: str, designs: list[dict[str, Any]], targets: list[dict[str, Any]] | None = None,
                       comment: str = "") -> dict:
    """Write designs (and optional targets) to an .ofp project that opens in the OpenFilters GUI.
    A relative path is placed in the data folder's projects directory."""
    if not os.path.isabs(path):
        path = os.path.join(settings.projects_dir(), path)
    if not path.lower().endswith(".ofp"):
        path += ".ofp"
    return {"path": await compute("save_project", path=path, designs=designs, target_specs=targets, comment=comment)}


def main():
    try:
        mcp.run("stdio")
    finally:
        if _manager is not None:
            _manager.shutdown()
        if _interactive is not None:
            _interactive.shutdown(wait=False, cancel_futures=True)
