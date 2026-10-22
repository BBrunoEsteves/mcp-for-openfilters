"""Independent characteristic-matrix (Abeles) calculation used to validate
OpenFilters. Written from textbook formulas (Macleod, Thin-Film Optical
Filters, ch. 2) and deliberately shares no code with OpenFilters."""

import numpy as np


def admittance(N, cos_theta, polarization):
    """Tilted optical admittance in units of the free-space admittance."""
    if polarization == "s":
        return N * cos_theta
    return N / cos_theta


def cos_in(N, n0_sin0):
    """cos(theta) inside a medium of complex index N = n - ik; branch chosen
    so that waves decay into the medium (Im(N cos) <= 0, Macleod convention)."""
    c = np.sqrt(1 - (n0_sin0 / N) ** 2 + 0j)
    flip = np.imag(N * c) > 0
    return np.where(flip, -c, c)


def rt(wavelengths, n_medium, layers, n_substrate, angle_deg=0.0, polarization="s"):
    """R, T and complex r for a stack.

    layers: list of (N_array_or_scalar, thickness_nm) ordered from the
    incident medium towards the substrate. Indices may be arrays over
    `wavelengths` (complex, Macleod's N = n - ik convention, k >= 0 absorbing).
    Returns (R, T, r)."""
    wl = np.asarray(wavelengths, dtype=float)
    n0 = np.broadcast_to(np.asarray(n_medium, dtype=complex), wl.shape)
    ns = np.broadcast_to(np.asarray(n_substrate, dtype=complex), wl.shape)
    n0_sin0 = n0 * np.sin(np.radians(angle_deg))

    eta0 = admittance(n0, cos_in(n0, n0_sin0), polarization)
    etas = admittance(ns, cos_in(ns, n0_sin0), polarization)

    B = np.ones_like(wl, dtype=complex)
    C = etas.copy()
    # Multiply from the substrate side upward: [B, C] = M1 M2 ... Mq [1, etas].
    for N, d in reversed(layers):
        N = np.broadcast_to(np.asarray(N, dtype=complex), wl.shape)
        c = cos_in(N, n0_sin0)
        eta = admittance(N, c, polarization)
        delta = 2 * np.pi * N * d * c / wl
        cd, sd = np.cos(delta), np.sin(delta)
        B, C = cd * B + 1j * sd / eta * C, 1j * eta * sd * B + cd * C

    r = (eta0 * B - C) / (eta0 * B + C)
    R = np.abs(r) ** 2
    T = 4 * np.real(eta0) * np.real(etas) / np.abs(eta0 * B + C) ** 2
    return R, T, r
