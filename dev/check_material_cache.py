"""Reproduce the stale-material bug through the real server (stdio + worker
pools): add a material, optimize right away, overwrite it, optimize again,
and run a campaign. Usage: python dev/check_material_cache.py [data_folder]"""

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [{"kind": "R", "from": 500, "to": 600, "step": 10, "value": 0, "tolerance": 0.01}]


def design(material):
    return {"substrate": "BK7", "layers": [{"material": material, "thickness": 15}, {"material": "SiO2", "thickness": 90}]}


async def call(session, name, args):
    result = await session.call_tool(name, args)
    text = next(c.text for c in result.content if c.type == "text")
    if result.is_error:
        raise RuntimeError(f"{name} failed: {text}")
    return json.loads(text)


async def main(home):
    env = dict(os.environ, MCP_FOR_OPENFILTERS_HOME=home, PYTHONPATH=os.path.join(ROOT, "src"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_for_openfilters"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Warm up the job workers first, so they hold a catalog without the new material.
            await call(session, "optimize_design", {"design": design("TiO2"), "targets": TARGETS, "method": "refine"})

            await call(session, "add_material", {"name": "TiO2_1600", "model": "cauchy",
                                                 "coefficients": [2.362725957, 0.0266752258, 0.0043124058]})
            r1 = await call(session, "optimize_design", {"design": design("TiO2_1600"), "targets": TARGETS,
                                                         "method": "refine"})
            print("optimize right after add_material: OK, chi2 =", r1["merit_chi2"])

            # Overwrite with a clearly different index and check workers use it.
            await call(session, "add_material", {"name": "TiO2_1600", "model": "constant", "n": 1.8,
                                                 "overwrite": True})
            spectrum = await call(session, "compute_spectrum", {"design": design("TiO2_1600"), "quantities": ["R"],
                                                                "wavelengths": [550], "plot": False})
            evaluation = await call(session, "evaluate_design", {"design": design("TiO2_1600"), "targets": TARGETS,
                                                                 "plot": False})
            r2 = await call(session, "optimize_design", {"design": design("TiO2_1600"), "targets": TARGETS,
                                                         "method": "refine", "options": {"max_iterations": 1}})
            print("after overwrite: R(550) =", spectrum["series"][0]["max"],
                  "| evaluate chi2 =", evaluation["merit_chi2"], "| optimize initial chi2 =", r2["initial_chi2"])
            assert abs(evaluation["merit_chi2"] - r2["initial_chi2"]) < 1e-9 * max(1, r2["initial_chi2"]), \
                "worker used a stale material"

            camp = await call(session, "run_optimization_campaign", {
                "targets": TARGETS, "starts": [design("TiO2_1600")] * 30, "methods": ["refine"], "top_n": 3})
            for _ in range(240):
                status = await call(session, "job_status", {"job_id": camp["job_id"]})
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.5)
            results = await call(session, "get_results", {"job_id": camp["job_id"], "plot": False})
            print("campaign over", camp["workers"], "workers:", status["status"], "| failed tasks:",
                  status["tasks_failed"], "| errors:", results["errors"])
            assert status["status"] == "done" and results["errors"] == 0
    print("material cache check passed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ofmcp-")))
