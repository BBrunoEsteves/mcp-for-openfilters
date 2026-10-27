"""Temperature dependence of a coating's spectral features (not part of OpenFilters).

Linear model: n_i(T) = n_i + dn_dT_i * dT (and k likewise with dk_dT), and each film's thickness
d_i(T) = d_i * (1 + strain_i * dT). A film clamped to its substrate (the default) has the in-plane strain
of the substrate, so its thickness changes by the Poisson-corrected strain

    strain_i = (1 + nu_i) / (1 - nu_i) * cte_i - 2 nu_i / (1 - nu_i) * cte_substrate

and a free film by its own cte_i. The elasto-optic effect and the temperature dependence of the substrate
are not included.

Materials are perturbed in memory (OpenFilters table materials sampled at the calculation's wavelengths),
so the user's material library is never touched. Spectral features are the peak of R or T nearest a
wavelength (a band-pass line or a reflection band) and its half-maximum centre, as engine.half_max_summary.
"""

import math

import numpy as np

from openfilters_core import materials as of_materials

from . import engine
from .engine import EngineError

THERMAL_KEYS = {"materials", "substrate_cte", "clamped"}
PROPERTY_KEYS = {"dn_dT", "dk_dT", "cte", "poisson"}
N_STEP = 0.002  # RIU, central differences for d(lambda)/dn
STRAIN_STEP = 0.002  # relative thickness change, central differences for d(lambda)/d(strain)


class _Catalog:
    """The user's catalog, with some materials replaced by shifted copies."""

    def __init__(self, shifted, mode="hold"):
        self.base, self.shifted = engine.catalog_for(mode), shifted

    def get_material(self, name):
        return self.shifted[name] if name in self.shifted else self.base.get_material(name)

    def __getattr__(self, attribute):
        return getattr(self.base, attribute)


def _shifted_material(name, dn, dk, wavelengths, mode="hold"):
    """An in-memory table copy of a regular material with n + dn and k + dk at the wavelengths."""
    material = engine._material(name)
    if material.is_mixture():
        raise EngineError(f"{name} is a mixture: thermal calculations support regular materials only")
    N = engine.material_N(name, wavelengths, mode)
    table = of_materials.material_table()
    table.name = name
    table.set_properties(list(wavelengths), [complex(z.real + dn, -max(-z.imag + dk, 0.0)) for z in N])
    return table


def _explicit(design):
    """The design with explicit layers (formulas expanded), as engine.design_from_filter returns it."""
    expanded = engine.design_from_filter(engine.build_filter(design))
    for key in ("layers", "back_layers"):
        if any(layer.get("graded") for layer in expanded.get(key) or []):
            raise EngineError("thermal calculations do not support graded layers")
    return expanded


def _values(design, quantity, wavelengths, angle, polarization, dn=None, dk=None, strain=None):
    """R, T or A of a perturbed design: dn/dk {material: shift}, strain {material: relative thickness change}."""
    strain = strain or {}
    perturbed = dict(design)
    for key in ("layers", "back_layers"):
        if design.get(key):
            perturbed[key] = [dict(layer, thickness=layer["thickness"] * (1.0 + strain.get(layer["material"], 0.0)))
                              for layer in design[key]]
    grid = sorted(set(wavelengths) | {float(design["center_wavelength"])})
    names = set(dn or {}) | set(dk or {})
    shifted = {name: _shifted_material(name, (dn or {}).get(name, 0.0), (dk or {}).get(name, 0.0), grid,
                                         engine.extrapolation_mode(design))
               for name in names}
    f = engine.build_filter(perturbed, wavelengths, materials=_Catalog(shifted, engine.extrapolation_mode(design)) if shifted else None)
    if quantity not in ("R", "T", "A"):
        raise EngineError("quantity must be R, T or A")
    method = getattr(f, engine.QUANTITIES[quantity])
    return np.asarray(method(float(angle), engine.polarization_value(polarization)), dtype=float)


def _grid(start, stop, step):
    count = int(math.floor((stop - start) / step + 1e-9)) + 1
    if count > 20001:
        raise EngineError("the peak needs too many wavelengths: narrow the window")
    return [start + i * step for i in range(count)]


def peak(design, quantity="T", near=None, window=None, angle=0.0, polarization="unpolarized", **perturbation):
    """The peak of R/T/A nearest `near` (default the design's center wavelength): peak wavelength and value,
    FWHM and half-maximum centre, found on `window` ({"from", "to", "step"}, default near +/- 25 nm) and then
    recomputed on a grid of FWHM/500 around the peak. The window is widened when an edge falls outside it."""
    near = float(near if near is not None else design["center_wavelength"])
    window = dict(window or {"from": near - 25.0, "to": near + 25.0, "step": 0.05})
    for _ in range(4):
        x = _grid(float(window["from"]), float(window["to"]), float(window["step"]))
        summary = engine.half_max_summary(x, _values(design, quantity, x, angle, polarization, **perturbation),
                                          {"near": near})
        if summary["fwhm"] is not None:
            break
        span = float(window["to"]) - float(window["from"])
        window = {"from": max(float(window["from"]) - span, 1.0), "to": float(window["to"]) + span,
                  "step": float(window["step"]) * 2}
    else:
        raise EngineError(f"no complete {quantity} peak near {near:g} nm (its half-maximum edges stay outside "
                          "the window): give a window that contains the whole peak")
    low, high = summary["half_max_edges"]
    width = high - low
    x = _grid(low - 0.5 * width, high + 0.5 * width, width / 500.0)
    fine = engine.half_max_summary(x, _values(design, quantity, x, angle, polarization, **perturbation),
                                   {"near": summary["peak_nm"]})
    if fine["fwhm"] is None:  # the peak is not where the coarse grid put it: keep the coarse result
        fine = summary
    return {"center_nm": fine["half_max_center_nm"], "peak_nm": fine["peak_nm"], "peak_value": fine["peak_value"],
            "fwhm_nm": fine["fwhm"], "half_max_edges_nm": fine["half_max_edges"]}


def _material_warnings(design, result):
    """engine.material_warnings over the peak's region (tables beyond their data, unphysical indices)."""
    low, high = result["half_max_edges_nm"]
    return engine.material_warnings(design, [low - (high - low), high + (high - low)])


def _layer_materials(design):
    names = []
    for key in ("layers", "back_layers"):
        for layer in design.get(key) or []:
            if layer["material"] not in names:
                names.append(layer["material"])
    return names


def strains(design, thermal):
    """Per material: thickness strain per kelvin, and the warnings about missing properties."""
    thermal = thermal or {}
    engine._check_keys(thermal, THERMAL_KEYS, "thermal")
    props = thermal.get("materials") or {}
    clamped = bool(thermal.get("clamped", True))
    out, warnings, missing = {}, [], []
    for name in _layer_materials(design):
        p = props.get(name)
        if p is None:
            missing.append(name)
            p = {}
        engine._check_keys(p, PROPERTY_KEYS, f"thermal properties of {name}")
        cte = float(p.get("cte", 0.0))
        if not cte:
            out[name] = 0.0
        elif not clamped:
            out[name] = cte
        else:
            if "poisson" not in p or "substrate_cte" not in thermal:
                raise EngineError(f"a film clamped to its substrate needs the film's poisson ratio and the "
                                  f"substrate_cte (fused silica 0.55e-6/K, BK7 7.1e-6/K) to give {name}'s strain; "
                                  "or set \"clamped\": false for free expansion")
            nu, alpha_s = float(p["poisson"]), float(thermal["substrate_cte"])
            out[name] = (1 + nu) / (1 - nu) * cte - 2 * nu / (1 - nu) * alpha_s
    if missing:
        warnings.append(f"no thermal properties given for {', '.join(missing)}: their dn/dT and expansion are "
                        "taken as 0")
    return out, warnings


def sensitivity(design, quantity="T", near=None, window=None, angle=0.0, polarization="unpolarized", thermal=None):
    """Shift of the peak's half-maximum centre per unit index change (nm/RIU) and per unit thickness
    strain (nm) of each material's layers, by central differences; with thermal properties, the resulting
    d(lambda)/dT in pm/K, each material's contribution and the dn/dT that would make the peak athermal."""
    design = _explicit(design)
    common = dict(quantity=quantity, window=window, angle=angle, polarization=polarization)
    base = peak(design, near=near, **common)
    material_warnings = _material_warnings(design, base)
    near = base["peak_nm"]
    rows = {}
    for name in _layer_materials(design):
        up = peak(design, near=near, dn={name: N_STEP}, **common)["center_nm"]
        down = peak(design, near=near, dn={name: -N_STEP}, **common)["center_nm"]
        thick = peak(design, near=near, strain={name: STRAIN_STEP}, **common)["center_nm"]
        thin = peak(design, near=near, strain={name: -STRAIN_STEP}, **common)["center_nm"]
        rows[name] = {"dlambda_dn_nm_per_RIU": engine.sig((up - down) / (2 * N_STEP), 6),
                      "dlambda_dstrain_nm": engine.sig((thick - thin) / (2 * STRAIN_STEP), 6)}
    out = {"quantity": quantity, "peak": base, "materials": rows,
           "note": "half-maximum centre shift per unit index change of all layers of a material (nm/RIU) and per "
                   "unit relative thickness change of them (nm); central differences of "
                   f"{N_STEP} RIU and {STRAIN_STEP:.1%}"}
    warnings = list(material_warnings)
    if thermal:
        strain, missing = strains(design, thermal)
        warnings += missing
        props = thermal.get("materials") or {}
        total = 0.0
        for name, row in rows.items():
            dn_dT = float((props.get(name) or {}).get("dn_dT", 0.0))
            index_part = row["dlambda_dn_nm_per_RIU"] * dn_dT * 1e3
            strain_part = row["dlambda_dstrain_nm"] * strain[name] * 1e3
            row.update(thickness_strain_per_K=engine.sig(strain[name], 6),
                       contribution_pm_per_K=engine.sig(index_part + strain_part, 6),
                       index_part_pm_per_K=engine.sig(index_part, 6), thickness_part_pm_per_K=engine.sig(strain_part, 6))
            total += index_part + strain_part
        for name, row in rows.items():
            # The dn/dT of this material that would cancel everything else (meaningless for a material
            # that barely moves the peak, e.g. a thin electrode at a node of the field).
            if abs(row["dlambda_dn_nm_per_RIU"]) >= 1.0:
                rest = total - row["index_part_pm_per_K"]
                row["athermal_dn_dT"] = engine.sig(-rest / (row["dlambda_dn_nm_per_RIU"] * 1e3), 4)
        out.update(dlambda_dT_pm_per_K=engine.sig(total, 6), clamped=bool(thermal.get("clamped", True)))
        if base["fwhm_nm"] and total:
            out["kelvin_per_fwhm"] = engine.sig(base["fwhm_nm"] * 1e3 / abs(total), 4)
    if warnings:
        out["warnings"] = warnings
    return out


def sweep(design, delta_T, thermal, quantity="T", near=None, window=None, angle=0.0, polarization="unpolarized"):
    """The peak at each temperature change (K, from the temperature the design's n,k and thicknesses
    describe), with every material's dn/dT, dk/dT and thickness strain applied together; a linear fit of the
    half-maximum centre gives d(lambda)/dT."""
    design = _explicit(design)
    if not delta_T:
        raise EngineError("delta_T needs at least one temperature change (K)")
    strain, warnings = strains(design, thermal)
    props = thermal.get("materials") or {}
    rows = []
    for dT in delta_T:
        dT = float(dT)
        dn = {name: float((props.get(name) or {}).get("dn_dT", 0.0)) * dT for name in strain}
        dk = {name: float((props.get(name) or {}).get("dk_dT", 0.0)) * dT for name in strain}
        p = peak(design, quantity=quantity, near=near, window=window, angle=angle, polarization=polarization,
                 dn=dn, dk=dk, strain={name: s * dT for name, s in strain.items()})
        if not rows:
            warnings += _material_warnings(design, p)
        near = p["peak_nm"]  # follow the peak as it moves
        rows.append({"delta_T_K": dT, **p})
    out = {"quantity": quantity, "rows": rows, "clamped": bool(thermal.get("clamped", True))}
    if len(rows) > 1:
        slope, intercept = np.polyfit([r["delta_T_K"] for r in rows], [r["center_nm"] for r in rows], 1)
        out["dlambda_dT_pm_per_K"] = engine.sig(slope * 1e3, 6)
        fwhm = [r["fwhm_nm"] for r in rows if r["fwhm_nm"]]
        if fwhm and slope:
            out["kelvin_per_fwhm"] = engine.sig(min(fwhm) / abs(slope), 4)
    if warnings:
        out["warnings"] = warnings
    return out
