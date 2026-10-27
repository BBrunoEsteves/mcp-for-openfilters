"""OpenFilters MCP server.

Claude orchestrates; the server computes. Short calculations run in a small
interactive worker pool; campaigns run as background jobs across all cores,
write their results to disk, and return compact summaries.
"""

import asyncio
import concurrent.futures as cf
import csv
import datetime
import functools
import inspect
import json
import multiprocessing
import os
import random
import time
from typing import Annotated, Any

from pydantic import BeforeValidator, Field

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, engine, jobs, plotting, refractiveindex, settings, tasks

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
optional flags refine_thickness / add_needles (default true), refine_index / add_steps / preserve_ot (mixtures).
To KEEP A LAYER AS IT IS in every optimization (e.g. an electrode whose thickness is set by its resistance),
give it "fixed": true (= refine_thickness false, no needles in it).
Instead of layers, a stack formula:
  "formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}  (letters = quarter waves).
A formula symbol can be fixed too: "materials": {"H": "TiO2", "L": "SiO2", "I": {"material": "ITO", "fixed": true}}
(every layer of that material stays fixed, so a fixed material must not also be used by a free symbol).
Mixtures (kind "mixture" in list_materials, e.g. IdealMixture, n 1.5-2.4) have a fixed index range at
center_wavelength: a homogeneous layer gives "index" inside it. A graded (rugate, inhomogeneous) layer of a
mixture: {"material": "IdealMixture", "graded": {"steps": [k, ...], "step_thicknesses": [nm, ...]}}, k being the
composition steps of the mixture (get_material_index lists the n of each step; IdealMixture: n = 1.5 + 0.01 k), or
"indices_at_cw": [n, ...] instead of steps (rounded to the nearest step). Its thickness is the sum of the steps.
Or a generated profile (expanded into steps at the mixture's own resolution, step optical thickness exact):
  "graded": {"profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2, "periods": 16,
                         "apodization": {"type": "hann"}},
             "matching": {"substrate": "quintic", "medium": "quintic", "periods": 1}}
  rugate: ln n modulated sinusoidally in optical thickness (no harmonics at lambda0/2, lambda0/3; "modulate": "n" /
  "coordinate": "physical" for the naive form), period lambda0/2 with lambda0 = "wavelength" (default the center
  wavelength), or "n_mean" + "delta_n"; "phase_deg"; "harmonics": [{"wavelength", "amplitude", "phase_deg"}, ...] for
  several notches; apodization none | sin2 / hann | quintic | gaussian, over the whole layer or over "taper_periods" at
  each end. Ramps: {"type": "quintic" (Southwell) | "linear", "n_start", "n_end", and "thickness" (physical nm) or
  "optical_thickness" or "qwot"}. Custom: {"type": "custom", "points": [[position nm, n], ...], "coordinate":
  "physical" | "optical"}. "matching" adds ramps (optical thickness "periods" x lambda0/2) to the substrate side and
  the medium side, from the neighbouring index; beyond the mixture's range they stop at its end, with a warning
  giving the mismatch left. "resolution": "native" (default) or a coarser index step, e.g. 0.05.
Designs with many graded steps are written to files: pass {"design_file": path} as the design to any tool.
Optional design fields kept in .ofp projects: "description", "needle_materials" (names), "step_spacing",
"wavelength_range" ({"from", "to", "step"}: what the OpenFilters GUI analyses).
Tabulated materials (n,k tables) used OUTSIDE their data: the design field "table_extrapolation" says how n,k are
filled in there: "hold" (default: the values at the nearest end of the data), "openfilters" (OpenFilters' own
extrapolation, as its GUI does) or "refuse". Results then carry a warning giving the n,k used and what the other
choice would give: tell the user. Optimizations, campaigns, tolerance analyses and batches refuse to start while a
material is outside its data and the design does not set table_extrapolation: ask the user which they want (show
the two sets of values), then set it in the design. More than 1.5x beyond the data is refused unless it is set.

Coatings on BOTH faces of the substrate are ONE design (= one OpenFilters filter): put the second face in
"back_layers" (also listed from the substrate outward; or "back_formula") and set "consider_backside": true
(the substrate is treated as thick/incoherent and both faces enter every R/T/A/OD result). Example:
  {"substrate": "BK7", "substrate_thickness_mm": 1, "consider_backside": true,
   "layers": [{"material": "TiO2", "thickness": 90.0}], "back_layers": [{"material": "SiO2", "thickness": 140.0}]}
Never split one two-sided part into two designs. With consider_backside false (default) only the front face is
computed, but an uncoated back face's ~4 % reflection is then also ignored. Optimizers (OpenFilters) vary only
the FRONT layers; back layers stay fixed but count in the merit when consider_backside is true. To design both
faces, optimize each face as its own one-sided design, then combine them into one design and evaluate it.

A target is a JSON object (used by evaluate/optimize/tolerance):
  {"kind": "R"|"T"|"A"|"R_phase"|"T_phase"|"R_GD"|"T_GD"|"R_GDD"|"T_GDD"|"R_color"|"T_color",
   "angle": 0, "polarization": "unpolarized"|"s"|"p",
   single wavelength:  "wavelength": 1064, "value": 0.995, "tolerance": 0.001,
   or a band:          "from": 400, "to": 700, "step": 5, "value": 0.9 (or "points": [[wl, value, tol], ...]),
   "inequality": "equal" | ">=" | "<=",  "direction": "normal"|"reverse"}
Use ">=" / "<=" for specifications like "R > 99 %": such a target is met only where it holds (its tolerance,
default 0.01, only weights the merit; a convergence allowance of 1e-4 x tolerance applies, e.g. 1e-6 for R
with the default tolerance); an "equal" target is met within its tolerance. Phase/GD/GDD targets need
s or p polarization.
Color targets: {"kind": "R_color", "color_space": "Lab", "values": [L, a, b], "tolerances": [1, 1, 1]}
(color_space: Lab, XYZ, xyY, Luv, LChab or LChuv; illuminant and observer as in compute_color).

Materials missing from list_materials: search_refractiveindex then import_refractiveindex_material (downloads
public data from refractiveindex.info; prefer thin-film entries for coatings and give the user the references;
check that the entry's data cover the wavelengths needed, see table_extrapolation above).

Workflow: list_materials -> describe_design / compute_spectrum -> evaluate_design -> optimize_design
(refine = thickness refinement, needle = adds layers, step = graded index) -> tolerance_analysis.
For high blocking use the "OD" quantity (optical density, -log10 T) rather than T.
Temperature (tunable or athermal filters): thermal_sensitivity gives how a peak moves per index and thickness
change of each material, and with dn/dT, expansion and Poisson ratios, d(lambda)/dT; thermal_sweep follows
the peak over temperature changes.
Tool calls time out after about 60 s in some apps: wait_seconds is capped at 45 s; long jobs return a job_id.
For many cases use run_batch (sweeps) and run_optimization_campaign (many starts, ranked, optional
yield analysis); they return a job_id: poll job_status, then get_results.
Plots are also saved as PNG files (paths in "plot_files"); some chat apps do not display tool images to
the user, so ALWAYS give the user the plot file paths. optimize_design, evaluate_design and campaign
get_results plot the design(s) against the targets automatically. Results, CSV tables and
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
        args = engine.resolve_design_files(args)
        return await asyncio.wrap_future(interactive_pool().submit(tasks.run, op, args))
    except engine.EngineError as error:
        raise ToolError(str(error)) from error
    except cf.process.BrokenProcessPool as error:
        global _interactive
        _interactive = None
        raise ToolError("the calculation process crashed; please retry") from error
    except Exception as error:
        # An unexpected failure: say what it was, as "Error executing tool" alone cannot be diagnosed.
        raise ToolError(f"{op} failed: {type(error).__name__}: {error}") from error


def checked(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except engine.EngineError as error:
        raise ToolError(str(error)) from error
    except ToolError:
        raise
    except Exception as error:
        raise ToolError(f"{getattr(fn, '__name__', 'operation')} failed: {type(error).__name__}: {error}") from error


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


async def targets_plot(designs, targets, title, name):
    """Plot designs (list of (label, design, is_reference)) against the R/T/A
    targets: one figure per (angle, polarization) used by the targets.
    Returns (pngs, plot_files-ready) or ([], None) when nothing to draw."""
    photometric = [t for t in targets if t.get("kind") in ("R", "T", "A")]
    pngs = await _dispersive_plots(designs, targets, title)
    if not photometric:
        return pngs
    lo = min(t.get("from", t.get("wavelength", 1e9)) for t in photometric)
    hi = max(t.get("to", t.get("wavelength", 0)) for t in photometric)
    margin = max(0.15 * (hi - lo), 20.0)
    # Fine enough to show narrow dips between target grid points (the between-points check uses a 4x grid).
    wavelengths = {"from": max(lo - margin, 200.0), "to": hi + margin, "step": max((hi - lo + 2 * margin) / 2000, 0.05)}
    combos = []
    for t in photometric:
        key = (float(t.get("angle", 0.0)), str(t.get("polarization", "unpolarized")))
        if key not in combos:
            combos.append(key)
    quantities = sorted({t["kind"] for t in photometric}, key="RTA".index)
    photometric_pngs = []
    for angle, pol in combos[:3]:
        curves, wvls = [], None
        for label, design, reference in designs:
            r = await compute("spectra", design=design, quantities=quantities, wavelengths=wavelengths,
                              angles=[angle], polarizations=[pol])
            wvls = r["wavelengths"]
            for s in r["series"]:
                curves.append((f"{s['quantity']} {label}", s["quantity"], s["values"], reference))
        shown = [t for t in photometric if (float(t.get("angle", 0.0)), str(t.get("polarization", "unpolarized"))) == (angle, pol)]
        photometric_pngs.append(plotting.design_vs_targets(wvls, curves, shown, f"{title} — {angle:g}°, {pol}"))
    return photometric_pngs + pngs


async def _dispersive_plots(designs, targets, title):
    """Phase / GD / GDD targets: one figure per quantity and polarization, on a fine grid (GD and GDD
    are numerical derivatives)."""
    kinds = [k for k in ("R_phase", "T_phase", "R_GD", "T_GD", "R_GDD", "T_GDD") if any(t.get("kind") == k for t in targets)]
    pngs = []
    for kind in kinds:
        chosen = [t for t in targets if t.get("kind") == kind]
        lo = min(t.get("from", t.get("wavelength", 1e9)) for t in chosen)
        hi = max(t.get("to", t.get("wavelength", 0)) for t in chosen)
        pad = max(0.05 * (hi - lo), 5.0)
        wavelengths = {"from": lo - pad, "to": hi + pad, "step": min(1.0, (hi - lo + 2 * pad) / 200)}
        for angle, pol in sorted({(float(t.get("angle", 0.0)), str(t.get("polarization", "s"))) for t in chosen})[:2]:
            curves, wvls = [], None
            for label, design, reference in designs:
                r = await compute("spectra", design=design, quantities=[kind], wavelengths=wavelengths,
                                  angles=[angle], polarizations=[pol])
                wvls = r["wavelengths"]
                curves += [(f"{kind} {label}", kind, s["values"], reference) for s in r["series"]]
            shown = [t for t in chosen if (float(t.get("angle", 0.0)), str(t.get("polarization", "s"))) == (angle, pol)]
            pngs.append(plotting.design_vs_targets(wvls, curves, shown, f"{title} — {kind}, {angle:g}°, {pol}",
                                                   ylabel=plotting.UNITS.get(kind, kind), photometric=False))
    return pngs


def _design_from_file(value):
    """A design given as {"design_file": path} (written by load_project / describe_design)."""
    try:
        return engine.resolve_design_files(value)
    except engine.EngineError as error:
        raise ValueError(str(error)) from error


# The formats are spelled out here too, not only in the server instructions: some apps do not
# show those instructions to the model, but every app passes on parameter descriptions.
Design = Annotated[dict[str, Any], BeforeValidator(_design_from_file), Field(description=(
    'Design: {"substrate": "BK7", "incident_medium": "void", "center_wavelength": 550, "substrate_thickness_mm": 1, '
    '"layers": '
    '[{"material": "TiO2", "thickness": 58.6}, {"material": "SiO2", "qwot": 1}]} with layers listed FROM THE '
    'SUBSTRATE OUTWARD (thickness in nm, or qwot = quarter waves at center_wavelength), or instead of layers '
    '"formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}. A layer that optimizations must '
    'not change gets "fixed": true; in a formula, a symbol {"material": "ITO", "fixed": true}. A part coated on both '
    'faces is ONE design: add "back_layers" and "consider_backside": true. A graded (rugate) layer of a mixture: '
    '{"material": "IdealMixture", "graded": {"steps": [k, ...], "step_thicknesses": [nm, ...]}}, k being the '
    'composition steps of the mixture (get_material_index lists the n of each step), or "indices_at_cw": [n, ...] '
    'instead of steps (rounded to the nearest step), or a generated profile "graded": {"profile": {"type": "rugate", '
    '"n_min": 1.5, "n_max": 2.2, "periods": 16, "apodization": {"type": "hann"}}, "matching": {"substrate": "quintic", '
    '"medium": "quintic"}} (ln n sinusoidal in optical thickness; types rugate, quintic, linear (n_start, n_end, '
    'thickness/optical_thickness/qwot), custom (points [[nm, n], ...]); see the server instructions); a homogeneous mixture layer gives "index" within the range of '
    'the mixture at center_wavelength. Tabulated materials used beyond their data: "table_extrapolation": '
    '"hold" (default) | "openfilters" | "refuse". A long design can be passed as {"design_file": path} (load_project and describe_design '
    'write them).'))]
Targets = Annotated[list[dict[str, Any]], Field(description=(
    'Targets, each {"kind": "R"|"T"|"A"|"R_phase"|"T_phase"|"R_GD"|"T_GD"|"R_GDD"|"T_GDD", "angle": 0, '
    '"polarization": "unpolarized"|"s"|"p", then either "wavelength": 1064 or "from": 400, "to": 700, "step": 5, '
    'and "value": 0.99 (R/T/A as fractions), "inequality": ">="|"<="|"equal", optional "tolerance" (weight in the '
    'merit, default 0.01). Colour: {"kind": "R_color"|"T_color", "color_space": "Lab", "values": [L, a, b], '
    '"tolerances": [1, 1, 1], "angle": 0}. An inequality is met only where it holds (within 1e-4 x tolerance).'))]
# Everything runs locally (open_world_hint False) except the refractiveindex.info tools (WEB_*), which
# download public data. Read-only tools only save plots, CSV tables and caches in the data folder; job tools
# also write job files there; DESTRUCTIVE tools may replace a file or stop work.
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
JOB = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
WEB_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WEB_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
Wavelengths = Annotated[dict[str, float] | list[float] | None,
                        Field(description='{"from": 380, "to": 1100, "step": 1} or an explicit list of wavelengths (nm).')]


# ---------------------------------------------------------------------------
# Server and materials
# ---------------------------------------------------------------------------

@mcp.tool(title="Server information", annotations=READ)
async def server_info() -> dict:
    """Version, build (git commit of a source checkout, and when this server started: a server started
    before a code change still runs the old code until the app is restarted), data folder, worker count
    and whether the compiled C++ core is in use."""
    accel = await compute("acceleration_status")
    return {"version": __version__, "build": BUILD, "started": STARTED, "data_folder": settings.home(),
            "workers": settings.workers(), **accel, "materials_folder": settings.materials_dir(),
            "jobs_folder": settings.jobs_dir()}


def _build():
    """The git commit this server runs from ("+changes" with uncommitted edits), or "release" for an
    installed package."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not os.path.isdir(os.path.join(root, ".git")):
        return "release"
    try:
        run = functools.partial(subprocess.run, cwd=root, capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console flash
        commit = run(["git", "log", "-1", "--format=%h %cs"]).stdout.strip()
        changed = run(["git", "status", "--porcelain", "--untracked-files=no", "--", "src"]).stdout.strip()
        return f"{commit}{' +changes' if changed else ''}" if commit else "source checkout"
    except (OSError, subprocess.SubprocessError):
        return "source checkout"


BUILD = _build()
STARTED = datetime.datetime.now().isoformat(timespec="seconds")


@mcp.tool(title="List materials", annotations=READ)
async def list_materials(name_filter: str | None = None) -> dict:
    """List available materials (built-in OpenFilters library plus the user's own) with n,k at a few
    wavelengths. Mixtures (variable composition) report their index range at 550 nm."""
    return {"materials": await compute("list_materials", name_filter=name_filter)}


@mcp.tool(title="Material optical constants", annotations=READ)
async def get_material_index(name: str, wavelengths: Wavelengths = None, plot: bool = False):
    """Tabulate n and k of a regular material over wavelengths (default 300-2000 nm, 50 nm steps).
    For a mixture (kind "mixture" in list_materials): its composition steps, the index of each step k at
    each wavelength (default 550 nm; up to 10 wavelengths), as used by graded layers."""
    if await compute("is_mixture", name=name):
        wvls = [550.0] if wavelengths is None else engine._wavelength_list(wavelengths)
        if len(wvls) > 10:
            raise ToolError("a mixture's steps are listed at up to 10 wavelengths")
        out = await compute("mixture_table", name=name, wavelengths=wvls)
        if plot:
            png = plotting.curves([r[0] for r in out["rows"]],
                                  {c: [r[i + 1] for r in out["rows"]] for i, c in enumerate(out["columns"][1:])},
                                  f"{name} composition steps", "Step", "n")
            return with_plots(out, png, f"steps_{name}")
        return out
    wvls = engine._wavelength_list(wavelengths, default=(300.0, 2000.0, 50.0)) if wavelengths is not None \
        else engine._wavelength_list(None, default=(300.0, 2000.0, 50.0))
    warnings = await compute("material_warnings", design={"substrate": name, "layers": []}, wavelengths=wvls)
    N = await compute("material_N", name=name, wavelengths=wvls)
    table = [[w, round(float(z.real), 5), round(float(-z.imag), 6) + 0.0] for w, z in zip(wvls, N)]  # no -0.0
    out = {"material": name, "columns": ["wavelength_nm", "n", "k"], "rows": table}
    if warnings:
        out["warnings"] = warnings
    if plot:
        png = plotting.curves(wvls, {"n": [r[1] for r in table], "k": [r[2] for r in table]},
                              f"{name} optical constants", "Wavelength (nm)", "n, k")
        return with_plots(out, png, f"index_{name}")
    return out


@mcp.tool(title="Add a material", annotations=DESTRUCTIVE)
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


@mcp.tool(title="Remove a material", annotations=DESTRUCTIVE)
async def remove_material(name: str) -> dict:
    """Delete one of the user's own materials (from add_material or import_refractiveindex_material).
    Built-in OpenFilters materials cannot be removed; if a user material had the same name as a
    built-in one, the built-in one is used again. Designs and projects that use it will no longer load."""
    return await compute("remove_material", name=name)


@mcp.tool(title="Search refractiveindex.info", annotations=WEB_READ)
async def search_refractiveindex(query: str, shelf: str | None = None, limit: int = 25,
                                 refresh: bool = False) -> dict:
    """Search the refractiveindex.info database of optical constants (public domain) by material
    name, formula or author, e.g. "Ta2O5", "silicon nitride", "HfO2 thin film", "N-BK7". Each result
    gives a `page` id to pass to import_refractiveindex_material (pass it exactly, spaces included), the
    source (author, year, range) and the database's category for it, e.g. "Thin film", "Amorphous thin
    film" or "Experimental data: bulk, thick film": thin-film data usually suit coatings best. shelf limits the search: main (inorganic), organic, glass, other, specs (supplier data sheets),
    popular_glass. The catalogue is downloaded once and cached for 30 days (refresh=true updates it)."""
    return await asyncio.to_thread(checked, refractiveindex.search, query, shelf, limit, refresh)


@mcp.tool(title="Import a refractiveindex.info material", annotations=WEB_WRITE)
async def import_refractiveindex_material(page: str, name: str | None = None,
                                          from_nm: float | None = None, to_nm: float | None = None,
                                          overwrite: bool = False, plot: bool = False):
    """Download one refractiveindex.info entry (`page` from search_refractiveindex) and add it to the
    user's materials as an n,k table, usable in designs like any other material. Tabulated data and
    dispersion formulas are both converted; k = 0 when the entry gives none. name defaults to
    <book>_<page> (e.g. Ta2O5_Bright-amorphous); from_nm/to_nm crop the table. Returns the material,
    its wavelength range, the references (cite them when using the data) and the web page."""
    doc = await asyncio.to_thread(checked, refractiveindex.load_page, page)
    notes = []
    table = checked(refractiveindex.to_table, doc, from_nm, to_nm, notes)
    page = page.strip().removesuffix(".yml")
    name = name or refractiveindex.default_name(page)
    summary = await compute("add_material", name=name, model="table", table=table, wavelength_unit="nm",
                            description=f"refractiveindex.info {page}", overwrite=overwrite)
    out = {"material": summary, "wavelength_range_nm": [table[0][0], table[-1][0]], "points": len(table),
           **({"notes": notes} if notes else {}),
           "absorbing": any(row[2] > 0 for row in table),
           "absorbing_ranges_nm": _crossing_ranges([r[0] for r in table], [r[2] for r in table], 1e-6),
           "references": refractiveindex.references(doc),
           "comments": refractiveindex._plain(doc.get("COMMENTS", "")), "web_page": refractiveindex.page_url(page)}
    if plot:
        wvls = [row[0] for row in table]
        png = plotting.curves(wvls, {"n": [row[1] for row in table], "k": [row[2] for row in table]},
                              f"{name} (refractiveindex.info)", "Wavelength (nm)", "n, k")
        return with_plots(out, png, f"index_{name}")
    return out


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

@mcp.tool(title="Describe a design", annotations=READ)
async def describe_design(design: Design, plot_index_profile: bool = False):
    """Validate a design and list its layers (thickness, index and QWOT at the center wavelength; graded
    layers: number of steps, index range, first and last index, optical thickness), total thickness, the
    substrate and incident medium indices, and the normalized design JSON (e.g. a formula expanded into
    layers; omitted for formulas of more than 20 layers, which can be passed as they are; written to a
    design_file when graded layers have many steps). Two-sided designs also list back_layers (with a
    warning if consider_backside is false). plot_index_profile draws n at the center wavelength versus
    depth, graded layers step by step, with the substrate and the incident medium at the ends."""
    out = await compute("describe", design=design)
    profile = out.pop("index_profile")
    warnings = await compute("material_warnings", design=design, wavelengths=[out["center_wavelength"]])
    if warnings:
        out["warnings"] = warnings
    if (design.get("formula") or design.get("back_formula")) and out["nb_layers"] + out.get("nb_back_layers", 0) > 20:
        # Hundreds of near-identical entries repeating the layer table (scenario S4.9).
        del out["design"]
        out["design_note"] = ("normalized design JSON omitted for this long stack: pass the formula itself to "
                              "every tool; the layers above give each layer's thickness")
    elif engine.graded_steps_count(out["design"]) > GRADED_INLINE_STEPS:
        steps = engine.graded_steps_count(out["design"])
        path = _write_design(out["design"], "design")
        out["design"] = {"design_file": path}
        out["design_note"] = (f"the normalized design has {steps} graded steps, so it is in design_file: pass "
                              f'{{"design_file": "{path}"}} as the design to any tool')
    if plot_index_profile:
        title = design.get("description") or design.get("name") or "Index profile"
        png = plotting.index_profile(profile, out["substrate_n@cw"], out["incident_medium_n@cw"],
                                     out["center_wavelength"], title[:80])
        return with_plots(out, png, "index_profile")
    return out


def _plot_title(design, kind):
    """The design's own description or name, else what it is and its center wavelength."""
    named = design.get("description") or design.get("name")
    if named:
        return str(named)[:80]
    return f"{kind} (center wavelength {float(design.get('center_wavelength', 550)):g} nm)"


GRADED_INLINE_STEPS = 50  # more graded steps than this: designs go to a file, not into the conversation


def _write_design(design, name):
    """Write a design to a JSON file in the outputs folder (compact) and return its path."""
    path = stamp(f"{name}.json")
    with open(path, "w", encoding="utf-8") as outfile:
        json.dump(design, outfile, separators=(",", ":"))
    return path


def sig(value, digits=6):
    """Round to significant figures, so tiny values (deep blocking) are not
    reported as 0."""
    return float(f"{float(value):.{digits}g}")


_crossing_ranges = engine.crossing_ranges

def _summarize_series(x, values, n_points, axis="nm", threshold=None):
    """Statistics with 8 significant figures (99.99955 % must not read 0.999996), the position of
    min and max, evenly spaced samples, and optional threshold ranges / FWHM."""
    import numpy as np
    i_min, i_max = int(np.argmin(values)), int(np.argmax(values))
    summary = {"min": sig(values[i_min], 8), f"at_{axis}_min": round(float(x[i_min]), 6),
               "max": sig(values[i_max], 8), f"at_{axis}_max": round(float(x[i_max]), 6),
               "mean": sig(np.mean(values), 8)}
    if n_points:
        idx = np.unique(np.linspace(0, len(x) - 1, min(n_points, len(x))).round().astype(int))
        summary["samples"] = [[round(float(x[i]), 6), sig(values[i], 8)] for i in idx]
    if threshold:
        if "above" in threshold:
            summary[f"ranges_above_{threshold['above']:g}"] = _crossing_ranges(x, values, float(threshold["above"]))
        if "below" in threshold:
            summary[f"ranges_below_{threshold['below']:g}"] = _crossing_ranges(x, values, float(threshold["below"]), False)
        if threshold.get("half_max"):
            summary.update(engine.half_max_summary(x, values, threshold, axis))
    return summary


@mcp.tool(title="Compute spectrum", annotations=READ)
async def compute_spectrum(design: Design,
                           quantities: list[str] = ["R", "T"],
                           wavelengths: Wavelengths = None,
                           angles: list[float] = [0.0],
                           polarizations: list[str] = ["unpolarized"],
                           reverse: bool = False,
                           sample_points: int = 25,
                           threshold: Annotated[dict[str, Any] | None, Field(description=(
                               '{"above": 0.999} or {"below": 0.01}: ranges where each series is above/below the '
                               'level, edges interpolated (e.g. the high-reflection band of a mirror); {"half_max": true}: '
                               'FWHM, peak and half-maximum centre of the highest peak (e.g. a band-pass); add "near": 532 '
                               'for the local peak closest to 532 nm (not a higher ripple at a band edge) or "from"/"to" '
                               'to search the peak only there. If an edge lies beyond the computed range, fwhm is null '
                               'and fwhm_at_least gives the width seen.'))] = None,
                           plot: bool = True,
                           save_csv: bool = False):
    """Compute R, T, A, OD, phase, GD or GDD spectra (quantity names R, T, A, OD, R_phase, T_phase, R_GD,
    T_GD, R_GDD, T_GDD) versus wavelength for one design at one or more
    angles and polarizations. Returns, per series, min/max/mean and `sample_points` evenly spaced
    samples (set sample_points=0 for statistics only); full data go to a CSV when save_csv=true.
    reverse=true illuminates from the substrate side (R, T, A, OD only). Quantity "OD" is optical density
    (-log10 T), best for blocking specs; summary values have 8 significant figures. GD (fs) and GDD (fs^2) are
    numerical derivatives over the wavelengths requested: use steps of 5 nm or less. Two-sided designs
    (back_layers + consider_backside: true) are computed with both faces."""
    result = await compute("spectra", design=design, quantities=quantities, wavelengths=wavelengths,
                           angles=angles, polarizations=polarizations, reverse=reverse)
    wvls = result["wavelengths"]
    out = {"wavelength_range_nm": [float(wvls[0]), float(wvls[-1])], "n_wavelengths": len(wvls), "series": []}
    if result.get("warnings"):
        out["warnings"] = result["warnings"]
    for s in result["series"]:
        out["series"].append({"quantity": s["quantity"], "angle": s["angle"], "polarization": s["polarization"],
                              **_summarize_series(wvls, s["values"], sample_points, threshold=threshold)})
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
        return with_plots(out, plotting.spectra(result, _plot_title(design, "Spectrum")), "spectrum")
    return out


@mcp.tool(title="Angle scan", annotations=READ)
async def compute_angle_scan(design: Design, wavelength: float, quantities: list[str] = ["R", "T"],
                             angles: Annotated[dict[str, float], Field(description='{"from": 0, "to": 85, "step": 1}')] = {"from": 0, "to": 85, "step": 1},
                             polarizations: list[str] = ["s", "p", "unpolarized"], sample_points: int = 25,
                             threshold: Annotated[dict[str, Any] | None, Field(description=(
                                 'as in compute_spectrum, over angle'))] = None, plot: bool = True):
    """R/T/A (or phase) versus angle of incidence at a single wavelength. Per curve: min/max with the angle
    where each occurs (e.g. Brewster's angle is at_deg_min of "R p"), mean, `sample_points` evenly spaced
    samples (0 for statistics only) and optional threshold ranges."""
    angle_list = engine._wavelength_list(angles)
    if angle_list[-1] >= 90:
        raise ToolError("angles must stay below 90")
    result = await compute("spectra", design=design, quantities=quantities, wavelengths=[wavelength],
                           angles=angle_list, polarizations=polarizations)
    curves = {}
    for s in result["series"]:
        curves.setdefault(f"{s['quantity']} {s['polarization']}", []).append(float(s["values"][0]))
    out = {"wavelength_nm": wavelength, "angle_range_deg": [angle_list[0], angle_list[-1]], "n_angles": len(angle_list),
           "curves": {k: _summarize_series(angle_list, vals, sample_points, axis="deg", threshold=threshold)
                      for k, vals in curves.items()}}
    if result.get("warnings"):
        out["warnings"] = result["warnings"]
    if plot:
        png = plotting.curves(angle_list, curves, f"Angle scan at {wavelength:g} nm", "Angle of incidence (deg)", "Value")
        return with_plots(out, png, "angle_scan")
    return out


@mcp.tool(title="Coating color", annotations=READ)
async def compute_color(design: Design, angles: list[float] = [0.0], mode: str = "reflection",
                        polarization: str = "unpolarized", illuminant: str = "CIE-D65",
                        observer: str = "CIE-1931") -> dict:
    """Color of the coating in reflection or transmission: XYZ, xy, L*a*b*, LCh, sRGB and hex, at each
    angle (use several angles to see the color shift)."""
    return await compute("color", design=design, angles=angles, mode=mode, polarization=polarization,
                         illuminant=illuminant, observer=observer)


@mcp.tool(title="Electric field in the coating", annotations=READ)
async def compute_electric_field(design: Design, wavelength: float, angle: float = 0.0,
                                 polarization: str = "s", plot: bool = True):
    """Standing-wave |E|^2 inside the coating (relative to the incident wave), e.g. for laser-damage
    analysis, sampled every 1 nm inside every layer (graded layers too). Returns the peak value and
    position, its value at each interface and, for graded layers, the peak inside each one."""
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
    if r.get("graded_layer_peaks"):
        out["graded_layer_peaks"] = [{"layer": p["layer"], "E_squared": round(p["E_squared"], 5),
                                      "position_nm": round(p["position_nm"], 2)} for p in r["graded_layer_peaks"]]
    if r.get("warnings"):
        out["warnings"] = r["warnings"]
    if plot:
        png = plotting.curves(r["position_nm"], {"|E|²": values}, f"Electric field at {wavelength:g} nm",
                              "Distance from substrate (nm)", "|E|² / |E_incident|²", r["layer_boundaries_nm"])
        return with_plots(out, png, "electric_field")
    return out


@mcp.tool(title="Optical monitoring curves", annotations=READ)
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
            ends[w].append(sig(values[j]))  # high reflectors end near T = 1e-6
    out = {"mode": mode, "signal_at_layer_ends": ends, "layer_boundaries_nm": [round(b, 3) for b in r["layer_boundaries_nm"]],
           "includes_backside": r["includes_backside"]}
    if r.get("warnings"):
        out["warnings"] = r["warnings"]
    if plot:
        png = plotting.curves(r["deposited_thickness_nm"], r["signal"], f"{mode.title()} monitoring",
                              "Deposited thickness (nm)", "Signal", r["layer_boundaries_nm"])
        return with_plots(out, png, "monitoring")
    return out


Thermal = Annotated[dict[str, Any], Field(description=(
    'Thermal properties: {"materials": {"TiO2": {"dn_dT": -1e-4, "cte": 5e-6, "poisson": 0.27, "dk_dT": 0}, '
    '"SiO2": {"dn_dT": 1e-5, "cte": 0.5e-6, "poisson": 0.17}}, "substrate_cte": 0.55e-6, "clamped": true}, per '
    'kelvin. dn_dT depends strongly on the deposition process (amorphous TiO2 ranges from about -2e-4 to +1e-4): '
    'use measured values when available and say which values were assumed. A film clamped to its substrate '
    '(default) changes thickness by (1+nu)/(1-nu)*cte - 2nu/(1-nu)*substrate_cte per kelvin (needs poisson and '
    'substrate_cte: fused silica 0.55e-6, BK7 7.1e-6); "clamped": false uses cte alone. Materials left out count as '
    '0. Not included: the elasto-optic effect and the substrate\'s own dn/dT.'))]
PeakWindow = Annotated[dict[str, float] | None, Field(description=(
    'Where to look for the peak first, {"from": 507, "to": 557, "step": 0.05}; default near +/- 25 nm. It is '
    'widened if the peak does not fit and then refined to FWHM/500.'))]


@mcp.tool(title="Thermal sensitivity of a peak", annotations=READ)
async def thermal_sensitivity(design: Design, near: float | None = None, quantity: str = "T",
                              thermal: Thermal | None = None, window: PeakWindow = None, angle: float = 0.0,
                              polarization: str = "unpolarized") -> dict:
    """How the peak of R or T nearest `near` (default the center wavelength; e.g. a band-pass line) moves
    when each material's index or thickness changes: d(lambda)/dn in nm per refractive-index unit and
    d(lambda)/d(strain) in nm per unit relative thickness change, for all layers of that material
    (half-maximum centre, central differences). These do not depend on any thermal property, so they can be
    combined with measured dn/dT later. With `thermal`, also d(lambda)/dT in pm/K, each material's
    contribution, kelvin per FWHM (the heating that moves the peak by its width) and, per material, the dn/dT
    that would make the peak athermal."""
    return await compute("thermal_sensitivity", design=design, quantity=quantity, near=near, window=window,
                         angle=angle, polarization=polarization, thermal=thermal)


@mcp.tool(title="Thermal tuning sweep", annotations=READ)
async def thermal_sweep(design: Design, delta_T: list[float], thermal: Thermal, near: float | None = None,
                        quantity: str = "T", window: PeakWindow = None, angle: float = 0.0,
                        polarization: str = "unpolarized", plot: bool = True):
    """The peak of R or T nearest `near` at each temperature change delta_T (K, relative to the temperature the
    design's materials and thicknesses describe), with every material's dn/dT, dk/dT and thickness strain
    applied together: half-maximum centre, peak wavelength and value, FWHM; a linear fit gives
    d(lambda)/dT in pm/K. The model is linear in temperature: check it against measurements over wide ranges."""
    r = await compute("thermal_sweep", design=design, delta_T=delta_T, thermal=thermal, quantity=quantity,
                      near=near, window=window, angle=angle, polarization=polarization)
    if plot and len(r["rows"]) > 1:
        x = [row["delta_T_K"] for row in r["rows"]]
        png = plotting.curves(x, {"half-maximum centre": [row["center_nm"] for row in r["rows"]]},
                              f"{quantity} peak versus temperature", "Temperature change (K)", "Wavelength (nm)")
        return with_plots(r, png, "thermal_sweep")
    return r


@mcp.tool(title="Evaluate a design", annotations=READ)
async def evaluate_design(design: Design, targets: Targets, plot: bool = True):
    """Merit (chi^2, OpenFilters' definition) of a design against targets, with per-target pass
    fraction and maximum deviation. all_targets_met is true when every point meets its target (inequalities must hold;
    "equal" targets must be within tolerance)."""
    out = await compute("evaluate", design=design, target_specs=targets)
    if plot:
        pngs = await targets_plot([("design", design, False)], targets, "Design vs targets", "evaluation")
        if pngs:
            return with_plots(out, pngs, "evaluation")
    return out


@mcp.tool(title="Layer sensitivity", annotations=READ)
async def layer_sensitivity(design: Design, targets: Targets, relative_step: float = 0.01) -> dict:
    """Change in merit (delta_chi2) and in worst margin when each layer thickness changes by +/- relative_step
    (default 1 %; the worse direction), to find the layers that need the tightest deposition control.
    worst_margin is the smallest distance to a limit over all target points in units of each point's
    tolerance (positive = met; e.g. 0.5 with tolerance 0.001 means 0.0005 inside the limit). It ranks layers
    of a design that meets its inequality targets with room to spare, where the merit stays 0.
    A graded layer is scaled as a whole (every step by the same factor: a deposition-rate error).
    most_sensitive lists the top layers."""
    return await compute("sensitivity", design=design, target_specs=targets, relative_step=relative_step)


# ---------------------------------------------------------------------------
# Optimization and campaigns (background jobs)
# ---------------------------------------------------------------------------

MAX_WAIT = 45.0  # stay under the ~60 s tool-call timeout of some clients


async def _wait(job_id, wait_seconds, ctx=None):
    """Wait for a job, sending progress notifications to clients that asked for them
    (tasks done of total, or elapsed seconds for a single-task job)."""
    start = time.time()
    deadline = start + min(max(float(wait_seconds), 0.0), MAX_WAIT)
    while time.time() < deadline:
        status = manager().status(job_id)
        if status["status"] != "running":
            break
        if ctx is not None:
            if status["tasks_total"] > 1:
                await ctx.report_progress(status["tasks_done"], status["tasks_total"],
                                          f"{status['tasks_done']} of {status['tasks_total']} tasks")
            else:
                elapsed = time.time() - start
                await ctx.report_progress(elapsed, None, f"{status['label']}: {elapsed:.0f} s")
        await asyncio.sleep(0.5)
    status = manager().status(job_id)
    if status["status"] == "running":
        return {**status, "note": "still running; call job_status / get_results later"}
    return manager().results(job_id, top_n=10)


@mcp.tool(title="Optimize a design", annotations=JOB)
async def optimize_design(design: Design, targets: Targets, method: str = "needle",
                          options: Annotated[dict[str, Any] | None, Field(description=(
                              "time_limit (s, default 300), max_iterations, min_thickness (nm; guaranteed in the "
                              "result: thin layers are clamped or removed and the design re-refined with that bound; a clamped design "
                              "that already meets every target has merit 0 and stays as clamped), "
                              "max_layers, max_total_thickness (nm; both STOP the optimizer when the design grows past them and return the "
                              "best design within them: they are not constraints, so start well inside them), "
                              "max_layer_thickness (nm; default the larger of "
                              "20000 and 10x the thickest starting layer), margin (optimize inequality targets that "
                              "much inside their limit, in the targets' units, e.g. 0.0005 for R <= 0.005; results are "
                              "still judged against the targets), needle_materials (list), nb_needles, nb_steps, "
                              "automatic (default true)"))] = None,
                          wait_seconds: float = 40.0, ctx: Context = None) -> dict:
    """Optimize a design with OpenFilters: method "refine" (thicknesses only), "needle" (refine and
    insert new layers until no improvement) or "step" (graded/step index, needs a mixture material).
    Waits up to wait_seconds (max 45); if not finished, returns a job_id to poll. Returns the best design found
    (not merely the last), stop_reason with a plain stop_explanation, thinnest_layer_nm,
    and an .ofp project (start + optimized) openable in OpenFilters. thin_layers_removed counts layers removed
    while enforcing min_thickness (a layer held at the minimum is dropped when that does not worsen the merit;
    removed_layers and a warning name them). Layers with "fixed": true (or a fixed formula symbol) are never
    changed or removed: use it for functional layers such as electrodes. A warning flags a result that meets its targets on their grid but not between grid points. One run finds a local optimum; for
    hard specifications use run_optimization_campaign (many starts). Only FRONT layers are optimized;
    back_layers (if any) stay fixed and count in the merit when consider_backside is true."""
    checked(engine.build_targets, targets)
    if method not in engine.METHODS:
        raise ToolError(f"method must be one of {', '.join(engine.METHODS)}")
    if not (await compute("describe", design=design))["nb_layers"]:
        raise ToolError("optimization needs at least one front layer to start from (e.g. a thin layer of one "
                        "of the coating materials); back layers are never optimized")
    await compute("check_long_job", operation="optimize", args={"design": design, "target_specs": targets})
    job_id = manager().submit("optimize", [{"op": "optimize", "params": {"method": method},
                                            "args": {"design": design, "target_specs": targets,
                                                     "method": method, "options": options}}],
                              meta={"design": design, "targets": targets}, label=f"optimize ({method})")
    out = await _wait(job_id, wait_seconds, ctx)
    if out.get("status") in ("done", "failed"):
        r = manager().result(job_id, 0)
        if not r.get("ok"):
            raise ToolError(r.get("error"))
        result = {**r["result"], "job_id": job_id, "outputs": out.get("outputs")}
        pngs = await targets_plot([("optimized", result["design"], False), ("start", design, True)],
                                  targets, f"Optimized ({method})", "optimized")
        return with_plots(result, pngs, "optimized") if pngs else result
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

    def label(name, value):
        # A swept object (e.g. a whole design) is shown in the result rows by its name or position.
        if isinstance(value, (dict, list)):
            position = next(i for i, v in enumerate(parameters[name]) if v is value) + 1
            return value.get("name") or f"{name} {position}" if isinstance(value, dict) else f"{name} {position}"
        return value

    return [({n: label(n, v) for n, v in zip(names, combo)}, substitute(template, dict(zip(names, combo))))
            for combo in combos]


@mcp.tool(title="Run a parameter sweep", annotations=JOB)
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
    Peak stats for a band-pass: peak_wavelength, peak_value, fwhm and center (middle of the half-maximum
    edges), for the peak between from/to or the local peak nearest "near", e.g. {"name": "center", "quantity":
    "T", "stat": "center", "near": 532}; fwhm is null when an edge is beyond the computed wavelengths.
    To compare several designs, sweep the whole design: template {"design": "$design", ...} with parameters
    {"design": [design1, design2, ...]}; rows show each design's "name" (or its position).
    mode "grid" = all combinations, "zip" = parallel lists. Returns a job_id; then job_status/get_results
    (one row per case with parameters and results; full table in summary.csv)."""
    if operation not in tasks.OPS:
        raise ToolError(f"operation must be one of {', '.join(tasks.OPS)}")
    cases = _expand(template, parameters, mode)
    task_list = [{"op": operation, "params": params, "args": args} for params, args in cases]
    # Fail fast on the first case rather than after thousands of tasks.
    first = dict(task_list[0]["args"])
    if operation == "tolerance":
        await compute("check_tolerance", **first)
    elif "design" in first:
        await compute("describe", design=first["design"])
    for _, args in cases[:200]:
        if isinstance(args.get("design"), dict):
            await compute("check_long_job", operation=operation, args=args)
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


@mcp.tool(title="Run an optimization campaign", annotations=JOB)
async def run_optimization_campaign(targets: Targets,
                                    starts: Annotated[list[dict[str, Any]] | None, Field(description="Explicit starting designs.")] = None,
                                    random_starts: Annotated[dict[str, Any] | None, Field(description=(
                                        'Generate starts: {"count": 50, "materials": ["TiO2", "SiO2"], "n_layers": [5, 25], '
                                        '"thickness": [20, 200], "substrate": "BK7", "seed": 1, ...other design keys}. '
                                        'n_layers only sizes the starting designs: the needle method adds layers, so '
                                        'limit the result with options max_layers.'))] = None,
                                    methods: list[str] = ["needle"],
                                    options: Annotated[dict[str, Any] | None, Field(description=(
                                        "Same as optimize_design options, applied to every optimization "
                                        "(e.g. max_layers, min_thickness, time_limit)."))] = None,
                                    top_n: int = 10,
                                    tolerance: Annotated[dict[str, Any] | None, Field(description=(
                                        'Optional yield stage on the top_n designs: {"n_trials": 200, "thickness_error": '
                                        '{"type": "relative", "sigma": 0.01}, "index_error": {"sigma": 0.005}, "seed": 0}'))] = None,
                                    label: str = "") -> dict:
    """Many optimizations from different starting designs, in parallel; ranks distinct results (by yield
    first when a tolerance stage is requested, then targets met, merit, layer count, total thickness) and
    saves the top_n designs as an .ofp project. Starts that converge to the same design are marked
    duplicate_of. Pass options min_thickness to avoid unmanufacturable sub-nanometre layers. Returns a job_id."""
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
    for d in designs:
        await compute("check_long_job", operation="optimize", args={"design": d, "target_specs": targets})
    if tolerance:
        if not isinstance(tolerance, dict):
            raise ToolError('tolerance is {"n_trials": 200, "thickness_error": {...}, "index_error": {...}, "seed": 0}')
        checked(engine.tolerance_errors, tolerance.get("thickness_error"), tolerance.get("index_error"))
    task_list = [{"op": "optimize", "params": {"start": i, "method": m},
                  "args": {"design": d, "target_specs": targets, "method": m, "options": options}}
                 for i, d in enumerate(designs) for m in methods]
    meta = {"targets": targets, "top_n": int(top_n), "tolerance": tolerance}
    max_layers = (options or {}).get("max_layers")
    if max_layers and "needle" in methods and max(len(d.get("layers") or []) for d in designs) >= int(max_layers) - 2:
        meta["warnings"] = [f"some starts have {max(len(d.get('layers') or []) for d in designs)} layers and "
                            f"max_layers is {max_layers}: the needle method has little or no room to add layers. "
                            "Use fewer starting layers or a larger max_layers."]
    job_id = manager().submit("campaign", task_list, meta=meta, label=label or "optimization campaign")
    out = {"job_id": job_id, "optimizations": len(task_list), "workers": manager().workers,
           "note": "poll job_status, then get_results"}
    if meta.get("warnings"):
        out["warnings"] = meta["warnings"]
    return out


@mcp.tool(title="Tolerance (yield) analysis", annotations=JOB)
async def tolerance_analysis(design: Design, targets: Targets, n_trials: int = 500,
                             thickness_error: Annotated[dict[str, Any] | None, Field(description=(
                                 '{"type": "relative"|"absolute", "sigma": 0.01 (fraction) or nm, '
                                 '"distribution": "normal"|"uniform", "systematic": 0.0 (common relative offset)}'))] = None,
                             index_error: Annotated[dict[str, Any] | None, Field(description='{"sigma": 0.005} relative index error, drawn per material per trial')] = None,
                             seed: int = 0, wait_seconds: float = 40.0, ctx: Context = None) -> dict:
    """Monte Carlo yield: random thickness (and optional index) errors; a trial passes when every target
    is met. Runs in parallel; returns yield with 95 % interval, merit statistics and the failure rate
    of each target. Both faces of a two-sided design are perturbed. A graded layer is scaled as a whole
    (all its steps by one factor: a deposition-rate error); index_error does not perturb mixtures."""
    n_trials = int(n_trials)
    if not 1 <= n_trials <= 1000000:
        raise ToolError("n_trials must be between 1 and 1e6")
    # Refuse a bad design or error model now, not in every task of a job (report 2, N3).
    await compute("check_tolerance", design=design, target_specs=targets, thickness_error=thickness_error,
                  index_error=index_error)
    chunks = max(1, min(manager().workers * 2, n_trials // 10 or 1))
    sizes = [n_trials // chunks + (1 if i < n_trials % chunks else 0) for i in range(chunks)]
    task_list = [{"op": "tolerance", "params": {"chunk": i},
                  "args": {"design": design, "target_specs": targets, "n_trials": size,
                           "thickness_error": thickness_error, "index_error": index_error, "seed": seed * 100003 + i}}
                 for i, size in enumerate(sizes) if size]
    job_id = manager().submit("tolerance", task_list, label="tolerance analysis")
    out = await _wait(job_id, wait_seconds, ctx)
    if out.get("status") == "done":
        return {"job_id": job_id, **out.get("summary", {})}
    return out


@mcp.tool(title="Job status", annotations=READ)
async def job_status(job_id: str) -> dict:
    """Progress of a background job (tasks done/total, failures, ETA, output files when done)."""
    return checked(manager().status, job_id)


@mcp.tool(title="List jobs", annotations=READ)
async def list_jobs(limit: int = 20) -> dict:
    """Most recent background jobs, newest first."""
    return {"jobs": manager().list(limit)}


@mcp.tool(title="Get job results", annotations=READ)
async def get_results(job_id: str, top_n: int = 10, sort_by: str | None = None, descending: bool = False,
                      include_designs: bool = False, plot: bool = True):
    """Result rows of a job (campaigns are ranked best first; batches in task order unless sort_by is
    given). Works on partial results while the job is running. include_designs adds the optimized
    design JSON of the returned rows. For finished campaigns, plots the top 3 designs against the targets."""
    out = checked(manager().results, job_id, top_n, sort_by, descending)
    job = manager()._load(job_id)
    if plot and job["kind"] == "campaign" and out["status"] == "done":
        best = []
        for row in out["rows"][:3]:
            r = manager().result(job_id, row["task"])
            if r.get("ok"):
                best.append((f"#{row.get('rank', len(best) + 1)}", r["result"]["design"], False))
        pngs = await targets_plot(best, job["meta"]["targets"], "Campaign: top designs", "campaign_top") if best else []
        if include_designs:
            for row in out["rows"]:
                r = manager().result(job_id, row["task"])
                if r.get("ok") and isinstance(r["result"], dict) and "design" in r["result"]:
                    row["design"] = r["result"]["design"]
        return with_plots(out, pngs, "campaign_top") if pngs else out
    if include_designs:
        for row in out["rows"]:
            r = manager().result(job_id, row["task"])
            if r.get("ok") and isinstance(r["result"], dict) and "design" in r["result"]:
                row["design"] = r["result"]["design"]
    return out


@mcp.tool(title="Cancel a job", annotations=DESTRUCTIVE)
async def cancel_job(job_id: str) -> dict:
    """Cancel the pending tasks of a running background job. Reports tasks_done, tasks_still_running
    (they finish on their own) and tasks_cancelled (never started); a finished job is left as it is."""
    return checked(manager().cancel, job_id)


# ---------------------------------------------------------------------------
# OpenFilters project files
# ---------------------------------------------------------------------------

EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(engine.__file__)), "openfilters_core", "examples")
EXAMPLE_PREFIXES = ("openfilters://examples/", "examples/", "example:")


def _example_names():
    return sorted(f[:-4] for f in os.listdir(EXAMPLES_DIR) if f.endswith(".ofp"))


def _project_path(path):
    """A relative path or bare name means the data folder's projects directory; .ofp is added if missing.
    "examples/<name>" (or the resource URI) is one of OpenFilters' example projects."""
    for prefix in EXAMPLE_PREFIXES:
        if path.lower().startswith(prefix):
            name = path[len(prefix):].strip().removesuffix(".ofp")
            match = next((n for n in _example_names() if n.lower() == name.lower()), None)
            if match is None:
                raise ToolError(f"no example {name!r}; examples: {', '.join(_example_names())}")
            return os.path.join(EXAMPLES_DIR, match + ".ofp")
    if not os.path.isabs(path):
        path = os.path.join(settings.projects_dir(), path)
    if not path.lower().endswith(".ofp"):
        path += ".ofp"
    return path


@mcp.tool(title="Load an OpenFilters project", annotations=READ)
async def load_project(path: str, full_graded_arrays: bool = False) -> dict:
    """Read an OpenFilters .ofp project: returns its designs (as design JSON) and targets.
    A relative path or bare name (e.g. "my_ar") is looked up in the data folder's projects directory,
    where save_project writes; give the full path for projects elsewhere.
    OpenFilters' example projects load as "examples/<name>": examples/AR, examples/Beamsplitter,
    examples/Color, examples/Edge, examples/Fourier, examples/Ramp, examples/Rugate.
    A design whose graded layers have many steps (rugate filters: thousands) is summarized (steps, index
    range, optical thickness per graded layer) and written whole to a design_file: pass
    {"design_file": path} as the design to any tool. full_graded_arrays=true returns the arrays instead."""
    path = _project_path(path)
    if not os.path.isfile(path):
        raise ToolError(f"no project at {path} (OpenFilters' examples load as examples/<name>: "
                        f"{', '.join(_example_names())})")
    out = await compute("load_project", path=path)
    if not full_graded_arrays:
        stem = os.path.splitext(os.path.basename(path))[0]
        for i, design in enumerate(out["designs"]):
            if engine.graded_steps_count(design) > GRADED_INLINE_STEPS:
                file = _write_design(design, f"{stem}_design{i + 1}")
                out["designs"][i] = {**engine.summarize_graded(design), "design_file": file}
        if any("design_file" in d for d in out["designs"]):
            out["note"] = ("graded layers are summarized; each such design is whole in its design_file: pass "
                           '{"design_file": path} as the design to any tool')
    return out


@mcp.tool(title="Save an OpenFilters project", annotations=DESTRUCTIVE)
async def save_project(path: str, designs: list[dict[str, Any]], targets: list[dict[str, Any]] | None = None,
                       comment: str = "") -> dict:
    """Write designs and their targets to an .ofp project that opens in the OpenFilters GUI.
    ALWAYS pass the targets the designs were optimized or evaluated against: the GUI's Design/Optimize
    menu stays disabled in a project without targets, so the user could not continue the work there.
    Each design becomes ONE filter in the project; a part coated on both faces must be a single design
    with back_layers and consider_backside: true (written as BackLayer lines), not two designs.
    Keep the fields optimize_design returns in each design (description, needle_materials, layer flags)
    and put anything else worth keeping (method, settings, yield) in comment or a design's description.
    A relative path is placed in the data folder's projects directory."""
    if path.lower().startswith(EXAMPLE_PREFIXES):
        raise ToolError("OpenFilters' example projects are read-only: save under another name")
    path = _project_path(path)
    result = {"path": await compute("save_project", path=path, designs=designs, target_specs=targets, comment=comment)}
    warnings = []
    if not targets:
        warnings.append("Saved without targets: in the OpenFilters GUI the Design/Optimize menu is disabled "
                        "until targets are added. Save again with the targets if there are any.")
    texts = [comment] + [d.get("description") or "" for d in designs if isinstance(d, dict)]
    if any(engine.latin1_text(t) != t for t in texts):
        warnings.append(".ofp files are Latin-1: characters outside it were replaced in the comment or "
                        "descriptions (Greek letters spelled out, e.g. lambda; dashes and quotes simplified).")
    warnings += await compute("gui_extrapolation_notes", designs=designs, target_specs=targets)
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


# ---------------------------------------------------------------------------
# Resources: reference material a user can attach to a conversation
# ---------------------------------------------------------------------------

@mcp.resource("openfilters://guide", title="Design and target format", mime_type="text/plain")
def guide_resource() -> str:
    """How designs, targets and units are written for the OpenFilters tools."""
    return INSTRUCTIONS


@mcp.resource("openfilters://materials", title="Available materials", mime_type="application/json")
async def materials_resource() -> dict:
    """Built-in and user materials with n,k at a few wavelengths."""
    return {"materials": await compute("list_materials", name_filter=None)}


EXAMPLE_TOPICS = {"AR": "antireflection coating", "Beamsplitter": "beam splitter", "Color": "color target",
                  "Edge": "edge filter", "Fourier": "Fourier-method inhomogeneous design",
                  "Ramp": "graded-index ramp", "Rugate": "rugate filter"}


def _register_example(name):
    # One fixed resource per example: apps list fixed resources in their attach menus,
    # but not resource templates.
    async def example() -> dict:
        return await compute("load_project", path=os.path.join(EXAMPLES_DIR, name + ".ofp"))

    topic = EXAMPLE_TOPICS.get(name, "example project")
    mcp.resource(f"openfilters://examples/{name}", name=f"example_{name}", title=f"OpenFilters example: {name}",
                 description=f"Designs and targets of the OpenFilters {name} example ({topic}).",
                 mime_type="application/json")(example)


for _name in _example_names():
    _register_example(_name)


# ---------------------------------------------------------------------------
# Prompts: ready-made design workflows (slash commands in Claude Code)
# ---------------------------------------------------------------------------

def _workflow(fn):
    """Prompt arguments carry no default values to the client, so Claude Desktop shows empty boxes and may send
    empty text: describe each box with its default, and treat an empty box as that default."""
    signature = inspect.signature(fn)
    defaults = {name: p.default for name, p in signature.parameters.items() if p.default is not p.empty}

    @functools.wraps(fn)
    def wrapper(**kwargs):
        return fn(**{k: v for k, v in kwargs.items() if str(v).strip() or k not in defaults})

    wrapper.__signature__ = signature.replace(parameters=[
        p.replace(annotation=Annotated[str, Field(description=(
            f"Default: {p.default}" if p.default else "Optional") if p.default is not p.empty else "Required")])
        for p in signature.parameters.values()])
    return wrapper


_MATERIALS = ("Check the materials with list_materials first; import any that is missing with search_refractiveindex "
              "and import_refractiveindex_material (thin-film data preferred), and use the imported name.")
_CLOSING = ("Report whether the targets are met, the layer list, the yield and the plot file paths; save the best "
            "designs with their targets using save_project so they open in the OpenFilters GUI.")


@mcp.prompt(title="Design an antireflection coating")
@_workflow
def design_antireflection(substrate: str = "BK7", from_nm: str = "420", to_nm: str = "680",
                          max_reflectance_percent: str = "0.5", max_angle_deg: str = "0",
                          materials: str = "TiO2, SiO2", max_layers: str = "10", min_thickness_nm: str = "5") -> str:
    """Antireflection coating over a band, optimized from many starts and checked for yield."""
    return f"""Design an antireflection coating on {substrate} with the OpenFilters tools.
Specification: R <= {max_reflectance_percent} % (unpolarized) from {from_nm} to {to_nm} nm, at angles 0 to {max_angle_deg} deg,
at most {max_layers} layers, no layer thinner than {min_thickness_nm} nm; materials {materials}.
{_MATERIALS}
1. Write the targets: one R target with inequality "<=" over the band (step about 5 nm) for each angle needed
   (values as fractions; an inequality counts as met only when it holds at every point).
2. Run run_optimization_campaign with about 40 random starts (random_starts n_layers [2, {max_layers}], the listed
   materials, the substrate), the needle method, options {{"max_layers": {max_layers}, "min_thickness":
   {min_thickness_nm}}} (n_layers only sizes the starting designs; a minimum thickness of 0 allows any layer), top_n 5
   and a tolerance stage (1 % relative thickness error, 200 trials).
3. Poll job_status, then get_results; the ranking puts the highest yield first. Pick the simplest design with a
   good yield; if no yield is good (designs sit exactly on the limit), rerun with options "margin" (e.g. a tenth of
   the reflectance limit, as a fraction) or suggest relaxing the specification.
{_CLOSING}"""


@mcp.prompt(title="Design a high reflector (mirror)")
@_workflow
def design_mirror(wavelength_nm: str = "1064", min_reflectance_percent: str = "99.9", bandwidth_nm: str = "40",
                  angle_deg: str = "0", substrate: str = "FusedSilica", materials: str = "TiO2, SiO2") -> str:
    """Dielectric mirror: quarter-wave stack sized from theory, then refined; field and yield checked."""
    return f"""Design a dielectric mirror on {substrate} with the OpenFilters tools.
Specification: R >= {min_reflectance_percent} % over {bandwidth_nm} nm centred on {wavelength_nm} nm at {angle_deg} deg;
materials {materials} (H = high index, L = low index).
{_MATERIALS}
1. Start from the quarter-wave stack formula "(HL)^p H" at {wavelength_nm} nm and increase p until compute_spectrum
   reaches the reflectance (use the "OD" quantity when the blocking is very high).
2. Write the R target (value as a fraction, inequality ">=") and refine with optimize_design (method "refine");
   use "needle" only if the band is not covered.
3. Check compute_electric_field at {wavelength_nm} nm (position of the |E|^2 peak, for laser damage) and
   tolerance_analysis.
{_CLOSING}"""


@mcp.prompt(title="Design an edge filter or dichroic")
@_workflow
def design_edge_filter(pass_from_nm: str = "600", pass_to_nm: str = "800", min_transmittance_percent: str = "95",
                       block_from_nm: str = "400", block_to_nm: str = "550", max_transmittance_percent: str = "1",
                       angle_deg: str = "0", substrate: str = "BK7", materials: str = "TiO2, SiO2",
                       min_thickness_nm: str = "5") -> str:
    """Long-pass, short-pass or dichroic filter with a pass band and a blocking band."""
    return f"""Design an edge filter or dichroic on {substrate} with the OpenFilters tools, at {angle_deg} deg incidence
(at oblique incidence, write s and p targets separately).
Specification: T >= {min_transmittance_percent} % from {pass_from_nm} to {pass_to_nm} nm and
T <= {max_transmittance_percent} % from {block_from_nm} to {block_to_nm} nm; materials {materials}.
{_MATERIALS}
1. Write a T ">=" target for the pass band and a T "<=" target for the blocking band (or an OD ">=" target for
   deep blocking).
2. Start from a quarter-wave stack centred in the blocking band with half-thickness end layers, for example
   "(0.5L H 0.5L)^p" for a long-pass edge; check it with compute_spectrum, then optimize_design with the needle
   method and options {{"min_thickness": {min_thickness_nm}}} to flatten the pass band without unmanufacturable
   layers.
3. For a hard specification, run run_optimization_campaign from about 30 random starts and compare.
{_CLOSING}"""


@mcp.prompt(title="Design a narrow band-pass filter")
@_workflow
def design_bandpass(center_nm: str = "532", fwhm_nm: str = "10", min_peak_transmittance_percent: str = "90",
                    block_from_nm: str = "400", block_to_nm: str = "700", substrate: str = "FusedSilica",
                    materials: str = "TiO2, SiO2") -> str:
    """Fabry-Perot band-pass: cavities and mirror pairs sized from theory, then optimized."""
    return f"""Design a narrow band-pass filter on {substrate} with the OpenFilters tools.
Specification: peak T >= {min_peak_transmittance_percent} % at {center_nm} nm, FWHM about {fwhm_nm} nm, and T <= 1 %
from {block_from_nm} to {block_to_nm} nm outside the pass band; materials {materials}.
{_MATERIALS}
1. Start from a single cavity "(HL)^p HH (LH)^p" at {center_nm} nm and choose p from the FWHM with compute_spectrum
   (more pairs give a narrower band); join two or three cavities with an L layer for steeper edges.
2. Write targets (T ">=" at the pass-band centre, T "<=" in the stop bands on either side) and refine with
   optimize_design.
3. Check the angle shift with compute_angle_scan and the yield with tolerance_analysis (cavities are sensitive).
{_CLOSING}"""


@mcp.prompt(title="Review a design for manufacturing")
@_workflow
def review_for_manufacturing(design_json: str, targets_json: str = "", thickness_error_percent: str = "1",
                             monitoring_wavelength_nm: str = "") -> str:
    """Yield, sensitive layers, electric field and monitoring signals of an existing design."""
    targets = targets_json or "none given: ask for the specification, or derive targets from the purpose of the design"
    monitoring = monitoring_wavelength_nm or "a suitable monitoring wavelength"
    return f"""Review this coating design for manufacturing with the OpenFilters tools.
Design: {design_json}
Targets: {targets}
1. describe_design (layers, total thickness, thinnest layer) and evaluate_design against the targets.
2. tolerance_analysis with {thickness_error_percent} % relative thickness error (500 trials), and layer_sensitivity to
   name the layers that need the tightest control.
3. compute_electric_field at the main wavelength (where is the |E|^2 peak?) and compute_monitoring_curves at
   {monitoring} (signal at the end of each layer).
Summarize the risks and suggest changes, such as removing very thin layers or re-optimizing with min_thickness."""


def main():
    try:
        mcp.run("stdio")
    finally:
        if _manager is not None:
            _manager.shutdown()
        if _interactive is not None:
            _interactive.shutdown(wait=False, cancel_futures=True)
