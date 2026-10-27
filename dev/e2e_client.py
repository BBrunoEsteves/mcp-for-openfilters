"""End-to-end check: start the server over stdio like Claude does and call
its tools. Usage: python dev/e2e_client.py [data_folder]"""

import asyncio
import json
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIRROR = {"substrate": "FusedSilica", "center_wavelength": 1064,
          "formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
TARGETS = [{"kind": "R", "from": 1000, "to": 1130, "step": 5, "value": 0.995, "tolerance": 0.005, "inequality": ">="},
           {"kind": "T", "from": 600, "to": 700, "step": 5, "value": 0.9, "tolerance": 0.02, "inequality": ">="}]


def show(name, result, limit=600):
    texts = [c.text for c in result.content if c.type == "text"]
    images = sum(1 for c in result.content if c.type == "image")
    flag = "ERROR " if result.is_error else ""
    print(f"--- {flag}{name}: {images} image(s)")
    for t in texts:
        print(t[:limit] + ("..." if len(t) > limit else ""))
    return json.loads(texts[0]) if texts and not result.is_error else None


async def main(home):
    env = dict(os.environ, OPENFILTERS_MCP_HOME=home, PYTHONPATH=os.path.join(ROOT, "src"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "openfilters_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            call = session.call_tool
            show("server_info", await call("server_info", {}))
            show("list_materials", await call("list_materials", {"name_filter": "SiO"}), 300)
            show("describe_design", await call("describe_design", {"design": MIRROR}), 300)
            show("describe_design (rugate profile)", await call("describe_design", {"design": {
                "substrate": "FusedSilica", "center_wavelength": 1064, "layers": [{"material": "IdealMixture", "graded": {
                    "profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2, "periods": 16, "apodization": {"type": "hann"}},
                    "matching": {"substrate": "quintic", "medium": "quintic"}}}]}, "plot_index_profile": True}), 600)
            show("compute_spectrum", await call("compute_spectrum", {
                "design": MIRROR, "quantities": ["R"], "wavelengths": {"from": 800, "to": 1300, "step": 1},
                "angles": [0, 30], "polarizations": ["s", "p"], "sample_points": 3}), 500)
            show("compute_angle_scan", await call("compute_angle_scan", {"design": MIRROR, "wavelength": 1064,
                                                                          "angles": {"from": 0, "to": 80, "step": 20}}), 300)
            show("compute_color", await call("compute_color", {"design": MIRROR, "angles": [0, 45]}), 300)
            show("compute_electric_field", await call("compute_electric_field", {"design": MIRROR, "wavelength": 1064}), 300)
            show("compute_monitoring_curves", await call("compute_monitoring_curves", {
                "design": {"substrate": "BK7", "layers": [["TiO2", 60], ["SiO2", 95], ["TiO2", 60]]},
                "wavelengths": [550, 633]}), 300)
            bandpass = {"substrate": "FusedSilica", "center_wavelength": 532,
                        "formula": {"formula": "(HL)^5 HH (LH)^5", "materials": {"H": "TiO2", "L": "SiO2"}}}
            heat = {"materials": {"TiO2": {"dn_dT": -1e-4, "cte": 5e-6, "poisson": 0.27},
                                  "SiO2": {"dn_dT": 1e-5, "cte": 0.5e-6, "poisson": 0.17}}, "substrate_cte": 0.55e-6}
            show("thermal_sensitivity", await call("thermal_sensitivity", {"design": bandpass, "thermal": heat}), 600)
            show("thermal_sweep", await call("thermal_sweep", {"design": bandpass, "delta_T": [0, 50, 100],
                                                               "thermal": heat}), 300)
            show("evaluate_design", await call("evaluate_design", {"design": MIRROR, "targets": TARGETS}), 300)
            show("bad input", await call("evaluate_design", {"design": {"layers": [["Unobtainium", 10]]}, "targets": TARGETS}))
            opt = show("optimize_design", await call("optimize_design", {
                "design": MIRROR, "targets": TARGETS, "method": "needle", "options": {"time_limit": 60}}), 400)
            progress = []

            async def on_progress(done, total, message):
                progress.append((done, total, message))

            show("tolerance_analysis", await call("tolerance_analysis", {
                "design": opt["design"], "targets": TARGETS, "n_trials": 2000,
                "thickness_error": {"type": "relative", "sigma": 0.01}}, progress_callback=on_progress), 600)
            print(f"progress notifications: {len(progress)}, last: {progress[-1] if progress else None}")
            batch = show("run_batch", await call("run_batch", {
                "operation": "spectrum",
                "template": {"design": {"substrate": "FusedSilica", "center_wavelength": "$cw",
                                        "formula": {"formula": "(HL)^${pairs} H", "materials": {"H": "TiO2", "L": "SiO2"}}},
                             "quantities": ["R"], "wavelengths": {"from": 400, "to": 1200, "step": 2},
                             "metrics": [{"name": "R_at_cw", "quantity": "R", "stat": "max"}]},
                "parameters": {"cw": [600, 800, 1064], "pairs": [4, 8]}}))
            camp = show("run_optimization_campaign", await call("run_optimization_campaign", {
                "targets": TARGETS,
                "random_starts": {"count": 8, "materials": ["TiO2", "SiO2"], "n_layers": [8, 16],
                                  "thickness": [50, 250], "substrate": "FusedSilica", "seed": 1},
                "methods": ["needle"], "options": {"time_limit": 40, "max_layers": 40}, "top_n": 3,
                "tolerance": {"n_trials": 50, "thickness_error": {"type": "relative", "sigma": 0.01}}}))
            for job in (batch["job_id"], camp["job_id"]):
                t0 = time.time()
                while True:
                    status = show("job_status", await call("job_status", {"job_id": job}), 400)
                    if status["status"] != "running" or time.time() - t0 > 400:
                        break
                    await asyncio.sleep(5)
                show("get_results", await call("get_results", {"job_id": job, "top_n": 6}), 1500)
            show("list_jobs", await call("list_jobs", {"limit": 5}), 400)
            saved = show("save_project", await call("save_project", {"path": "e2e_test", "designs": [MIRROR], "targets": TARGETS}))
            show("load_project", await call("load_project", {"path": saved["path"]}), 300)
            # Needs internet access (refractiveindex.info database on GitHub).
            show("search_refractiveindex", await call("search_refractiveindex", {"query": "HfO2", "limit": 3}), 400)
            show("import_refractiveindex_material", await call("import_refractiveindex_material", {
                "page": "main/SiO2/nk/Malitson", "overwrite": True}), 400)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, ".e2e_home")))
