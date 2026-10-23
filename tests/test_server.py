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
