OpenFilters thin-film design for Claude (Claude Desktop and Claude Code), with the OpenFilters
calculation engine built in. Nothing else to install besides Python.

> **Built and tested with Claude.** The server follows the open MCP standard, but it has so far been
> developed and tested only with Claude (Claude Desktop and Claude Code). Other MCP clients may work,
> but they are untested: tool descriptions, plots and long-running jobs may behave differently there.

## Changes in this version

**New name: MCP for OpenFilters** (was *openfilters-mcp*), so that it is not mistaken for part of OpenFilters
itself; it remains an unofficial connector built on the OpenFilters calculation engine. The package and command
are now `mcp-for-openfilters` and the server is registered as `mcp-for-openfilters`. Upgrading: fully quit
Claude Desktop and Claude Code, then run the new installer; it removes the old `openfilters` registration and
environment. Your data folder (`~/OpenFilters-MCP`) is kept and still used.

**Fixes (please update):**

- Optimizations could return broken designs (every thickness "NaN") that were reported as meeting all targets
  with 100 % yield and ranked first in campaigns. They are now detected and never returned.
- Targets like "R <= 0.2 %" counted as met up to 1.2 % (the default tolerance was used as a pass margin); they
  are now met only when they hold.
- Your own material with the same name as a built-in one (e.g. `TiO2`) was ignored in favour of the built-in
  data.
- Installs from source (computers without a ready-made package) silently used the slow calculation core.
- Optimizations ran a slow solver even with the compiled core installed (now about 3x faster).
- Group delay and GDD were reported in s and s^2 under fs labels, and GD/GDD targets never worked; everything
  is now in fs and fs^2, as in the OpenFilters GUI.
- A mixture index outside the mixture's range crashed the calculation process; it is now refused with the range.
- Saving a project with characters outside Latin-1 (e.g. "λ") failed on Windows.
- The electric field inside graded layers was only sampled at step boundaries (peaks could be missed).
- A polarization angle (e.g. 30°, the fourth choice of OpenFilters' dialogs) was refused by every tool.
- Optimizations with an absorptance (A) target always failed (a bug in OpenFilters itself).
- About 4 % of optimizations with few target points (e.g. a single laser wavelength and three or more layers)
  ended with every thickness "NaN": OpenFilters' compiled solver used uninitialized memory. Fixed in the
  calculation core.
- On macOS, reflectance beyond the critical angle (light from the substrate side at steep angles) was slightly
  wrong.
- The needle method could crawl for its whole time limit, or stop with new layers left at 0 nm and return a
  much worse design (a two-layer V-coat campaign: 10 of 12 starts met the target, now 12 of 12, in seconds).
- Cancelled campaigns now rank and save the starts that finished (duplicates marked, project file written).

**New:**

- `simulate_random_errors`: OpenFilters' Preproduction > Simulate random errors (mean, standard deviation and
  worst case of the curves for random thickness errors, no targets needed).
- Spectroscopic (broadband) optical monitoring, and monitoring from the substrate side.
- Thermal model: `thermal_sensitivity` and `thermal_sweep` give a peak's shift with temperature
  (dn/dT, expansion, Poisson ratio), kelvin per FWHM and the dn/dT that makes a design athermal.
- Graded (rugate, inhomogeneous) layers: generated profiles (rugate with apodization and several notches,
  quintic and linear ramps, custom profiles) with index matching to the substrate and the medium; supported by
  plots, the electric field, tolerance analysis and layer sensitivity.
- Layers kept fixed in every optimization (`"fixed": true`, e.g. an electrode).
- Tabulated materials used beyond their data: choose "hold", "openfilters" or "refuse" (`table_extrapolation`);
  long jobs ask first.
- Materials from the refractiveindex.info database: search it and import any entry, on demand.
- `remove_material` deletes one of your own materials.
- The needle method keeps improving where OpenFilters' own needle search stops early.
- Ready-made workflows (antireflection, mirror, edge filter, band-pass, manufacturing review) in the "+"
  menu of Claude Desktop and as `/` commands in Claude Code; OpenFilters example projects to attach.
- Progress updates during long optimizations and yield analyses.
- Peak width (FWHM) and threshold ranges in spectra; a `margin` option for inequality targets, and warnings
  when a design meets its targets on the grid but not between grid points.

## Install

1. Install **Python 3.10 or newer** (skip if you have it), then open a new terminal:
   - **Windows:** `winget install Python.Python.3.13` (or from python.org)
   - **macOS:** `brew install python` (or from python.org; Apple's built-in `python3` 3.9 is too old)
   - **Linux (Debian/Ubuntu):** `sudo apt install python3 python3-venv`
2. Download into one folder:
   - `install_mcp_for_openfilters.py`
   - the package for your computer. File names say which: `cp312` = Python 3.12;
     `win_amd64` = Windows, `macosx_11_0_arm64` = Mac with Apple Silicon, `manylinux` = Linux.
     If unsure, download all `.whl` files: the right one is picked automatically.
3. In a terminal, in that folder, run:
   - **Windows:** `py install_mcp_for_openfilters.py`
   - **macOS/Linux:** `python3 install_mcp_for_openfilters.py`

   The installer creates a private Python environment, installs the
   package, registers it in Claude Desktop and Claude Code, and runs a self-test.
4. Fully quit Claude Desktop and reopen it. In a new chat: *"Run the MCP for OpenFilters server_info tool."*

To update an existing installation, fully quit Claude Desktop and Claude Code first (the installer
replaces the environment the running server uses), then run the installer again.

To remove it: `py install_mcp_for_openfilters.py --uninstall` (`python3` on macOS/Linux) (your data folder is kept).

Your materials, projects and results are stored in `~/MCP-for-OpenFilters` (or in `~/OpenFilters-MCP` if you
used an earlier version). `.ofp` project files open in
the OpenFilters GUI.
