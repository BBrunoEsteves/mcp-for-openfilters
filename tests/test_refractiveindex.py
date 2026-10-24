import os

import numpy as np
import pytest
import yaml

from openfilters_mcp import engine, refractiveindex as rii

MALITSON = yaml.safe_load("""
DATA:
  - type: formula 1
    wavelength_range: 0.21 6.7
    coefficients: 0 0.6961663 0.0684043 0.4079426 0.1162414 0.8974794 9.896161
""")


def test_sellmeier_formula_matches_published_fused_silica():
    rows = rii.to_table(MALITSON)
    assert rows[0][0] == pytest.approx(210.0) and rows[-1][0] == pytest.approx(6700.0)
    n = np.interp(587.6, [r[0] for r in rows], [r[1] for r in rows])
    assert n == pytest.approx(1.4585, abs=2e-4)  # n_d of fused silica
    assert all(r[2] == 0 for r in rows)


def test_formula_2_is_formula_1_with_squared_poles():
    wl = np.linspace(0.4, 2.0, 50)
    c1 = [0, 0.6961663, 0.0684043, 0.4079426, 0.1162414, 0.8974794, 9.896161]
    c2 = [c ** 2 if i in (2, 4, 6) else c for i, c in enumerate(c1)]
    assert np.allclose(rii.formula_n(1, c1, wl), rii.formula_n(2, c2, wl))


def test_cauchy_formula_5():
    assert rii.formula_n(5, [1.5, 0.004, -2], np.array([0.5]))[0] == pytest.approx(1.5 + 0.004 / 0.25)


def test_separate_n_and_k_grids_keep_the_whole_n_range():
    # Entries often give k over part of the n range (ZnS: n 0.4-14 um, k 0.4-1 um); the import used to
    # keep only the overlap, so the infrared n was extrapolated (n = 21 at 10 um).
    doc = {"DATA": [{"type": "tabulated n", "data": "0.4 2.0\n0.6 1.9\n0.8 1.8"},
                    {"type": "tabulated k", "data": "0.5 0.01\n1.0 0.0"}]}
    notes = []
    rows = rii.to_table(doc, notes=notes)
    assert [r[0] for r in rows] == [400.0, 500.0, 600.0, 800.0]
    assert rows[0][2] == 0 and rows[1][1] == pytest.approx(1.95) and rows[2][2] == pytest.approx(0.008)
    assert notes and "set to 0" in notes[0]


def test_crop_keeps_one_point_beyond_each_end():
    doc = {"DATA": [{"type": "tabulated nk", "data": "\n".join(f"{w / 1000} 2.0 0.1" for w in range(300, 1300, 100))}]}
    rows = rii.to_table(doc, from_nm=450, to_nm=750)
    assert [r[0] for r in rows] == [400.0, 500.0, 600.0, 700.0, 800.0]


def test_entry_without_n_is_refused():
    with pytest.raises(engine.EngineError):
        rii.to_table({"DATA": [{"type": "tabulated k", "data": "0.5 0.1\n0.6 0.1"}]})


def test_catalogue_is_flattened_with_plain_names_and_categories():
    tree = yaml.safe_load("""
- DIVIDER: "Research data"
- SHELF: main
  name: "MAIN"
  content:
    - DIVIDER: "Ta - Tantalum"
    - BOOK: Ta2O5
      name: "Ta<sub>2</sub>O<sub>5</sub> (Tantalum pentoxide)"
      content:
        - DIVIDER: "Thin film"
        - PAGE: Bright
          name: "Bright et al. 2013: n,k 0.5&ndash;1000 µm"
          data: main/Ta2O5/nk/Bright.yml
""")
    [entry] = rii.flatten(tree)
    assert entry == {"page": "main/Ta2O5/nk/Bright", "shelf": "main", "book": "Ta2O5",
                     "material": "Ta2O5 (Tantalum pentoxide)", "source": "Bright et al. 2013: n,k 0.5–1000 µm",
                     "category": "Thin film"}


def test_page_ids_cannot_leave_the_database():
    for bad in ("../secrets", "main/../../x", "http://evil/x", "a b"):
        with pytest.raises(engine.EngineError):
            rii.load_page(bad)


@pytest.mark.skipif(not os.environ.get("OPENFILTERS_TEST_NETWORK"), reason="set OPENFILTERS_TEST_NETWORK=1 to run")
def test_live_database(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    assert any(e["page"] == "main/SiO2/nk/Malitson" for e in rii.search("SiO2", limit=200)["results"])
    rows = rii.to_table(rii.load_page("main/SiO2/nk/Malitson"))
    assert np.interp(587.6, [r[0] for r in rows], [r[1] for r in rows]) == pytest.approx(1.4585, abs=2e-4)
