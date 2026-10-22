"""Validate the OpenFilters calculation core against analytic results and an
independent transfer-matrix implementation."""

import numpy as np
import pytest

from openfilters_core import optical_filter
from openfilters_core.definitions import S, P, UNPOLARIZED

from reference_tmm import rt

WVLS = np.arange(400.0, 1200.0 + 1e-9, 2.0)


def make_filter(layers, substrate="L", medium="void", wvls=WVLS):
    """layers: (material, thickness) ordered from the substrate upward."""
    f = optical_filter.optical_filter()
    f.set_wavelengths_by_range(wvls[0], wvls[-1], wvls[1] - wvls[0])
    f.set_substrate(substrate)
    f.set_medium(medium)
    f.set_consider_backside(False)
    for material, thickness in layers:
        f.add_layer(material, thickness)
    return f


def index_of(f, name, wvls=WVLS):
    # get_index() is real-only; get_N() gives the complex index n - ik.
    N = f.get_material_catalog().get_material(name).get_N(f.wvls)
    return np.array([N[i] for i in range(len(wvls))], dtype=complex)


def reference(f, layers, substrate, medium, angle, pol):
    stack = [(index_of(f, m), d) for m, d in reversed(layers)]
    if pol == "unpolarized":
        Rs, Ts, _ = rt(WVLS, index_of(f, medium), stack, index_of(f, substrate), angle, "s")
        Rp, Tp, _ = rt(WVLS, index_of(f, medium), stack, index_of(f, substrate), angle, "p")
        return (Rs + Rp) / 2, (Ts + Tp) / 2
    R, T, _ = rt(WVLS, index_of(f, medium), stack, index_of(f, substrate), angle, pol)
    return R, T


POL = {"s": S, "p": P, "unpolarized": UNPOLARIZED}


def test_quarter_wave_mirror_analytic():
    # (HL)^N H on L substrate, quarter wave at 800 nm, in air.
    nH, nL, n0, ns, N, wl0 = 2.4, 1.5, 1.0, 1.5, 6, 800.0
    layers = []
    for _ in range(N):
        layers += [("H", wl0 / (4 * nH)), ("L", wl0 / (4 * nL))]
    layers += [("H", wl0 / (4 * nH))]
    f = make_filter(layers)
    R = np.array(f.reflection(0.0, UNPOLARIZED))
    Y = (nH / nL) ** (2 * N) * nH ** 2 / ns
    R_analytic = ((n0 - Y) / (n0 + Y)) ** 2
    i = int(np.argmin(abs(WVLS - wl0)))
    assert R[i] == pytest.approx(R_analytic, abs=1e-10)


@pytest.mark.parametrize("angle", [0.0, 30.0, 60.0])
@pytest.mark.parametrize("pol", ["s", "p", "unpolarized"])
@pytest.mark.parametrize("case", ["dielectric_dispersive", "metal_dielectric"])
def test_against_reference_tmm(case, pol, angle):
    if case == "dielectric_dispersive":
        layers = [("TiO2", 87.3), ("SiO2", 140.0)] * 5 + [("TiO2", 45.0)]
        substrate, medium = "FusedSilica", "void"
    else:
        layers = [("SiO2", 120.0), ("Cr", 8.0), ("SiO2", 95.0), ("Ag", 40.0), ("TiO2", 30.0)]
        substrate, medium = "BK7", "void"
    f = make_filter(layers, substrate, medium)
    R = np.array(f.reflection(angle, POL[pol]))
    T = np.array(f.transmission(angle, POL[pol]))
    A = np.array(f.absorption(angle, POL[pol]))
    R_ref, T_ref = reference(f, layers, substrate, medium, angle, pol)
    np.testing.assert_allclose(R, R_ref, atol=1e-9)
    np.testing.assert_allclose(T, T_ref, atol=1e-9)
    np.testing.assert_allclose(A, 1 - R_ref - T_ref, atol=1e-9)
