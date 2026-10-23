# Privacy

OpenFilters MCP runs entirely on your computer.

- It does not send your designs, materials, results or any other data anywhere, and it contains no
  telemetry or analytics.
- The only network access is by two tools you or Claude call explicitly: `search_refractiveindex` and
  `import_refractiveindex_material` download public files (the catalogue and the chosen material) from the
  refractiveindex.info database on GitHub (raw.githubusercontent.com). These requests contain only the
  name of the public file; as with any download, GitHub sees your IP address.
- It reads only the files you point it to (material tables, `.ofp` projects) and writes only to its data
  folder (`~/OpenFilters-MCP` by default, or `OPENFILTERS_MCP_HOME`) and to paths you give it.
- What you type in Claude and the results Claude reads from the tools are handled by the Claude app you
  use, under Anthropic's privacy policy, not by this project.

Questions: open an issue at https://github.com/BBrunoEsteves/openfilters-mcp/issues.
