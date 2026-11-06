# MCP for OpenFilters
<!-- mcp-name: io.github.BBrunoEsteves/mcp-for-openfilters -->

*An unofficial connector built on OpenFilters: not affiliated with or endorsed by the OpenFilters authors or
Polytechnique Montréal.*

Design, simulate and optimize **optical thin-film coatings** by talking to Claude.

This project is an [MCP](https://modelcontextprotocol.io) server around the calculation core of
**[OpenFilters](https://www.polymtl.ca/larfis/en/links)** (Stéphane Larouche and Ludvik Martinu,
Polytechnique Montréal), ported to Python 3 and run without its GUI. Claude plans the work and reads
compact summaries; the server does the computing — in parallel on all CPU cores — and writes full
results to disk, including `.ofp` project files that open in the OpenFilters GUI.

> Claude orchestrates; the server computes. One tool call can launch a whole campaign.

> **Built and tested with Claude.** The server follows the open MCP standard, but it has so far been
> developed and tested only with Claude (Claude Desktop and Claude Code). Other MCP clients may work,
> but they are untested: tool descriptions, plots and long-running jobs may behave differently there.

## What it can do

| Area | Tools |
|---|---|
| Materials | `list_materials`, `get_material_index`, `add_material` (constant, Cauchy, Sellmeier, n,k tables from files), `search_refractiveindex` + `import_refractiveindex_material` (any of the ~3,500 entries of the [refractiveindex.info](https://refractiveindex.info) database, downloaded on demand) |
| Single design | `describe_design`, `compute_spectrum` (R/T/A, phase, GD, GDD; s/p/unpolarized or any polarization angle; any angle of incidence; reverse side), `compute_angle_scan`, `compute_color`, `compute_electric_field`, `compute_monitoring_curves` (chosen wavelengths or spectroscopic; reverse side) |
| Targets & merit | `evaluate_design`, `layer_sensitivity` |
| Optimization | `optimize_design` — OpenFilters' refinement, **needle** and **step** methods |
| Temperature | `thermal_sensitivity` (how a peak moves per index and thickness change of each material; d&lambda;/dT, kelvin per FWHM, athermal dn/dT), `thermal_sweep` (peak versus temperature) — tunable and athermal filters |
| Campaigns | `run_batch` (parameter sweeps), `run_optimization_campaign` (many starts → ranked → optional yield stage), `tolerance_analysis` (Monte Carlo yield with thickness and index errors), `simulate_random_errors` (OpenFilters' Preproduction: mean, standard deviation and worst case of the curves) |
| Jobs | `job_status`, `get_results`, `list_jobs`, `cancel_job` — results survive a server restart |
| Files | `load_project`, `save_project` (native `.ofp`) |

Example requests to Claude:

* *"Design a dichroic on BK7 with R > 99 % at 532 and 1064 nm and T > 90 % at 633 nm for 0–20°,
  at most 40 layers of TiO2/SiO2. Run 100 needle optimizations from random starts and rank the
  best 10 by yield at ±1 % thickness error."*
* *"Add my ALD Al2O3 from this ellipsometry file and sweep the AR coating thickness ±10 %."*
* *"Find thin-film HfO2 data on refractiveindex.info, import it and redo the mirror with it."*
* *"Where is the electric-field peak of this mirror at 1064 nm, and what monitoring signal should I
  expect at 633 nm at the end of each layer?"*

Ready-made workflows (prompts) appear as `/` commands in Claude Code and in the attachment menu of Claude
Desktop: **design an antireflection coating**, **a mirror**, **an edge filter or dichroic**, **a narrow band-pass
filter**, and **review a design for manufacturing**. The design-format guide, the material list and the
OpenFilters example projects are available as resources (`openfilters://guide`, `openfilters://materials`,
`openfilters://examples/<name>`). Long optimizations and yield analyses report progress to apps that show it.

## Installation

Requires Python ≥ 3.10. OpenFilters itself does **not** need to be installed: its calculation engine is
built in (the OpenFilters GUI is only useful to open the `.ofp` files).

### With the installer (recommended)

From the [release page](https://github.com/BBrunoEsteves/mcp-for-openfilters/releases), download
`install_mcp_for_openfilters.py` and the package for your computer (or all `.whl` files) into one folder, then:

```bash
python install_mcp_for_openfilters.py
```

It creates a private environment in `~/.mcp-for-openfilters`, installs the package, registers the server in
Claude Desktop (backing up its settings) and Claude Code, and runs a self-test. Then fully quit and reopen
Claude Desktop. `--uninstall` removes it; `--from-github` downloads the packages with the GitHub CLI.

Packages are built for Windows (64-bit), Linux (x86-64) and macOS (Apple Silicon), Python 3.10–3.14, with
the compiled fast core included: no compiler needed.

### Manually

```bash
pip install mcp_for_openfilters-<version>-<python>-<platform>.whl
claude mcp add --scope user mcp-for-openfilters -- mcp-for-openfilters
```

For Claude Desktop, add to `claude_desktop_config.json` (Settings → Developer → Edit config):

```json
{"mcpServers": {"mcp-for-openfilters": {"command": "<full path to mcp-for-openfilters>"}}}
```

### Upgrading from openfilters-mcp (before 0.2.0)

The project was called *openfilters-mcp* until version 0.2.0. Fully quit Claude Desktop and Claude Code,
then run the new installer: it removes the old `openfilters` registration and the old private environment
(`~/.openfilters-mcp`) and registers `mcp-for-openfilters`. Your data folder `~/OpenFilters-MCP` is kept
and still used, and the old `OPENFILTERS_MCP_*` environment variables are still read. After a manual
install, `pip uninstall openfilters-mcp` and replace the `openfilters` entry in your Claude settings.

### From source (development)

```bash
git clone https://github.com/BBrunoEsteves/mcp-for-openfilters
cd mcp-for-openfilters
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # .venv/bin/pip on macOS/Linux
python dev/fetch_compiled.py               # fast C++ core compiled by GitHub Actions (or build it locally)
```

### Configuration

| Environment variable | Default | Meaning |
|---|---|---|
| `MCP_FOR_OPENFILTERS_HOME` | `~/MCP-for-OpenFilters` | data folder: `materials/`, `jobs/`, `projects/`, `outputs/` |
| `MCP_FOR_OPENFILTERS_WORKERS` | CPU count − 1 | parallel worker processes for jobs |

The names used before 0.2.0 (`OPENFILTERS_MCP_HOME`, `OPENFILTERS_MCP_WORKERS`) still work, and an existing
`~/OpenFilters-MCP` folder is used when `~/MCP-for-OpenFilters` does not exist.

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

Materials given as n,k tables have no data outside their wavelength range. There the design field
`"table_extrapolation"` decides: `"hold"` (default: n,k stay at the value at the nearest end of the data),
`"openfilters"` (OpenFilters' own extrapolation, identical to its GUI) or `"refuse"`. Every result computed
outside the data says so, with the n,k used and what the other choice would give; optimizations, campaigns
and yield analyses do not start until the choice is made (Claude asks you).

A target:

```json
{"kind": "R", "from": 1000, "to": 1100, "step": 5, "value": 0.99, "tolerance": 0.005,
 "inequality": ">=", "angle": 0, "polarization": "unpolarized"}
```

## Validation

The test suite (`pytest`) checks the ported core against analytic results (quarter-wave stacks) and
an independent transfer-matrix implementation (dispersive dielectrics, absorbing metals, 0–60°,
s/p/unpolarized; agreement < 1e-9) and textbook closed forms (quarter-wave antireflection and absentee
layers, V-coat zero, Brewster angle, quarter-wave stack peak reflectance and stop-band width), round-trips every example project through `.ofp` files, and runs
the refinement and needle optimizations of the OpenFilters examples.

## How the port was made

`src/openfilters_core` is generated from the original OpenFilters 1.1.1 sources by
`tools/port_from_py2.py`, an auditable script (mechanical Python 2 → 3 conversions, a short list of
targeted fixes, package-relative imports). The `_abeles` C++ extension uses the Python 3 C-API port by
Emil Kleijn; `_moremath` is ported by the same script. See `src/openfilters_core/PROVENANCE.md`.

## Privacy

Everything runs on your computer: no data is sent anywhere and there is no telemetry. The only network
access is downloading public refractiveindex.info data when you ask for it. See
[PRIVACY.md](PRIVACY.md).

## Citing

If you use this in published work, please cite OpenFilters:

> S. Larouche and L. Martinu, "OpenFilters: open-source software for the design, optimization, and
> synthesis of optical filters," *Applied Optics* **47**, C219–C230 (2008).

## Contact

- **Bug reports and feature requests:** [GitHub Issues](https://github.com/BBrunoEsteves/mcp-for-openfilters/issues)
  (please include the output of the `server_info` tool).
- **Collaboration and other questions:** Bruno Esteves, Center for MicroElectroMechanical Systems (CMEMS),
  University of Minho: [mcp.for.openfilters@gmail.com](mailto:mcp.for.openfilters@gmail.com).

## License

GPL-2.0-or-later, like OpenFilters. OpenFilters is © Stéphane Larouche; this project is not
affiliated with or endorsed by its authors or Polytechnique Montréal.
