"""Background jobs.

A job is a folder holding its task files, one result file per finished task
and, once complete, summary tables and .ofp projects. Tasks run in a pool of
worker processes; because workers write their own result files, a job that
was running when the server stopped is resumed (missing tasks only) the next
time the server starts.
"""

import concurrent.futures as cf
import csv
import datetime
import functools
import json
import multiprocessing
import os
import threading
import time
import uuid

from . import engine, settings, tasks

MAX_TASKS = 100000


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as outfile:
        json.dump(data, outfile, indent=1, default=tasks._json_default)
    os.replace(tmp, path)


def _read_json(path):
    with open(path, encoding="utf-8") as infile:
        return json.load(infile)


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------

def result_row(index, op, result):
    row = {"task": index, **result.get("params", {})}
    if not result.get("ok"):
        row["error"] = result.get("error", "unknown error")
        return row
    r = result["result"]
    if op == "spectrum":
        row.update(r["metrics"])
    elif op in ("optimize", "evaluate"):
        for key in ("merit_chi2", "all_targets_met", "nb_layers", "total_thickness_nm", "iterations",
                    "stop_reason", "elapsed_s", "initial_chi2", "thin_layers_removed", "error_detail", "warnings"):
            if key in r:
                row[key] = r[key]
    elif op == "tolerance":
        for key in ("yield", "passes", "n_trials", "chi2_median", "chi2_p90", "nominal_chi2"):
            if key in r:
                row[key] = r[key]
    elif op == "color":
        c = r["colors"][0]
        row.update(L=c["Lab"][0], a=c["Lab"][1], b=c["Lab"][2], x=c["xy"][0], y=c["xy"][1], hex=c["hex"])
    else:
        row["result"] = r
    return row


def rank_key(row):
    """Best first. With a yield stage, yield comes first (the chance a real coating meets the
    specification: a nominal pass with 0 % yield is worth less than a 1e-7 miss with 26 %);
    then meets all targets, low merit, few layers, thin. Merits below ZERO_MERIT are rounding noise
    (3e-23 vs 5e-22), so layer count and thickness decide between them."""
    if "error" in row:
        return (3, 0, 0, 0, 0, 0)
    merit = row.get("merit_chi2", float("inf"))
    return (0 if "yield" in row else 1, -row.get("yield", 0.0), 0 if row.get("all_targets_met") else 1,
            0.0 if merit < ZERO_MERIT else merit, row.get("nb_layers", 0), row.get("total_thickness_nm", 0.0))


ZERO_MERIT = 1e-9  # chi2 this small = every point within ~3e-5 of its tolerance-weighted goal


def write_table(path, rows):
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()})


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------

class JobManager:
    def __init__(self, workers=None):
        self.workers = workers or settings.workers()
        self._pool = None
        self._lock = threading.RLock()
        self._futures = {}
        self.resume_interrupted()

    # -- infrastructure -----------------------------------------------------

    @property
    def pool(self):
        with self._lock:
            if self._pool is None:
                self._pool = cf.ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"),
                                                    initializer=tasks.worker_init)
            return self._pool

    def shutdown(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def job_dir(self, job_id):
        path = os.path.join(settings.jobs_dir(), job_id)
        if not os.path.isdir(path) or os.path.basename(os.path.normpath(job_id)) != job_id:
            raise engine.EngineError(f"unknown job {job_id}")
        return path

    def _load(self, job_id):
        return _read_json(os.path.join(self.job_dir(job_id), "job.json"))

    def _save(self, job):
        with self._lock:
            _write_json(os.path.join(settings.jobs_dir(), job["id"], "job.json"), job)

    def _done_indices(self, job_id):
        folder = os.path.join(self.job_dir(job_id), "results")
        return {int(name[:6]) for name in os.listdir(folder) if name.endswith(".json")}

    # -- submission ---------------------------------------------------------

    def submit(self, kind, task_list, meta=None, label=""):
        if not task_list:
            raise engine.EngineError("nothing to run")
        if len(task_list) > MAX_TASKS:
            raise engine.EngineError(f"too many tasks ({len(task_list)}; max {MAX_TASKS})")
        task_list, meta = engine.resolve_design_files(task_list), engine.resolve_design_files(meta)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{kind}-{uuid.uuid4().hex[:6]}"
        folder = os.path.join(settings.jobs_dir(), job_id)
        os.makedirs(os.path.join(folder, "tasks"))
        os.makedirs(os.path.join(folder, "results"))
        job = {"id": job_id, "kind": kind, "label": label, "created": time.time(), "status": "running",
               "stages": [], "meta": meta or {}, "folder": folder}
        self._save(job)
        self._add_stage(job, task_list)
        return job_id

    def _add_stage(self, job, task_list):
        folder = os.path.join(settings.jobs_dir(), job["id"])
        start = job["stages"][-1][1] if job["stages"] else 0
        for i, task in enumerate(task_list):
            _write_json(os.path.join(folder, "tasks", f"{start + i:06d}.json"), task)
        job["stages"].append([start, start + len(task_list), task_list[0]["op"]])
        job["stage_started"] = time.time()
        self._save(job)
        self._schedule(job["id"], range(start, start + len(task_list)))

    def _schedule(self, job_id, indices):
        folder = self.job_dir(job_id)
        done = self._done_indices(job_id)
        for index in indices:
            if index in done:
                continue
            future = self.pool.submit(tasks.run_job_task, folder, index)
            with self._lock:
                self._futures.setdefault(job_id, {})[index] = future
            future.add_done_callback(functools.partial(self._task_finished, job_id, index))
        self._check_stage(job_id)

    def _task_finished(self, job_id, index, future):
        with self._lock:
            self._futures.get(job_id, {}).pop(index, None)
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:  # the worker process died
            path = os.path.join(self.job_dir(job_id), "results", f"{index:06d}.json")
            if not os.path.exists(path):
                _write_json(path, {"ok": False, "params": {}, "error": f"worker failure: {error!r}"})
            if isinstance(error, cf.process.BrokenProcessPool):
                with self._lock:
                    self._pool = None
        self._check_stage(job_id)

    def _check_stage(self, job_id):
        with self._lock:
            job = self._load(job_id)
            if job["status"] != "running":
                return
            start, end, _ = job["stages"][-1]
            done = self._done_indices(job_id)
            if not all(i in done for i in range(start, end)) or job.get("finalizing"):
                return
            job["finalizing"] = True
            self._save(job)
        threading.Thread(target=self._finalize, args=(job_id,), daemon=True).start()

    def _finalize(self, job_id):
        job = self._load(job_id)
        try:
            next_tasks = FINALIZERS[job["kind"]](self, job)
            job = self._load(job_id)
            job.pop("finalizing", None)
            if next_tasks:
                self._save(job)
                self._add_stage(job, next_tasks)
                return
            start, end, _ = job["stages"][0]
            results = [self.result(job_id, i) for i in range(start, end)]
            if results and not any(r.get("ok") for r in results):
                job["status"], job["error"] = "failed", f"every task failed; first error: {results[0].get('error')}"
            else:
                job["status"] = "done"
        except Exception as error:
            job = self._load(job_id)
            job.pop("finalizing", None)
            job["status"], job["error"] = "failed", f"{type(error).__name__}: {error}"
        job["finished"] = time.time()
        self._save(job)

    def resume_interrupted(self):
        root = settings.jobs_dir()
        for job_id in sorted(os.listdir(root)):
            path = os.path.join(root, job_id, "job.json")
            if not os.path.exists(path):
                continue
            job = _read_json(path)
            if job.get("status") == "running":
                job.pop("finalizing", None)
                job["resumed"] = time.time()
                self._save(job)
                for name in os.listdir(os.path.join(root, job_id, "tasks")):
                    if name.endswith(tasks.STARTED_SUFFIX):  # from before the restart
                        os.remove(os.path.join(root, job_id, "tasks", name))
                start, end, _ = job["stages"][-1]
                self._schedule(job_id, range(start, end))

    # -- queries ------------------------------------------------------------

    def status(self, job_id):
        job = self._load(job_id)
        start, end, op = job["stages"][-1]
        done = self._done_indices(job_id)
        finished = sum(1 for i in range(start, end) if i in done)
        failed = sum(1 for i in range(start, end) if i in done and
                     not _read_json(os.path.join(job["folder"], "results", f"{i:06d}.json")).get("ok"))
        info = {"job_id": job_id, "kind": job["kind"], "label": job.get("label", ""), "status": job["status"],
                "stage": f"{len(job['stages'])} ({op})", "tasks_done": finished, "tasks_total": end - start,
                "tasks_failed": failed, "folder": job["folder"]}
        if job["status"] == "running" and finished:
            rate = finished / max(time.time() - job.get("stage_started", job["created"]), 1e-6)
            info["eta_s"] = round((end - start - finished) / rate, 1)
        if len(job["stages"]) > 1:  # e.g. a campaign's yield stage: the counts above are for this stage
            info["earlier_stages"] = [f"{e - b} {o} tasks" for b, e, o in job["stages"][:-1]]
        if job.get("error"):
            info["error"] = job["error"]
        if job["status"] == "done":
            info["outputs"] = job.get("outputs", {})
        return info

    def list(self, limit=20):
        root = settings.jobs_dir()
        rows = []
        for job_id in sorted(os.listdir(root), reverse=True)[:limit]:
            try:
                rows.append({k: v for k, v in self.status(job_id).items() if k != "folder"})
            except Exception:
                continue
        return rows

    def cancel(self, job_id):
        job = self._load(job_id)
        if job["status"] != "running":
            return {"job_id": job_id, "status": job["status"], "tasks_cancelled": 0,
                    "note": f"the job is already {job['status']}; nothing to cancel"}
        # The marker stops tasks the process pool has already queued (they cannot be cancelled as futures):
        # a worker checks it before starting a task (tasks.run_job_task).
        open(os.path.join(job["folder"], tasks.CANCEL_MARKER), "w").close()
        with self._lock:
            futures = list(self._futures.get(job_id, {}).values())
        for f in futures:
            f.cancel()
        job["status"] = "cancelled"
        job["finished"] = time.time()
        self._save(job)
        start, end, _ = job["stages"][-1]
        done = self._done_indices(job_id)
        running = sum(1 for i in range(start, end) if i not in done
                      and os.path.exists(os.path.join(job["folder"], "tasks", f"{i:06d}{tasks.STARTED_SUFFIX}")))
        return {"job_id": job_id, "status": "cancelled", "tasks_done": len(done & set(range(start, end))),
                "tasks_still_running": running, "tasks_cancelled": end - start - len(done & set(range(start, end))) - running,
                "note": "tasks already running finish on their own (bounded by their time limits); "
                        "get_results still works on what finished"}

    def rows(self, job_id, stage=None):
        job = self._load(job_id)
        stages = job["stages"] if stage is None else [job["stages"][stage]]
        rows = []
        for start, end, op in stages:
            for i in range(start, end):
                path = os.path.join(job["folder"], "results", f"{i:06d}.json")
                if os.path.exists(path):
                    rows.append(result_row(i, op, _read_json(path)))
        return rows

    def result(self, job_id, index):
        job = self._load(job_id)
        return _read_json(os.path.join(job["folder"], "results", f"{int(index):06d}.json"))

    def results(self, job_id, top_n=10, sort_by=None, descending=False):
        job = self._load(job_id)
        summary = os.path.join(job["folder"], "summary.json")
        if job["status"] == "done" and os.path.exists(summary):
            rows = _read_json(summary)["rows"]
        else:
            rows = self.rows(job_id, stage=0) if job["kind"] in ("campaign",) else self.rows(job_id)
            if job["kind"] == "campaign":
                rows.sort(key=rank_key)
                for rank, row in enumerate(r for r in rows if "error" not in r):
                    row["rank"] = rank + 1  # provisional: duplicates are only marked when the campaign finishes
        if sort_by:
            rows = sorted([r for r in rows if sort_by in r], key=lambda r: r[sort_by], reverse=descending)
        out = {"job_id": job_id, "status": job["status"], "rows_total": len(rows), "rows": rows[:int(top_n)],
               "errors": sum(1 for r in rows if "error" in r)}
        if job.get("outputs"):
            out["outputs"] = job["outputs"]
        if job.get("summary"):
            out["summary"] = job["summary"]
        return out


# ---------------------------------------------------------------------------
# Finalizers: build summaries; may return the tasks of a next stage
# ---------------------------------------------------------------------------

def _finish_table(manager, job, rows, extra_outputs=None, summary=None):
    folder = job["folder"]
    write_table(os.path.join(folder, "summary.csv"), rows)
    _write_json(os.path.join(folder, "summary.json"), {"rows": rows, "summary": summary or {}})
    job = manager._load(job["id"])
    job["outputs"] = {"summary_csv": os.path.join(folder, "summary.csv"), **(extra_outputs or {})}
    if summary:
        job["summary"] = summary
    manager._save(job)


def finalize_batch(manager, job):
    rows = manager.rows(job["id"])
    outputs = {}
    start, end, op = job["stages"][0]
    if op == "optimize":
        designs = {}
        for i in range(start, end):
            r = manager.result(job["id"], i)
            if r.get("ok"):
                designs[i] = r["result"]["design"]
        _write_json(os.path.join(job["folder"], "designs.json"), designs)
        outputs["designs_json"] = os.path.join(job["folder"], "designs.json")
    _finish_table(manager, job, rows, outputs)


def finalize_optimize(manager, job):
    r = manager.result(job["id"], 0)
    outputs = {}
    if r.get("ok"):
        meta = job["meta"]
        path = os.path.join(job["folder"], "optimized.ofp")
        start = dict(meta["design"], description=meta["design"].get("description") or "Starting design (before optimization)")
        engine.save_project(path, [start, r["result"]["design"]], meta["targets"],
                            "Filter 1: starting design; filter 2: optimized design")
        outputs["project"] = path
    _finish_table(manager, job, manager.rows(job["id"]), outputs)


def finalize_tolerance(manager, job):
    passes = trials = 0
    chi2, failures = [], None
    for i in range(*job["stages"][0][:2]):
        r = manager.result(job["id"], i)
        if not r.get("ok"):
            raise engine.EngineError(r.get("error"))
        res = r["result"]
        passes, trials = passes + res["passes"], trials + res["n_trials"]
        chi2 += res["chi2_values"]
        f = res["target_failures"]
        failures = f if failures is None else [a + b for a, b in zip(failures, f)]
    import numpy as np
    summary = {"yield": passes / trials, "passes": passes, "n_trials": trials,
               "yield_95ci": _wilson(passes, trials),
               "nominal_passes": res["nominal_passes"], "nominal_chi2": res["nominal_chi2"],
               "chi2_median": float(np.median(chi2)), "chi2_p90": float(np.percentile(chi2, 90)),
               "target_failure_rate": [round(x / trials, 4) for x in failures],
               "thickness_error": res["thickness_error"], "index_error": res["index_error"]}
    _finish_table(manager, job, manager.rows(job["id"]), summary=summary)


def _wilson(k, n, z=1.96):
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def same_design(a, b, chi2_a=None, chi2_b=None):
    """Same layer materials and thicknesses within 0.5 nm or 0.5 % (starts often converge to one design).
    With merits equal to 1e-6 (relative), thicknesses within 3 nm or 3 % also count: a merit that is flat
    along one direction leaves converged copies a nanometre or two apart (campaign 2026-10-24: chi2
    285.92732 vs 285.92733, one layer 102.04 vs 100.80 nm)."""
    la, lb = a["layers"], b["layers"]
    close = (chi2_a is not None and chi2_b is not None
             and abs(chi2_a - chi2_b) <= 1e-6 * max(abs(chi2_a), abs(chi2_b), 1e-12))
    nm, rel = (3.0, 0.03) if close else (0.5, 0.005)
    return len(la) == len(lb) and all(
        x["material"] == y["material"] and "thickness" in x and "thickness" in y
        and abs(x["thickness"] - y["thickness"]) <= max(nm, rel * max(x["thickness"], y["thickness"]))
        for x, y in zip(la, lb))


def distinct_top(manager, job, rows, n):
    """The first n rows (already ranked) with distinct designs. Every repeat of an earlier distinct design
    gets duplicate_of, also beyond the first n (scenario S5.12: two identical rows outside the top were
    left unmarked)."""
    top, kept = [], []
    for row in rows:
        if "error" in row:
            continue
        design = manager.result(job["id"], row["task"])["result"]["design"]
        original = next((r for r, d in kept
                         if same_design(design, d, row.get("merit_chi2"), r.get("merit_chi2"))), None)
        if original is not None:
            row["duplicate_of"] = original["task"]
        else:
            if len(top) < n:
                top.append(row)
            kept.append((row, design))
    return top


def finalize_campaign(manager, job):
    meta = job["meta"]
    if len(job["stages"]) == 1:
        rows = sorted(manager.rows(job["id"], stage=0), key=rank_key)
        best = distinct_top(manager, job, rows, meta["top_n"])
        if meta.get("tolerance") and best:
            stage_tasks = []
            for rank, row in enumerate(best):
                design = manager.result(job["id"], row["task"])["result"]["design"]
                stage_tasks.append({"op": "tolerance", "params": {"rank": rank + 1, "task": row["task"]},
                                    "args": {**meta["tolerance"], "design": design, "target_specs": meta["targets"]}})
            return stage_tasks
    rows = sorted(manager.rows(job["id"], stage=0), key=rank_key)
    if len(job["stages"]) > 1:
        # Stage-1 tasks carry the index of the stage-0 optimization they test.
        by_task = {row["task"]: row for row in rows}
        start, end, _ = job["stages"][1]
        for i in range(start, end):
            r = manager.result(job["id"], i)
            if r.get("ok"):
                row = by_task[r["params"]["task"]]
                row["yield"] = r["result"]["yield"]
                # Yields from a few hundred trials differ by noise: 0.315 and 0.22 at n = 200 overlap.
                row["yield_ci95"] = _wilson(r["result"]["passes"], r["result"]["n_trials"])
                row["yield_chi2_median"] = r["result"]["chi2_median"]
        rows.sort(key=rank_key)
    top = distinct_top(manager, job, rows, meta["top_n"])
    designs = [manager.result(job["id"], r["task"])["result"]["design"] for r in top]
    for rank, (row, design) in enumerate(zip(top, designs)):
        label = f"Campaign {job['id']}, rank {rank + 1} (start {row['task']})"
        if row.get("yield") is not None:
            label += f", yield {row['yield']:.1%}"
        design["description"] = label + ". " + design.get("description", "")
    outputs = {}
    if designs:
        path = os.path.join(job["folder"], "top_designs.ofp")
        engine.save_project(path, designs, meta["targets"],
                            "Top designs of campaign " + job["id"] + ", best first")
        outputs["project"] = path
        _write_json(os.path.join(job["folder"], "top_designs.json"), designs)
        outputs["designs_json"] = os.path.join(job["folder"], "top_designs.json")
    # Every distinct design gets a rank; top_n only limits the designs saved and plotted.
    distinct = top + [r for r in rows if r not in top and "error" not in r and "duplicate_of" not in r]
    for rank, row in enumerate(distinct):
        row["rank"] = rank + 1
    summary = {"starts": len(rows), "met_all_targets": sum(1 for r in rows if r.get("all_targets_met")),
               "failed": sum(1 for r in rows if "error" in r),
               # Not failures (a design is returned) but worth seeing: OpenFilters' optimizer stopped on an error.
               "openfilters_errors": sum(1 for r in rows if r.get("stop_reason") == "openfilters_error"),
               "duplicates": sum(1 for r in rows if "duplicate_of" in r),
               "stop_reasons": {reason: engine.STOP_REASONS.get(reason, "")
                                for reason in sorted({r["stop_reason"] for r in rows if r.get("stop_reason")})}}
    if top and all(r.get("yield") is not None for r in top) and not any(r["yield"] for r in top):
        summary["note"] = "No design passed the yield stage (all yields 0): the ranking is by merit."
    if meta.get("warnings"):
        summary["warnings"] = meta["warnings"]
    _finish_table(manager, job, top + [r for r in rows if r not in top], outputs, summary)


FINALIZERS = {"batch": finalize_batch, "optimize": finalize_optimize, "tolerance": finalize_tolerance,
              "campaign": finalize_campaign}
