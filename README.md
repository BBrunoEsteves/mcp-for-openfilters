# OpenFilters MCP

Design, simulate and optimize **optical thin-film coatings** by talking to Claude.

This project is an [MCP](https://modelcontextprotocol.io) server around the calculation core of
**[OpenFilters](https://www.polymtl.ca/larfis/en/links)** (Stéphane Larouche and Ludvik Martinu,
Polytechnique Montréal), ported to Python 3 and run without its GUI. Claude plans the work and reads
compact summaries; the server does the computing — in parallel on all CPU cores — and writes full
results to disk, including `.ofp` project files that open in the OpenFilters GUI.

> Claude orchestrates; the server computes. One tool call can launch a whole campaign.

## What it can do

| Area | Tools |
|---|---|
| Materials | `list_materials`, `get_material_index`, `add_material` (constant, Cauchy, Sellmeier, n,k tables from files) |
| Single design | `describe_design`, `compute_spectrum` (R/T/A, phase, GD, GDD; s/p/unpolarized; any angle; reverse side), `compute_angle_scan`, `compute_color`, `compute_electric_field`, `compute_monitoring_curves` |
| Targets & merit | `evaluate_design`, `layer_sensitivity` |
| Optimization | `optimize_design` — OpenFilters' refinement, **needle** and **step** methods |
| Campaigns | `run_batch` (parameter sweeps), `run_optimization_campaign` (many starts → ranked → optional yield stage), `tolerance_analysis` (Monte Carlo yield with thickness and index errors) |
| Jobs | `job_status`, `get_results`, `list_jobs`, `cancel_job` — results survive a server restart |
| Files | `load_project`, `save_project` (native `.ofp`) |

Example requests to Claude:

* *"Design a dichroic on BK7 with R > 99 % at 532 and 1064 nm and T > 90 % at 633 nm for 0–20°,
  at most 40 layers of Ta2O5/SiO2. Run 100 needle optimizations from random starts and rank the
  best 10 by yield at ±1 % thickness error."*
* *"Add my ALD Al2O3 from this ellipsometry file and sweep the AR coating thickness ±10 %."*
* *"Where is the electric-field peak of this mirror at 1064 nm, and what monitoring signal should I
  expect at 633 nm at the end of each layer?"*

## Installation

Requires Python ≥ 3.10.

```bash
pip install openfilters-mcp
```

or from source:

```bash
git clone <this repository>
cd open_filters_connector
python -m venv .venv
.venv/Scripts/pip install -e .     # Windows (use .venv/bin/pip on macOS/Linux)
```

The fast C++ core is compiled during installation when a C++ compiler is available (Visual Studio
Build Tools on Windows, Xcode command-line tools on macOS, gcc on Linux). Without one, everything
still works using OpenFilters' pure-Python implementation, only slower. `server_info` tells you
which one is active.

### Register with Claude

**Claude Code**

```bash
claude mcp add openfilters -- openfilters-mcp
```

**Claude Desktop** — add to `claude_desktop_config.json` (Settings → Developer → Edit config):

```json
{
  "mcpServers": {
    "openfilters": { "command": "openfilters-mcp" }
  }
}
```

If `openfilters-mcp` is not on your PATH, use the full path of the Python executable and
`"args": ["-m", "openfilters_mcp"]`.

### Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `OPENFILTERS_MCP_HOME` | `~/OpenFilters-MCP` | data folder: `materials/`, `jobs/`, `projects/`, `outputs/` |
| `OPENFILTERS_MCP_WORKERS` | CPU count − 1 | parallel worker processes for jobs |

## Conventions

* wavelengths and thicknesses in **nm**, angles in degrees, R/T/A as fractions (0–1);
* complex index **N = n − ik** (k ≥ 0 absorbs), as in OpenFilters and Macleod;
* layers are listed **from the substrate outward**;
* the merit function is OpenFilters' χ² = Σ ((target − value)/tolerance)², with satisfied
  inequality targets contributing zero.

A design:

```json
{"substrate": "FusedSilica", "incident_medium": "void", "center_wavelength": 550,
 "layers": [{"material": "TiO2", "thickness": 58.6}, {"material": "SiO2", "qwot": 1}]}
```

or with a stack formula: `{"formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}}`.

A target:

```json
{"kind": "R", "from": 1000, "to": 1100, "step": 5, "value": 0.99, "tolerance": 0.005,
 "inequality": ">=", "angle": 0, "polarization": "unpolarized"}
```

## Validation

The test suite (`pytest`) checks the ported core against analytic results (quarter-wave stacks) and
an independent transfer-matrix implementation (dispersive dielectrics, absorbing metals, 0–60°,
s/p/unpolarized; agreement < 1e-9), round-trips every example project through `.ofp` files, and runs
the refinement and needle optimizations of the OpenFilters examples.

## How the port was made

`src/openfilters_core` is generated from the original OpenFilters 1.1.1 sources by
`tools/port_from_py2.py`, an auditable script (mechanical Python 2 → 3 conversions, a short list of
targeted fixes, package-relative imports). The `_abeles` C++ extension uses the Python 3 C-API port by
Emil Kleijn; `_moremath` is ported by the same script. See `src/openfilters_core/PROVENANCE.md`.

## Citing

If you use this in published work, please cite OpenFilters:

> S. Larouche and L. Martinu, "OpenFilters: open-source software for the design, optimization, and
> synthesis of optical filters," *Applied Optics* **47**, C219–C230 (2008).

## License

GPL-2.0-or-later, like OpenFilters. OpenFilters is © Stéphane Larouche; this project is not
affiliated with or endorsed by its authors or Polytechnique Montréal.
