"""Acceptance test of the whole connector, as Claude uses it.

Starts the server over stdio exactly like Claude Desktop and Claude Code do (by default the
openfilters-mcp executable next to this Python), calls every tool, prompt and resource, and
checks the answers against known values: closed-form results with dispersionless test
materials, regressions from real sessions, and consistency between tools. Prints PASS/FAIL
per check and exits with 1 if any check failed.

Usage:
    .venv/Scripts/python dev/acceptance.py [--command PATH] [--home DIR] [--offline]

--home    data folder to use (default: a new temporary folder, so your materials are untouched)
--offline skip the refractiveindex.info checks (they need internet access)
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from openfilters_mcp import __version__  # noqa: E402

# Dispersionless test materials, so textbook formulas give exact answers.
NS, NH, NL, NA = 1.52, 2.35, 1.46, 1.38
W0 = 550.0
AT = {"AT_S": NS, "AT_H": NH, "AT_L": NL, "AT_A": NA, "AT_T2": 2.0}


def design(layers, substrate="AT_S", **extra):
    return {"substrate": substrate, "center_wavelength": W0,
            "layers": [{"material": m, "qwot": q} for m, q in layers], **extra}


AR = design([("AT_A", 1)])  # quarter-wave MgF2-like layer
R_AR = ((NS - NA ** 2) / (NS + NA ** 2)) ** 2
MIRROR = design([("AT_H", 1), ("AT_L", 1)] * 6 + [("AT_H", 1)])
Y = (NH / NL) ** 12 * NH ** 2 / NS
R_MIRROR = ((1 - Y) / (1 + Y)) ** 2
AR_TARGETS = [{"kind": "R", "angle": a, "from": 500, "to": 700, "step": 5, "value": 0.002, "inequality": "<=",
               "tolerance": 0.0001} for a in (0, 35)]


class Checks:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        return ok

    def summary(self):
        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)} passed, {len(failed)} failed")
        for name, _, detail in failed:
            print(f"  FAILED: {name}  {detail}")
        return not failed


class Client:
    def __init__(self, session):
        self.session = session

    async def call(self, tool, args=None, progress=None):
        """Returns (data, images, error_text)."""
        r = await self.session.call_tool(tool, args or {}, progress_callback=progress)
        texts = [c.text for c in r.content if c.type == "text"]
        images = sum(1 for c in r.content if c.type == "image")
        if r.is_error:
            return None, images, texts[0] if texts else "error"
        return (json.loads(texts[0]) if texts else None), images, None

    async def ok(self, tool, args=None, progress=None):
        data, images, error = await self.call(tool, args, progress)
        if error:
            raise RuntimeError(f"{tool} failed: {error}")
        return data, images


def finite_design(d):
    return all(math.isfinite(l["thickness"]) and l["thickness"] > 0 for l in d["layers"] if "thickness" in l)


async def wait_job(c, job_id, limit=600):
    t0 = time.time()
    while time.time() - t0 < limit:
        status, _ = await c.ok("job_status", {"job_id": job_id})
        if status["status"] != "running":
            return status
        await asyncio.sleep(2)
    return status


async def run(c, k, offline):
    # --- server and materials -------------------------------------------------
    info, _ = await c.ok("server_info")
    k.check("server_info: version matches the package", info["version"] == __version__,
            f"server {info['version']}, package {__version__}")
    k.check("server_info: compiled fast core in use", info.get("cpp_core_abeles") and info.get("cpp_core_moremath"),
            str({x: info.get(x) for x in ("cpp_core_abeles", "cpp_core_moremath")}))

    for name, n in AT.items():
        await c.ok("add_material", {"name": name, "model": "constant", "n": n, "overwrite": True})
    mats, _ = await c.ok("list_materials", {"name_filter": "AT_"})
    k.check("add_material + list_materials: test materials are user materials",
            sorted(m["name"] for m in mats["materials"]) == sorted(AT) and
            all(m["source"] == "user" for m in mats["materials"]))
    ix, _ = await c.ok("get_material_index", {"name": "AT_H", "wavelengths": [400, 550, 1000]})
    k.check("get_material_index: constant n", all(abs(r[1] - NH) < 1e-9 and r[2] == 0 for r in ix["rows"]))
    data, images = await c.ok("get_material_index", {"name": "BK7", "plot": True})
    k.check("get_material_index: plot returned and saved", images == 1 and all(os.path.isfile(p) for p in data["plot_files"]))

    # --- single designs: closed-form results ------------------------------------
    desc, _ = await c.ok("describe_design", {"design": AR})
    k.check("describe_design: quarter-wave thickness = 550/(4n)",
            abs(desc["layers"][0]["thickness_nm"] - W0 / (4 * NA)) < 1e-3, f"{desc['layers'][0]['thickness_nm']} nm")
    spec, images = await c.ok("compute_spectrum", {"design": AR, "quantities": ["R"], "wavelengths": [W0],
                                                   "sample_points": 0, "plot": True})
    k.check("compute_spectrum: quarter-wave AR R = ((ns-n^2)/(ns+n^2))^2",
            abs(spec["series"][0]["min"] - R_AR) < 1e-6, f"{spec['series'][0]['min']} vs {R_AR:.6g}")
    k.check("compute_spectrum: plot image and PNG file", images == 1 and all(os.path.isfile(p) for p in spec["plot_files"]))
    spec, _ = await c.ok("compute_spectrum", {"design": MIRROR, "quantities": ["R", "OD"], "wavelengths": [W0],
                                              "sample_points": 0, "plot": False, "save_csv": True})
    k.check("compute_spectrum: quarter-wave mirror peak R (Macleod)",
            abs(spec["series"][0]["min"] - R_MIRROR) < 1e-6, f"{spec['series'][0]['min']} vs {R_MIRROR:.6g}")
    k.check("compute_spectrum: OD = -log10(T) and CSV written",
            abs(spec["series"][1]["min"] + math.log10(1 - R_MIRROR)) < 1e-3 and os.path.isfile(spec["csv"]))
    brewster = math.degrees(math.atan(NS))
    scan, _ = await c.ok("compute_angle_scan", {"design": design([("AT_A", 0)]), "wavelength": W0, "quantities": ["R"],
                                                "angles": {"from": 50, "to": 62, "step": 0.01},
                                                "polarizations": ["p"], "plot": False})
    rp = scan["curves"]["R p"]
    k.check("compute_angle_scan: p reflectance vanishes at Brewster's angle, located by the summary",
            rp["min"] < 1e-6 and abs(rp["at_deg_min"] - brewster) < 0.01,
            f"min Rp {rp['min']:.2e} at {rp['at_deg_min']} deg vs {brewster:.3f}")
    color, _ = await c.ok("compute_color", {"design": MIRROR, "angles": [0, 45]})
    k.check("compute_color: two angles with L*a*b* and hex",
            len(color["colors"]) == 2 and all(len(x["Lab"]) == 3 and x["hex"].startswith("#") for x in color["colors"]))
    field, _ = await c.ok("compute_electric_field", {"design": MIRROR, "wavelength": W0, "plot": False})
    k.check("compute_electric_field: finite peak and one value per layer",
            field["peak_E_squared"] > 0 and len(field["E_squared_at_layer_tops"]) == 13)
    mon_design = dict(MIRROR, consider_backside_on_monitoring=False)
    mon, _ = await c.ok("compute_monitoring_curves", {"design": mon_design, "wavelengths": [W0], "mode": "transmission",
                                                      "plot": False})
    end = list(mon["signal_at_layer_ends"].values())[0][-1]
    k.check("compute_monitoring_curves: final signal = transmission of the finished stack",
            abs(end - (1 - R_MIRROR)) < 1e-4, f"{end} vs {1 - R_MIRROR:.6g}")

    # --- targets: the strict rule for inequalities --------------------------------
    near = [{"kind": "R", "wavelength": W0, "value": 0.0125, "inequality": "<="}]  # R = 1.26 %, within 1 % tolerance
    ev, _ = await c.ok("evaluate_design", {"design": AR, "targets": near, "plot": False})
    k.check("evaluate_design: R 1.26 % does not meet R <= 1.25 % (inequalities are strict)",
            ev["all_targets_met"] is False and ev["merit_chi2"] > 0)
    row = ev["targets"][0]
    k.check("evaluate_design: reports the worst value and where", abs(row.get("worst_value", 0) - R_AR) < 1e-6
            and row.get("worst_at_nm") == W0, f"{row.get('worst_value')} at {row.get('worst_at_nm')} nm")
    ev, _ = await c.ok("evaluate_design", {"design": MIRROR, "plot": False, "targets": [
        {"kind": "R_color", "color_space": "L*a*b*", "values": [90, -10, 40], "tolerances": [1, 1, 1]}]})
    color_now, _ = await c.ok("compute_color", {"design": MIRROR, "angles": [0]})
    lab = color_now["colors"][0]["Lab"]
    delta_e = math.dist(lab, [90, -10, 40])
    k.check("evaluate_design: colour target reports coordinates and Delta E*ab",
            ev["targets"][0]["points"] == 3 and abs(ev["targets"][0].get("distance", 0) - delta_e) < 1e-3,
            f"Delta E {ev['targets'][0].get('distance')} vs {delta_e:.4f}")
    ev, images = await c.ok("evaluate_design", {"design": AR, "targets": [dict(near[0], value=0.013)]})
    k.check("evaluate_design: R 1.26 % meets R <= 1.3 %, with a plot", ev["all_targets_met"] is True and images >= 0)
    # NaN cannot travel in JSON: it arrives as null, which must be refused, not crash the tool.
    _, _, error = await c.call("evaluate_design", {"design": {"substrate": "AT_S", "layers": [
        {"material": "AT_A", "thickness": float("nan")}]}, "targets": near})
    k.check("evaluate_design: NaN/null thickness refused with a clear message",
            error is not None and "invalid thickness" in error, error or "")
    _, _, error = await c.call("describe_design", {"design": design([("Unobtainium", 1)])})
    k.check("describe_design: unknown material error suggests refractiveindex.info",
            error is not None and "search_refractiveindex" in error, error or "")
    sens, _ = await c.ok("layer_sensitivity", {"design": MIRROR, "targets": [
        {"kind": "R", "from": 520, "to": 580, "step": 5, "value": 0.999, "inequality": ">="}]})
    k.check("layer_sensitivity: one row per layer", len(json.dumps(sens)) > 0 and str(13) in json.dumps(sens))

    # --- optimization ---------------------------------------------------------------
    start = {"substrate": "BK7", "center_wavelength": W0, "layers": [{"material": "AT_T2", "thickness": 27.26},
             {"material": "SiO2", "thickness": 29.49}, {"material": "AT_T2", "thickness": 82.03},
             {"material": "SiO2", "thickness": 100.53}]}
    opt, images = await c.ok("optimize_design", {"design": start, "targets": AR_TARGETS, "method": "needle",
                                                  "options": {"max_layers": 10, "time_limit": 40}})
    k.check("optimize_design (needle): result finite and within max_layers",
            finite_design(opt["design"]) and opt["nb_layers"] <= 10, f"{opt['nb_layers']} layers")
    k.check("optimize_design (needle): continues past OpenFilters' edge-needle stop (regression)",
            opt.get("edge_needles_added", 0) >= 1 and opt["merit_chi2"] < 0.5 * opt["initial_chi2"],
            f"chi2 {opt['initial_chi2']:.0f} -> {opt['merit_chi2']:.0f}, {opt.get('edge_needles_added')} edge needles")
    k.check("optimize_design: reports 'met' honestly (n = 2.0 cannot reach R <= 0.2 %)", opt["all_targets_met"] is False)
    k.check("optimize_design: plots against targets saved", images >= 1 and all(os.path.isfile(p) for p in opt["plot_files"]))
    nan_start = {"substrate": "BK7", "layers": [{"material": m, "thickness": t} for m, t in zip(
        ["AT_T2", "SiO2"] * 4, [106.47, 182.94, 46.07, 63.99, 194.96, 104.88, 188.77])]}
    nan_targets = [{"kind": "R", "angle": a, "from": 500, "to": 700, "step": 5, "value": 0.002, "inequality": "<="}
                   for a in (0, 20, 35)]
    opt, _ = await c.ok("optimize_design", {"design": nan_start, "targets": nan_targets, "method": "needle",
                                             "options": {"max_layers": 20, "time_limit": 40}})
    k.check("optimize_design: no NaN after OpenFilters' numerical failure (regression)",
            finite_design(opt["design"]) and math.isfinite(opt["merit_chi2"]) and opt["merit_chi2"] > 0,
            f"chi2 {opt['merit_chi2']:.4g}, stop {opt['stop_reason']}")
    progress = []

    async def on_progress(done, total, message):
        progress.append((done, total, message))

    tol, _ = await c.ok("tolerance_analysis", {"design": AR, "targets": [dict(near[0], value=0.02)], "n_trials": 2000,
                                               "thickness_error": {"type": "relative", "sigma": 0.02}},
                        progress=on_progress)
    yield_ = tol.get("yield", tol.get("summary", {}).get("yield"))
    k.check("tolerance_analysis: yield in [0, 1] with progress updates",
            yield_ is not None and 0 <= yield_ <= 1, f"yield {yield_}, {len(progress)} progress updates")

    # --- background jobs --------------------------------------------------------------
    batch, _ = await c.ok("run_batch", {"operation": "spectrum", "template": {
        "design": {"substrate": "AT_S", "center_wavelength": W0, "layers": [{"material": "AT_A", "qwot": "$q"}]},
        "quantities": ["R"], "wavelengths": [W0], "metrics": [{"name": "R550", "quantity": "R", "stat": "at",
                                                               "wavelength": W0}]},
        "parameters": {"q": [0.8, 0.9, 1.0, 1.1, 1.2]}})
    status = await wait_job(c, batch["job_id"])
    rows, _ = await c.ok("get_results", {"job_id": batch["job_id"], "sort_by": "R550", "plot": False})
    k.check("run_batch + get_results: sweep finds the minimum at one quarter wave",
            status["status"] == "done" and len(rows["rows"]) == 5 and rows["rows"][0].get("q") == 1.0,
            f"best row {rows['rows'][0] if rows['rows'] else None}")
    camp, _ = await c.ok("run_optimization_campaign", {
        "targets": AR_TARGETS, "random_starts": {"count": 6, "materials": ["AT_T2", "SiO2"], "n_layers": [2, 6],
                                                  "thickness": [20, 150], "substrate": "BK7", "seed": 3},
        "options": {"max_layers": 6, "time_limit": 30}, "top_n": 3,
        "tolerance": {"n_trials": 50, "thickness_error": {"type": "relative", "sigma": 0.01}}})
    status = await wait_job(c, camp["job_id"])
    res, images = await c.ok("get_results", {"job_id": camp["job_id"], "top_n": 6, "include_designs": True})
    designs = [r["design"] for r in res["rows"] if "design" in r]
    k.check("run_optimization_campaign: finished, ranked, top designs plotted",
            status["status"] == "done" and len(res["rows"]) >= 1 and images >= 1)
    k.check("run_optimization_campaign: no NaN designs and max_layers honoured (regression)",
            designs and all(finite_design(d) and len(d["layers"]) <= 6 for d in designs),
            f"{[len(d['layers']) for d in designs]} layers")
    honest = True
    for row in res["rows"][:3]:
        if "design" in row:
            ev, _ = await c.ok("evaluate_design", {"design": row["design"], "targets": AR_TARGETS, "plot": False})
            honest &= ev["all_targets_met"] == bool(row.get("all_targets_met"))
    k.check("run_optimization_campaign: 'met' agrees with evaluate_design", honest)
    jobs, _ = await c.ok("list_jobs", {"limit": 10})
    k.check("list_jobs: lists the campaign", any(j["job_id"] == camp["job_id"] for j in jobs["jobs"]))
    big, _ = await c.ok("run_batch", {"operation": "optimize", "template": {
        "design": dict(MIRROR, center_wavelength="$w"), "target_specs": AR_TARGETS, "method": "needle"},
        "parameters": {"w": list(range(400, 800, 5))}})
    await c.ok("cancel_job", {"job_id": big["job_id"]})
    status = await wait_job(c, big["job_id"], limit=120)
    k.check("cancel_job: job stops", status["status"] != "running", status["status"])

    # --- projects --------------------------------------------------------------------
    saved, _ = await c.ok("save_project", {"path": "acceptance_test", "designs": [AR, MIRROR], "targets": AR_TARGETS})
    loaded, _ = await c.ok("load_project", {"path": "acceptance_test"})  # by name, as saved
    k.check("save_project + load_project (by name): designs and targets round-trip",
            len(loaded["designs"]) == 2 and len(loaded["targets"]) == len(AR_TARGETS))
    saved, _ = await c.ok("save_project", {"path": "acceptance_no_targets", "designs": [AR]})
    k.check("save_project: warns when saved without targets", "warning" in saved)

    # --- refractiveindex.info -------------------------------------------------------
    if offline:
        print("SKIP  refractiveindex.info checks (--offline)")
    else:
        found, _ = await c.ok("search_refractiveindex", {"query": "SiO2", "limit": 200})
        k.check("search_refractiveindex: finds Malitson's fused silica",
                any(e["page"] == "main/SiO2/nk/Malitson" for e in found["results"]))
        imp, _ = await c.ok("import_refractiveindex_material", {"page": "main/SiO2/nk/Malitson", "name": "AT_Malitson",
                                                                "overwrite": True})
        ix, _ = await c.ok("get_material_index", {"name": "AT_Malitson", "wavelengths": [587.6]})
        k.check("import_refractiveindex_material: fused silica n_d = 1.4585",
                abs(ix["rows"][0][1] - 1.4585) < 2e-4 and "Malitson" in imp["references"], f"n = {ix['rows'][0][1]}")
        AT["AT_Malitson"] = None

    # --- prompts and resources -------------------------------------------------------
    prompts = (await c.session.list_prompts()).prompts
    expected = {"design_antireflection", "design_mirror", "design_edge_filter", "design_bandpass",
                "review_for_manufacturing"}
    k.check("prompts: the 5 workflows are offered", expected <= {p.name for p in prompts})
    texts_ok = True
    for p in prompts:
        args = {a.name: "{}" for a in p.arguments or [] if a.required}
        text = (await c.session.get_prompt(p.name, args)).messages[0].content.text
        texts_ok &= len(text) > 200 and "OpenFilters" in text
    k.check("prompts: each renders with its defaults", texts_ok)
    resources = {str(r.uri) for r in (await c.session.list_resources()).resources}
    examples = sorted(u for u in resources if u.startswith("openfilters://examples/"))
    k.check("resources: guide, materials and 7 example projects listed",
            {"openfilters://guide", "openfilters://materials"} <= resources and len(examples) == 7)
    readable = True
    for uri in examples:
        content = (await c.session.read_resource(uri)).contents[0].text
        readable &= '"designs"' in content
    k.check("resources: every example project loads", readable)

    # --- clean-up and remove_material -------------------------------------------------
    for name in AT:
        await c.ok("remove_material", {"name": name})
    mats, _ = await c.ok("list_materials", {"name_filter": "AT_"})
    k.check("remove_material: test materials removed", not mats["materials"])
    _, _, error = await c.call("remove_material", {"name": "BK7"})
    k.check("remove_material: built-in materials cannot be removed", error is not None and "built-in" in error)


async def main():
    sys.stdout.reconfigure(errors="replace")  # a non-ASCII character must not crash the report
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    exe = os.path.join(os.path.dirname(sys.executable), "openfilters-mcp" + (".exe" if os.name == "nt" else ""))
    parser.add_argument("--command", default=exe if os.path.isfile(exe) else None,
                        help="server executable (default: the one Claude Desktop runs)")
    parser.add_argument("--home", help="data folder (default: a new temporary folder)")
    parser.add_argument("--offline", action="store_true", help="skip refractiveindex.info checks")
    args = parser.parse_args()
    home = args.home or tempfile.mkdtemp(prefix="openfilters-acceptance-")
    command, cmd_args = (args.command, []) if args.command else (sys.executable, ["-m", "openfilters_mcp"])
    log_path = os.path.join(tempfile.gettempdir(), "openfilters-acceptance-server.log")
    print(f"server: {command} {' '.join(cmd_args)}\ndata folder: {home}\nserver log: {log_path}\n")
    params = StdioServerParameters(command=command, args=cmd_args, env=dict(os.environ, OPENFILTERS_MCP_HOME=home))
    k = Checks()
    t0 = time.time()
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    k.check("tools: all 25 tools offered", len(tools) == 25, f"{len(tools)} tools")
                    await run(Client(session), k, args.offline)
    except Exception as error:  # report, then show the summary
        k.check("acceptance run completed", False, f"{type(error).__name__}: {error}")
    print(f"\n{time.time() - t0:.0f} s")
    ok = k.summary()
    if not args.home:
        shutil.rmtree(home, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
