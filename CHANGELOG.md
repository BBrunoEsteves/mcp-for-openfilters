# Changelog

## 0.2.0 — unreleased

Renamed to **MCP for OpenFilters** (package and command `mcp-for-openfilters`, module `mcp_for_openfilters`,
registration `mcp-for-openfilters`), so that the connector is not mistaken for part of OpenFilters itself.
Environment variables are now `MCP_FOR_OPENFILTERS_HOME` / `_WORKERS` and the default data folder
`~/MCP-for-OpenFilters`; the old variables and an existing `~/OpenFilters-MCP` folder still work. The installer
removes the old `openfilters` registration (only one that points to openfilters-mcp) and its environment.
0.1.3 was never released; its changes follow the fixes below.

From the pre-release scenario run through the live tools (2026-11-06; Batch 1 and the new-feature checks all
reproduce their reference values):

- Fix: a needle optimization could crawl for its whole time limit (300 s by default) after inserting needles:
  OpenFilters' Levenberg-Marquardt, which has no iteration limit, kept improving by tiny steps with the new
  layers stuck at its 0 nm bound (same in the pure-Python core). Two starts of a 12-start V-coat campaign held
  it for 5 minutes and still failed. A run that gains less than 1 % over 10000 iterations now stops as
  "stalled", and the needle method restarts it with those layers at 1 nm: the same campaign takes 2 s and
  both starts meet the target.
- Messages about materials used outside their data name only the wavelengths used (8000-12000 nm, not
  1771-12000 nm).
- `compute_spectrum` returns one series per distinct polarization and angle (30 and "30deg", 45 and
  "unpolarized" were computed twice).
- `tolerance_analysis` warns when the design itself does not meet the targets (the yield then only counts
  trials whose errors happen to fix it).

From the two pre-release chat reports (Batch 6: 8/8 pass; Batches 1-5: 57/57 pass, one sub-check blocked by the
chat's own permission guard):

- Fix (OpenFilters' C++ solver): the Levenberg-Marquardt work arrays were allocated uninitialized. With more
  thicknesses than target points (e.g. a single-wavelength V-coat target and three or more layers), leftover memory
  entered the solve and about 4 % of such runs ended with every thickness NaN, depending on what had run before.
  The arrays now start at zero, as in OpenFilters' pure-Python solver, which never failed (see PROVENANCE.md).
- Fix (OpenFilters' optimizer): every optimization with an absorptance (A) target failed ("first argument must be
  dR"): without the backside, OpenFilters set the sample's derivatives to the whole per-parameter list. In
  campaigns the error was caught and the start returned unoptimized. Now indexed per parameter; A targets
  optimize (checked against a brute-force scan of the reachable absorptance).
- Fix: needles that stayed at 0 nm made OpenFilters' needle method stop "converged" after one iteration with the
  needles unused (a V-coat start: not met, chi2 11.2). Such runs restart with those layers at 1 nm (now met).
- Fix: "optically invisible layer" warnings described the starting design, not the result (a campaign row named a
  Ge layer that the optimized design no longer had).
- Fix: the between-grid worst value could lie just outside the target's range (399.75 nm for 400-900 nm).
- Cancelled campaigns are ranked on the starts that finished: duplicates marked, summary.csv and top_designs.ofp
  written, marked as partial (copies of one design used to take separate ranks, and nothing was saved).
- Clearer: the 0 nm removal note adds up ("2 layer(s) ended at 0 nm and were removed, merging the 2 pair(s) ...:
  6 layers before, 2 now"); every material used beyond its data is named in one refusal; thermal tools name the
  graded layers they cannot handle and suggest layer_sensitivity; kelvin_per_fwhm says which FWHM it uses; a
  half-maximum search with `near` says when the peak it found is not the highest; campaign rows give
  thinnest_layer_nm; the start-size warning appears only when a start has no room for one needle.
- Plots: target legends name only what is drawn (markers or bands); start curves of R and T differ in style;
  layer boundaries in monitoring plots are dotted, not grid-like.

## 0.1.3 — not released (included in 0.2.0)

From a scenario run through the live tools (2026-10-30):

- Plots name what they show: target plots (optimize, evaluate, campaigns) said "R / T / A" on the y axis, and
  monitoring and angle-scan plots "Signal" / "Value"; they now say Reflectance, Transmittance, ... Titles give
  the angle of incidence and the polarization in words ("30° incidence, polarization angle 30°", not "30°, 30").
- Fix: `job_status` could fail with "Permission denied: job.json" on Windows while a job was running (the file
  was being rewritten); job files are now read and replaced with a short retry.
- `compute_spectrum` and `simulate_random_errors` without wavelengths use the design's `wavelength_range` (e.g. from
  a project) instead of 380-1100 nm; random errors otherwise use OpenFilters' default range, as its dialog.
- `run_batch` refuses spectrum metrics for a quantity, angle or polarization the batch does not compute before
  starting (every task used to fail).
- `simulate_random_errors` accepts `"sigma"` (as tolerance_analysis) for the error size.

Checked against OpenFilters' GUI dialogs (2026-10-30):

- Fix: no tool accepted a polarization angle (the fourth choice of OpenFilters' dialogs, e.g. 30°): numbers were
  refused by the tool schemas and numeric text by the engine, and a target read back from a project ("30deg")
  could not be used again. Every tool and target now takes "s", "p", "unpolarized" or an angle in degrees
  (0 = p, 90 = s; R = sin² ψ Rs + cos² ψ Rp). Polarization and tilt were checked against an independent
  transfer-matrix code (s, p, any ψ, 0-85°, absorbing metal, total internal reflection, backside, reverse).
- New tool `simulate_random_errors`: OpenFilters' Preproduction > Simulate random errors (its only entry), run
  by OpenFilters' own code: mean, standard deviation, ±k σ and worst case of R, T, A, phase, GD, GDD and colour
  (ΔE 2000) for random thickness errors (relative or nm, uniform or normal), without targets; plots with the
  dialog's Show choices, statistics CSV and optional CSV of every test.
- `compute_monitoring_curves`: OpenFilters' "spectroscopic" option (every wavelength of the design's wavelength
  range, or any range): map of the signal versus thickness and wavelength, the curve at the center wavelength,
  and the spectrum after each layer (CSV); `direction: "reverse"` (monitoring from the substrate side).
- Documentation: in reverse direction the angle is measured in the substrate (or the exit medium with the
  backside), as in OpenFilters.

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
- Graded profiles generated by the server: `"graded": {"profile": {...}}` instead of steps. Rugate (ln n
  sinusoidal in optical thickness, so no false harmonics at lambda0/2 and lambda0/3; several notches with
  `harmonics`; apodization sin2/Hann, quintic or Gaussian, whole-layer or tapered), quintic (Southwell) and
  linear ramps, and custom (position, n) profiles. They are rounded to the mixture's own steps (or a coarser
  `resolution`), with each step's optical thickness exact. `"matching"` adds quintic or linear ramps to the
  substrate and the medium; where the mixture cannot reach their index, a warning gives the mismatch left
  and its reflectance.

From a second rugate test (2026-10-27):

- Fix: `compute_electric_field` sampled graded layers only at their step boundaries (OpenFilters' own
  behaviour), so inside thick steps |E|^2 was drawn as a straight line and its peak could be missed. Every
  layer is now sampled every 1 nm; graded layers also report their own peak (`graded_layer_peaks`).
- `tolerance_analysis` supports graded layers: each one is scaled as a whole (all steps by one drawn factor,
  a deposition-rate error). A warning says that `index_error` does not perturb mixtures.
- `tolerance_analysis`, tolerance batches and campaigns with a yield stage check the design and the error
  model before creating a job (a graded design used to fail in every task).
- `layer_sensitivity` includes graded layers (scaled as a whole) instead of skipping them silently.

- New design field `table_extrapolation` for tabulated materials used beyond their data: `"hold"` (default,
  end values), `"openfilters"` (OpenFilters' own extrapolation, as in its GUI) or `"refuse"`. Warnings give
  the n,k used and what the other choice would give. Optimizations, campaigns, tolerance analyses and batches
  do not start until the choice is made when a material is outside its data; more than 1.5x beyond the data
  is computed only when it is set. Saving a project warns when the GUI will extrapolate where the connector
  held values.
- Documentation: the connector has been developed and tested only with Claude (Claude Desktop and Claude
  Code); other MCP clients are untested.

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
