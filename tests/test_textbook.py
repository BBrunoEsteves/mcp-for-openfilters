"""Closed-form results from thin-film optics textbooks, computed through the engine.

References: H. A. Macleod, Thin-Film Optical Filters (CRC Press), chapters 2-5;
M. Born and E. Wolf, Principles of Optics, section 1.5. Non-dispersive materials
are used so the formulas apply exactly.
"""

import math

import numpy as np
import pytest

from mcp_for_openfilters import engine

NS, NH, NL, NA = 1.52, 2.35, 1.46, 1.38  # substrate, high, low, MgF2-like
W0 = 550.0


@pytest.fixture(autouse=True)
def materials(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)
    for name, n in (("S", NS), ("H", NH), ("L", NL), ("A", NA), ("V", NA * math.sqrt(NS))):
        engine.add_material(name, "constant", n=n)


def design(layers, **extra):
    return {"substrate": "S", "center_wavelength": W0,
            "layers": [{"material": m, "qwot": q} for m, q in layers], **extra}


def value(d, quantity="R", wavelength=W0, angle=0.0, polarization="unpolarized"):
    r = engine.spectra(d, [quantity], [wavelength], [angle], [polarization])
    return float(r["series"][0]["values"][0])


def test_quarter_wave_antireflection_layer():
    # Macleod 3.2: R = ((ns - n^2) / (ns + n^2))^2 at the design wavelength.
    assert value(design([("A", 1)])) == pytest.approx(((NS - NA ** 2) / (NS + NA ** 2)) ** 2, abs=1e-12)


def test_half_wave_layer_is_absentee():
    # Macleod 2.6: a half-wave layer leaves the reflectance of the bare substrate unchanged.
    bare = ((NS - 1) / (NS + 1)) ** 2
    assert value(design([("H", 2)])) == pytest.approx(bare, abs=1e-12)
    assert value(design([("H", 2), ("L", 2)])) == pytest.approx(bare, abs=1e-12)


def test_quarter_quarter_v_coat_is_zero():
    # Macleod 3.3: two quarter waves give R = 0 when n_inner = n_outer * sqrt(ns).
    assert value(design([("V", 1), ("A", 1)])) == pytest.approx(0.0, abs=1e-12)


def test_brewster_angle_of_bare_substrate():
    # Born & Wolf 1.5.3: p-polarized reflectance vanishes at tan(theta) = ns.
    brewster = math.degrees(math.atan(NS))
    d = design([("A", 0)])  # zero-thickness layer: bare surface
    assert value(d, angle=brewster, polarization="p") == pytest.approx(0.0, abs=1e-12)
    assert value(d, angle=brewster, polarization="s") > 0.1


def test_quarter_wave_stack_peak_reflectance():
    # Macleod 5.1: R = ((1 - (nH/nL)^(2p) nH^2/ns) / (1 + (nH/nL)^(2p) nH^2/ns))^2 for (HL)^p H.
    p = 6
    y = (NH / NL) ** (2 * p) * NH ** 2 / NS
    assert value(design([("H", 1), ("L", 1)] * p + [("H", 1)])) == pytest.approx(((1 - y) / (1 + y)) ** 2, abs=1e-12)


def test_quarter_wave_stack_stop_band_width():
    # Macleod 5.1: the high-reflectance zone spans g = W0/wavelength in 1 +/- (2/pi) asin((nH-nL)/(nH+nL)).
    half_width = 2 / math.pi * math.asin((NH - NL) / (NH + NL))
    d = design([("H", 1), ("L", 1)] * 40 + [("H", 1)])
    # Follow the band outward from its centre (side lobes further out can exceed 50 %).
    offsets = np.linspace(0, 1.5 * half_width, 1501)
    for sign in (-1, 1):
        r = engine.spectra(d, ["R"], list(W0 / (1 + sign * offsets)))["series"][0]["values"]
        edge = offsets[np.argmax(r < 0.5)]
        assert edge == pytest.approx(half_width, rel=0.1)
        assert r[offsets < 0.97 * half_width].min() > 0.9999


@pytest.mark.parametrize("polarization", ["s", "p"])
def test_lossless_stack_conserves_energy_at_oblique_incidence(polarization):
    d = design([("H", 1), ("L", 1)] * 5 + [("H", 0.7)])
    wavelengths = list(np.linspace(400, 900, 51))
    r = engine.spectra(d, ["R", "T"], wavelengths, [45.0], [polarization])["series"]
    assert np.allclose(r[0]["values"] + r[1]["values"], 1.0, atol=1e-12)
