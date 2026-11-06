# MCP for OpenFilters

Design, simulate and optimize **optical thin-film coatings** (antireflection coatings, mirrors, edge and band-pass
filters, dichroics, polarizers, rugate filters) by talking to Claude. This plugin adds one MCP server whose
calculations are done by the engine of **OpenFilters** (Stéphane Larouche and Ludvik Martinu, Polytechnique
Montréal), ported to Python 3 and run without its graphical interface.

*Unofficial: this plugin is not made, affiliated with or endorsed by the OpenFilters authors or Polytechnique
Montréal.* Source code, documentation and issue tracker: <https://github.com/BBrunoEsteves/mcp-for-openfilters>.

## What it can do

Spectra (reflectance, transmittance, absorptance, optical density, phase, group delay and GDD) at any angle and
polarization, colour, electric field inside the coating, angle scans, optical monitoring curves, OpenFilters'
refinement, needle and step optimizations, many-start optimization campaigns, Monte Carlo yield, random-error
analysis, thermal shift of a band-pass line, graded-index (rugate) profiles, and `.ofp` project files that open in
the OpenFilters program. The server has 28 tools; long jobs run in the background on all CPU cores.

## What it runs and what it downloads

- **The server:** `uvx mcp-for-openfilters==<version>`, the exact version pinned in `.claude-plugin/plugin.json`. When the plugin starts, [uv](https://docs.astral.sh/uv/)
  downloads that exact version of the `mcp-for-openfilters` package from [PyPI](https://pypi.org/project/mcp-for-openfilters/)
  together with its dependencies (the MCP SDK, NumPy, Matplotlib, PyYAML), and runs it locally. You need `uv`
  installed. The package is built from the public repository above by its GitHub Actions workflow.
- **Optical data, only when you ask for it:** the tools `search_refractiveindex` and
  `import_refractiveindex_material` download refractive-index data from the public
  [refractiveindex.info database](https://github.com/polyanskiy/refractiveindex.info-database), read from GitHub
  (`raw.githubusercontent.com`): the database's catalogue (cached for 30 days) and the pages you choose. No data of
  yours is sent with these requests.
- **Nothing else** leaves your computer. There is no telemetry, no account and no API key.

## Privacy Policy

The server runs entirely on your computer. It collects no personal data, sends nothing anywhere except the two
downloads above, keeps no logs outside your machine, and shares nothing with third parties. It reads only the files
you point it to and writes only to its data folder (`~/MCP-for-OpenFilters`, or `MCP_FOR_OPENFILTERS_HOME`), where
your materials, projects and results stay until you delete them. What you type in Claude is handled by the Claude
app under Anthropic's privacy policy. Full text:
<https://github.com/BBrunoEsteves/mcp-for-openfilters/blob/main/PRIVACY.md>.

## Getting started

After installing the plugin, ask Claude for example: *"Design a broadband antireflection coating for BK7 from 430 to
670 nm with TiO2 and SiO2"* or *"Show the reflectance of a quarter-wave mirror (HL)^8 H at 1064 nm"*. Wavelengths
are in nm, angles in degrees, and layers are listed from the substrate outward.

## License and contact

GPL-2.0-or-later, as OpenFilters. If you use it in published work, please cite OpenFilters: S. Larouche and
L. Martinu, *Applied Optics* **47**, C219–C230 (2008). Bug reports: GitHub Issues. Other questions:
mcp.for.openfilters@gmail.com.
