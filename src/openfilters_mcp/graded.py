"""Graded-index profiles expanded into a mixture's composition steps.

A layer {"material": <mixture>, "graded": {"profile": {...}, "matching": {...}, "resolution": ...}}
becomes {"steps": [...], "step_thicknesses": [...]}: the profile n(s) is sampled finely, each sample
is rounded to the nearest composition step, and runs of equal steps are merged. In optical
coordinates a run's physical thickness is its optical thickness divided by the rounded step's index,
so every step's optical thickness is exact.

Profiles (all indices at the profile's wavelength, default the design's center wavelength):
- rugate: ln n (or n) = mean + amplitude * envelope * sin(2 pi s / (lambda0 / 2) + phase), optionally
  a sum of harmonics. Modulating ln n in optical thickness gives no reflection at lambda0/2, lambda0/3
  (modulating n with a large contrast makes a false second harmonic).
- linear / quintic: a ramp from n_start to n_end (quintic: Southwell's 10t^3 - 15t^4 + 6t^5).
- custom: points [[position nm, n], ...], interpolated linearly.
Matching ramps (quintic or linear, in optical thickness) join the profile to the substrate side and
the medium side; an index beyond the mixture's range is matched only up to the end of the range.
"""

import math

import numpy as np

from .engine import EngineError

PROFILE_TYPES = ("rugate", "linear", "quintic", "custom")
PROFILE_KEYS = {
    "rugate": {"type", "wavelength", "n_min", "n_max", "n_mean", "delta_n", "periods", "modulate", "coordinate",
               "phase_deg", "apodization", "harmonics"},
    "linear": {"type", "wavelength", "n_start", "n_end", "thickness", "optical_thickness", "qwot", "coordinate"},
    "quintic": {"type", "wavelength", "n_start", "n_end", "thickness", "optical_thickness", "qwot", "coordinate"},
    "custom": {"type", "wavelength", "points", "coordinate"},
}
APODIZATIONS = ("none", "sin2", "hann", "quintic", "gaussian")
MATCHING_KEYS = {"substrate", "medium", "periods"}
SAMPLES_PER_HALF_WAVE = 8000  # samples per lambda0/2 of profile length (optical or physical)
MAX_SAMPLES = 2_000_000


def check_keys(obj, valid, where):
    unknown = sorted(set(obj) - set(valid))
    if unknown:
        raise EngineError(f"{where}: unknown key{'s' if len(unknown) > 1 else ''} {', '.join(map(repr, unknown))}; "
                          f"valid keys: {', '.join(sorted(valid))}")


def _number(obj, key, where, default=None, positive=False):
    if key not in obj:
        if default is None:
            raise EngineError(f"{where} needs {key!r}")
        return default
    try:
        value = float(obj[key])
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or (positive and value <= 0):
        raise EngineError(f"{where}: {key} must be a {'positive ' if positive else ''}finite number")
    return value


def wavelength(graded, center_wavelength):
    """The wavelength the profile is defined at (its indices and optical thicknesses)."""
    profile = graded.get("profile")
    if isinstance(profile, dict) and "wavelength" in profile:
        return _number(profile, "wavelength", "graded profile", positive=True)
    return float(center_wavelength)


def smoothstep5(t):
    t = np.clip(t, 0.0, 1.0)
    return t ** 3 * (10 - 15 * t + 6 * t * t)


def _envelope(x, periods, apodization, where):
    """Amplitude envelope at x periods from the start of a profile of `periods` periods."""
    if apodization is None:
        apodization = {"type": "none"}
    if isinstance(apodization, str):
        apodization = {"type": apodization}
    check_keys(apodization, {"type", "taper_periods"}, f"{where} apodization")
    kind = apodization.get("type", "none")
    if kind not in APODIZATIONS:
        raise EngineError(f"{where}: apodization type must be one of {', '.join(APODIZATIONS)}")
    if kind == "none":
        return np.ones_like(x)
    taper = _number(apodization, "taper_periods", f"{where} apodization", periods / 2, positive=True)
    taper = min(taper, periods / 2)
    edge = np.minimum(x, periods - x) / taper  # 0 at the ends, >= 1 in the untapered middle
    if kind in ("sin2", "hann"):
        return np.sin(np.pi / 2 * np.clip(edge, 0.0, 1.0)) ** 2
    if kind == "quintic":
        return smoothstep5(edge)
    return np.exp(-4.5 * (1 - np.clip(edge, 0.0, 1.0)) ** 2)  # gaussian, 1.1 % at the ends


def _rugate(profile, wl, where):
    """(length, coordinate, n(u) for u in [0, 1]) of a rugate profile."""
    modulate = profile.get("modulate", "ln_n")
    if modulate not in ("ln_n", "n"):
        raise EngineError(f"{where}: modulate must be \"ln_n\" (recommended) or \"n\"")
    log = modulate == "ln_n"
    if "n_min" in profile or "n_max" in profile:
        n_min, n_max = _number(profile, "n_min", where, positive=True), _number(profile, "n_max", where, positive=True)
    else:
        mean, delta = _number(profile, "n_mean", where, positive=True), _number(profile, "delta_n", where, positive=True)
        if log:  # mean is the geometric mean, delta the full swing
            n_max = delta / 2 + math.sqrt(delta * delta / 4 + mean * mean)
            n_min = n_max - delta
        else:
            n_min, n_max = mean - delta / 2, mean + delta / 2
    if not 0 < n_min < n_max:
        raise EngineError(f"{where}: n_min must be below n_max (both positive)")
    periods = _number(profile, "periods", where, positive=True)
    coordinate = profile.get("coordinate", "optical")
    if coordinate not in ("optical", "physical"):
        raise EngineError(f"{where}: coordinate must be \"optical\" (recommended) or \"physical\"")
    if log:
        mean, amplitude = (math.log(n_max) + math.log(n_min)) / 2, (math.log(n_max) - math.log(n_min)) / 2
        n_bar = math.exp(mean)
    else:
        mean, amplitude = (n_max + n_min) / 2, (n_max - n_min) / 2
        n_bar = mean
    # Period: lambda/2 of optical thickness, or lambda/(2 n_bar) of physical thickness.
    scale = 1.0 if coordinate == "optical" else 1.0 / n_bar
    length = periods * wl / 2 * scale
    harmonics = profile.get("harmonics") or [{"wavelength": wl, "amplitude": 1.0, "phase_deg": profile.get("phase_deg", 0.0)}]
    terms = []
    for j, h in enumerate(harmonics):
        if not isinstance(h, dict):
            raise EngineError(f"{where}: harmonics are objects {{\"wavelength\": nm, \"amplitude\": 1, \"phase_deg\": 0}}")
        check_keys(h, {"wavelength", "lambda0", "amplitude", "phase_deg"}, f"{where} harmonic {j + 1}")
        hw = _number(h, "wavelength" if "wavelength" in h else "lambda0", f"{where} harmonic {j + 1}", wl, positive=True)
        terms.append((wl / hw, _number(h, "amplitude", f"{where} harmonic {j + 1}", 1.0),
                      math.radians(_number(h, "phase_deg", f"{where} harmonic {j + 1}", 0.0))))
    apodization = profile.get("apodization")

    def n_of(u):
        x = u * periods  # position in periods of the profile's wavelength
        wave = sum(a * np.sin(2 * np.pi * x * ratio + phase) for ratio, a, phase in terms)
        value = mean + amplitude * _envelope(x, periods, apodization, where) * wave
        return np.exp(value) if log else value

    return length, coordinate, n_of


def _ramp(profile, wl, where, shape):
    n_start, n_end = _number(profile, "n_start", where, positive=True), _number(profile, "n_end", where, positive=True)
    coordinate = profile.get("coordinate", "optical")
    given = [k for k in ("thickness", "optical_thickness", "qwot") if k in profile]
    if len(given) != 1:
        raise EngineError(f"{where} needs one of thickness (physical nm), optical_thickness (nm) or qwot")
    if given[0] == "thickness":
        length, coordinate = _number(profile, "thickness", where, positive=True), "physical"
    else:
        length = (_number(profile, "optical_thickness", where, positive=True) if given[0] == "optical_thickness"
                  else _number(profile, "qwot", where, positive=True) * wl / 4)
        coordinate = "optical"
    ease = smoothstep5 if shape == "quintic" else (lambda t: t)
    return length, coordinate, lambda u: n_start + (n_end - n_start) * ease(u)


def _custom(profile, where):
    points = profile.get("points")
    try:
        points = sorted((float(z), float(n)) for z, n in points)
    except (TypeError, ValueError) as error:
        raise EngineError(f"{where}: points must be [[position nm, n], ...]") from error
    if len(points) < 2 or points[-1][0] <= points[0][0] or not all(math.isfinite(z) and n > 0 for z, n in points):
        raise EngineError(f"{where}: points need at least two positions (nm) with positive indices")
    coordinate = profile.get("coordinate", "physical")
    if coordinate not in ("optical", "physical"):
        raise EngineError(f"{where}: coordinate must be \"optical\" or \"physical\"")
    z, n = np.array(points).T
    length = z[-1] - z[0]
    return length, coordinate, lambda u: np.interp(z[0] + u * length, z, n)


def _main_piece(profile, wl, where):
    if not isinstance(profile, dict) or profile.get("type") not in PROFILE_TYPES:
        raise EngineError(f"{where}: profile needs \"type\": one of {', '.join(PROFILE_TYPES)}")
    check_keys(profile, PROFILE_KEYS[profile["type"]], f"{where} {profile['type']} profile")
    if profile["type"] == "rugate":
        return _rugate(profile, wl, where)
    if profile["type"] == "custom":
        return _custom(profile, where)
    return _ramp(profile, wl, where, profile["type"])


def _fresnel(a, b):
    return ((a - b) / (a + b)) ** 2


def expand(material, graded, table, wl, neighbours, where):
    """Steps, step thicknesses and warnings of a profile layer. table: the mixture's step indices at wl;
    neighbours: {"substrate": (name, n), "medium": (name, n)} at wl, for matching (None if unknown)."""
    check_keys(graded, {"profile", "matching", "resolution"}, where)
    profile = graded["profile"]
    warnings = []
    table = np.asarray(table, dtype=float)
    lo, hi = float(table.min()), float(table.max())
    length, coordinate, n_main = _main_piece(profile, wl, where)
    # Requested indices beyond the mixture: refused (a matching target beyond it is clipped below).
    probe = n_main(np.linspace(0.0, 1.0, 20001))
    half = abs(table[1] - table[0]) / 2 if len(table) > 1 else 0.0
    if profile["type"] != "rugate" or not profile.get("harmonics"):
        if probe.min() < lo - half - 1e-9 or probe.max() > hi + half + 1e-9:
            raise EngineError(f"{where}: the profile spans n = {probe.min():.4f}-{probe.max():.4f} but {material} covers "
                              f"{lo:.4f}-{hi:.4f} at {wl:g} nm")
    elif probe.min() < lo - half or probe.max() > hi + half:
        warnings.append(f"{where}: the sum of harmonics reaches n = {probe.min():.4f}-{probe.max():.4f}, beyond {material}'s "
                        f"{lo:.4f}-{hi:.4f}; it is clipped there")

    pieces = [(length, coordinate, n_main)]
    matching = graded.get("matching")
    if matching:
        if not isinstance(matching, dict):
            raise EngineError(f"{where}: matching is {{\"substrate\": \"quintic\", \"medium\": \"quintic\", \"periods\": 1}}")
        check_keys(matching, MATCHING_KEYS, f"{where} matching")
        ramp_length = _number(matching, "periods", f"{where} matching", 1.0, positive=True) * wl / 2
        for side in ("substrate", "medium"):
            shape = matching.get(side, "none")
            if shape not in ("none", "quintic", "linear"):
                raise EngineError(f"{where}: matching {side} must be \"quintic\", \"linear\" or \"none\"")
            if shape == "none":
                continue
            name, target = neighbours[side]
            reached = min(max(target, lo), hi)
            if abs(reached - target) > 1e-6:
                warnings.append(f"{where}: the {side}-side ramp stops at n = {reached:.4f}, the end of {material}'s range; "
                                f"{name} (n = {target:.4f} at {wl:g} nm) is left unmatched by {abs(target - reached):.4f} "
                                f"(R = {100 * _fresnel(target, reached):.2g} % at that interface)")
            end = float(n_main(np.array([0.0 if side == "substrate" else 1.0]))[0])
            ease = smoothstep5 if shape == "quintic" else (lambda t: t)
            a, b = (reached, end) if side == "substrate" else (end, reached)
            ramp = (ramp_length, "optical", lambda u, a=a, b=b, ease=ease: a + (b - a) * ease(u))
            pieces = [ramp] + pieces if side == "substrate" else pieces + [ramp]
    elif profile["type"] == "rugate":
        for side, u in (("substrate", 0.0), ("medium", 1.0)):
            if neighbours.get(side):
                name, target = neighbours[side]
                end = float(n_main(np.array([u]))[0])
                if _fresnel(target, end) > 1e-3:
                    warnings.append(f"{where}: the profile starts/ends at n = {end:.4f} next to {name} (n = {target:.4f}): "
                                    f"R = {100 * _fresnel(target, end):.2g} % there gives ripple across the spectrum; "
                                    "\"matching\": {\"substrate\": \"quintic\", \"medium\": \"quintic\"} adds index-matching ramps")

    resolution = graded.get("resolution", "native")
    if resolution != "native":
        try:
            resolution = float(resolution)
        except (TypeError, ValueError):
            resolution = math.nan
        if not math.isfinite(resolution) or resolution <= 0:
            raise EngineError(f"{where}: resolution must be \"native\" or an index step > 0")

    order = np.argsort(table)
    ordered = table[order]
    total = sum(p[0] for p in pieces)
    samples, thick = [], []
    for piece_length, piece_coordinate, n_of in pieces:
        count = int(min(MAX_SAMPLES, max(2000, math.ceil(piece_length / (wl / 2) * SAMPLES_PER_HALF_WAVE))))
        n = n_of((np.arange(count) + 0.5) / count)
        if resolution != "native":
            n = lo + np.round((n - lo) / resolution) * resolution
        n = np.clip(n, lo, hi)
        j = np.clip(np.searchsorted(ordered, n), 1, len(ordered) - 1)
        nearest = np.where(np.abs(ordered[j - 1] - n) <= np.abs(ordered[j] - n), j - 1, j)
        k = order[nearest]
        delta = piece_length / count
        samples.append(k)
        thick.append(delta / table[k] if piece_coordinate == "optical" else np.full(count, delta))
    k, d = np.concatenate(samples), np.concatenate(thick)
    starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
    steps = [int(s) for s in k[starts]]
    thicknesses = [float(t) for t in np.add.reduceat(d, starts)]
    if total <= 0:
        raise EngineError(f"{where}: the profile has no thickness")
    return steps, thicknesses, warnings
