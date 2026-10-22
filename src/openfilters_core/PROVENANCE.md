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
