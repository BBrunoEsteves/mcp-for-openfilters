import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_sync():
    spec = importlib.util.spec_from_file_location("sync_packaging", os.path.join(ROOT, "dev", "sync_packaging.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distribution_manifests_are_up_to_date():
    sync = load_sync()
    for path, content in sync.manifests().items():
        with open(os.path.join(ROOT, path), encoding="utf-8") as infile:
            assert infile.read() == sync.render(content), f"{path} is stale: run dev/sync_packaging.py"


def test_readme_names_the_registry_server():
    # The MCP Registry verifies PyPI ownership through this line in the package description.
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as infile:
        assert f"mcp-name: {load_sync().NAME} " in infile.read()
