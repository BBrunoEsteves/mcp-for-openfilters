# Provenance

This directory is the calculation core of **OpenFilters** 1.1.1
(Stéphane Larouche and Ludvik Martinu, Polytechnique Montréal, GPL-2.0-or-later;
https://www.polymtl.ca/larfis/en/links), without the wxPython GUI.

Source chain:
1. Original sources, mirrored at https://github.com/gyger/OpenFilters
2. Partial Python 3 port by Emil Kleijn, https://github.com/EmilKleijn/OpenFilters
   (commit 800b52f, 2025-02-17)
3. This copy, with further changes:
   - removed GUI, installer and packaging scripts
   - re-encoded Latin-1 sources as UTF-8 (moremath/QR.py)
   - fixed detection of the compiled `_abeles`/`_moremath` extensions (the C++
     source folders were being imported as empty namespace packages), and added
     the `OPENFILTERS_PURE_PYTHON` environment variable to force the pure-Python
     implementation
   - project files (.ofp) are written in Latin-1, the encoding they are read in
     (the writer used the OS locale)
   - **deviation from OpenFilters:** tabulated (regular) materials hold n and k
     constant beyond their data instead of extrapolating the PCHIP end segments,
     which gave unphysical values (n < 0, or n = 4.9 for TiO2 far from its data).
     Values within the data are unchanged; the OpenFilters GUI still extrapolates.
     This is the connector's default only: a design with "table_extrapolation":
     "openfilters" uses OpenFilters' original extrapolation (mcp_for_openfilters/engine.py
     rebuilds those tables without the padding).
   - **bug fix in OpenFilters' C++ solver** (moremath/_moremath/Levenberg_Marquardt.cpp):
     the work arrays were allocated uninitialized. `b` has max(nb_par, nb_points) rows
     but only the first nb_points are ever set, so with more fitted parameters than
     target points (e.g. a single-wavelength target and three or more layers) leftover
     heap memory entered the QR solve and occasionally made every thickness NaN,
     depending on what had run before in the process. The arrays now start at 0.0,
     as in the pure-Python solver (Levenberg_Marquardt.py), which never failed.
   - **bug fix in OpenFilters' optimization** (optimization_refinement.py): for absorptance
     (A) targets without the backside, the sample's dR and dT were set to the whole list of
     per-parameter derivatives instead of one parameter's (8 lines, where the derivatives are
     set up and in add_parameters), so every optimization with an A target failed ("first
     argument must be dR"). They are now indexed by parameter, as in the R and T branches.
