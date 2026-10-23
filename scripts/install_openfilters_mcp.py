"""Install OpenFilters MCP for Claude Desktop and Claude Code.

Usage (Python 3.10 or newer, no other requirement):

    python install_openfilters_mcp.py              # install from the package files next to this script
    python install_openfilters_mcp.py --uninstall  # remove it again

Put this script in the same folder as the downloaded package file(s)
(openfilters_mcp-*.whl from the release page); the one matching this
computer is chosen automatically. Alternatively --from-github downloads the
packages with the GitHub CLI (gh, logged in with access to the repository).

What it does:
  1. creates a private Python environment for the server (your own Python
     packages are not touched): ~/.openfilters-mcp/venv
  2. installs the package into it
  3. registers the server as "openfilters" in Claude Desktop (its config file
     is backed up first) and in Claude Code (if found)
  4. runs a quick self-test
Restart Claude Desktop afterwards (fully quit it, then reopen).
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import venv

NAME = "openfilters"
REPO = "BBrunoEsteves/openfilters-mcp"
HOME = os.path.join(os.path.expanduser("~"), ".openfilters-mcp")


def say(message):
    print(message, flush=True)


def fail(message):
    say("\nERROR: " + message)
    sys.exit(1)


def venv_python(venv_dir):
    return os.path.join(venv_dir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv_dir, "bin", "python")


def server_command(venv_dir):
    return os.path.join(venv_dir, "Scripts", "openfilters-mcp.exe") if os.name == "nt" \
        else os.path.join(venv_dir, "bin", "openfilters-mcp")


def desktop_data_dirs():
    """Existing Claude Desktop user-data folders (where claude_desktop_config.json lives).

    On Windows the Microsoft Store version keeps its data in %LOCALAPPDATA%\\Claude-Data\\claude_<id>
    (older Store versions: the package's virtualized %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\
    Roaming\\Claude, which only holds a .user-data-left.* marker once the data has moved out).
    """
    if sys.platform == "win32":
        appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
        candidates = [os.path.join(appdata, "Claude")]
        candidates += sorted(glob.glob(os.path.join(local, "Claude-Data", "claude_*")))
        candidates += [folder for folder in sorted(glob.glob(os.path.join(
                           local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude")))
                       if not glob.glob(os.path.join(folder, ".user-data-left.*"))]
    elif sys.platform == "darwin":
        candidates = [os.path.expanduser("~/Library/Application Support/Claude")]
    else:
        candidates = [os.path.expanduser("~/.config/Claude")]
    return [folder for folder in candidates if os.path.isdir(folder)]


def find_claude_cli():
    """The `claude` command, or the newest copy bundled with Claude Desktop."""
    found = shutil.which("claude")
    if found:
        return found
    executable = "claude.exe" if sys.platform == "win32" else "claude"
    candidates = [path for folder in desktop_data_dirs()
                  for path in glob.glob(os.path.join(folder, "claude-code", "*", "*", executable))]
    candidates.sort(key=os.path.getmtime)
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------

def get_packages(args, workdir):
    if args.from_github:
        if not shutil.which("gh"):
            fail("--from-github needs the GitHub CLI (https://cli.github.com), logged in with `gh auth login`.")
        tag = [args.version] if args.version else []
        say(f"Downloading packages from {REPO} ...")
        result = subprocess.run(["gh", "release", "download", *tag, "--repo", REPO, "--pattern", "*.whl",
                                 "--dir", workdir], capture_output=True, text=True)
        if result.returncode != 0:
            fail("could not download the release: " + result.stderr.strip())
        return workdir
    folder = os.path.abspath(args.packages)
    if not glob.glob(os.path.join(folder, "openfilters_mcp-*.whl")):
        fail(f"no openfilters_mcp-*.whl package found in {folder}.\n"
             "Download the package for this computer from the release page into the same folder as this "
             "script, or use --from-github.")
    return folder


def install(args):
    if sys.version_info < (3, 10):
        fail(f"Python 3.10 or newer is needed (this is {sys.version.split()[0]}).")
    venv_dir = os.path.abspath(args.venv)
    with tempfile.TemporaryDirectory() as tmp:
        packages = get_packages(args, tmp)
        if os.path.exists(venv_dir):
            say(f"Replacing the previous installation in {venv_dir}")
            shutil.rmtree(venv_dir)
        say(f"Creating a private Python environment in {venv_dir} ...")
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = venv_python(venv_dir)
        say("Installing the package and its dependencies (about 190 MB; may take a few minutes) ...")
        # --find-links lets pip pick the package matching this Python and system.
        result = subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                 "--only-binary", "openfilters-mcp", "--find-links", packages, "openfilters-mcp"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            fail("pip could not install the package:\n" + result.stderr.strip() +
                 f"\n\nCheck that a package for Python {sys.version_info.major}.{sys.version_info.minor} on this "
                 "system is in the folder (file names contain e.g. cp312 and win_amd64 / macosx / manylinux).")
    self_test(venv_dir)
    command = server_command(venv_dir)
    if not args.no_desktop:
        register_desktop(command, args.desktop_config)
    if not args.no_claude_code:
        register_claude_code(command)
    say("\nDone. Fully quit Claude Desktop (system tray / menu bar -> Quit) and reopen it, then start a new chat\n"
        "and try: \"Run openfilters server_info\".")
    say("Your materials, projects and results will be in " + os.path.join(os.path.expanduser("~"), "OpenFilters-MCP"))


def self_test(venv_dir):
    say("Self-test ...")
    code = ("from openfilters_core import abeles, moremath\n"
            "fast = abeles.get_abeles_dll_import_success() and moremath.get_moremath_dll_import_success()\n"
            "from openfilters_mcp import engine\n"
            "r = engine.spectra({'substrate': 'BK7', 'layers': [['TiO2', 60], ['SiO2', 95]]}, ['R'], [550])\n"
            "print('fast core' if fast else 'pure Python (slower)', float(r['series'][0]['values'][0]))\n")
    result = subprocess.run([venv_python(venv_dir), "-c", code], capture_output=True, text=True)
    if result.returncode != 0:
        fail("the installed package does not run:\n" + result.stderr.strip())
    say(f"  OK: calculation works ({result.stdout.strip()})")


def desktop_config_paths(config_path=None):
    if config_path:
        return [config_path]
    return [os.path.join(folder, "claude_desktop_config.json") for folder in desktop_data_dirs()]


def register_desktop(command, config_path=None):
    paths = desktop_config_paths(config_path)
    if not paths:
        say("Claude Desktop not found; skipping. Install it, then run this installer again.")
    for path in paths:
        register_desktop_config(command, path)


def register_desktop_config(command, path):
    config = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as infile:
            text = infile.read()
        if text.strip():
            try:
                config = json.loads(text)
            except json.JSONDecodeError:
                fail(f"{path} is not valid JSON; fix or remove it, then run the installer again.")
        backup = path + time.strftime(".backup-%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        say(f"Claude Desktop settings backed up to {backup}")
    config.setdefault("mcpServers", {})[NAME] = {"command": command, "args": []}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as outfile:
        json.dump(config, outfile, indent=2)
    os.replace(tmp, path)
    say(f"Registered in Claude Desktop ({path}).")


def register_claude_code(command):
    cli = find_claude_cli()
    if not cli:
        say("Claude Code not found; to add it later run:\n"
            f"  claude mcp add --scope user {NAME} -- \"{command}\"")
        return
    subprocess.run([cli, "mcp", "remove", "--scope", "user", NAME], capture_output=True, text=True)
    result = subprocess.run([cli, "mcp", "add", "--scope", "user", NAME, "--", command], capture_output=True, text=True)
    if result.returncode == 0:
        say("Registered in Claude Code.")
    else:
        say("Could not register in Claude Code (" + (result.stderr or result.stdout).strip() + "); run:\n"
            f"  claude mcp add --scope user {NAME} -- \"{command}\"")


def uninstall(args):
    for path in [] if args.no_desktop else desktop_config_paths(args.desktop_config):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as infile:
            config = json.load(infile)
        if NAME in config.get("mcpServers", {}):
            shutil.copy2(path, path + time.strftime(".backup-%Y%m%d-%H%M%S"))
            del config["mcpServers"][NAME]
            with open(path, "w", encoding="utf-8") as outfile:
                json.dump(config, outfile, indent=2)
            say(f"Removed from Claude Desktop ({path}).")
    cli = find_claude_cli()
    if cli and not args.no_claude_code:
        subprocess.run([cli, "mcp", "remove", "--scope", "user", NAME], capture_output=True, text=True)
        say("Removed from Claude Code.")
    venv_dir = os.path.abspath(args.venv)
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir)
        say(f"Deleted {venv_dir}.")
    say("Your data folder (~/OpenFilters-MCP: materials, projects, results) was kept.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uninstall", action="store_true", help="remove the server and its registrations")
    parser.add_argument("--packages", default=os.path.dirname(os.path.abspath(__file__)),
                        help="folder with the downloaded .whl package files (default: this script's folder)")
    parser.add_argument("--from-github", action="store_true", help="download the packages with the GitHub CLI")
    parser.add_argument("--version", help="release tag to download with --from-github (default: latest)")
    parser.add_argument("--venv", default=os.path.join(HOME, "venv"), help="where to create the private environment")
    parser.add_argument("--no-desktop", action="store_true", help="do not register in Claude Desktop")
    parser.add_argument("--no-claude-code", action="store_true", help="do not register in Claude Code")
    parser.add_argument("--desktop-config", help=argparse.SUPPRESS)  # for testing
    args = parser.parse_args()
    uninstall(args) if args.uninstall else install(args)


if __name__ == "__main__":
    main()
