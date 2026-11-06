"""Install MCP for OpenFilters for Claude Desktop and Claude Code.

Usage (Python 3.10 or newer, no other requirement):

    python install_mcp_for_openfilters.py              # install from the package files next to this script
    python install_mcp_for_openfilters.py --uninstall  # remove it again

Put this script in the same folder as the downloaded package file(s)
(mcp_for_openfilters-*.whl from the release page); the one matching this
computer is chosen automatically. Alternatively --from-github downloads the
packages with the GitHub CLI (gh, logged in with access to the repository).

What it does:
  1. creates a private Python environment for the server (your own Python
     packages are not touched): ~/.mcp-for-openfilters/venv
  2. installs the package into it
  3. registers the server as "mcp-for-openfilters" in Claude Desktop (its
     config file is backed up first) and in Claude Code (if found)
  4. runs a quick self-test
An installation of the earlier openfilters-mcp (registered as "openfilters",
environment in ~/.openfilters-mcp) is replaced: its registrations and its
private environment are removed; the data folder is kept and still used.
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

NAME = "mcp-for-openfilters"
LEGACY_NAME = "openfilters"  # registration of openfilters-mcp (before 0.2.0)
LEGACY_HOME = os.path.join(os.path.expanduser("~"), ".openfilters-mcp")
REPO = "BBrunoEsteves/mcp-for-openfilters"
HOME = os.path.join(os.path.expanduser("~"), ".mcp-for-openfilters")


def say(message):
    print(message, flush=True)


def fail(message):
    say("\nERROR: " + message)
    sys.exit(1)


def data_folder():
    """The data folder the server will use (as settings.default_home)."""
    chosen = os.environ.get("MCP_FOR_OPENFILTERS_HOME") or os.environ.get("OPENFILTERS_MCP_HOME")
    if chosen:
        return chosen
    user = os.path.expanduser("~")
    path, legacy = os.path.join(user, "MCP-for-OpenFilters"), os.path.join(user, "OpenFilters-MCP")
    return legacy if not os.path.exists(path) and os.path.isdir(legacy) else path


def is_legacy_entry(entry):
    """A Claude Desktop entry written for openfilters-mcp (not some other server called "openfilters")."""
    return isinstance(entry, dict) and "openfilters-mcp" in str(entry.get("command", ""))


def venv_python(venv_dir):
    return os.path.join(venv_dir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv_dir, "bin", "python")


def server_command(venv_dir):
    return os.path.join(venv_dir, "Scripts", "mcp-for-openfilters.exe") if os.name == "nt" \
        else os.path.join(venv_dir, "bin", "mcp-for-openfilters")


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
    if not glob.glob(os.path.join(folder, "mcp_for_openfilters-*.whl")):
        fail(f"no mcp_for_openfilters-*.whl package found in {folder}.\n"
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
                                 "--only-binary", "mcp-for-openfilters", "--find-links", packages, "mcp-for-openfilters"],
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
    remove_legacy_venv()
    say("\nDone. Fully quit Claude Desktop (system tray / menu bar -> Quit) and reopen it, then start a new chat\n"
        "and try: \"Run the MCP for OpenFilters server_info tool\".")
    say("Your materials, projects and results will be in " + data_folder())


def self_test(venv_dir):
    say("Self-test ...")
    code = ("from openfilters_core import abeles, moremath\n"
            "fast = abeles.get_abeles_dll_import_success() and moremath.get_moremath_dll_import_success()\n"
            "from mcp_for_openfilters import engine\n"
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
    servers = config.setdefault("mcpServers", {})
    servers[NAME] = {"command": command, "args": []}
    if is_legacy_entry(servers.get(LEGACY_NAME)):
        del servers[LEGACY_NAME]
        say(f"Removed the old \"{LEGACY_NAME}\" (openfilters-mcp) registration from Claude Desktop.")
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
    remove_legacy_claude_code(cli)
    result = subprocess.run([cli, "mcp", "add", "--scope", "user", NAME, "--", command], capture_output=True, text=True)
    if result.returncode == 0:
        say("Registered in Claude Code.")
    else:
        say("Could not register in Claude Code (" + (result.stderr or result.stdout).strip() + "); run:\n"
            f"  claude mcp add --scope user {NAME} -- \"{command}\"")


def remove_legacy_claude_code(cli):
    """Remove the "openfilters" registration of openfilters-mcp from Claude Code, if that is what it is."""
    found = subprocess.run([cli, "mcp", "get", LEGACY_NAME], capture_output=True, text=True)
    if found.returncode == 0 and "openfilters-mcp" in found.stdout:
        subprocess.run([cli, "mcp", "remove", "--scope", "user", LEGACY_NAME], capture_output=True, text=True)
        say(f"Removed the old \"{LEGACY_NAME}\" (openfilters-mcp) registration from Claude Code.")


def remove_legacy_venv():
    """Delete the private environment of openfilters-mcp (created by its installer, nothing else in it)."""
    venv_dir = os.path.join(LEGACY_HOME, "venv")
    if os.path.isfile(os.path.join(venv_dir, "pyvenv.cfg")):
        shutil.rmtree(venv_dir, ignore_errors=True)
        say(f"Deleted the old openfilters-mcp environment ({venv_dir}).")
        try:
            os.rmdir(LEGACY_HOME)
        except OSError:
            pass


def uninstall(args):
    for path in [] if args.no_desktop else desktop_config_paths(args.desktop_config):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as infile:
            config = json.load(infile)
        servers = config.get("mcpServers", {})
        names = [name for name in servers
                 if name == NAME or name == LEGACY_NAME and is_legacy_entry(servers[name])]
        if names:
            shutil.copy2(path, path + time.strftime(".backup-%Y%m%d-%H%M%S"))
            for name in names:
                del servers[name]
            with open(path, "w", encoding="utf-8") as outfile:
                json.dump(config, outfile, indent=2)
            say(f"Removed from Claude Desktop ({path}).")
    cli = find_claude_cli()
    if cli and not args.no_claude_code:
        subprocess.run([cli, "mcp", "remove", "--scope", "user", NAME], capture_output=True, text=True)
        remove_legacy_claude_code(cli)
        say("Removed from Claude Code.")
    venv_dir = os.path.abspath(args.venv)
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir)
        say(f"Deleted {venv_dir}.")
    remove_legacy_venv()
    say(f"Your data folder ({data_folder()}: materials, projects, results) was kept.")


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
