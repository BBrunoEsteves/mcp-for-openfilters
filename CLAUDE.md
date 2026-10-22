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
- Build C++ extensions in place (needs MSVC Build Tools): `.venv/Scripts/python setup.py build_ext --inplace`

## Conventions

nm, degrees, R/T/A as fractions; N = n − ik; layers listed from the substrate outward. Never print to
stdout in server or worker code (stdout is the MCP channel). User-facing errors: raise
`engine.EngineError` (converted to `ToolError`).

## Status (2026-10-22)

Done: Python 3 port validated (analytic + independent TMM, < 1e-9), all optimizers working, 22 MCP tools
working end to end, 38 tests passing. Pending: compile C++ core (waiting for MSVC Build Tools), register
in Claude Desktop/Code, GitHub repo + CI wheels, PyPI, MCP Registry, Claude plugin submission.
