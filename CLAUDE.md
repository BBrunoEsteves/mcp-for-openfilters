# OpenFilters MCP connector

General-purpose MCP server that lets Claude drive OpenFilters (thin-film optical coating design,
Larouche & Martinu, Polytechnique Montréal, GPL-2.0+) without its GUI. Goal: a published,
reusable connector (PyPI + MCP Registry + Claude plugin), not one specific application.

Principle: Claude orchestrates, the server computes. One tool call = one whole campaign; heavy work
runs in parallel worker processes, results go to disk, tools return compact summaries.

## Layout

- `src/openfilters_core/` — OpenFilters calculation core ported to Python 3. **Generated**: do not edit
  by hand; change `tools/port_from_py2.py` and regenerate:
  `python tools/port_from_py2.py third_party/openfilters_original third_party/openfilters_py3 src/openfilters_core`
  (then restore `src/openfilters_core/PROVENANCE.md`, which the script deletes).
  `third_party/` holds pristine clones of gyger/OpenFilters and EmilKleijn/OpenFilters (gitignored).
- `src/openfilters_mcp/engine.py` — JSON ⇄ OpenFilters bridge (designs, spectra, targets, optimize, Monte Carlo, .ofp).
- `src/openfilters_mcp/tasks.py` — operations run in worker processes; workers write their own result files.
- `src/openfilters_mcp/jobs.py` — on-disk background jobs, multi-stage (campaign → yield stage), resumed after restart.
- `src/openfilters_mcp/server.py` — MCP tools (mcp SDK 2.x `MCPServer`, stdio).
- `tests/` — pytest; `tests/reference_tmm.py` is an independent transfer-matrix code used for validation.
- `dev/e2e_client.py` — starts the server over stdio and calls every tool.

## Commands

- Tests: `.venv/Scripts/python -m pytest tests -q`
- End-to-end: `.venv/Scripts/python dev/e2e_client.py`
- Fast C++ core without a local compiler: `.venv/Scripts/python dev/fetch_compiled.py` (downloads what GitHub Actions
  compiled; rerun after changing C++ code or after regenerating the core). With a compiler: `python setup.py build_ext --inplace`.

## Conventions

nm, degrees, R/T/A as fractions; N = n − ik; layers listed from the substrate outward. Never print to
stdout in server or worker code (stdout is the MCP channel). User-facing errors: raise
`engine.EngineError` (converted to `ToolError`).

## Status (2026-10-22)

Done: Python 3 port validated (analytic + independent TMM, < 1e-9; needle derivatives vs finite differences),
all optimizers working, 22 MCP tools working end to end, 50 tests. Private GitHub repo
BBrunoEsteves/openfilters-mcp; CI compiles the C++ core and tests on Windows/macOS/Linux and builds wheels
(cp310-cp314, runtime DLLs bundled). Compiled core is 15-30x faster than pure Python.
Registered locally (user scope) in Claude Code and Claude Desktop, pointing at .venv/Scripts/openfilters-mcp.exe;
config backups in .config_backups/. Claude Desktop shows tool images only to the model, so plots are also saved
as PNG files (plot_files). After changing server code, the user must fully quit and reopen Claude Desktop.

Optimization notes: never pass min_thickness to OpenFilters' needle/step loops (they cycle add/remove);
engine.optimize enforces it with bounded refinement (_enforce_min_thickness) and tracks the best design
(_Monitor). Check script: dev/check_min_thickness.py.

Two-sided parts: ONE design with back_layers + consider_backside: true (documented in the server instructions
after a session split a part into two designs). Optimizers vary front layers only; tolerance/sensitivity
perturb both faces; a design with no front layers is evaluated with a zero-thickness placeholder layer.
Spectrum values use 6 significant figures; "OD" quantity = -log10 T; wait_seconds capped at 45 s
(clients time out tool calls at ~60 s).

Releases: pushing a tag v* makes CI publish a GitHub release with all wheels + scripts/install_openfilters_mcp.py
(stdlib installer: private venv in ~/.openfilters-mcp/venv, registers in Claude Desktop and Claude Code).
v0.1.0 released (private) 2026-10-23; v0.1.1 (installer finds the Microsoft Store Claude Desktop, whose data is in
%LOCALAPPDATA%\Claude-Data\claude_<id>) 2026-10-23;
v0.1.2 (.ofp projects keep targets, layer flags and how designs were made) 2026-10-23. Update scripts/RELEASE_NOTES.md (release text) and bump the version in pyproject.toml and src/openfilters_mcp/__init__.py
before tagging the next one.

Pending: make public (consider contacting Larouche/Martinu first), PyPI, MCP Registry, Claude plugin;
Intel-Mac wheels if needed.
