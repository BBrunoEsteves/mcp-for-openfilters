"""Write the version and tool list into the distribution manifests.

Run after bumping the version or changing tools; tests/test_packaging.py fails
when the manifests are out of date.

    .venv/Scripts/python dev/sync_packaging.py
"""

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from openfilters_mcp import __version__, server  # noqa: E402

NAME = "io.github.BBrunoEsteves/openfilters-mcp"
REPO = "https://github.com/BBrunoEsteves/openfilters-mcp"
DESCRIPTION = "Design, simulate and optimize optical thin-film coatings with the OpenFilters engine."
AUTHOR = {"name": "Bruno Esteves"}
KEYWORDS = ["thin film", "optical coating", "optics", "interference filter", "OpenFilters"]


def tools():
    return [{"name": t.name, "description": t.title}
            for t in asyncio.run(server.mcp.list_tools())]


def manifests(version=__version__):
    """Every distribution file, as {relative path: JSON content or text}."""
    pin = f"openfilters-mcp=={version}"
    return {
        "server.json": {
            "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
            "name": NAME,
            "title": "OpenFilters",
            "description": DESCRIPTION,
            "repository": {"url": REPO, "source": "github"},
            "version": version,
            "packages": [{"registryType": "pypi", "identifier": "openfilters-mcp", "version": version,
                          "transport": {"type": "stdio"}}],
        },
        ".claude-plugin/marketplace.json": {
            "name": "openfilters",
            "owner": AUTHOR,
            "plugins": [{"name": "openfilters", "source": "./claude-plugin", "description": DESCRIPTION}],
        },
        "claude-plugin/.claude-plugin/plugin.json": {
            "name": "openfilters",
            "displayName": "OpenFilters",
            "version": version,
            "description": DESCRIPTION,
            "author": AUTHOR,
            "homepage": REPO,
            "repository": REPO,
            "license": "GPL-2.0-or-later",
            "keywords": KEYWORDS,
            "privacyPolicyUrl": f"{REPO}/blob/main/PRIVACY.md",
            "mcpServers": {"openfilters": {"command": "uvx", "args": [f"openfilters-mcp@{version}"]}},
        },
        "packaging/mcpb/manifest.json": {
            "manifest_version": "0.4",
            "name": "openfilters-mcp",
            "display_name": "OpenFilters",
            "version": version,
            "description": DESCRIPTION,
            "long_description": ("Thin-film optical coating design with the calculation engine of OpenFilters "
                                 "(S. Larouche and L. Martinu, Polytechnique Montréal): spectra, color, electric "
                                 "field, monitoring curves, refinement/needle/step optimization, campaigns and "
                                 "Monte Carlo yield, with .ofp projects that open in the OpenFilters GUI. "
                                 "Unofficial: not affiliated with or endorsed by the OpenFilters authors. "
                                 "Developed and tested with Claude (Claude Desktop and Claude Code); other MCP "
                                 "clients are untested."),
            "author": AUTHOR,
            "repository": {"type": "git", "url": REPO},
            "homepage": REPO,
            "documentation": f"{REPO}#readme",
            "support": f"{REPO}/issues",
            "license": "GPL-2.0-or-later",
            "keywords": KEYWORDS,
            "privacy_policies": [f"{REPO}/blob/main/PRIVACY.md"],
            "server": {"type": "uv", "entry_point": "src/server.py"},
            "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.10"}},
            "tools": tools(),
            "tools_generated": False,
        },
        "packaging/mcpb/pyproject.toml": (
            "# Claude Desktop installs this with uv; the server comes pre-built from PyPI.\n"
            "[project]\n"
            'name = "openfilters-mcp-extension"\n'
            f'version = "{version}"\n'
            'requires-python = ">=3.10"\n'
            f'dependencies = ["{pin}"]\n'
        ),
    }


def render(content):
    return content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False) + "\n"


def main():
    for path, content in manifests().items():
        full = os.path.join(ROOT, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as outfile:
            outfile.write(render(content))
        print("wrote", path)


if __name__ == "__main__":
    main()
