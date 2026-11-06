"""Locations and limits, configurable through environment variables.

MCP_FOR_OPENFILTERS_HOME     data folder (default: ~/MCP-for-OpenFilters)
MCP_FOR_OPENFILTERS_WORKERS  parallel worker processes (default: CPU count - 1)

Before version 0.2.0 the project was called openfilters-mcp: the old variables
(OPENFILTERS_MCP_HOME, OPENFILTERS_MCP_WORKERS) are still read, and an existing
~/OpenFilters-MCP data folder keeps being used when the new one does not exist.
"""

import os

LEGACY_HOME = "OpenFilters-MCP"


def _env(name):
    return os.environ.get("MCP_FOR_OPENFILTERS_" + name) or os.environ.get("OPENFILTERS_MCP_" + name)


def default_home():
    user = os.path.expanduser("~")
    path = os.path.join(user, "MCP-for-OpenFilters")
    legacy = os.path.join(user, LEGACY_HOME)
    if not os.path.exists(path) and os.path.isdir(legacy):
        return legacy
    return path


def home():
    path = _env("HOME") or default_home()
    os.makedirs(path, exist_ok=True)
    return path


def _subdir(name):
    path = os.path.join(home(), name)
    os.makedirs(path, exist_ok=True)
    return path


def materials_dir():
    """User materials (.mat files); they take precedence over built-in ones."""
    return _subdir("materials")


def jobs_dir():
    return _subdir("jobs")


def projects_dir():
    return _subdir("projects")


def workers():
    value = _env("WORKERS")
    if value:
        return max(1, int(value))
    return max(1, (os.cpu_count() or 2) - 1)
