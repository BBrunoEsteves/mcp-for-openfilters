import asyncio
import re

from openfilters_mcp import server


def test_every_tool_has_title_and_annotations():
    # The Claude directory requires them; destructive_hint must be set on tools that write.
    for tool in asyncio.run(server.mcp.list_tools()):
        assert tool.title, tool.name
        a = tool.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is not None, tool.name
        if not a.read_only_hint:
            assert a.destructive_hint is not None, tool.name


def test_prompts_render_with_their_defaults():
    prompts = asyncio.run(server.mcp.list_prompts())
    assert {p.name for p in prompts} >= {"design_antireflection", "design_mirror", "design_edge_filter",
                                         "design_bandpass", "review_for_manufacturing"}
    for p in prompts:
        required = {a.name: "{}" for a in p.arguments or [] if a.required}
        text = asyncio.run(server.mcp.get_prompt(p.name, required)).messages[0].content.text
        assert not re.search(r"\{[a-z_]+\}", text), p.name  # every placeholder filled


def test_each_example_is_its_own_resource():
    # Claude Desktop lists fixed resources in its attach menu, not resource templates.
    uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    assert {"openfilters://examples/AR", "openfilters://examples/Edge"} <= uris
    contents = list(asyncio.run(server.mcp.read_resource("openfilters://examples/Edge")))
    assert '"designs"' in contents[0].content


def test_prompt_default_materials_exist():
    # The workflows once defaulted to Ta2O5, which OpenFilters does not ship.
    import inspect
    from openfilters_mcp import engine
    names = set(engine.catalog().get_material_names())
    for fn in (server.design_antireflection, server.design_mirror, server.design_edge_filter, server.design_bandpass):
        params = inspect.signature(fn).parameters
        for material in params["materials"].default.split(","):
            assert material.strip() in names, (fn.__name__, material)
        assert params["substrate"].default in names, fn.__name__


def test_load_project_finds_saved_name(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    d = {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 100}]}
    targets = [{"kind": "R", "wavelength": 550, "value": 0.02, "inequality": "<="}]
    saved = asyncio.run(server.save_project("by_name", [d], targets))
    loaded = asyncio.run(server.load_project("by_name"))
    assert saved["path"].endswith("by_name.ofp") and len(loaded["designs"]) == 1


def test_empty_prompt_boxes_use_the_defaults():
    # Claude Desktop shows empty boxes (prompt arguments carry no defaults) and may send "".
    blank = {a.name: "" for a in next(p for p in asyncio.run(server.mcp.list_prompts())
                                      if p.name == "design_antireflection").arguments}
    text = asyncio.run(server.mcp.get_prompt("design_antireflection", blank)).messages[0].content.text
    assert "coating on BK7" in text and "materials TiO2, SiO2." in text
    described = next(p for p in asyncio.run(server.mcp.list_prompts()) if p.name == "design_mirror").arguments
    assert all(a.description and a.description.startswith("Default:") for a in described)
