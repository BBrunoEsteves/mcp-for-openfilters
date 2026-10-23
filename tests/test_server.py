import asyncio

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
        assert "{" not in text.replace("{}", ""), p.name  # every placeholder filled


def test_example_resources_list_the_openfilters_examples():
    contents = list(asyncio.run(server.mcp.read_resource("openfilters://examples")))
    assert '"AR"' in contents[0].content and '"Edge"' in contents[0].content
