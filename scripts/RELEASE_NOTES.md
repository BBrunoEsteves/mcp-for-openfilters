OpenFilters thin-film design for Claude (Claude Desktop and Claude Code), with the OpenFilters
calculation engine built in. Nothing else to install besides Python.

## Install

1. Install **Python 3.10 or newer** (python.org; on Windows tick "Add python.exe to PATH").
2. Download into one folder:
   - `install_openfilters_mcp.py`
   - the package for your computer. File names say which: `cp312` = Python 3.12;
     `win_amd64` = Windows, `macosx_11_0_arm64` = Mac with Apple Silicon, `manylinux` = Linux.
     If unsure, download all `.whl` files: the right one is picked automatically.
3. In a terminal, in that folder, run:
   ```
   python install_openfilters_mcp.py
   ```
   (on macOS/Linux use `python3`). The installer creates a private Python environment, installs the
   package, registers it in Claude Desktop and Claude Code, and runs a self-test.
4. Fully quit Claude Desktop and reopen it. In a new chat: *"Run openfilters server_info."*

To remove it: `python install_openfilters_mcp.py --uninstall` (your data folder is kept).

Your materials, projects and results are stored in `~/OpenFilters-MCP`. `.ofp` project files open in
the OpenFilters GUI.
