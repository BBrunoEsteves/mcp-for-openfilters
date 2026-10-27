OpenFilters thin-film design for Claude (Claude Desktop and Claude Code), with the OpenFilters
calculation engine built in. Nothing else to install besides Python.

> **Built and tested with Claude.** The server follows the open MCP standard, but it has so far been
> developed and tested only with Claude (Claude Desktop and Claude Code). Other MCP clients may work,
> but they are untested: tool descriptions, plots and long-running jobs may behave differently there.

## Changes in this version

**Fixes (please update):**

- Optimizations could return broken designs (every thickness "NaN") that were reported as meeting all targets
  with 100 % yield and ranked first in campaigns. They are now detected and never returned.
- Targets like "R <= 0.2 %" counted as met up to 1.2 % (the default tolerance was used as a pass margin); they
  are now met only when they hold.
- Your own material with the same name as a built-in one (e.g. `TiO2`) was ignored in favour of the built-in
  data.
- Installs from source (computers without a ready-made package) silently used the slow calculation core.

**New:**

- Materials from the refractiveindex.info database: search it and import any entry, on demand.
- `remove_material` deletes one of your own materials.
- The needle method keeps improving where OpenFilters' own needle search stops early.
- Ready-made workflows (antireflection, mirror, edge filter, band-pass, manufacturing review) in the "+"
  menu of Claude Desktop and as `/` commands in Claude Code; OpenFilters example projects to attach.
- Progress updates during long optimizations and yield analyses.

## Install

1. Install **Python 3.10 or newer** (skip if you have it), then open a new terminal:
   - **Windows:** `winget install Python.Python.3.13` (or from python.org)
   - **macOS:** `brew install python` (or from python.org; Apple's built-in `python3` 3.9 is too old)
   - **Linux (Debian/Ubuntu):** `sudo apt install python3 python3-venv`
2. Download into one folder:
   - `install_openfilters_mcp.py`
   - the package for your computer. File names say which: `cp312` = Python 3.12;
     `win_amd64` = Windows, `macosx_11_0_arm64` = Mac with Apple Silicon, `manylinux` = Linux.
     If unsure, download all `.whl` files: the right one is picked automatically.
3. In a terminal, in that folder, run:
   - **Windows:** `py install_openfilters_mcp.py`
   - **macOS/Linux:** `python3 install_openfilters_mcp.py`

   The installer creates a private Python environment, installs the
   package, registers it in Claude Desktop and Claude Code, and runs a self-test.
4. Fully quit Claude Desktop and reopen it. In a new chat: *"Run openfilters server_info."*

To update an existing installation, fully quit Claude Desktop and Claude Code first (the installer
replaces the environment the running server uses), then run the installer again.

To remove it: `py install_openfilters_mcp.py --uninstall` (`python3` on macOS/Linux) (your data folder is kept).

Your materials, projects and results are stored in `~/OpenFilters-MCP`. `.ofp` project files open in
the OpenFilters GUI.
