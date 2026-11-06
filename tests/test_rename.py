"""The 0.2.0 rename (openfilters-mcp -> mcp-for-openfilters) keeps existing installations working."""

import importlib.util
import json
import os

from mcp_for_openfilters import settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _installer():
    path = os.path.join(ROOT, "scripts", "install_mcp_for_openfilters.py")
    spec = importlib.util.spec_from_file_location("installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clear_env(monkeypatch):
    for name in ("MCP_FOR_OPENFILTERS_HOME", "OPENFILTERS_MCP_HOME",
                 "MCP_FOR_OPENFILTERS_WORKERS", "OPENFILTERS_MCP_WORKERS"):
        monkeypatch.delenv(name, raising=False)


def test_new_and_legacy_environment_variables(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path / "old"))
    monkeypatch.setenv("OPENFILTERS_MCP_WORKERS", "3")
    assert settings.home() == str(tmp_path / "old")
    assert settings.workers() == 3
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path / "new"))
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_WORKERS", "2")
    assert settings.home() == str(tmp_path / "new")
    assert settings.workers() == 2


def test_existing_legacy_data_folder_is_kept(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert settings.default_home() == str(tmp_path / "MCP-for-OpenFilters")
    (tmp_path / "OpenFilters-MCP").mkdir()
    assert settings.default_home() == str(tmp_path / "OpenFilters-MCP")
    assert _installer().data_folder() == str(tmp_path / "OpenFilters-MCP")
    (tmp_path / "MCP-for-OpenFilters").mkdir()
    assert settings.default_home() == str(tmp_path / "MCP-for-OpenFilters")


def test_installer_replaces_only_the_old_openfilters_registration(tmp_path):
    installer = _installer()
    path = tmp_path / "claude_desktop_config.json"
    old = {"command": r"C:\Users\x\.openfilters-mcp\venv\Scripts\openfilters-mcp.exe", "args": []}
    path.write_text(json.dumps({"mcpServers": {"openfilters": old, "other": {"command": "x"}}}), encoding="utf-8")
    installer.register_desktop_config("NEW.exe", str(path))
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers == {"mcp-for-openfilters": {"command": "NEW.exe", "args": []}, "other": {"command": "x"}}

    foreign = {"command": "someone-elses-openfilters-server"}
    path.write_text(json.dumps({"mcpServers": {"openfilters": foreign}}), encoding="utf-8")
    installer.register_desktop_config("NEW.exe", str(path))
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["openfilters"] == foreign and "mcp-for-openfilters" in servers


def test_server_and_package_names():
    from mcp_for_openfilters import server
    assert server.mcp.name == "MCP for OpenFilters"
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as infile:
        text = infile.read()
    assert 'name = "mcp-for-openfilters"' in text
    assert 'mcp-for-openfilters = "mcp_for_openfilters.server:main"' in text
