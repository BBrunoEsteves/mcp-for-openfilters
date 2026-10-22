"""Put the C++ core compiled by GitHub Actions into the source tree, so a
development checkout runs at full speed without a local compiler.

Usage: python dev/fetch_compiled.py [run_id]   (default: latest successful CI run)
Requires the GitHub CLI (gh), logged in.
"""

import glob
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def gh(*args):
    return subprocess.run(["gh", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def main(run_id=None):
    if run_id is None:
        runs = json.loads(gh("run", "list", "--workflow", "CI", "--status", "success", "--limit", "1",
                             "--json", "databaseId"))
        if not runs:
            sys.exit("no successful CI run found")
        run_id = str(runs[0]["databaseId"])
    platform = {"win32": "windows-latest", "darwin": "macos-latest"}.get(sys.platform, "ubuntu-latest")
    tag = sysconfig.get_config_var("EXT_SUFFIX").split(".")[1].split("-")[0]  # e.g. cp314
    with tempfile.TemporaryDirectory() as tmp:
        gh("run", "download", run_id, "-n", f"wheels-{platform}", "-D", tmp)
        wheels = [w for w in glob.glob(os.path.join(tmp, "*.whl")) if f"-{tag}-" in os.path.basename(w)]
        if not wheels:
            sys.exit(f"no {tag} wheel for {platform} in run {run_id}")
        with zipfile.ZipFile(wheels[0]) as z:
            libs = [n for n in z.namelist() if ".libs/" in n and not n.endswith("/")]
            for name in z.namelist():
                if name.endswith((".pyd", ".so")):
                    target = os.path.join(ROOT, "src", name)
                    with open(target, "wb") as out:
                        out.write(z.read(name))
                    print("installed", os.path.relpath(target, ROOT))
                    # Runtime libraries bundled by the wheel repair step go next
                    # to the extension, where the loader looks first.
                    for lib in libs:
                        with open(os.path.join(os.path.dirname(target), os.path.basename(lib)), "wb") as out:
                            out.write(z.read(lib))
    print("from CI run", run_id)


if __name__ == "__main__":
    main(*sys.argv[1:2])
