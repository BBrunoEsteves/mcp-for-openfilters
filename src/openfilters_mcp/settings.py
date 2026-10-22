"""Locations and limits, configurable through environment variables.

OPENFILTERS_MCP_HOME     data folder (default: ~/OpenFilters-MCP)
OPENFILTERS_MCP_WORKERS  parallel worker processes (default: CPU count - 1)
"""

import os


def home():
    path = os.environ.get("OPENFILTERS_MCP_HOME") or os.path.join(os.path.expanduser("~"), "OpenFilters-MCP")
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
    value = os.environ.get("OPENFILTERS_MCP_WORKERS")
    if value:
        return max(1, int(value))
    return max(1, (os.cpu_count() or 2) - 1)
