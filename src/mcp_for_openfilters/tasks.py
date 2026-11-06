"""Operations executed in worker processes.

A task is {"op": name, "args": {...}, "params": {...}}: `args` are passed to
the engine function and `params` are labels (e.g. sweep values) copied into
the result row. Results are written to disk by the worker itself so they
survive a restart of the server.
"""

import json
import os
import sys
import traceback

import numpy as np

from . import engine, thermal


def worker_init():
    """Worker processes inherit the server's stdout, which carries the MCP
    protocol; send anything printed by the core (Python or C++) to stderr."""
    try:
        os.dup2(sys.stderr.fileno(), 1)
    except (OSError, ValueError, AttributeError):
        pass
    sys.stdout = sys.stderr


# run_batch peak statistics -> key of engine.half_max_summary.
PEAK_STATS = {"peak_wavelength": "peak_nm", "peak_value": "peak_value", "fwhm": "fwhm", "center": "half_max_center_nm"}


def spectrum_metrics(result, metrics):
    """Reduce spectra to scalar metrics.

    metrics: [{"name": "R_mean_vis", "quantity": "R", "from": 400, "to": 700,
               "stat": "mean"|"min"|"max"|"std"|"at"|"peak_wavelength"|"peak_value"|"fwhm"|"center",
               "wavelength": 550, "near": 532, "angle": 0, "polarization": "unpolarized"}]
    The peak stats use the peak between from and to (or the local peak nearest "near"); "center" is
    the middle of its half-maximum edges, "fwhm" is None when an edge is beyond the computed range."""
    wvls = result["wavelengths"]
    out = {}
    for i, m in enumerate(metrics or []):
        name = m.get("name") or f"{m.get('stat', 'mean')}_{m['quantity']}_{i}"
        candidates = [s for s in result["series"] if s["quantity"] == m["quantity"]
                      and ("angle" not in m or abs(s["angle"] - float(m["angle"])) < 1e-9)
                      and ("polarization" not in m or s["polarization"] ==
                           engine.polarization_name(engine.polarization_value(m["polarization"])))]
        if not candidates:
            raise engine.EngineError(f"metric {name}: no computed series matches {m}")
        values = candidates[0]["values"]
        stat = m.get("stat", "mean")
        if stat == "at":
            out[name] = float(np.interp(float(m["wavelength"]), wvls, values))
            continue
        if stat in PEAK_STATS:
            peak = engine.half_max_summary(wvls, values, {k: m[k] for k in ("near", "from", "to") if k in m})
            out[name] = peak[PEAK_STATS[stat]]
            continue
        if stat not in ("mean", "min", "max", "std"):
            raise engine.EngineError(f"metric {name}: stat must be one of mean, min, max, std, at, "
                                     f"{', '.join(PEAK_STATS)}")
        mask = (wvls >= float(m.get("from", wvls[0]))) & (wvls <= float(m.get("to", wvls[-1])))
        if not mask.any():
            raise engine.EngineError(f"metric {name}: empty wavelength band")
        out[name] = float({"mean": np.mean, "min": np.min, "max": np.max, "std": np.std}[stat](values[mask]))
    return out


def check_spectrum_metrics(args):
    """Refuse metrics that no computed series can match, before a batch runs (they failed every task)."""
    quantities = args.get("quantities", ("R", "T"))
    angles = [float(a) for a in args.get("angles", (0.0,))]
    polarizations = [engine.polarization_name(engine.polarization_value(p))
                     for p in args.get("polarizations", ("unpolarized",))]
    for i, m in enumerate(args.get("metrics") or []):
        name = m.get("name") or f"metric {i + 1}"
        if m.get("quantity") not in quantities:
            raise engine.EngineError(f"metric {name}: quantity {m.get('quantity')!r} is not computed "
                                     f"(quantities: {', '.join(quantities)})")
        if "angle" in m and not any(abs(float(m["angle"]) - a) < 1e-9 for a in angles):
            raise engine.EngineError(f"metric {name}: angle {m['angle']} is not computed (angles: "
                                     f"{', '.join(f'{a:g}' for a in angles)})")
        if "polarization" in m and engine.polarization_name(engine.polarization_value(m["polarization"]))                 not in polarizations:
            raise engine.EngineError(f"metric {name}: polarization {m['polarization']!r} is not computed "
                                     f"(polarizations: {', '.join(polarizations)}); add it to polarizations")
        if m.get("stat", "mean") not in ("mean", "min", "max", "std", "at", *PEAK_STATS):
            raise engine.EngineError(f"metric {name}: stat must be one of mean, min, max, std, at, "
                                     f"{', '.join(PEAK_STATS)}")
        if m.get("stat") == "at" and "wavelength" not in m:
            raise engine.EngineError(f"metric {name}: stat \"at\" needs a wavelength")


def _spectrum_task(args):
    metrics = args.pop("metrics", None)
    result = engine.spectra(**args)
    row = spectrum_metrics(result, metrics) if metrics else {}
    if not metrics:  # default: mean of each series
        for s in result["series"]:
            row[f"mean_{s['quantity']}_{s['angle']:g}deg_{s['polarization']}"] = float(np.mean(s["values"]))
    return {"metrics": row}


def _tolerance_task(args):
    return engine.tolerance(**args, keep_values=True)


OPS = {
    "spectrum": _spectrum_task,
    "evaluate": lambda a: engine.evaluate(**a),
    "optimize": lambda a: engine.optimize(**a),
    "tolerance": _tolerance_task,
    "check_tolerance": lambda a: engine.check_tolerance(**a),
    "check_long_job": lambda a: engine.check_long_job(**a),
    "sensitivity": lambda a: engine.sensitivity(**a),
    "color": lambda a: engine.color(**a),
    "thermal_sensitivity": lambda a: thermal.sensitivity(**a),
    "thermal_sweep": lambda a: thermal.sweep(**a),
}


def run(op, args):
    """Interactive call: returns the result or raises EngineError."""
    if op in OPS:
        return OPS[op](dict(args))
    return getattr(engine, op)(**args)


CANCEL_MARKER = "cancelled"  # file in a job folder: queued tasks are skipped
STARTED_SUFFIX = ".started"  # tasks/<index>.started: the task began running


def run_job_task(job_dir, index):
    """Run task `index` of a job and store its result file (skipped if the job was cancelled)."""
    if os.path.exists(os.path.join(job_dir, CANCEL_MARKER)):
        return None
    open(os.path.join(job_dir, "tasks", f"{index:06d}{STARTED_SUFFIX}"), "w").close()
    with open(os.path.join(job_dir, "tasks", f"{index:06d}.json"), encoding="utf-8") as infile:
        task = json.load(infile)
    try:
        result = {"ok": True, "params": task.get("params", {}), "result": OPS[task["op"]](dict(task["args"]))}
    except engine.EngineError as error:
        result = {"ok": False, "params": task.get("params", {}), "error": str(error)}
    except Exception as error:  # keep the campaign going; report the failure
        result = {"ok": False, "params": task.get("params", {}), "error": f"{type(error).__name__}: {error}",
                  "traceback": traceback.format_exc(limit=5)}
    path = os.path.join(job_dir, "results", f"{index:06d}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as outfile:
        json.dump(result, outfile, default=_json_default)
    os.replace(tmp, path)
    return index


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, complex):
        return [value.real, value.imag]
    raise TypeError(f"not serializable: {type(value)}")
