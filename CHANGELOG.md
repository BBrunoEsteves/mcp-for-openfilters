# Changelog

## 0.1.3 — 2026-10-24

From a field test (tunable Fabry-Perot filter with an ITO heater, 2026-10-26):

- Thermal model (new tools `thermal_sensitivity`, `thermal_sweep`): the peak of R or T nearest a wavelength,
  its shift per index and thickness change of each material, and with dn/dT, expansion coefficients and
  Poisson ratios (films clamped to the substrate, or free), d(lambda)/dT, kelvin per FWHM and the dn/dT that
  makes a design athermal; or the peak over a list of temperature changes. Materials are perturbed in
  memory. Validated against the hand-made analysis of that test (20 values within 0.07 pm/K).

- Fix: saving a project whose description or comment contained characters outside Latin-1 (e.g. "λ") failed on
  Windows with no message; they are now spelled out (λ -> lambda) with a warning, and project files are
  always written in Latin-1, the encoding they are read in (accented text read back garbled on macOS/Linux).
- Unexpected tool failures now say what went wrong instead of only "Error executing tool".
- Tabulated materials hold n,k at their end values beyond their data instead of extrapolating the interpolation
  (a 520-544 nm table gave n = 4.9 at 1550 nm; imported ITO n < 0), with a warning; more than 1.5x beyond the
  data is refused. This deliberately differs from OpenFilters (see `openfilters_core/PROVENANCE.md`). Colour,
  electric field and monitoring calculations now check materials too.
- Layers can be kept fixed in every optimization: `"fixed": true` on a layer, or a formula symbol
  `{"material": "ITO", "fixed": true}`. Fix: the best design recorded during an optimization lost the layer
  flags, and minimum-thickness rounds could change or remove fixed layers.
- `{"half_max": true}` takes `"near"` (the local peak nearest a wavelength, not a higher band-edge ripple) or
  `"from"`/`"to"`, and returns the peak and half-maximum centre; an edge beyond the computed range gives
  `fwhm: null` with `fwhm_at_least`.
- Removed layers are named (`removed_layers` and a warning), and job result rows include `thin_layers_removed`.
- `server_info` gives the build (git commit of a source checkout) and when the server started, to spot a
  server still running old code.
- `run_batch` spectrum metrics: `peak_wavelength`, `peak_value`, `fwhm`, `center`; sweeping whole designs
  labels rows by design name.
- Warnings: a T/R = 1 (or A = 0) target that absorption makes unreachable (the optimizer then slowly thins the
  absorbing layers, e.g. heater electrodes, and removes them with min_thickness); a start already at `max_total_thickness` (a stop, not a constraint); a fixed layer
  next to a free one of the same material (OpenFilters merges them).
- Fix: a formula symbol written `[mixture, index]` crashed the material check.

From a rugate / graded-layer test (2026-10-26):

- Fix: a mixture index outside the mixture's range (e.g. 1.4 on IdealMixture, 1.5-2.4) crashed the calculation
  process ("please retry"); it is now refused with the range, by every tool including describe_design. Graded
  layers are checked too (steps in range, matching lengths, thicknesses).
- Graded layers accept "indices_at_cw" instead of "steps" (rounded to the nearest composition step, with a
  warning); their "thickness" is optional and must equal the sum of the steps.
- describe_design summarizes graded layers (steps, index range, first and last index, optical thickness, QWOT)
  and its index profile plot draws them step by step, with the substrate and the incident medium.
- load_project summarizes long graded layers and writes each such design whole to a file (OpenFilters' Rugate
  example went from 331 403 characters to under 20 000); any tool takes {"design_file": path} as a design.
- get_material_index lists a mixture's composition steps (the index of each step).
- Spectrum plots name the quantity on the y axis, always show a legend and use the design's description as title.

Earlier in 0.1.3:

- `remove_material` tool.
- Fix: refractiveindex.info imports kept only the range where both n and k are given (ZnS: 0.4-1 um instead of
  0.4-14 um); the whole n range is kept, with k = 0 where the entry gives none (stated in `notes`).
- Fix: colour hex codes were built from linear RGB (too dark); sRGB is now gamma-encoded (`linear_RGB` also given).
- Fix: GD/GDD at the first and last wavelength of a target band reused the neighbouring estimate, so band edges
  escaped the optimizer; such targets are evaluated with one extra (unweighted) step at each end.
- Fix: a layer with the same index as its neighbour (e.g. Ge on a Ge substrate) ran off to 4e16 nm; thicknesses
  are capped (`max_layer_thickness`) and such layers are flagged as optically invisible. OpenFilters failures
  report `error_detail`.
- New `margin` option keeps inequality targets away from their limits; a warning flags results that meet their
  targets on the grid but not between grid points.
- Plots: one colour per design, solid unpolarized curves, and phase/GD/GDD targets plotted.
- Material summaries give the data range of tabulated materials; absorptance no longer shows -8e-16.
- Fix: refractiveindex.info entries whose id contains a space (495 of them, e.g. "Rodriguez-de Marcos") could
  not be imported.
- Unknown design or layer keys are refused with a "did you mean" suggestion (a misspelt key was ignored).
- `compute_spectrum` and `compute_angle_scan` can return threshold ranges ({"above": 0.999}, {"below": ...},
  interpolated edges) and the FWHM of a peak ({"half_max": true}); summaries use 8 significant figures.
- `compute_angle_scan` returns per-curve min/max with the angle where each occurs, samples and the angle range,
  instead of every value rounded to 6 decimals.
- Imports report where the material absorbs (`absorbing_ranges_nm`).
- Fix (porting bug): optimizations ran OpenFilters' slow pure-Python solver even with the compiled core installed;
  they now use the compiled one (about 3x faster).
- Fix: group delay and GDD were reported in s and s^2 (labelled fs, fs^2), and GD/GDD targets were compared in
  those units, so dispersive designs could not be optimized. Everything is now in fs and fs^2, as in the
  OpenFilters GUI, including targets saved in and read from projects.
- Materials used outside their data range give a warning; where a material's model gives an impossible index
  (e.g. built-in Ge or BK7 at 10 um), the tool refuses and suggests importing data for that range.
- An optimization can no longer hang inside one solver step: it stops at the time limit with the best design.
- A malformed stack formula gives a clear error instead of crashing.
- GD/GDD on a coarse wavelength grid (they are numerical derivatives) give a warning.
- `docs/SCENARIO_TESTS.md`: 42 realistic scenarios in 4 batches for a Claude chat to run and report on.
- Colour targets report the calculated coordinates, the deviation per coordinate and the colour distance
  (ΔE\*ab for L\*a\*b\*).
- The design and target formats are described in each tool's parameters, for apps that do not pass the
  server instructions to the model.
- Optimization results explain their stop reason in plain words (`stop_explanation`); campaign summaries
  list the stop reasons that occurred.
- Campaigns with a yield stage rank by yield first (a nominal pass with 0 % yield no longer beats a near-miss
  with a good yield).
- The antireflection and edge-filter workflows ask for a minimum layer thickness (default 5 nm), so they no
  longer return sub-nanometre layers.
- Fix: deviations are reported to 6 significant figures (a 1e-7 miss read as 0 next to "not met").
- Campaigns rank distinct designs: starts that converge to the same design are marked `duplicate_of` instead of
  filling the top list (and the yield stage) with copies.
- Fix: a start failed when OpenFilters' needle method removed every layer (a crash inside OpenFilters, in both
  its C++ and Python code); the best design found before the failure is now kept.
- Workflow boxes in Claude Desktop show their default values, and an empty box uses the default (it used
  to produce a broken request).
- Results give each target's worst value and where it occurs (`worst_value`, `worst_at_nm`).
- Fix: colour targets were rejected ("unknown keyword L*a*b*"); colour spaces now accept Lab/L*a*b*, XYZ, xyY,
  Luv, LCh(ab)/LChuv.
- Fix: the workflows defaulted to Ta2O5, which OpenFilters does not ship; they now default to TiO2/SiO2 and tell
  Claude to import missing materials from refractiveindex.info.
- Fix: `load_project` now finds projects by the name they were saved under (like `save_project`).
- Unknown-material errors suggest list_materials and refractiveindex.info; k is no longer shown as -0.0.
- Acceptance test (`dev/acceptance.py`, 42 checks against known answers) and a chat test plan
  (`docs/TEST_PLAN.md`).
- Fix: a null thickness (how JSON carries NaN) crashed the tool instead of giving a clear message.
- Fix: inequality targets ("R <= 0.2 %") counted as met whenever the value was within the tolerance
  (default 1 % absolute); they are now met only where they hold. The tolerance still weights the merit.
- Fix: OpenFilters' optimizer can turn every thickness into NaN (a layer pinned at 0 nm); such designs scored
  chi2 = 0, "met all targets" and 100 % yield and ranked first in campaigns. The optimizer now stops, keeps the
  best finite design and restarts without the 0 nm layers; NaN thicknesses are refused everywhere.
- Needle method: where OpenFilters stops because its best needle lies at a layer edge (it only accepts minima
  inside layers), the connector inserts a thin layer there and continues while the merit improves
  (`edge_needles_added` in the result). 0 nm layers are removed from results.
- Each OpenFilters example project is its own resource (selectable in Claude Desktop's attach menu).
- The antireflection prompt and the campaign tool explain that `n_layers` only sizes the starting designs and
  pass `max_layers` to limit the result.
- Fix: a user material with the same name as a built-in one (e.g. your own `TiO2`) was silently ignored
  in favour of the built-in data.
- `search_refractiveindex` and `import_refractiveindex_material`: any entry of the refractiveindex.info
  database (tabulated data or dispersion formulas), downloaded on demand.
- Prompts (ready-made workflows: antireflection, mirror, edge filter/dichroic, band-pass, manufacturing
  review) and resources (format guide, materials, OpenFilters example projects).
- `optimize_design` and `tolerance_analysis` send progress notifications while they wait.
- Validation against closed-form textbook results (quarter-wave AR, absentee layer, V-coat, Brewster angle,
  quarter-wave stack peak and stop-band width, energy conservation).
- Every tool has a title and annotations (read-only, destructive, local), as Claude's directory requires.
- Source package: the C++ headers were missing, so installs from source silently used the slow
  pure-Python core. CI now checks that a source install builds the compiled core.
- Distribution files for PyPI, the MCP Registry, a Claude Code plugin and a Claude Desktop extension
  (generated by `dev/sync_packaging.py`); privacy policy, citation file.

## 0.1.2 — 2026-10-23

- `.ofp` projects keep targets, a wavelength range covering them, design descriptions, needle materials,
  step spacing and per-layer flags, and are read back the same way.
- Optimization and campaign results describe how each design was made.
- Fix: optimization ignored per-layer flags, so fixed layers were refined.

## 0.1.1 — 2026-10-23

- The installer finds the Microsoft Store version of Claude Desktop.

## 0.1.0 — 2026-10-23

First release: Python 3 port of the OpenFilters calculation core (validated against analytic results and an
independent transfer-matrix code), 22 MCP tools (spectra, color, electric field, monitoring curves,
refinement/needle/step optimization, sweeps, campaigns, Monte Carlo yield, `.ofp` projects), background jobs,
automatic plots, wheels with the compiled core for Windows, macOS (Apple Silicon) and Linux, and an installer.
