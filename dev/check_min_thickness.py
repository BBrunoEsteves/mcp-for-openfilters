"""Check min_thickness enforcement and best-design tracking on the cases that
exposed the needle add/remove cycle. Usage: python dev/check_min_thickness.py"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from mcp_for_openfilters import engine as e  # noqa: E402

AR = {"center_wavelength": 550, "formula": {"formula": "0.25H 0.25L H L", "materials": {"H": "TiO2", "L": "SiO2"}},
      "substrate": "BK7"}
AR_T = [{"angle": 0, "from": 450, "kind": "R", "polarization": "unpolarized", "step": 5, "to": 650,
         "tolerance": 0.005, "value": 0}]
DICH = {"substrate": "BK7", "layers": [{"material": m, "thickness": t} for m, t in [("TiO2", 80), ("SiO2", 120)] * 6]}
DICH_T = [{"kind": "R", "from": 520, "to": 545, "step": 2.5, "value": 0.99, "tolerance": 0.01, "inequality": ">="},
          {"kind": "T", "from": 600, "to": 680, "step": 5, "value": 0.9, "tolerance": 0.02, "inequality": ">="},
          {"kind": "R", "from": 1040, "to": 1090, "step": 5, "value": 0.99, "tolerance": 0.01, "inequality": ">="}]

CASES = [
    ("AR, user's options", AR, AR_T, {"max_total_thickness": 1000, "min_thickness": 10,
                                      "needle_materials": ["TiO2", "SiO2"], "time_limit": 300}),
    ("AR, no min", AR, AR_T, {"time_limit": 300}),
    ("AR, min 20 nm", AR, AR_T, {"min_thickness": 20, "time_limit": 300}),
    ("AR, min 30 nm", AR, AR_T, {"min_thickness": 30, "time_limit": 300}),
    ("dichroic, no min", DICH, DICH_T, {"time_limit": 120, "max_layers": 60}),
    ("dichroic, min 10 nm", DICH, DICH_T, {"min_thickness": 10, "time_limit": 120, "max_layers": 60}),
]

for name, design, targets, options in CASES:
    t0 = time.time()
    r = e.optimize(design, targets, "needle", options)
    print(f"{name:20s} {time.time() - t0:6.1f}s stop={r['stop_reason']:26s} met={r['all_targets_met']!s:5s} "
          f"chi2={r['merit_chi2']:10.4g} layers={r['nb_layers']:2d} thinnest={r['thinnest_layer_nm']} "
          f"removed={r['thin_layers_removed']} rounds={r['rounds']}")
    check = e.evaluate(r["design"], targets)["merit_chi2"]
    assert abs(check - r["merit_chi2"]) <= 1e-9 * max(1.0, check), (check, r["merit_chi2"])
    if options.get("min_thickness"):
        assert r["thinnest_layer_nm"] >= options["min_thickness"] - 1e-6, r["thinnest_layer_nm"]
print("all checks passed")
