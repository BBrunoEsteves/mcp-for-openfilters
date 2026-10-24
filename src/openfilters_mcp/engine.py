"""Bridge between JSON-friendly descriptions and the OpenFilters core.

Everything here takes and returns plain dicts/lists/floats so it can run in
worker processes and be serialized to disk. Conventions:

* wavelengths and thicknesses in nm, angles in degrees;
* R, T, A as fractions (0-1); phase in degrees, GD in fs, GDD in fs^2;
* complex indices follow OpenFilters/Macleod: N = n - ik with k >= 0;
* layers are listed from the substrate outward (layer 0 touches the
  substrate), as in OpenFilters.
"""

import math
import os
import time

import numpy as np

from openfilters_core import color as of_color
from openfilters_core import materials as of_materials
from openfilters_core import optical_filter
from openfilters_core import project as of_project
from openfilters_core import targets as of_targets
from openfilters_core import version as of_version
from openfilters_mcp import __version__
from openfilters_core.definitions import BACK, FRONT, P, S, UNPOLARIZED
from openfilters_core.optimization_needles import optimization_needles
from openfilters_core.optimization_refinement import THICKNESS, optimization_refinement
from openfilters_core.optimization_steps import optimization_steps

from . import settings


class EngineError(ValueError):
    """Invalid input; the message is meant to be shown to the user."""


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

_catalog = None
_catalog_signature = None


def _user_materials_signature(user_dir):
    """Names, sizes and modification times of the user's material files.
    Each worker process keeps its own catalog, so this is how a worker
    notices materials added, overwritten or copied in by anyone else."""
    entries = []
    for entry in os.scandir(user_dir):
        if entry.name.lower().endswith(".mat"):
            stat = entry.stat()
            entries.append((entry.name, stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(entries))


def catalog(reload=False):
    """Built-in OpenFilters materials plus the user's own material folder
    (user materials take precedence). Reloaded whenever the user folder
    changes."""
    global _catalog, _catalog_signature
    user_dir = settings.materials_dir()
    signature = (user_dir, _user_materials_signature(user_dir))
    if _catalog is None or reload or signature != _catalog_signature:
        # OpenFilters prefers the catalog LAST in the list, so the user's goes last.
        _catalog = of_materials.material_catalogs(
            [of_materials.material_catalog(), of_materials.material_catalog(user_dir)])
        _catalog_signature = signature
    return _catalog


def _material(name):
    try:
        return catalog().get_material(name)
    except of_materials.material_error as error:
        raise EngineError(f"Unknown material '{name}'. Use list_materials to see what is available.") from error


def material_N(name, wavelengths):
    """Complex index (n - ik) of a regular material at the wavelengths."""
    material = _material(name)
    if material.is_mixture():
        raise EngineError(f"'{name}' is a mixture; its index depends on the composition.")
    f = optical_filter.optical_filter(catalog())
    f.set_wavelengths([float(w) for w in wavelengths])
    N = material.get_N(f.wvls)
    return np.array([N[i] for i in range(len(wavelengths))], dtype=complex)


def list_materials(name_filter=None, wavelengths=(400.0, 550.0, 800.0, 1550.0)):
    user_dir = settings.materials_dir()
    rows = []
    for name in catalog().get_material_names():
        if name_filter and name_filter.lower() not in name.lower():
            continue
        material = _material(name)
        row = {"name": name, "model": of_materials.MODELS.get(material.get_model(), material.get_model()),
               "kind": "mixture" if material.is_mixture() else "regular",
               "source": "user" if os.path.exists(os.path.join(user_dir, name + ".mat")) else "built-in",
               "description": material.get_description()}
        if not material.is_mixture():
            N = material_N(name, wavelengths)
            row["n,k"] = {f"{w:g}": [round(z.real, 4), round(-z.imag, 5)] for w, z in zip(wavelengths, N)}
        else:
            row["index_range_at_550"] = [round(x, 4) for x in material.get_index_range(550.0)]
        rows.append(row)
    return rows


MODEL_NAMES = {"constant": "constant", "cauchy": "Cauchy", "sellmeier": "Sellmeier", "table": "table"}


def add_material(name, model, description="", n=None, k=0.0, coefficients=None, table=None,
                 wavelength_unit="nm", overwrite=False):
    """Write a regular (non-mixture) material file to the user material
    folder and return its summary."""
    if not name or any(c in name for c in r'\/:*?"<>| '):
        raise EngineError("Material names must be non-empty and contain no spaces or path characters.")
    path = os.path.join(settings.materials_dir(), name + ".mat")
    if os.path.exists(path) and not overwrite:
        raise EngineError(f"Material '{name}' already exists in the user folder; pass overwrite=true to replace it.")
    model = model.lower()
    if model not in MODEL_NAMES:
        raise EngineError("model must be constant, cauchy, sellmeier or table")

    if model == "constant":
        if n is None:
            raise EngineError("constant model needs n (and optionally k)")
        properties = f"{_index_str(n, k)}"
    elif model in ("cauchy", "sellmeier"):
        expected = {"cauchy": (3, 6), "sellmeier": (6, 9)}[model]
        if not coefficients or len(coefficients) not in expected:
            raise EngineError(
                "cauchy takes [A, B, C] (n = A + B/l^2 + C/l^4, l in um) optionally followed by "
                "[Ak, exponent, edge] for Urbach absorption; sellmeier takes [B1, C1, B2, C2, B3, C3] "
                "(C in um^2) optionally followed by the same 3 absorption terms")
        properties = " ".join(f"{c:.10g}" for c in coefficients)
    else:
        scale = {"nm": 1.0, "um": 1000.0, "µm": 1000.0, "ev": None}[wavelength_unit.lower()]
        if not table or len(table) < 2:
            raise EngineError("table model needs rows [wavelength, n, k] (at least 2)")
        rows = []
        for row in table:
            wvl = 1239.84193 / row[0] if scale is None else row[0] * scale
            rows.append((wvl, row[1], row[2] if len(row) > 2 else 0.0))
        rows.sort()
        properties = "\n" + "\n".join(f"\t{w:.6f}\t{_index_str(nn, kk)}" for w, nn, kk in rows)

    lines = [f"Description: {description or name}", "Kind: regular", f"Model: {MODEL_NAMES[model]}"]
    if model == "table":
        lines += ["Properties:" + properties, "End"]
    else:
        lines += [f"Properties: {properties}"]
    text = "\n".join(lines) + "\n"
    # Validate with OpenFilters' own parser before writing.
    try:
        of_materials.parse_material(name, text.splitlines(True))
    except of_materials.material_error as error:
        raise EngineError(f"Invalid material: {error}") from error
    # Latin-1, like every OpenFilters data file (keeps them readable by the GUI).
    with open(path, "w", encoding="latin-1", errors="replace") as outfile:
        outfile.write(text)
    catalog(reload=True)
    return next(row for row in list_materials(name) if row["name"] == name)


def remove_material(name):
    """Delete a material from the user folder (built-in materials cannot be removed)."""
    path = os.path.join(settings.materials_dir(), str(name) + ".mat")
    if not name or any(c in name for c in r'\/:*?"<>| ') or not os.path.isfile(path):
        builtin = name in of_materials.material_catalog().get_material_names()
        raise EngineError(f"'{name}' is not in your material folder" + (
            " (it is a built-in OpenFilters material, which cannot be removed)" if builtin else ""))
    os.remove(path)
    catalog(reload=True)
    still = name in catalog().get_material_names()
    return {"removed": name, "file": path,
            "note": f"the built-in '{name}' is used again" if still else f"'{name}' no longer exists"}


def _index_str(n, k):
    k = abs(float(k))
    return f"{float(n):.8g}" if k == 0 else f"{float(n):.8g}-{k:.8g}j"


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------

def _wavelength_list(spec, default=(380.0, 1100.0, 1.0)):
    """Accept {"from":..,"to":..,"step":..}, a list of wavelengths, or None."""
    if spec is None:
        spec = {"from": default[0], "to": default[1], "step": default[2]}
    if isinstance(spec, dict):
        start, stop = float(spec["from"]), float(spec["to"])
        step = float(spec.get("step", 1.0))
        if step <= 0 or stop <= start:
            raise EngineError("wavelength range needs from < to and step > 0")
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        if count > 200000:
            raise EngineError("too many wavelengths (max 200000)")
        return [start + i * step for i in range(count)]
    values = [float(w) for w in spec]
    if not values:
        raise EngineError("empty wavelength list")
    return values


def polarization_value(polarization):
    if isinstance(polarization, (int, float)):
        return float(polarization)
    key = str(polarization).strip().lower()
    table = {"s": S, "te": S, "p": P, "tm": P, "unpolarized": UNPOLARIZED, "u": UNPOLARIZED,
             "avg": UNPOLARIZED, "average": UNPOLARIZED, "mean": UNPOLARIZED}
    if key not in table:
        raise EngineError("polarization must be s, p, unpolarized, or a mixing angle in degrees (0 = p, 90 = s)")
    return table[key]


def polarization_name(value):
    return {S: "s", P: "p", UNPOLARIZED: "unpolarized"}.get(value, f"{value:g}deg")


def build_filter(design, wavelengths=None):
    """Create an OpenFilters filter from a design dict."""
    if not isinstance(design, dict):
        raise EngineError("design must be an object")
    f = optical_filter.optical_filter(catalog())
    try:
        f.set_center_wavelength(float(design.get("center_wavelength", 550.0)))
        if wavelengths is not None:
            f.set_wavelengths(list(wavelengths))
        f.set_substrate(design.get("substrate", "FusedSilica"))
        f.set_substrate_thickness(float(design.get("substrate_thickness_mm", 1.0)) * 1e6)
        f.set_medium(design.get("incident_medium", "void"), FRONT)
        f.set_medium(design.get("exit_medium", design.get("incident_medium", "void")), BACK)
        f.set_consider_backside(bool(design.get("consider_backside", False)))
        if "consider_backside_on_monitoring" in design:
            f.set_consider_backside_on_monitoring(bool(design["consider_backside_on_monitoring"]))
        if "illuminant" in design:
            f.set_illuminant(design["illuminant"])
        if "observer" in design:
            f.set_observer(design["observer"])
        # Settings saved in .ofp projects so they are kept when the GUI opens them.
        if design.get("description"):
            f.set_description(" ".join(str(design["description"]).split()))
        if design.get("needle_materials"):
            f.set_needle_materials([str(m) for m in design["needle_materials"]])
        if design.get("step_spacing"):
            f.set_step_spacing(float(design["step_spacing"]))
        if wavelengths is None and design.get("wavelength_range"):
            r = design["wavelength_range"]
            f.set_wavelengths_by_range(float(r["from"]), float(r["to"]), float(r.get("step", 1.0)))
        for side, key, formula_key in ((FRONT, "layers", "formula"), (BACK, "back_layers", "back_formula")):
            if design.get(formula_key):
                formula = design[formula_key]
                if isinstance(formula, str):
                    raise EngineError(f"{formula_key} must be {{\"formula\": \"(HL)^8 H\", \"materials\": {{\"H\": \"TiO2\", \"L\": \"SiO2\"}}}}")
                # OpenFilters expects {symbol: (material, index_for_mixtures)}.
                symbols = {sym: (tuple(v) if isinstance(v, (list, tuple)) else (v, None))
                           for sym, v in formula["materials"].items()}
                f.set_stack_formula(formula["formula"], symbols, side)
                f.apply_stack_formula(side)
            for i, layer in enumerate(design.get(key) or []):
                _add_layer(f, layer, side, i)
    except (optical_filter.filter_error, of_materials.material_error) as error:
        raise EngineError(str(error)) from error
    return f


def _add_layer(f, layer, side, i):
    if isinstance(layer, (list, tuple)):
        layer = {"material": layer[0], "thickness": layer[1]}
    material = layer.get("material")
    if material is None:
        raise EngineError(f"layer {i} has no material")
    index = layer.get("index")
    position = f.get_nb_layers(side)
    if layer.get("graded"):
        graded = layer["graded"]
        if not isinstance(graded, dict) or "steps" not in graded or "step_thicknesses" not in graded:
            raise EngineError(f"graded layer {i} needs {{\"steps\": [...], \"step_thicknesses\": [...]}} (as returned by describe_design or load_project)")
        f.add_graded_layer_from_steps(material, [int(x) for x in graded["steps"]],
                                      [float(t) for t in graded["step_thicknesses"]], side=side)
        return
    def number(key):
        # JSON carries NaN as null; anything that is not a finite number is refused here.
        # (A NaN thickness evaluates as an empty coating, R = 0, and would pass "<=" targets.)
        try:
            value = float(layer[key])
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            raise EngineError(f"layer {i} has an invalid {key} ({layer[key]!r}); it must be a finite number")
        return value

    if index is not None:
        index = number("index")
    if "qwot" in layer:
        mat = _material(material)
        n = index if index is not None else mat.get_index(f.get_center_wavelength())
        thickness = number("qwot") * f.get_center_wavelength() / (4.0 * n)
    elif "thickness" in layer:
        thickness = number("thickness")
    else:
        raise EngineError(f"layer {i} needs thickness (nm) or qwot")
    if thickness < 0:
        raise EngineError(f"layer {i} has a negative thickness")
    f.add_layer(material, thickness, side=side, index=index)
    for flag, setter in _LAYER_FLAGS:
        if flag in layer:
            getattr(f, "set_" + setter)(position, bool(layer[flag]), side)


# Per-layer optimization flags: design key -> OpenFilters set_/get_ suffix.
_LAYER_FLAGS = (("refine_thickness", "refine_layer_thickness"), ("refine_index", "refine_layer_index"),
                ("preserve_ot", "preserve_OT"), ("add_needles", "add_needles"), ("add_steps", "add_steps"))
# OpenFilters' defaults for filter settings that design_from_filter reports only when changed.
_DEFAULT_WAVELENGTH_RANGE = (300.0, 1000.0, 1.0)
_DEFAULT_STEP_SPACING = 0.01


def _default_layer_flags(side, mixture):
    """The flags optical_filter.add_layer gives a new layer."""
    front = side == FRONT
    return {"refine_thickness": front, "refine_index": front and mixture, "preserve_ot": False,
            "add_needles": front, "add_steps": front and mixture}


def design_from_filter(f):
    """Inverse of build_filter (graded layers are reported, not rebuilt)."""
    def name(material):
        return material.get_name() if hasattr(material, "get_name") else str(material)

    design = {
        "substrate": name(f.get_substrate()),
        "substrate_thickness_mm": f.get_substrate_thickness() / 1e6,
        "incident_medium": name(f.get_medium(FRONT)),
        "exit_medium": name(f.get_medium(BACK)),
        "consider_backside": bool(f.get_consider_backside()),
        "center_wavelength": f.get_center_wavelength(),
    }
    if f.get_description():
        design["description"] = f.get_description()
    if f.get_needle_materials():
        design["needle_materials"] = list(f.get_needle_materials())
    if f.get_step_spacing() != _DEFAULT_STEP_SPACING:
        design["step_spacing"] = f.get_step_spacing()
    wavelength_range = tuple(float(x) for x in f.get_wavelengths_by_range())
    if wavelength_range[2] > 0 and wavelength_range != _DEFAULT_WAVELENGTH_RANGE:
        design["wavelength_range"] = dict(zip(("from", "to", "step"), wavelength_range))
    for side, key in ((FRONT, "layers"), (BACK, "back_layers")):
        layers = []
        for i in range(f.get_nb_layers(side)):
            layer = {"material": f.get_layer_material_name(i, side)}
            # Full precision, so a design round-trips to the same spectra.
            layer["thickness"] = float(f.get_layer_thickness(i, side))
            mixture = _material(layer["material"]).is_mixture()
            if f.is_graded(i, side):
                step_thicknesses, steps = f.get_layer_step_profile(i, side)
                layer["graded"] = {"step_thicknesses": [float(t) for t in step_thicknesses],
                                   "steps": [int(x) for x in steps],
                                   "indices_at_cw": [float(n) for n in f.get_layer_index(i, side)]}
            elif mixture:
                layer["index"] = float(f.get_layer_index(i, side))
            # Only flags that differ from what a new layer gets, to keep designs short.
            defaults = _default_layer_flags(side, mixture)
            for flag, getter in _LAYER_FLAGS:
                value = bool(getattr(f, "get_" + getter)(i, side))
                if value != defaults[flag]:
                    layer[flag] = value
            layers.append(layer)
        if layers or side == FRONT:
            design[key] = layers
    return design


def describe(design):
    f = build_filter(design)
    cw = f.get_center_wavelength()

    def side_rows(side):
        rows = []
        for i in range(f.get_nb_layers(side)):
            thickness = float(f.get_layer_thickness(i, side))
            material = f.get_layer_material_name(i, side)
            row = {"#": i + 1, "material": material, "thickness_nm": round(thickness, 3)}
            if not f.is_graded(i, side):
                n = float(f.get_layer_index(i, side)) if _material(material).is_mixture()                     else _material(material).get_index(cw)
                row["n@cw"] = round(n, 4)
                row["qwot@cw"] = round(4 * n * thickness / cw, 4)
            rows.append(row)
        return rows

    rows, back = side_rows(FRONT), side_rows(BACK)
    out = {"layers": rows, "nb_layers": len(rows), "total_thickness_nm": round(float(f.get_total_thickness(FRONT)), 3),
           "center_wavelength": cw, "materials": sorted({r["material"] for r in rows + back}),
           "consider_backside": bool(f.get_consider_backside())}
    if back:
        out.update(back_layers=back, nb_back_layers=len(back),
                   back_total_thickness_nm=round(float(f.get_total_thickness(BACK)), 3))
        if not f.get_consider_backside():
            out["warning"] = ("back_layers are defined but consider_backside is false, so calculations ignore "
                              "the back face; set consider_backside: true to include it")
    out["design"] = design_from_filter(f)
    return out


# ---------------------------------------------------------------------------
# Spectra
# ---------------------------------------------------------------------------

QUANTITIES = {
    "R": "reflection", "T": "transmission", "A": "absorption",
    "R_phase": "reflection_phase", "T_phase": "transmission_phase",
    "R_GD": "reflection_GD", "T_GD": "transmission_GD",
    "R_GDD": "reflection_GDD", "T_GDD": "transmission_GDD",
    "OD": "transmission",  # optical density, -log10(T)
}
REVERSIBLE = {"R", "T", "A", "OD"}


def spectra(design, quantities=("R", "T"), wavelengths=None, angles=(0.0,), polarizations=("unpolarized",),
            reverse=False):
    """Return {"wavelengths": [...], "series": [{"quantity","angle","polarization","values"}]}."""
    wvls = _wavelength_list(wavelengths)
    f = build_filter(design, wvls)
    series = []
    for q in quantities:
        if q not in QUANTITIES:
            raise EngineError(f"unknown quantity {q}; choose from {', '.join(QUANTITIES)}")
        if reverse and q not in REVERSIBLE:
            raise EngineError(f"{q} cannot be computed in reverse direction")
        method = getattr(f, QUANTITIES[q] + ("_reverse" if reverse else ""))
        for angle in angles:
            if not 0 <= float(angle) < 90:
                raise EngineError("angles must be in [0, 90)")
            for pol in polarizations:
                values = method(float(angle), polarization_value(pol))
                values = np.array([values[i] for i in range(len(wvls))], dtype=float)
                if q == "OD":
                    values = -np.log10(np.maximum(values, 1e-300))
                series.append({"quantity": q, "angle": float(angle), "polarization": polarization_name(polarization_value(pol)),
                               "values": values})
    return {"wavelengths": np.array(wvls), "series": series}


def color(design, angles=(0.0,), polarization="unpolarized", illuminant="CIE-D65", observer="CIE-1931",
          mode="reflection"):
    if mode not in ("reflection", "transmission"):
        raise EngineError("mode must be reflection or transmission")
    if illuminant not in of_color.get_illuminant_names():
        raise EngineError(f"illuminant must be one of {', '.join(of_color.get_illuminant_names())}")
    if observer not in of_color.get_observer_names():
        raise EngineError(f"observer must be one of {', '.join(of_color.get_observer_names())}")
    f = build_filter(design)
    out = []
    for angle in angles:
        R_color, T_color = f.color(float(angle), polarization_value(polarization), illuminant, observer)
        c = R_color if mode == "reflection" else T_color
        X, Y, Z = c.XYZ()
        x, y, _ = c.xyY()
        L, a, b = c.Lab()
        _, C, h = c.LChab()
        R, G, B = c.RGB()
        out.append({"angle": float(angle), "XYZ": [X, Y, Z], "xy": [x, y], "Lab": [L, a, b], "LCh": [L, C, h],
                    "sRGB": [R, G, B],
                    "hex": "#%02x%02x%02x" % tuple(int(round(max(0.0, min(1.0, v)) * 255)) for v in (R, G, B)),
                    "out_of_gamut": bool(c.get_RGB_error())})
    return {"illuminant": illuminant, "observer": observer, "mode": mode,
            "polarization": polarization_name(polarization_value(polarization)), "colors": out}


def electric_field(design, wavelength, angle=0.0, polarization="s"):
    """|E|^2 through the front stack, relative to the incident wave
    (tangential field, Macleod's convention)."""
    pol = polarization_value(polarization)
    if pol not in (S, P):
        raise EngineError("electric field needs s or p polarization")
    wavelength, angle = float(wavelength), float(angle)
    design = dict(design, consider_backside=False)
    f = build_filter(design, [wavelength])
    positions, fields = f.electric_field(wavelength, angle, pol)
    # OpenFilters returns |E| relative to the field at the substrate. With
    # E_substrate = 1, Macleod gives |E_incident|^2 = Re(eta_s) / (eta_0 T).
    T = f.transmission(angle, pol)[0]
    if T <= 0:
        raise EngineError("electric field normalization needs a non-zero transmission")
    n0 = material_N(design.get("incident_medium", "void"), [wavelength])[0]
    ns = material_N(design.get("substrate", "FusedSilica"), [wavelength])[0]
    sin0 = n0 * math.sin(math.radians(angle))
    eta = lambda N: (N * np.sqrt(1 - (sin0 / N) ** 2 + 0j)) if pol == S else (N / np.sqrt(1 - (sin0 / N) ** 2 + 0j))
    scale = (eta(n0).real * T) / eta(ns).real
    depth, value, boundaries = [], [], []
    for layer_positions, layer_field in zip(positions, fields):
        depth += [float(x) for x in layer_positions]
        value += [float(v) ** 2 * scale for v in layer_field]
        boundaries.append(float(layer_positions[-1]))
    return {"position_nm": depth, "E_squared": value, "layer_boundaries_nm": boundaries,
            "note": "position from the substrate interface outward; |E|^2 relative to the incident wave"}


def monitoring(design, wavelengths, angle=0.0, polarization="unpolarized", mode="reflection"):
    """Monitoring signal versus deposited thickness at each monitoring
    wavelength (as used for optical monitoring during deposition)."""
    f = build_filter(design)
    wvls = [float(w) for w in wavelengths]
    if mode == "reflection":
        thickness, signal = f.reflection_monitoring(wvls, float(angle), polarization_value(polarization))
    elif mode == "transmission":
        thickness, signal = f.transmission_monitoring(wvls, float(angle), polarization_value(polarization))
    else:
        raise EngineError("mode must be reflection or transmission")
    # thickness[layer][point]; signal[wavelength][layer][point].
    deposited = [float(x) for layer_thickness in thickness for x in layer_thickness]
    boundaries = [float(layer_thickness[-1]) for layer_thickness in thickness]
    curves = {f"{w:g}": [float(v) for layer_signal in signal[i] for v in layer_signal]
              for i, w in enumerate(wvls)}
    return {"deposited_thickness_nm": deposited, "signal": curves, "layer_boundaries_nm": boundaries,
            "includes_backside": bool(f.get_consider_backside_on_monitoring())}


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

TARGET_KINDS = {
    # short name: (discrete kind, spectrum kind)
    "R": ("Reflection", "ReflectionSpectrum"), "T": ("Transmission", "TransmissionSpectrum"),
    "A": ("Absorption", "AbsorptionSpectrum"),
    "R_phase": ("ReflectionPhase", "ReflectionPhaseSpectrum"), "T_phase": ("TransmissionPhase", "TransmissionPhaseSpectrum"),
    "R_GD": ("ReflectionGD", "ReflectionGDSpectrum"), "T_GD": ("TransmissionGD", "TransmissionGDSpectrum"),
    "R_GDD": ("ReflectionGDD", "ReflectionGDDSpectrum"), "T_GDD": ("TransmissionGDD", "TransmissionGDDSpectrum"),
    "R_color": ("ReflectionColor", None), "T_color": ("TransmissionColor", None),
}
INEQUALITIES = {"equal": None, "=": None, "==": None, "smaller": "smaller", "<": "smaller", "<=": "smaller",
                "max": "smaller", "larger": "larger", ">": "larger", ">=": "larger", "min": "larger"}


def target_lines(spec):
    """Translate a target dict into OpenFilters' text format."""
    kind = spec.get("kind")
    if kind not in TARGET_KINDS:
        raise EngineError(f"target kind must be one of {', '.join(TARGET_KINDS)}")
    discrete, spectral = TARGET_KINDS[kind]
    angle = float(spec.get("angle", 0.0))
    pol = polarization_value(spec.get("polarization", "unpolarized"))
    if kind not in ("R", "T", "A", "R_color", "T_color") and pol not in (S, P):
        raise EngineError(f"{kind} targets need s or p polarization")
    inequality = INEQUALITIES.get(str(spec.get("inequality", "equal")).lower(), "bad")
    if inequality == "bad":
        raise EngineError("inequality must be equal, smaller (<=) or larger (>=)")
    photometric = kind in ("R", "T", "A")

    def check_value(v):
        if photometric and not -1e-9 <= v <= 1 + 1e-9:
            raise EngineError(f"{kind} target values are fractions between 0 and 1 (got {v})")
        return v

    lines = []
    if kind in ("R_color", "T_color"):
        values, tolerances = spec.get("values"), spec.get("tolerances", [1.0, 1.0, 1.0])
        if not values or len(values) != 3:
            raise EngineError("color targets need values [3 numbers] in color_space (default Lab)")
        lines += [f"Kind: {discrete}", f"Angle: {angle}", f"Polarization: {pol}",
                  f"Illuminant: {spec.get('illuminant', 'CIE-D65')}", f"Observer: {spec.get('observer', 'CIE-1931')}",
                  f"{spec.get('color_space', 'L*a*b*')}: {values[0]} {values[1]} {values[2]}",
                  f"Tolerances: {tolerances[0]} {tolerances[1]} {tolerances[2]}"]
    elif "wavelength" in spec:
        lines += [f"Kind: {discrete}", f"Angle: {angle}", f"Polarization: {pol}",
                  f"Wavelength: {float(spec['wavelength'])}", f"Value: {check_value(float(spec['value']))}",
                  f"Tolerance: {float(spec.get('tolerance', 0.01))}"]
    elif "from" in spec and "to" in spec:
        start, stop = float(spec["from"]), float(spec["to"])
        step = float(spec.get("step", max((stop - start) / 100.0, 0.1)))
        tol = float(spec.get("tolerance", 0.01))
        points = spec.get("points")
        if points is None:
            if "value" not in spec:
                raise EngineError("spectrum targets need value (constant) or points [[wavelength, value, tolerance], ...]")
            value = check_value(float(spec["value"]))
            points = [[start, value, tol], [stop, value, tol]]
        lines += [f"Kind: {spectral}", f"Angle: {angle}", f"Polarization: {pol}",
                  f"From: {start}", f"To: {stop}", f"By: {step}", "Points:"]
        for point in points:
            w, v = float(point[0]), check_value(float(point[1]))
            t = float(point[2]) if len(point) > 2 else tol
            lines.append(f"\t{w} {v} {t}")
        lines.append("End")
    else:
        raise EngineError("target needs wavelength (single point) or from/to (spectral band)")
    if spec.get("direction", "normal").lower() in ("reverse", "back"):
        lines.insert(3, "Direction: Reverse")
    if inequality:
        lines.append(f"Inequality: {inequality}")
    return lines


def build_targets(specs):
    if not specs:
        raise EngineError("at least one target is needed")
    file_version = of_version.version("1.1")
    out = []
    for i, spec in enumerate(specs):
        try:
            out.append(of_targets.parse_target([l + "\n" for l in target_lines(spec)], file_version))
        except of_targets.target_error as error:
            raise EngineError(f"target {i}: {error}") from error
    return out


# ---------------------------------------------------------------------------
# Merit evaluation and optimization
# ---------------------------------------------------------------------------

def _evaluator(f, targets):
    """A refinement object prepared on a clone; used only to evaluate."""
    f = f.clone()
    if f.get_nb_layers(FRONT) == 0:
        # OpenFilters' merit code needs a front layer; a zero-thickness layer
        # is optically invisible (identity characteristic matrix).
        f.add_layer(f.get_substrate(), 0.0)
    optimizer = optimization_refinement(f, targets)
    optimizer.calculate_values()
    return optimizer


def target_report(optimizer, specs):
    """Per-target statistics from an evaluated optimizer."""
    calc = np.array(optimizer.all_calculated_values, dtype=float)
    goal = np.array(optimizer.all_target_values, dtype=float)
    tol = np.array(optimizer.all_tolerances, dtype=float)
    ineq = np.array(optimizer.all_inequalities)
    deviation = goal - calc
    # Satisfied inequalities do not contribute (as in OpenFilters).
    slack = 1e-9 * np.maximum(np.abs(goal), 1.0)
    ok_ineq = ((ineq < 0) & (calc <= goal + slack)) | ((ineq > 0) & (calc >= goal - slack))
    deviation[ok_ineq] = 0.0
    # An inequality ("R <= 0.2 %") is met only when it holds: its tolerance only weights the
    # merit. An equality is met within its tolerance. Non-finite values never pass.
    passed = np.where(ineq != 0, ok_ineq, np.abs(deviation) <= tol * (1 + 1e-9)) & np.isfinite(calc)
    rows = []
    starts = list(optimizer.target_starting_position) + [len(calc)]
    for i, spec in enumerate(specs):
        sl = slice(starts[i], starts[i + 1])
        d = deviation[sl]
        rows.append({"target": i, "kind": spec.get("kind"), "points": int(d.size),
                     "pass_fraction": round(float(np.mean(passed[sl])), 4),
                     "max_abs_deviation": round(float(np.max(np.abs(d))), 6),
                     "chi2_contribution": round(float(np.sum((d / tol[sl]) ** 2)), 6)})
    return rows, bool(np.all(passed))


def evaluate(design, target_specs):
    f = build_filter(design)
    optimizer = _evaluator(f, build_targets(target_specs))
    rows, all_pass = target_report(optimizer, target_specs)
    return {"merit_chi2": float(optimizer.get_chi_2()), "all_targets_met": all_pass, "targets": rows,
            "nb_layers": f.get_nb_layers(), "total_thickness_nm": round(float(f.get_total_thickness()), 3)}


METHODS = {"refine": optimization_refinement, "needle": optimization_needles, "step": optimization_steps}


class _Monitor:
    """Implements OpenFilters' parent.update() hook: enforces limits, detects
    add/remove cycles, remembers the best design seen, reports progress."""

    def __init__(self, optimizer, deadline, max_layers, max_total_thickness, progress):
        self.optimizer, self.progress = optimizer, progress
        self.deadline = deadline
        self.max_layers, self.max_total_thickness = max_layers, max_total_thickness
        self.stop_reason, self.last_report = None, 0.0
        self.best_chi2, self.best_layers = float("inf"), None
        self.states, self.last_count = {}, None

    def update(self, working, status):
        o = self.optimizer
        count = o.nb_front_layers
        thickness = sum(_flat(o.front_thickness))
        chi2 = o.get_chi_2()
        if not (math.isfinite(thickness) and math.isfinite(chi2)):
            # OpenFilters' Levenberg-Marquardt step can turn every thickness into NaN
            # (seen with layers pinned at 0 nm); stop and keep the best design so far.
            self.stop_reason = "numerical_error"
            o.stop()
            return
        within_limits = (not self.max_layers or count <= self.max_layers) and \
                        (not self.max_total_thickness or thickness <= self.max_total_thickness)
        if within_limits and chi2 and chi2 < self.best_chi2 * (1 - 1e-12):
            snapshot = _snapshot(o)
            if snapshot is not None:
                self.best_chi2, self.best_layers = chi2, snapshot
        if count != self.last_count:
            # The needle/step loops can alternate between adding and removing
            # the same layers forever; three visits to a state means a cycle.
            self.last_count = count
            key = (count, round(chi2, 6))
            self.states[key] = self.states.get(key, 0) + 1
            if self.states[key] >= 3:
                self.stop_reason = "cycling"
        if self.deadline and time.time() > self.deadline:
            self.stop_reason = "time_limit"
        elif self.max_layers and count > self.max_layers:
            self.stop_reason = "max_layers"
        elif self.max_total_thickness and thickness > self.max_total_thickness:
            self.stop_reason = "max_total_thickness"
        if self.stop_reason:
            o.stop()
        if self.progress and time.time() - self.last_report > 2.0:
            self.last_report = time.time()
            self.progress({"iteration": o.get_iteration(), "chi2": chi2, "nb_layers": count})


def _snapshot(o):
    """Front layers of an optimizer's current state as design layers (None
    for graded layers, which are not tracked)."""
    layers = []
    for i in range(o.nb_front_layers):
        thickness = o.front_thickness[i]
        if isinstance(thickness, list):
            return None
        material = o.materials[o.front_layers[i]]
        layer = {"material": material.get_name(), "thickness": float(thickness)}
        if material.is_mixture():
            layer["index"] = float(o.front_index[i])
        layers.append(layer)
    return layers


def _flat(thicknesses):
    for t in thicknesses:
        if isinstance(t, list):
            yield from t
        else:
            yield t


def _thin_layers(layers, min_thickness):
    return [i for i, l in enumerate(layers) if not l.get("graded") and l["thickness"] < min_thickness]


def _remove_layer(layers, i, center_wavelength):
    """Remove layer i, giving its optical thickness to its neighbours
    (OpenFilters' rule), then merge identical neighbours."""
    layers = [dict(l) for l in layers]

    def n(layer):
        return layer["index"] if "index" in layer else _material(layer["material"]).get_index(center_wavelength)

    ot = layers[i]["thickness"] * n(layers[i])
    neighbours = [k for k in (i - 1, i + 1) if 0 <= k < len(layers) and not layers[k].get("graded")]
    for k in neighbours:
        layers[k]["thickness"] += ot / len(neighbours) / n(layers[k])
    del layers[i]
    merged = []
    for layer in layers:
        prev = merged[-1] if merged else None
        if prev and not prev.get("graded") and not layer.get("graded") and prev["material"] == layer["material"]                 and abs(prev.get("index", 0.0) - layer.get("index", 0.0)) < 1e-9:
            prev["thickness"] += layer["thickness"]
        else:
            merged.append(layer)
    return merged


def _enforce_min_thickness(design, targets, options, deadline, progress, floor, center_wavelength):
    """Make every layer at least `floor` thick: clamp thin layers to the
    floor and refine with the floor as a bound; then try removing layers
    stuck at the floor, keeping a removal when the merit does not get worse
    (fewer layers are preferred). Returns (design, chi2, removed, iterations)."""
    def clamp(layers):
        return [dict(l, thickness=max(l["thickness"], floor)) if not l.get("graded") else l for l in layers]

    def refine(layers):
        d, chi2, _, it = _run("refine", dict(design, layers=clamp(layers)), targets, options, deadline, progress, floor)
        return d, chi2, it

    current, chi2, iterations = refine(design["layers"])
    removed = 0
    while time.time() < deadline and len(current["layers"]) > 1:
        stuck = [i for i, l in enumerate(current["layers"]) if not l.get("graded") and l["thickness"] <= floor * (1 + 1e-6)]
        best = None
        for i in stuck:
            candidate, c_chi2, it = refine(_remove_layer(current["layers"], i, center_wavelength))
            iterations += it
            if best is None or c_chi2 < best[1]:
                best = (candidate, c_chi2)
        if best is None or best[1] > chi2 * (1 + 1e-3) + 1e-12:
            break
        current, chi2 = best
        removed += 1
    return current, chi2, removed, iterations


def _finite(design):
    return all(math.isfinite(t) for l in design["layers"]
               for t in (l["graded"]["step_thicknesses"] if l.get("graded") else [l["thickness"]]))


def _edge_needle(o):
    """Where OpenFilters' needle method stopped for lack of needles: the most
    negative merit derivative at a layer edge, as (insert position in the
    front layers, material name), or None.

    OpenFilters only inserts needles at minima strictly inside a layer, so an
    improvement at an interface or at the outer surface (dMF still falling at
    the edge of the layer) ends the needle method although the merit would
    improve. Uses the dMF profile of its last needle search."""
    if getattr(o, "can_add_needles", True) or not getattr(o, "dMF", None):
        return None
    best = None
    for i_material, material_nb in enumerate(o.needle_material_nbs):
        for i_parameter, (kind, layer_nb) in enumerate(o.parameters):
            values = o.dMF[i_material][i_parameter] if i_parameter < len(o.dMF[i_material]) else []
            if kind != THICKNESS or len(values) < 2:
                continue
            for value, position in ((values[0], layer_nb), (values[-1], layer_nb + 1)):
                neighbours = [o.front_layers[k] for k in (position - 1, position) if 0 <= k < o.nb_front_layers]
                if material_nb in neighbours:
                    continue  # same material as a neighbour: that is a thickness change, not a new layer
                if value < 0 and (best is None or value < best[0]):
                    best = (value, position, o.materials[material_nb].get_name())
    return best and best[1:]


def _run(method, design, targets, options, deadline, progress, floor=None, info=None):
    """One OpenFilters optimization. Returns (design, chi2, stop_reason,
    iterations); the design is the best one seen, not merely the last, and
    never one with non-finite thicknesses.
    floor (refine only): lower bound on every layer thickness.
    info (dict, optional): receives "edge_needle" (see _edge_needle) when the
    returned design is the needle method's final state."""
    f = build_filter(design)
    if f.get_nb_layers(FRONT) == 0:
        raise EngineError("optimization needs at least one front layer to start from (e.g. a thin layer of one "
                          "of the coating materials); back layers are never optimized")
    o = METHODS[method](f, targets)
    if floor:
        # OpenFilters bounds thicknesses at 0; raise the bound to the floor.
        for i, (kind, _) in enumerate(o.parameters):
            if kind == THICKNESS:
                o.parameter_min[i] = max(o.parameter_min[i], floor)
        o.optimizer.set_limits(o.parameter_min, o.parameter_max)
    if options.get("max_iterations"):
        o.max_iterations = o.initial_max_iterations = int(options["max_iterations"])
    if method == "needle":
        if options.get("needle_materials"):
            o.set_needle_materials(options["needle_materials"])
        if options.get("nb_needles"):
            o.set_nb_needles(int(options["nb_needles"]))
    if method == "step" and options.get("nb_steps"):
        o.set_nb_steps(int(options["nb_steps"]))
    if method in ("needle", "step"):
        o.set_automatic_mode(bool(options.get("automatic", True)))
    monitor = _Monitor(o, deadline, options.get("max_layers"), options.get("max_total_thickness"), progress)
    o.set_parent(monitor)
    o.go()
    o.copy_to_filter()
    final_design = design_from_filter(f)
    finite = _finite(final_design)
    final_chi2 = _evaluator(f, targets).get_chi_2() if finite else float("inf")
    limits_ok = finite and (not options.get("max_layers") or f.get_nb_layers() <= options["max_layers"]) and \
        (not options.get("max_total_thickness") or f.get_total_thickness() <= options["max_total_thickness"])
    best_design, best_chi2 = (final_design, final_chi2) if limits_ok else (None, float("inf"))
    if monitor.best_layers is not None:
        candidate = dict(final_design, layers=monitor.best_layers)
        if _finite(candidate):
            chi2 = _evaluator(build_filter(candidate), targets).get_chi_2()
            if chi2 < best_chi2:
                best_design, best_chi2 = candidate, chi2
    if best_design is None:
        if finite:  # never within the limits: report the last design
            best_design, best_chi2 = final_design, final_chi2
        else:
            best_design, best_chi2 = design, _evaluator(build_filter(design), targets).get_chi_2()
    stop = monitor.stop_reason or ("converged" if o.get_stop_criteria_met() else "finished")
    if info is not None and method == "needle" and best_design is final_design and not monitor.stop_reason:
        info["edge_needle"] = _edge_needle(o)
    return best_design, float(best_chi2), stop, o.get_iteration()


EDGE_NEEDLE_NM = 1.0
MAX_CONTINUATIONS = 20


def _drop_empty_layers(layers):
    """Remove layers thinner than 1 pm (left at OpenFilters' 0 nm bound) and merge
    the identical neighbours they separated."""
    merged = []
    for layer in layers:
        if not layer.get("graded") and layer["thickness"] < 1e-3:
            continue
        prev = merged[-1] if merged else None
        if prev and not prev.get("graded") and not layer.get("graded") and prev["material"] == layer["material"] \
                and abs(prev.get("index", 0.0) - layer.get("index", 0.0)) < 1e-9:
            prev["thickness"] += layer["thickness"]
        else:
            merged.append(dict(layer))
    return merged


def _continue_needles(current, chi2, stop, info, targets, options, deadline, progress):
    """Carry on where OpenFilters' needle method stops early: restart once after a
    numerical failure (without the 0 nm layers that trigger it), and insert a thin
    layer at the best layer edge while that improves the merit (see _edge_needle).
    Returns (design, chi2, stop, iterations, edge needles inserted)."""
    iterations = inserted = 0
    restarted = False
    max_layers = options.get("max_layers")
    for _ in range(MAX_CONTINUATIONS):
        if time.time() >= deadline or chi2 <= 0.0:
            break
        if stop == "numerical_error" and not restarted:
            restarted = True
            layers = _drop_empty_layers(current["layers"])
        elif stop in ("converged", "finished") and info.get("edge_needle"):
            position, material = info["edge_needle"]
            if max_layers and len(current["layers"]) + 1 > max_layers:
                break
            layers = [dict(l) for l in current["layers"]]
            layers.insert(position, {"material": material, "thickness": EDGE_NEEDLE_NM})
        else:
            break
        info = {}
        candidate, c_chi2, c_stop, its = _run("needle", dict(current, layers=layers), targets, options,
                                              deadline, progress, info=info)
        iterations += its
        if not c_chi2 < chi2 * (1 - 1e-4):
            break
        inserted += stop != "numerical_error"
        current, chi2, stop = candidate, c_chi2, c_stop
    return current, chi2, stop, iterations, inserted


def optimization_description(method, initial_chi2, chi2, all_pass, min_thickness, stop):
    """One line saying how a design was made, stored as the filter description in .ofp projects
    (the format has no fields for the optimizer settings)."""
    text = (f"Optimized with OpenFilters MCP {__version__} on {time.strftime('%Y-%m-%d')}: {method} method, "
            f"merit chi2 {initial_chi2:.6g} -> {chi2:.6g}, all targets met: {'yes' if all_pass else 'no'}")
    if min_thickness:
        text += (f", minimum layer thickness {min_thickness:g} nm enforced by the connector "
                 "(MinimumThickness left at 0: OpenFilters' needle and step methods cycle with it)")
    return text + f", stop: {stop}."


def optimize(design, target_specs, method="needle", options=None, progress=None):
    """Optimize a design and return the best design found.

    min_thickness is enforced after optimization: the thinnest offending
    layer is removed and the design re-refined, repeatedly; with the needle
    or step method, further rounds then try to improve the cleaned design.
    The result honours min_thickness unless stop_reason says otherwise."""
    options = dict(options or {})
    if method not in METHODS:
        raise EngineError(f"method must be one of {', '.join(METHODS)}")
    min_thickness = float(options.get("min_thickness") or 0.0)
    targets = build_targets(target_specs)
    start = time.time()
    deadline = start + float(options.get("time_limit", 300))
    design = design_from_filter(build_filter(design))
    cw = design["center_wavelength"]
    initial = _evaluator(build_filter(design), targets).get_chi_2()

    info = {}
    edge_needles = 0
    current, chi2, stop, iterations = _run(method, design, targets, options, deadline, progress, info=info)
    if method == "needle":
        current, chi2, stop, its, edge_needles = _continue_needles(
            current, chi2, stop, info, targets, options, deadline, progress)
        iterations += its
    removed, rounds = 0, 1
    if min_thickness:
        best = None
        for rounds in range(1, 6):
            clean, clean_chi2, n_removed, its = _enforce_min_thickness(
                current, targets, options, deadline, progress, min_thickness, cw)
            removed += n_removed
            iterations += its
            if best is None or clean_chi2 < best[1] * (1 - 1e-3):
                best = (clean, clean_chi2)
            else:
                break  # another round did not improve the clean design
            if method == "refine" or time.time() >= deadline:
                break
            current, chi2, stop, it = _run(method, best[0], targets, options, deadline, progress)
            iterations += it
        current, chi2 = best
        if _thin_layers(current["layers"], min_thickness * (1 - 1e-9)):
            stop = "min_thickness_not_reached"

    # 0 nm layers (left at OpenFilters' lower bound) are optically invisible: drop them.
    current = dict(current, layers=_drop_empty_layers(current["layers"]) or current["layers"])
    f = build_filter(current)
    final = _evaluator(f, targets)
    rows, all_pass = target_report(final, target_specs)
    if time.time() >= deadline and stop not in ("max_layers", "max_total_thickness", "min_thickness_not_reached"):
        stop = "time_limit"
    thicknesses = [l["thickness"] for l in current["layers"] if not l.get("graded")]
    current = dict(current, description=optimization_description(
        method, initial, final.get_chi_2(), all_pass, min_thickness, stop))
    return {"method": method, "initial_chi2": float(initial), "merit_chi2": float(final.get_chi_2()),
            "all_targets_met": all_pass, "targets": rows, "iterations": iterations, "stop_reason": stop,
            "nb_layers": f.get_nb_layers(), "total_thickness_nm": round(float(f.get_total_thickness()), 3),
            "thinnest_layer_nm": round(min(thicknesses), 3) if thicknesses else None,
            "thin_layers_removed": removed, "edge_needles_added": edge_needles,
            "rounds": rounds if min_thickness else 1,
            "elapsed_s": round(time.time() - start, 2), "design": current}


# ---------------------------------------------------------------------------
# Tolerance analysis
# ---------------------------------------------------------------------------

def tolerance(design, target_specs, n_trials=200, thickness_error=None, index_error=None, seed=0, keep_values=False):
    """Monte Carlo yield: perturb thicknesses (and optionally material
    indices) and count designs meeting all targets.

    thickness_error: {"type": "relative"|"absolute", "sigma": 0.01 (or nm),
                      "distribution": "normal"|"uniform", "systematic": 0.0}
    index_error: {"sigma": 0.005 (relative), "per_material": true}
    """
    thickness_error = dict(thickness_error or {"type": "relative", "sigma": 0.01})
    rng = np.random.default_rng(seed)
    base = build_filter(design)
    nominal = design_from_filter(base)
    targets = build_targets(target_specs)
    nominal_eval = _evaluator(base, targets)
    _, nominal_pass = target_report(nominal_eval, target_specs)

    # Both faces are perturbed (independently drawn errors per layer).
    sides = [key for key in ("layers", "back_layers") if nominal.get(key)]
    if any(l.get("graded") for key in sides for l in nominal[key]):
        raise EngineError("tolerance analysis does not support graded layers yet")
    materials_used = sorted({l["material"] for key in sides for l in nominal[key]})
    kind = thickness_error.get("type", "relative")
    sigma = float(thickness_error.get("sigma", 0.01))
    dist = thickness_error.get("distribution", "normal")
    systematic = float(thickness_error.get("systematic", 0.0))
    idx_sigma = float((index_error or {}).get("sigma", 0.0))

    def draw(size):
        if dist == "uniform":
            return rng.uniform(-sigma, sigma, size)
        return rng.normal(0.0, sigma, size)

    merits, passes, per_target_fail = [], 0, np.zeros(len(target_specs))
    for _ in range(int(n_trials)):
        trial = dict(nominal)
        for key in sides:
            d = np.array([l["thickness"] for l in nominal[key]])
            e = draw(len(d))
            if kind == "relative":
                d = d * (1 + e + systematic)
            else:
                d = d + e + systematic * d
            trial[key] = [dict(l, thickness=max(float(t), 0.0)) for l, t in zip(nominal[key], d)]
        f = build_filter(trial)
        if idx_sigma:
            _scale_indices(f, {m: 1 + rng.normal(0.0, idx_sigma) for m in materials_used})
        ev = _evaluator(f, targets)
        rows, ok = target_report(ev, target_specs)
        merits.append(ev.get_chi_2())
        passes += ok
        per_target_fail += [r["pass_fraction"] < 1 for r in rows]
    merits = np.array(merits)
    extra = {"chi2_values": merits.tolist(), "target_failures": per_target_fail.tolist()} if keep_values else {}
    return {**extra, "n_trials": int(n_trials), "passes": int(passes), "yield": passes / float(n_trials),
            "nominal_passes": nominal_pass,
            "nominal_chi2": float(nominal_eval.get_chi_2()),
            "chi2_median": float(np.median(merits)), "chi2_p90": float(np.percentile(merits, 90)),
            "target_failure_rate": [round(float(x) / n_trials, 4) for x in per_target_fail],
            "thickness_error": thickness_error, "index_error": index_error, "seed": seed}


def _scaled_material(material, factor):
    """A table copy of a regular material with its real index multiplied by
    factor (works for every dispersion model)."""
    wvls = list(np.geomspace(150.0, 30000.0, 1500))
    probe = optical_filter.optical_filter(catalog())
    probe.set_wavelengths(wvls)
    N = material.get_N(probe.wvls)
    scaled = of_materials.material_table()
    scaled.set_name(material.get_name())
    scaled.set_properties(wvls, [complex(N[i].real * factor, N[i].imag) for i in range(len(wvls))])
    return scaled


def _scale_indices(f, factors):
    for i, material in enumerate(f.get_materials()):
        factor = factors.get(material.get_name())
        if factor and not material.is_mixture():
            f.materials[i] = _scaled_material(material, factor)
    f.reset_n()


def sensitivity(design, target_specs, relative_step=0.01):
    """Merit change when each layer thickness is increased by relative_step."""
    nominal = design_from_filter(build_filter(design))
    targets = build_targets(target_specs)
    base = _evaluator(build_filter(nominal), targets).get_chi_2()
    rows = []
    for key, side in (("layers", "front"), ("back_layers", "back")):
        if key == "back_layers" and not nominal.get("consider_backside"):
            continue  # the back face does not enter the calculation
        for i, layer in enumerate(nominal.get(key) or []):
            if layer.get("graded"):
                continue
            layers = [dict(l) for l in nominal[key]]
            layers[i]["thickness"] = layer["thickness"] * (1 + relative_step)
            chi2 = _evaluator(build_filter(dict(nominal, **{key: layers})), targets).get_chi_2()
            rows.append({"side": side, "#": i + 1, "material": layer["material"], "thickness_nm": layer["thickness"],
                         "delta_chi2": round(float(chi2 - base), 6)})
    return {"base_chi2": float(base), "relative_step": relative_step, "layers": rows}


# ---------------------------------------------------------------------------
# Projects (.ofp)
# ---------------------------------------------------------------------------

def load_project(path):
    try:
        p = of_project.read_project(path, catalog())
    except Exception as error:
        raise EngineError(f"Cannot read project: {error}") from error
    designs = [design_from_filter(f) for f in p.get_filters()]
    targets = []
    for t in p.get_targets():
        targets.append(_target_to_spec(t))
    return {"path": os.path.abspath(path), "comment": p.get_comment(), "designs": designs, "targets": targets}


def _target_to_spec(t):
    kind_name = of_targets.KIND_NAMES[t.get_kind()]
    short = next(k for k, (d, s) in TARGET_KINDS.items() if kind_name in (d, s))
    spec = {"kind": short, "angle": t.get_angle(), "polarization": polarization_name(t.get_polarization())}
    inequality = t.get_inequality()
    spec["inequality"] = {-1: "smaller", 1: "larger"}.get(inequality, "equal")
    if t.get_kind() in of_targets.DISCRETE_TARGETS:
        w, v, d = t.get_values()
        spec.update(wavelength=w[0], value=v[0], tolerance=d[0])
    elif t.get_kind() in of_targets.SPECTRUM_TARGETS:
        start, stop, step, w, v, d = t.get_points()
        spec.update({"from": start, "to": stop, "step": step, "points": [[a, b, c] for a, b, c in zip(w, v, d)]})
    else:
        illuminant, observer = t.get_illuminant_and_observer()
        values, tolerances = t.get_values()
        spec.update(illuminant=illuminant, observer=observer,
                    color_space=of_targets.COLOR_SPACE_NAMES[t.get_color_space()],
                    values=list(values), tolerances=list(tolerances))
    return spec


def target_wavelength_range(target_specs):
    """A wavelength range for the GUI's analysis that covers every target, with a margin,
    or None without targets."""
    wavelengths = []
    for spec in target_specs or []:
        if str(spec.get("kind", "")).endswith("_color"):
            wavelengths += [380.0, 780.0]
        if "wavelength" in spec:
            wavelengths.append(float(spec["wavelength"]))
        wavelengths += [float(spec[key]) for key in ("from", "to") if key in spec]
        wavelengths += [float(point[0]) for point in spec.get("points") or []]
    if not wavelengths:
        return None
    low, high = min(wavelengths), max(wavelengths)
    margin = max(0.1 * (high - low), 50.0)
    low, high = max(math.floor((low - margin) / 10) * 10, 10.0), math.ceil((high + margin) / 10) * 10
    # About 2000 points at most, as in the GUI's own default range.
    step = next(s for s in (0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0) if (high - low) / s <= 2000 or s == 50.0)
    return {"from": float(low), "to": float(high), "step": step}


def save_project(path, designs, target_specs=None, comment=""):
    """Write an .ofp project. Designs without a wavelength_range get one covering the targets,
    so the GUI analyses the region that matters."""
    p = of_project.project(catalog())
    if comment:
        p.set_comment(comment)
    wavelength_range = target_wavelength_range(target_specs)
    for design in designs:
        if wavelength_range and not design.get("wavelength_range"):
            design = dict(design, wavelength_range=wavelength_range)
        p.add_filter(build_filter(design))
    for t in build_targets(target_specs) if target_specs else []:
        p.add_target(t)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    of_project.write_project(p, path)
    return os.path.abspath(path)


def acceleration_status():
    from openfilters_core import abeles, moremath
    return {"cpp_core_abeles": abeles.get_abeles_dll_import_success(),
            "cpp_core_moremath": moremath.get_moremath_dll_import_success()}
