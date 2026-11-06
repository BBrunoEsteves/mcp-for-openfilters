"""Bridge between JSON-friendly descriptions and the OpenFilters core.

Everything here takes and returns plain dicts/lists/floats so it can run in
worker processes and be serialized to disk. Conventions:

* wavelengths and thicknesses in nm, angles in degrees;
* R, T, A as fractions (0-1); phase in degrees, GD in fs, GDD in fs^2;
* complex indices follow OpenFilters/Macleod: N = n - ik with k >= 0;
* layers are listed from the substrate outward (layer 0 touches the
  substrate), as in OpenFilters.
"""

import json
import math
import os
import random
import re
import threading
import time

import numpy as np

from openfilters_core import color as of_color
from openfilters_core import materials as of_materials
from openfilters_core import data_holder as of_data_holder
from openfilters_core import optical_filter
from openfilters_core import preproduction as of_preproduction
from openfilters_core import project as of_project
from openfilters_core import stack as of_stack
from openfilters_core import targets as of_targets
from openfilters_core import version as of_version
from mcp_for_openfilters import __version__
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


# What a tabulated material gives beyond its data (design key "table_extrapolation"):
#   hold        n,k held at the end values (the port's default, see openfilters_core/PROVENANCE.md)
#   openfilters OpenFilters' own extrapolation of the interpolation's end segments (as in its GUI)
#   refuse      any calculation outside the data is refused
TABLE_EXTRAPOLATION = ("hold", "openfilters", "refuse")


def extrapolation_mode(design):
    mode = design.get("table_extrapolation", "hold") if isinstance(design, dict) else "hold"
    if mode not in TABLE_EXTRAPOLATION:
        raise EngineError(f"table_extrapolation must be one of {', '.join(TABLE_EXTRAPOLATION)} (\"hold\": n,k held "
                          "at the end of the data; \"openfilters\": OpenFilters' own extrapolation; \"refuse\")")
    return mode


def _openfilters_table(material):
    """A copy of a table material with OpenFilters' original dispersion object: the table without the
    port's end padding, so PCHIP extrapolates its end segments as in the OpenFilters GUI."""
    wvls, N = material.get_properties()
    copy = of_materials.material_table()
    copy.name = material.get_name()
    copy.set_properties(list(wvls), list(N))
    copy.dispersion = of_materials.abeles.table(len(wvls))
    for i in range(len(wvls)):
        copy.dispersion.set_table(i, wvls[i], N[i])
    return copy


class _OpenFiltersCatalog:
    """The user's catalog with every table material extrapolated as OpenFilters does."""

    def __init__(self, base):
        self.base, self.copies = base, {}

    def get_material(self, name):
        material = self.base.get_material(name)
        if material.get_model() != of_materials.TABLE:
            return material
        if name not in self.copies:
            self.copies[name] = _openfilters_table(material)
        return self.copies[name]

    def __getattr__(self, attribute):
        return getattr(self.base, attribute)


_openfilters_catalog = None


def catalog_for(mode):
    """The catalog a calculation uses for a table_extrapolation mode."""
    global _openfilters_catalog
    base = catalog()
    if mode != "openfilters":
        return base
    if _openfilters_catalog is None or _openfilters_catalog.base is not base:
        _openfilters_catalog = _OpenFiltersCatalog(base)
    return _openfilters_catalog


def _material(name, mode="hold"):
    try:
        return catalog_for(mode).get_material(name)
    except of_materials.material_error as error:
        raise EngineError(f"Unknown material '{name}'. Use list_materials to see what is available, or "
                          "search_refractiveindex and import_refractiveindex_material to add it.") from error


def material_N(name, wavelengths, mode="hold"):
    """Complex index (n - ik) of a regular material at the wavelengths (mode: table_extrapolation)."""
    material = _material(name, mode)
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
            row["n,k"] = {f"{w:g}": [round(float(z.real), 4), round(float(-z.imag), 5) + 0.0]
                          for w, z in zip(wavelengths, N)}
            data = getattr(material, "wvls", None)
            if data is not None and len(data):  # tabulated: n,k held constant outside the data
                row["data_range_nm"] = [round(float(data[0]), 3), round(float(data[-1]), 3)]
                outside = [f"{w:g}" for w in wavelengths if not data[0] <= w <= data[-1]]
                if outside:
                    row["note"] = (f"n,k at {', '.join(outside)} nm are outside the data: held at the value at the "
                                   "nearest end of the data, not measured")
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
            "note": f"removed; the built-in '{name}' is used again" if still else "removed"}


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
    """s, p, unpolarized, or the polarization angle psi in degrees, as in OpenFilters' dialogs:
    R = sin^2(psi) Rs + cos^2(psi) Rp (0 = p, 90 = s, 45 = unpolarized). A number may come as text
    ("30", "30deg", "30°"), which is also how polarization_name reports it."""
    message = ("polarization must be s, p, unpolarized, or a polarization angle in degrees between 0 and 90 "
               "(0 = p, 90 = s, 45 = unpolarized)")
    if isinstance(polarization, bool):
        raise EngineError(message)
    if isinstance(polarization, (int, float)):
        value = float(polarization)
    else:
        key = str(polarization).strip().lower()
        table = {"s": S, "te": S, "p": P, "tm": P, "unpolarized": UNPOLARIZED, "u": UNPOLARIZED,
                 "avg": UNPOLARIZED, "average": UNPOLARIZED, "mean": UNPOLARIZED}
        if key in table:
            return table[key]
        try:
            value = float(re.sub(r"\s*(deg(rees?)?|°)$", "", key))
        except ValueError:
            raise EngineError(message) from None
    if not 0.0 <= value <= 90.0:  # OpenFilters' own range (targets.py refuses others)
        raise EngineError(message)
    return value


def polarization_name(value):
    return {S: "s", P: "p", UNPOLARIZED: "unpolarized"}.get(value, f"{value:g}deg")


DESIGN_KEYS = {"substrate", "incident_medium", "exit_medium", "center_wavelength", "consider_backside",
               "substrate_thickness_mm", "consider_backside_on_monitoring", "illuminant", "observer", "description",
               "needle_materials", "step_spacing", "wavelength_range", "layers", "back_layers", "formula",
               "back_formula", "name", "notes", "table_extrapolation"}
LAYER_KEYS = {"material", "thickness", "qwot", "index", "graded", "fixed", "refine_thickness", "refine_index", "preserve_ot",
              "add_needles", "add_steps"}


def _check_keys(obj, valid, what):
    """A misspelt key (e.g. substrate_thickness) would otherwise be ignored silently."""
    unknown = sorted(set(obj) - valid)
    if unknown:
        import difflib
        hints = [f"{k!r}" + (f" (did you mean {m[0]!r}?)" if (m := difflib.get_close_matches(k, valid, 1)) else "")
                 for k in unknown]
        raise EngineError(f"unknown {what} key{'s' if len(unknown) > 1 else ''} {', '.join(hints)}; "
                          f"valid keys: {', '.join(sorted(valid))}")


def build_filter(design, wavelengths=None, materials=None):
    """Create an OpenFilters filter from a design dict. materials: a catalog to use instead of the
    user's (e.g. with materials perturbed for a thermal calculation)."""
    if not isinstance(design, dict):
        raise EngineError("design must be an object")
    _check_keys(design, DESIGN_KEYS, "design")
    design, _ = expand_profiles(design)
    f = optical_filter.optical_filter(materials or catalog_for(extrapolation_mode(design)))
    if "table_extrapolation" in design:
        f.table_extrapolation = design["table_extrapolation"]  # kept by design_from_filter
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
            f.set_description(latin1_text(" ".join(str(design["description"]).split())))
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
                symbols, fixed = _formula_symbols(formula["materials"], formula_key, f.get_center_wavelength())
                f.set_stack_formula(formula["formula"], symbols, side)
                f.apply_stack_formula(side)
                for position in range(f.get_nb_layers(side)):
                    if f.get_layer_material_name(position, side) in fixed:
                        _fix_layer(f, position, side)
            for i, layer in enumerate(design.get(key) or []):
                _add_layer(f, layer, side, i)
    except of_materials.material_does_not_exist_error as error:
        raise EngineError(f"{str(error).rstrip('.')}. Use list_materials to see what is available, or search_refractiveindex and "
                          "import_refractiveindex_material to add it from the refractiveindex.info database.") from error
    except of_stack.stack_error as error:
        raise EngineError(f"{error} Stack formulas use letters for quarter waves, e.g. \"(HL)^8 H\" or "
                          "\"(0.5L H 0.5L)^5\".") from error
    except (optical_filter.filter_error, of_materials.material_error) as error:
        raise EngineError(str(error)) from error
    return f


def resolve_design_files(obj):
    """Replace every {"design_file": path} in a tool's arguments by the design stored in that JSON file
    (load_project and describe_design write long designs to files, so they need not pass through the
    conversation). A relative path is looked up in the outputs folder."""
    if isinstance(obj, dict):
        if set(obj) == {"design_file"}:
            path = str(obj["design_file"])
            if not os.path.isabs(path):
                path = os.path.join(settings.home(), "outputs", path)
            try:
                with open(path, encoding="utf-8") as infile:
                    design = json.load(infile)
            except (OSError, ValueError) as error:
                raise EngineError(f"cannot read design_file {path}: {error}") from error
            if not isinstance(design, dict):
                raise EngineError(f"design_file {path} does not hold a design object")
            return design
        return {key: resolve_design_files(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [resolve_design_files(value) for value in obj]
    return obj


def _has_profiles(design):
    return any(isinstance(layer, dict) and isinstance(layer.get("graded"), dict) and "profile" in layer["graded"]
               for key in ("layers", "back_layers") for layer in design.get(key) or [])


def expand_profiles(design):
    """The design with every graded {"profile": ...} layer expanded into steps and step thicknesses
    (graded.py), and the warnings of the expansion (ranges clipped, mismatch left)."""
    if not isinstance(design, dict) or not _has_profiles(design):
        return design, []
    from . import graded as of_graded
    cw = float(design.get("center_wavelength", 550.0))
    out, warnings = dict(design), []
    for key, medium_key in (("layers", "incident_medium"), ("back_layers", "exit_medium")):
        layers = list(design.get(key) or [])
        medium = design.get(medium_key, design.get("incident_medium", "void"))

        label = "layer" if key == "layers" else "back layer"

        def neighbour(j, wl, end, matched):
            """(name, n at wl) of what lies at index j (-1: substrate, len: medium); end: the side facing us.
            Another profile: refused when it is to be matched, else None (no unmatched-ripple hint)."""
            if j < 0:
                name = design.get("substrate", "FusedSilica")
                return name, float(material_N(name, [wl])[0].real)
            if j >= len(layers):
                return medium, float(material_N(medium, [wl])[0].real)
            other = layers[j]
            other = {"material": other[0]} if isinstance(other, (list, tuple)) else other
            name = other.get("material")
            graded = other.get("graded")
            if isinstance(graded, dict) and "profile" in graded:
                if not matched:
                    return None
                raise EngineError(f"{label} {j + 1} is a profile too, so the profile next to it cannot be matched to it: "
                                  "put both in one profile, or match only the sides facing other layers")
            if isinstance(graded, dict):
                steps, _ = graded_steps(name, graded, cw, f"graded layer {j + 1}")
                return f"{label} {j + 1} ({name})", mixture_steps(name, wl)[steps[end]]
            if other.get("index") is not None:
                return f"{label} {j + 1} ({name})", float(other["index"])
            return f"{label} {j + 1} ({name})", float(material_N(name, [wl])[0].real)

        for i, layer in enumerate(layers):
            if not (isinstance(layer, dict) and isinstance(layer.get("graded"), dict) and "profile" in layer["graded"]):
                continue
            where = f"graded layer {i + 1}" if key == "layers" else f"back graded layer {i + 1}"
            material = layer.get("material")
            wl = of_graded.wavelength(layer["graded"], cw)
            table = mixture_steps(material, wl)
            matching = layer["graded"].get("matching")
            profile = layer["graded"]["profile"]
            # Neighbours of the sides to match; with no matching at all, of both sides for a rugate (ripple hint).
            # (Malformed matching or profile values are refused by graded.expand.)
            matched = {side for side in ("substrate", "medium")
                       if isinstance(matching, dict) and matching.get(side, "none") != "none"}
            hint = (set() if matching or not isinstance(profile, dict) or profile.get("type") != "rugate"
                    else {"substrate", "medium"})
            neighbours = {side: neighbour(i + offset, wl, end, side in matched) if side in matched | hint else None
                          for side, offset, end in (("substrate", -1, -1), ("medium", 1, 0))}
            steps, thicknesses, notes = of_graded.expand(material, layer["graded"], table, wl, neighbours, where)
            layers[i] = {k: v for k, v in layer.items() if k != "thickness"} | {
                "graded": {"steps": steps, "step_thicknesses": thicknesses}}
            warnings += notes
        if design.get(key):
            out[key] = layers
    return out, warnings


def _check_mixture_index(name, index, center_wavelength, where):
    """OpenFilters cannot evaluate a mixture beyond its data: the compiled core crashed the calculation
    process and the pure-Python one raised "Extrapolation not allowed". Refuse it here, clearly."""
    material = _material(name)
    if not material.is_mixture():
        return
    low, high = sorted(material.get_index_range(center_wavelength))
    if not low - 1e-9 <= index <= high + 1e-9:
        raise EngineError(f"{where}: index {index:g} is outside the range of the mixture {name} at the center "
                          f"wavelength {center_wavelength:g} nm [{low:.4f}, {high:.4f}]. Use an index inside it or "
                          "another material (list_materials gives each mixture's range).")


GRADED_KEYS = {"steps", "step_thicknesses", "indices_at_cw"}


def mixture_steps(name, wavelength):
    """The index of each composition step k of a mixture at a wavelength (k = 0, 1, ...)."""
    material = _material(name)
    if not material.is_mixture():
        raise EngineError(f"{name} is not a mixture: graded layers need a mixture material (list_materials shows "
                          "kind \"mixture\")")
    return [float(n) for n in material.get_deposition_steps(float(wavelength))]


def is_mixture(name):
    return _material(name).is_mixture()


def mixture_table(name, wavelengths):
    """A mixture's composition steps: the index of each step k at each wavelength, and its range."""
    columns = [mixture_steps(name, w) for w in wavelengths]
    return {"material": name, "kind": "mixture", "n_steps": len(columns[0]),
            "columns": ["step"] + [f"n@{w:g}" for w in wavelengths],
            "rows": [[k] + [round(column[k], 5) for column in columns] for k in range(len(columns[0]))],
            "note": "graded layers give these step numbers (\"steps\") or indices (\"indices_at_cw\", rounded to the "
                    "nearest step at the center wavelength); a homogeneous layer of a mixture takes any \"index\" "
                    "within the range at the center wavelength"}


def graded_steps_count(design):
    return sum(len(layer["graded"].get("step_thicknesses") or [])
               for key in ("layers", "back_layers") for layer in design.get(key) or []
               if isinstance(layer, dict) and isinstance(layer.get("graded"), dict))


def summarize_graded(design):
    """The design with each graded layer's arrays replaced by graded_summary (for reading, not reuse)."""
    cw = float(design.get("center_wavelength", 550.0))
    out = dict(design)
    for key in ("layers", "back_layers"):
        if design.get(key):
            out[key] = [{k: v for k, v in layer.items() if k != "graded"} | graded_summary(
                            layer["graded"]["indices_at_cw"], layer["graded"]["step_thicknesses"], cw)
                        if isinstance(layer.get("graded"), dict) and "indices_at_cw" in layer["graded"] else layer
                        for layer in design[key]]
    return out


def graded_steps(material, graded, center_wavelength, where):
    """Steps and step thicknesses of a graded layer, validated. Steps are the mixture's composition steps
    (integers, see mixture_steps); alternatively "indices_at_cw" are rounded to the nearest step."""
    if not isinstance(graded, dict) or "step_thicknesses" not in graded or not ("steps" in graded or "indices_at_cw" in graded):
        raise EngineError(f"{where} needs {{\"step_thicknesses\": [...], \"steps\": [...]}} or "
                          "{\"step_thicknesses\": [...], \"indices_at_cw\": [...]} (steps: the mixture's composition "
                          "steps; get_material_index lists them)")
    _check_keys(graded, GRADED_KEYS, where)
    table = mixture_steps(material, center_wavelength)
    try:
        thicknesses = [float(t) for t in graded["step_thicknesses"]]
    except (TypeError, ValueError) as error:
        raise EngineError(f"{where}: step_thicknesses must be numbers (nm)") from error
    if not thicknesses or not all(math.isfinite(t) and t >= 0 for t in thicknesses) or not sum(thicknesses):
        raise EngineError(f"{where}: step_thicknesses must be a non-empty list of thicknesses >= 0 nm")
    if "steps" in graded:
        steps = graded["steps"]
        if not all(isinstance(k, (int, float)) and float(k).is_integer() for k in steps):
            raise EngineError(f"{where}: steps must be integers (the mixture's composition steps)")
        steps = [int(k) for k in steps]
        bad = [k for k in steps if not 0 <= k < len(table)]
        if bad:
            raise EngineError(f"{where}: step {bad[0]} is outside {material}'s steps 0-{len(table) - 1} (n = "
                              f"{table[0]:.4f}-{table[-1]:.4f} at {center_wavelength:g} nm)")
    else:
        steps = [_nearest_step(table, float(n), material, center_wavelength, where) for n in graded["indices_at_cw"]]
    if len(steps) != len(thicknesses):
        raise EngineError(f"{where}: {len(steps)} steps but {len(thicknesses)} step_thicknesses")
    return steps, thicknesses


def _nearest_step(table, n, material, center_wavelength, where):
    """The step whose index is nearest n; refused beyond the ends of the steps (by more than half a step)."""
    half = abs(table[1] - table[0]) / 2 if len(table) > 1 else 0.0
    if not min(table) - half - 1e-9 <= n <= max(table) + half + 1e-9:
        raise EngineError(f"{where}: index {n:g} is outside the range of the mixture {material} at the center "
                          f"wavelength {center_wavelength:g} nm [{min(table):.4f}, {max(table):.4f}]")
    return min(range(len(table)), key=lambda j: abs(table[j] - n))


def _formula_symbols(materials, formula_key, center_wavelength):
    """OpenFilters' {symbol: (material, index_for_mixtures)} from a formula's materials, and the
    materials of the symbols marked fixed. A symbol is a name, [name, index], or
    {"material": name, "index": n, "fixed": true}."""
    symbols, fixed, free = {}, set(), set()
    for sym, value in materials.items():
        if isinstance(value, dict):
            _check_keys(value, {"material", "index", "fixed"}, f"{formula_key} symbol {sym}")
            if "material" not in value:
                raise EngineError(f"{formula_key} symbol {sym} needs a material")
            pair = (value["material"], value.get("index"))
            (fixed if value.get("fixed") else free).add(pair[0])
        else:
            pair = tuple(value) if isinstance(value, (list, tuple)) else (value, None)
            free.add(pair[0])
        if pair[1] is not None:
            _check_mixture_index(pair[0], float(pair[1]), center_wavelength, f"{formula_key} symbol {sym}")
        symbols[sym] = pair
    if fixed & free:
        name = sorted(fixed & free)[0]
        raise EngineError(f"{formula_key}: {name} is used by a fixed symbol and by a free one, so its layers cannot "
                          "be told apart after the formula is expanded. Give the fixed layer its own symbol only, or "
                          "list the layers explicitly with \"fixed\": true on the fixed ones.")
    return symbols, fixed


def _add_layer(f, layer, side, i):
    if isinstance(layer, (list, tuple)):
        layer = {"material": layer[0], "thickness": layer[1]}
    material = layer.get("material")
    if material is None:
        raise EngineError(f"layer {i + 1} has no material")
    _check_keys(layer, LAYER_KEYS, f"layer {i + 1}")
    index = layer.get("index")
    position = f.get_nb_layers(side)
    if layer.get("graded"):
        steps, thicknesses = graded_steps(material, layer["graded"], f.get_center_wavelength(), f"graded layer {i + 1}")
        if layer.get("thickness") is not None:
            # thickness is optional for a graded layer (the sum of its steps); a different value is a mistake.
            total = sum(thicknesses)
            if abs(float(layer["thickness"]) - total) > max(0.01, 1e-4 * total):
                raise EngineError(f"graded layer {i + 1}: thickness {float(layer['thickness']):g} nm differs from the sum "
                                  f"of its step_thicknesses ({total:.4f} nm); omit thickness (it is that sum) or fix it")
        f.add_graded_layer_from_steps(material, steps, thicknesses, side=side)
        _apply_layer_flags(f, layer, position, side)
        return
    def number(key):
        # JSON carries NaN as null; anything that is not a finite number is refused here.
        # (A NaN thickness evaluates as an empty coating, R = 0, and would pass "<=" targets.)
        try:
            value = float(layer[key])
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value):
            raise EngineError(f"layer {i + 1} has an invalid {key} ({layer[key]!r}); it must be a finite number")
        return value

    if index is not None:
        index = number("index")
        _check_mixture_index(material, index, f.get_center_wavelength(), f"layer {i + 1}")
    if "qwot" in layer:
        mat = _material(material)
        n = index if index is not None else mat.get_index(f.get_center_wavelength())
        thickness = number("qwot") * f.get_center_wavelength() / (4.0 * n)
    elif "thickness" in layer:
        thickness = number("thickness")
    else:
        raise EngineError(f"layer {i + 1} needs thickness (nm) or qwot")
    if thickness < 0:
        raise EngineError(f"layer {i + 1} has a negative thickness")
    f.add_layer(material, thickness, side=side, index=index)
    _apply_layer_flags(f, layer, position, side)


def _apply_layer_flags(f, layer, position, side):
    for flag, setter in _LAYER_FLAGS:
        if flag in layer:
            getattr(f, "set_" + setter)(position, bool(layer[flag]), side)
    if layer.get("fixed"):
        _fix_layer(f, position, side)


def _fix_layer(f, position, side):
    """Keep a layer as it is in every optimization: OpenFilters' refine_thickness false (which also
    stops needles in it) and, for a mixture, refine_index false."""
    f.set_refine_layer_thickness(position, False, side)
    if f.get_layer_material(position, side).is_mixture():
        f.set_refine_layer_index(position, False, side)
        f.set_add_steps(position, False, side)


# Per-layer optimization flags: design key -> OpenFilters set_/get_ suffix.
_LAYER_FLAGS = (("refine_thickness", "refine_layer_thickness"), ("refine_index", "refine_layer_index"),
                ("preserve_ot", "preserve_OT"), ("add_needles", "add_needles"), ("add_steps", "add_steps"))
# OpenFilters' defaults for filter settings that design_from_filter reports only when changed.
_DEFAULT_WAVELENGTH_RANGE = (300.0, 1000.0, 1.0)
_DEFAULT_STEP_SPACING = 0.01


def _default_layer_flags(side, mixture, graded=False):
    """The flags optical_filter.add_layer gives a new layer (all off for a graded layer: OpenFilters'
    add_graded_layer leaves it out of every optimization unless told otherwise)."""
    front = side == FRONT and not graded
    return {"refine_thickness": front, "refine_index": front and mixture, "preserve_ot": False,
            "add_needles": front, "add_steps": front and mixture}


def name_of(material):
    """A material's name, whether OpenFilters gives the object or the name."""
    return material.get_name() if hasattr(material, "get_name") else str(material)


def design_from_filter(f):
    """Inverse of build_filter (graded layers are reported, not rebuilt)."""
    name = name_of

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
    if getattr(f, "table_extrapolation", None):
        design["table_extrapolation"] = f.table_extrapolation
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
            defaults = _default_layer_flags(side, mixture, f.is_graded(i, side))
            for flag, getter in _LAYER_FLAGS:
                value = bool(getattr(f, "get_" + getter)(i, side))
                if value != defaults[flag]:
                    layer[flag] = value
            layers.append(layer)
        if layers or side == FRONT:
            design[key] = layers
    return design


def graded_summary(indices, thicknesses, center_wavelength):
    """A graded layer in a few numbers: steps, index range, first/last index (for matching to the
    substrate and the medium) and optical thickness, all at the center wavelength."""
    indices, thicknesses = [float(n) for n in indices], [float(d) for d in thicknesses]
    optical = sum(n * d for n, d in zip(indices, thicknesses))
    return {"graded": True, "n_steps": len(indices), "n_min@cw": round(min(indices), 4),
            "n_max@cw": round(max(indices), 4), "n_first@cw": round(indices[0], 4), "n_last@cw": round(indices[-1], 4),
            "optical_thickness_nm": round(optical, 3), "qwot@cw": round(4 * optical / center_wavelength, 4)}


def describe(design):
    """Layer table, totals and the normalized design. "index_profile" lists [thickness, n at the center
    wavelength] for every front layer, graded layers step by step (for plots; the server drops it)."""
    f = build_filter(design)
    cw = f.get_center_wavelength()
    profile = []

    def side_rows(side):
        rows = []
        for i in range(f.get_nb_layers(side)):
            thickness = float(f.get_layer_thickness(i, side))
            material = f.get_layer_material_name(i, side)
            row = {"#": i + 1, "material": material, "thickness_nm": round(thickness, 3)}
            if f.is_graded(i, side):
                step_thicknesses, _ = f.get_layer_step_profile(i, side)
                indices = f.get_layer_index(i, side)
                row.update(graded_summary(indices, step_thicknesses, cw))
                segments = [[float(d), float(n)] for d, n in zip(step_thicknesses, indices)]
            else:
                n = float(f.get_layer_index(i, side)) if _material(material).is_mixture()                     else _material(material).get_index(cw)
                row["n@cw"] = round(n, 4)
                row["qwot@cw"] = round(4 * n * thickness / cw, 4)
                segments = [[thickness, n]]
            if side == FRONT:
                profile.extend(segments)
            rows.append(row)
        return rows

    rows, back = side_rows(FRONT), side_rows(BACK)
    def real_index(name):
        return round(float(material_N(name, [cw])[0].real), 4)

    out = {"layers": rows, "nb_layers": len(rows), "total_thickness_nm": round(float(f.get_total_thickness(FRONT)), 3),
           "center_wavelength": cw, "materials": sorted({r["material"] for r in rows + back}),
           "consider_backside": bool(f.get_consider_backside()),
           "substrate_n@cw": real_index(name_of(f.get_substrate())),
           "incident_medium_n@cw": real_index(name_of(f.get_medium(FRONT))), "index_profile": profile}
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
# OpenFilters computes group delay in s and GDD in s^2 (its GUI scales them for display and for targets);
# the connector speaks fs and fs^2 everywhere, like the GUI.
TIME_UNITS = {"R_GD": 1e15, "T_GD": 1e15, "R_GDD": 1e30, "T_GDD": 1e30}


def _design_materials(design):
    names = {design.get("substrate", "FusedSilica"), design.get("incident_medium", "void"),
             design.get("exit_medium", design.get("incident_medium", "void"))}
    for key in ("layers", "back_layers"):
        names |= {l.get("material") for l in design.get(key) or [] if isinstance(l, dict)}
    for key in ("formula", "back_formula"):
        if isinstance(design.get(key), dict):
            for value in (design[key].get("materials") or {}).values():  # name, [name, index] or {"material": ...}
                names.add(value.get("material") if isinstance(value, dict)
                          else value[0] if isinstance(value, (list, tuple)) else value)
    return sorted(n for n in names if isinstance(n, str))


# Tabulated n,k are held constant beyond the data (see openfilters_core/PROVENANCE.md); a
# calculation further than this factor beyond the data is refused instead.
TABLE_REACH = 1.5


def _data_range(material):
    data = getattr(material, "wvls", None)
    return (float(data[0]), float(data[-1])) if data is not None and len(data) else None


def outside_data(design, wavelengths):
    """[(material, (data from, to), [outside ranges as (from, to)])] for tabulated materials of the design
    used outside their data at these wavelengths."""
    wavelengths = [float(w) for w in wavelengths]
    if not wavelengths:
        return []
    design, _ = expand_profiles(design)
    lo, hi = min(wavelengths), max(wavelengths)
    out = []
    for name in _design_materials(design):
        material = _material(name)
        data = None if material.is_mixture() else _data_range(material)
        if data is None:
            continue
        # the part of the wavelengths used outside the data (e.g. 8000-12000 nm, not 1771-12000 nm)
        ranges = ([(lo, min(hi, data[0]))] if lo < data[0] * (1 - 1e-6) else []) + \
                 ([(max(lo, data[1]), hi)] if hi > data[1] * (1 + 1e-6) else [])
        if ranges:
            out.append((name, data, ranges))
    return out


def _nm_range(a, b):
    """"8000-12000 nm", or "10000 nm" for a single wavelength (not "10000-10000 nm")."""
    return f"{a:g} nm" if abs(b - a) < 1e-9 else f"{a:g}-{b:g} nm"


def _nk(z):
    return f"n = {z.real:.4g}, k = {-z.imag + 0.0:.3g}"  # k < 0 (gain) shown as it is


def material_warnings(design, wavelengths, invisible=True):
    """Warnings for tabulated materials used outside their data, saying what was used there (held end
    values or OpenFilters' extrapolation, per table_extrapolation) and what the other mode would give;
    errors for "refuse", for use far beyond the data (TABLE_REACH) when no mode was chosen, and where a
    material's model gives an unphysical index (n <= 0), e.g. a Sellmeier formula near its infrared
    pole: OpenFilters evaluates every model at any wavelength without checking."""
    wavelengths = [float(w) for w in wavelengths]
    if not wavelengths:
        return []
    mode = extrapolation_mode(design)
    chosen = isinstance(design, dict) and "table_extrapolation" in design
    design, warnings = expand_profiles(design)
    lo, hi = min(wavelengths), max(wavelengths)
    grid = sorted(set(np.linspace(lo, hi, 64).tolist()) | set(wavelengths[:: max(1, len(wavelengths) // 200)]))
    outside = outside_data(design, wavelengths)
    # Name every material that is refused in one message (S4.7: Ge was named, the Si substrate only after a retry).
    described = [(name, f"{data[0]:g}-{data[1]:g} nm", " and ".join(_nm_range(a, b) for a, b in ranges))
                 for name, data, ranges in outside]
    if mode == "refuse" and outside:
        if len(described) == 1:
            name, data, where = described[0]
            raise EngineError(f"{name}: its data cover {data} and it is used at {where}, which table_extrapolation "
                              "\"refuse\" forbids. Limit the wavelengths, use a material with data there, or choose "
                              "\"hold\" or \"openfilters\".")
        listed = "; ".join(f"{name} (data {data}, used at {where})" for name, data, where in described)
        raise EngineError(f"these materials are used outside their data, which table_extrapolation \"refuse\" forbids: "
                          f"{listed}. Limit the wavelengths, use materials with data there, or choose \"hold\" or "
                          "\"openfilters\".")
    far = [d for d, (name, data, ranges) in zip(described, outside)
           if not chosen and (lo < data[0] / TABLE_REACH or hi > data[1] * TABLE_REACH)]
    if far:
        advice = ("Use a material with data for this range (search_refractiveindex and import_refractiveindex_material), "
                  "or ask the user and set the design's table_extrapolation to \"hold\" (end values) or \"openfilters\" "
                  "(OpenFilters' extrapolation) to compute anyway.")
        if len(far) == 1:
            name, data, where = far[0]
            raise EngineError(f"{name}: its data cover {data}, so its n,k at {where} (more than {TABLE_REACH:g}x beyond "
                              f"the data) are unknown. " + advice)
        listed = "; ".join(f"{name} (data {data}, n,k needed at {where})" for name, data, where in far)
        raise EngineError(f"these materials are used more than {TABLE_REACH:g}x beyond the data, where their n,k are "
                          f"unknown: {listed}. " + advice.replace("Use a material", "Use materials"))
    for name, data, ranges in outside:
        where = " and ".join(_nm_range(a, b) for a, b in ranges)
        ends = [a if a < data[0] else b for a, b in ranges]  # the farthest wavelength on each side
        held, extrapolated = material_N(name, ends), material_N(name, ends, "openfilters")
        used, other = (held, extrapolated) if mode == "hold" else (extrapolated, held)
        values = "; ".join(f"at {w:g} nm {_nk(u)}" for w, u in zip(ends, used))
        others = "; ".join(f"{_nk(o)}" for o in other)
        how = ("held at the value at the nearest end of the data" if mode == "hold"
               else "extrapolated as OpenFilters does (end segments of the interpolation)")
        other_name = "OpenFilters' extrapolation" if mode == "hold" else "Holding the end values"
        warnings.append(f"{name}: its data cover {data[0]:g}-{data[1]:g} nm; at {where} n,k are {how} "
                        f"(table_extrapolation \"{mode}\"{'' if chosen else ', the default'}): {values}. "
                        f"{other_name} would give {others} there. Not measured: results there are approximate.")
    for name in _design_materials(design):
        if _material(name).is_mixture():
            continue
        N = material_N(name, grid, mode)
        bad = [(w, z) for w, z in zip(grid, N) if not (np.isfinite(z.real) and np.isfinite(z.imag)) or z.real <= 0]
        if bad:
            w, z = bad[0]
            raise EngineError(f"{name} gives an unphysical index (n = {z.real:.4g}) at {w:g} nm"
                              f"{' with table_extrapolation openfilters' if mode == 'openfilters' else ''}: its model "
                              "is not valid there. Use a material with data for this range (search_refractiveindex "
                              "and import_refractiveindex_material).")
    return warnings + (_invisible_layers(design, (lo + hi) / 2) if invisible else []) + _graded_rounding(design)


def check_long_job(operation, args):
    """Before a job: refuse long work whose materials are used outside their data unless the design
    chose table_extrapolation (see require_extrapolation_choice)."""
    design = args.get("design")
    if args.get("target_specs"):
        wavelengths = target_wavelengths(args["target_specs"])
    elif operation == "spectrum":
        wavelengths = _wavelength_list(args.get("wavelengths"))
    else:
        return
    what = {"optimize": "This optimization", "tolerance": "This tolerance analysis"}.get(operation, f"This {operation} job")
    require_extrapolation_choice(design, wavelengths, what)


def require_extrapolation_choice(design, wavelengths, what):
    """Long work (optimizations, campaigns, tolerance analyses, batches) must not run on a guess nobody
    chose: refuse it when a table material is used outside its data and the design does not set
    table_extrapolation."""
    if not isinstance(design, dict) or "table_extrapolation" in design:
        return
    outside = outside_data(design, wavelengths)
    if outside:
        listed = "; ".join(f"{name} (data {d[0]:g}-{d[1]:g} nm, used at "
                           f"{' and '.join(_nm_range(a, b) for a, b in r)})" for name, d, r in outside)
        raise EngineError(f"{what} uses materials outside their measured data: {listed}. Ask the user how n,k should "
                          "be filled in there before starting, and set the design's \"table_extrapolation\": "
                          "\"hold\" (end values, the default for quick calculations), \"openfilters\" (OpenFilters' "
                          "own extrapolation, as in its GUI) or \"refuse\". compute_spectrum's warnings show the n,k "
                          "each choice gives.")


def _graded_rounding(design):
    """Graded layers given by indices_at_cw are rounded to the mixture's steps: say by how much."""
    warnings = []
    cw = float(design.get("center_wavelength", 550.0))
    for key in ("layers", "back_layers"):
        for i, layer in enumerate(design.get(key) or []):
            graded = layer.get("graded") if isinstance(layer, dict) else None
            if not isinstance(graded, dict) or "steps" in graded or "indices_at_cw" not in graded:
                continue
            table = mixture_steps(layer["material"], cw)
            worst = max(abs(table[_nearest_step(table, float(n), layer["material"], cw, "")] - float(n))
                        for n in graded["indices_at_cw"])
            if worst > 1e-6:
                warnings.append(f"{'layer' if key == 'layers' else 'back layer'} {i + 1}: indices_at_cw rounded to {layer['material']}'s composition steps "
                                f"(about {abs(table[1] - table[0]):.4g} apart at {cw:g} nm); largest change {worst:.3g}")
    return warnings


def _invisible_layers(design, wavelength):
    """Warnings for front layers with the same index as what is next to them (e.g. Ge on a Ge substrate):
    they are optically invisible, their thickness is meaningless and an optimizer cannot control it."""
    layers = [l for l in design.get("layers") or [] if isinstance(l, dict) and not l.get("graded") and "index" not in l]
    if not layers or design.get("formula"):
        return []
    names = [design.get("substrate", "FusedSilica")] + [l.get("material") for l in layers] + [design.get("incident_medium", "void")]
    try:
        N = [material_N(n, [wavelength])[0] for n in names]
    except EngineError:
        return []
    out = []
    for i in range(1, len(names) - 1):
        for j, where in ((i - 1, "the substrate" if i == 1 else f"layer {i - 1}"),
                         (i + 1, "the incident medium" if i == len(names) - 2 else f"layer {i + 1}")):
            if abs(N[i] - N[j]) < 1e-9:
                out.append(f"layer {i} ({names[i]}) has the same index as {where} at {wavelength:g} nm: it is optically "
                           "invisible, so its thickness is meaningless; remove it or merge it with its neighbour")
                break
    return out


def target_wavelengths(target_specs):
    """Every wavelength the targets use (colour targets: the visible range)."""
    out = []
    for spec in target_specs or []:
        if str(spec.get("kind", "")).endswith("_color"):
            out += [380.0, 780.0]
        if "wavelength" in spec:
            out.append(float(spec["wavelength"]))
        out += [float(spec[key]) for key in ("from", "to") if key in spec]
        out += [float(point[0]) for point in spec.get("points") or []]
    return out


def spectra(design, quantities=("R", "T"), wavelengths=None, angles=(0.0,), polarizations=("unpolarized",),
            reverse=False):
    """Return {"wavelengths": [...], "series": [{"quantity","angle","polarization","values"}]}.
    Without wavelengths: the design's wavelength_range if it has one (what the OpenFilters GUI analyses,
    e.g. a loaded project), else 380-1100 nm."""
    if wavelengths is None and design.get("wavelength_range"):
        wavelengths = design["wavelength_range"]
    wvls = _wavelength_list(wavelengths)
    # OpenFilters takes GD/GDD at the first and last wavelength from the neighbouring point (no one-sided
    # derivative), so a uniform grid gets one extra step at each end, computed and then dropped.
    grid, pad = wvls, 0
    if any(q in TIME_UNITS for q in quantities) and len(wvls) >= 3:
        steps = np.diff(wvls)
        if steps[0] > 0 and np.allclose(steps, steps[0], rtol=1e-6) and wvls[0] - steps[0] > 0:
            grid, pad = [wvls[0] - float(steps[0])] + list(wvls) + [wvls[-1] + float(steps[0])], 1
    f = build_filter(design, grid)
    warnings = material_warnings(design, wvls)
    if any(q in TIME_UNITS for q in quantities):
        # OpenFilters takes GD and GDD as numerical derivatives over the requested wavelengths:
        # a few widely spaced points give wrong values (e.g. 0.95 fs^2 instead of +4/-4 fs^2 at 40 nm).
        gaps = np.diff(sorted(wvls))
        if len(wvls) < 5 or (len(gaps) and gaps.max() > 5.0):
            warnings.append("GD/GDD are numerical derivatives over the requested wavelengths: use at least 5 "
                            "points with steps of 5 nm or less, or the values can be wrong")
    series = []
    # One series per distinct case: 30 and "30deg", or 45 and "unpolarized", are the same polarization.
    angles = list(dict.fromkeys(float(a) for a in angles))
    polarizations = list(dict.fromkeys(polarization_value(p) for p in polarizations))
    for q in dict.fromkeys(quantities):
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
                values = np.array([values[i] for i in range(pad, pad + len(wvls))], dtype=float)
                if q == "OD":
                    values = -np.log10(np.maximum(values, 1e-300))
                elif q in ("R", "T", "A") and values.min() > -1e-9:
                    values = np.maximum(values, 0.0)  # round-off (A = 1 - R - T came out at -8e-16)
                values = values * TIME_UNITS.get(q, 1.0)
                series.append({"quantity": q, "angle": float(angle), "polarization": polarization_name(polarization_value(pol)),
                               "values": values})
    return {"wavelengths": np.array(wvls), "series": series, "warnings": warnings}


def _srgb_encode(v):
    """sRGB transfer curve: OpenFilters' RGB is linear, and a hex colour built from linear values is
    far too dark (#0a327e instead of #367bbb)."""
    v = min(max(float(v), 0.0), 1.0)
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def color(design, angles=(0.0,), polarization="unpolarized", illuminant="CIE-D65", observer="CIE-1931",
          mode="reflection"):
    if mode not in ("reflection", "transmission"):
        raise EngineError("mode must be reflection or transmission")
    if illuminant not in of_color.get_illuminant_names():
        raise EngineError(f"illuminant must be one of {', '.join(of_color.get_illuminant_names())}")
    if observer not in of_color.get_observer_names():
        raise EngineError(f"observer must be one of {', '.join(of_color.get_observer_names())}")
    warnings = material_warnings(design, [380.0, 780.0])  # the visible range the colour integrates over
    f = build_filter(design)
    out = []
    for angle in angles:
        R_color, T_color = f.color(float(angle), polarization_value(polarization), illuminant, observer)
        c = R_color if mode == "reflection" else T_color
        X, Y, Z = c.XYZ()
        x, y, _ = c.xyY()
        L, a, b = c.Lab()
        _, C, h = c.LChab()
        linear = c.RGB()  # OpenFilters: linear RGB (Rec. 709 / sRGB primaries), clipped to [0, 1]
        srgb = [_srgb_encode(v) for v in linear]
        out.append({"angle": float(angle), "XYZ": [X, Y, Z], "xy": [x, y], "Lab": [L, a, b], "LCh": [L, C, h],
                    "sRGB": srgb, "linear_RGB": list(linear),
                    "hex": "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in srgb),
                    "out_of_gamut": bool(c.get_RGB_error())})
    return _with_warnings({"illuminant": illuminant, "observer": observer, "mode": mode,
                           "polarization": polarization_name(polarization_value(polarization)), "colors": out},
                          warnings)


def electric_field(design, wavelength, angle=0.0, polarization="s"):
    """|E|^2 through the front stack, relative to the incident wave
    (tangential field, Macleod's convention)."""
    pol = polarization_value(polarization)
    if pol not in (S, P):
        raise EngineError("electric field needs s or p polarization")
    wavelength, angle = float(wavelength), float(angle)
    design = dict(design, consider_backside=False)
    warnings = material_warnings(design, [wavelength])
    f = build_filter(design, [wavelength])
    graded = [i for i in range(f.get_nb_layers(FRONT)) if f.is_graded(i, FRONT)]
    if graded:
        # OpenFilters samples a homogeneous layer every monitoring_sublayer_thickness (1 nm) but a graded
        # layer only at its step boundaries, so |E|^2 inside a thick step was a straight line and its peak
        # could be missed. Cut the steps into the same 1 nm pieces (same index): identical physics, and
        # identical points to a homogeneous layer.
        design = design_from_filter(f)
        piece = f.get_monitoring_sublayer_thickness()
        for i in graded:
            layer = design["layers"][i]
            steps, thicknesses = [], []
            for k, d in zip(layer["graded"]["steps"], layer["graded"]["step_thicknesses"]):
                whole = int(d // piece)
                rest = d - whole * piece
                pieces = [piece] * whole + ([rest] if rest > 1e-9 or not whole else [])
                steps += [k] * len(pieces)
                thicknesses += pieces
            design["layers"][i] = dict(layer, graded={"steps": steps, "step_thicknesses": thicknesses})
        f = build_filter(design, [wavelength])
    positions, fields = f.electric_field(wavelength, angle, pol)
    # OpenFilters returns |E| relative to the field at the substrate. With
    # E_substrate = 1, Macleod gives |E_incident|^2 = Re(eta_s) / (eta_0 T).
    T = f.transmission(angle, pol)[0]
    if T <= 0:
        raise EngineError("electric field normalization needs a non-zero transmission")
    mode = extrapolation_mode(design)
    n0 = material_N(design.get("incident_medium", "void"), [wavelength], mode)[0]
    ns = material_N(design.get("substrate", "FusedSilica"), [wavelength], mode)[0]
    sin0 = n0 * math.sin(math.radians(angle))
    eta = lambda N: (N * np.sqrt(1 - (sin0 / N) ** 2 + 0j)) if pol == S else (N / np.sqrt(1 - (sin0 / N) ** 2 + 0j))
    scale = (eta(n0).real * T) / eta(ns).real
    depth, value, boundaries, graded_peaks = [], [], [], []
    for i, (layer_positions, layer_field) in enumerate(zip(positions, fields)):
        x, e2 = [float(x) for x in layer_positions], [float(float(v) ** 2 * scale) for v in layer_field]
        depth += x
        value += e2
        boundaries.append(x[-1])
        if i in graded:
            j = int(np.argmax(e2))
            graded_peaks.append({"layer": i + 1, "E_squared": e2[j], "position_nm": x[j]})
    out = {"position_nm": depth, "E_squared": value, "layer_boundaries_nm": boundaries,
           "note": "position from the substrate interface outward; |E|^2 relative to the incident wave, "
                   "sampled every 1 nm inside every layer"}
    if graded_peaks:
        out["graded_layer_peaks"] = graded_peaks
    return _with_warnings(out, warnings)


def _with_warnings(result, warnings):
    if warnings:
        result["warnings"] = warnings
    return result


MONITORING_INLINE_WAVELENGTHS = 20  # more wavelengths (spectroscopic monitoring): a matrix, not a dict


def monitoring(design, wavelengths, angle=0.0, polarization="unpolarized", mode="reflection", direction="normal"):
    """Monitoring signal versus deposited thickness at each monitoring
    wavelength (as used for optical monitoring during deposition).

    wavelengths: a list, a range {"from", "to", "step"}, or "spectroscopic": every wavelength of the
    design's wavelength_range, as OpenFilters' "spectroscopic" option (broadband monitoring).
    direction "reverse": light from the substrate side (OpenFilters' reverse-direction monitoring)."""
    if isinstance(wavelengths, str):
        if wavelengths.strip().lower() != "spectroscopic":
            raise EngineError('wavelengths must be a list, {"from", "to", "step"} or "spectroscopic"')
        wvls = [float(w) for w in build_filter(design).get_wavelengths()]
    else:
        wvls = _wavelength_list(wavelengths)
    warnings = material_warnings(design, wvls)
    f = build_filter(design)
    if mode not in ("reflection", "transmission"):
        raise EngineError("mode must be reflection or transmission")
    direction = str(direction).lower()
    if direction not in ("normal", "reverse"):
        raise EngineError('direction must be "normal" or "reverse" (light from the substrate side)')
    angle = float(angle)
    if not 0 <= angle < 90:
        raise EngineError("angle must be in [0, 90)")
    method = getattr(f, f"{mode}_monitoring" + ("_reverse" if direction == "reverse" else ""))
    thickness, signal = method(wvls, angle, polarization_value(polarization))
    # thickness[layer][point]; signal[wavelength][layer][point].
    deposited = [float(x) for layer_thickness in thickness for x in layer_thickness]
    boundaries = [float(layer_thickness[-1]) for layer_thickness in thickness]
    matrix = np.array([[v for layer_signal in signal[i] for v in layer_signal] for i in range(len(wvls))], dtype=float)
    out = {"deposited_thickness_nm": deposited, "wavelengths": wvls, "signal_map": matrix,
           "layer_boundaries_nm": boundaries, "direction": direction,
           "center_wavelength": float(f.get_center_wavelength()),
           "includes_backside": bool(f.get_consider_backside_on_monitoring())}
    if len(wvls) <= MONITORING_INLINE_WAVELENGTHS:
        out["signal"] = {f"{w:g}": matrix[i].tolist() for i, w in enumerate(wvls)}
    return _with_warnings(out, warnings)


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


PAD_WEIGHT = 1e6  # tolerance factor of padding points (no weight in the merit)


def padded(spec):
    """GD/GDD band targets get one extra wavelength step at each end for evaluation and optimization:
    OpenFilters takes GD/GDD as numerical derivatives, and at the first and last point of a band it
    reuses the neighbouring point's estimate, so the band edges escaped the optimizer (a dispersive
    mirror "met" GDD -20 +/- 5 fs^2 on its grid but was at -13.5 fs^2 at both edges)."""
    return spec.get("kind") in TIME_UNITS and "from" in spec and "to" in spec and "wavelength" not in spec


def target_lines(spec, pad=False):
    """Translate a target dict into OpenFilters' text format (pad: see padded())."""
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
    to_of = 1.0 / TIME_UNITS.get(kind, 1.0)  # GD/GDD targets are given in fs / fs^2

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
                  f"{_color_space(spec.get('color_space', 'Lab'))}: {values[0]} {values[1]} {values[2]}",
                  f"Tolerances: {tolerances[0]} {tolerances[1]} {tolerances[2]}"]
    elif "wavelength" in spec:
        lines += [f"Kind: {discrete}", f"Angle: {angle}", f"Polarization: {pol}",
                  f"Wavelength: {float(spec['wavelength'])}", f"Value: {check_value(float(spec['value'])) * to_of!r}",
                  f"Tolerance: {float(spec.get('tolerance', 0.01)) * to_of!r}"]
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
        if pad and padded(spec):
            first, last = points[0], points[-1]
            first_tol = float(first[2]) if len(first) > 2 else tol
            last_tol = float(last[2]) if len(last) > 2 else tol
            start, stop = start - step, stop + step
            points = ([[start, float(first[1]), first_tol * PAD_WEIGHT]] + [list(p) for p in points]
                      + [[stop, float(last[1]), last_tol * PAD_WEIGHT]])
        lines += [f"Kind: {spectral}", f"Angle: {angle}", f"Polarization: {pol}",
                  f"From: {start}", f"To: {stop}", f"By: {step}", "Points:"]
        for point in points:
            w, v = float(point[0]), check_value(float(point[1]))
            t = float(point[2]) if len(point) > 2 else tol
            lines.append(f"\t{w} {v * to_of!r} {t * to_of!r}")
        lines.append("End")
    else:
        raise EngineError("target needs wavelength (single point) or from/to (spectral band)")
    if spec.get("direction", "normal").lower() in ("reverse", "back"):
        lines.insert(3, "Direction: Reverse")
    if inequality:
        lines.append(f"Inequality: {inequality}")
    return lines


# OpenFilters' keywords for colour spaces, with the usual spellings accepted.
_COLOR_SPACE_ALIASES = {"lab": "Lab", "l*a*b*": "Lab", "cielab": "Lab", "xyz": "XYZ", "xyy": "xyY",
                        "luv": "Luv", "l*u*v*": "Luv", "cieluv": "Luv", "lch": "LChab", "lchab": "LChab",
                        "lch(ab)": "LChab", "lchuv": "LChuv", "lch(uv)": "LChuv"}


def _color_space(name):
    key = _COLOR_SPACE_ALIASES.get(str(name).strip().lower())
    if key is None:
        raise EngineError(f"color_space must be one of {', '.join(of_targets.COLOR_SPACE_NAMES.values())} (got {name!r})")
    return key


def build_targets(specs, pad=False):
    """OpenFilters targets; pad=True for evaluation and optimization (see padded()), False for projects."""
    if not specs:
        raise EngineError("at least one target is needed")
    file_version = of_version.version("1.1")
    out = []
    for i, spec in enumerate(specs):
        try:
            out.append(of_targets.parse_target([l + "\n" for l in target_lines(spec, pad)], file_version))
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
    # Convergence slack of 1e-4 of the tolerance, in the target's own units (1e-6 for R with the default
    # tolerance 0.01): optima sit on the limit, and R = 0.5 % + 2e-8 is not a miss (scenarios S5.6, S5.12).
    # (It was 1e-9 * max(|goal|, 1), which for GDD targets, in s^2 internally, let any value pass.)
    slack = 1e-4 * np.abs(tol)
    ok_ineq = ((ineq < 0) & (calc <= goal + slack)) | ((ineq > 0) & (calc >= goal - slack))
    deviation[ok_ineq] = 0.0
    # An inequality ("R <= 0.2 %") is met only when it holds: its tolerance only weights the
    # merit. An equality is met within its tolerance. Non-finite values never pass.
    passed = np.where(ineq != 0, ok_ineq, np.abs(deviation) <= tol * (1 + 1e-9)) & np.isfinite(calc)
    rows = []
    starts = list(optimizer.target_starting_position) + [len(calc)]
    counted = np.zeros(len(calc), dtype=bool)
    for i, spec in enumerate(specs):
        pads = 1 if padded(spec) else 0  # padding points (see padded()) are not part of the target
        sl = slice(starts[i] + pads, starts[i + 1] - pads)
        counted[sl] = True
        d = deviation[sl]
        unit = TIME_UNITS.get(spec.get("kind"), 1.0)  # GD/GDD reported in fs / fs^2
        row = {"target": i, "kind": spec.get("kind"), "points": int(d.size),
               "pass_fraction": round(float(np.mean(passed[sl])), 4),
               # Significant figures: a tiny miss (1e-7) must not read as 0 next to "not met".
               "max_abs_deviation": sig(np.max(np.abs(d)) * unit),
               "chi2_contribution": sig(np.sum((d / tol[sl]) ** 2))}
        # The value furthest on the wrong side: the highest for "<=", the lowest for ">=",
        # the furthest from the goal for "equal" (e.g. "worst R = 1.08 % at 420 nm").
        if spec.get("kind") in ("R_color", "T_color") and np.all(np.isfinite(calc[sl])):
            # The coordinates in the target's colour space; their distance is ΔE*ab (1976) for Lab.
            row.update(color_space=_color_space(spec.get("color_space", "Lab")),
                       calculated=[sig(v) for v in calc[sl]], target_values=[sig(v) for v in goal[sl]],
                       coordinate_deviation=[sig(v) for v in calc[sl] - goal[sl]],
                       distance=sig(np.linalg.norm(calc[sl] - goal[sl])))
        # Colour targets (3 coordinates, no wavelength per point) have no single worst value.
        values, kind = calc[sl], ineq[starts[i]]
        wvls = optimizer.wvls[i] if i < len(optimizer.wvls) else None
        if wvls is not None and pads and len(wvls) == values.size + 2 * pads:
            wvls = wvls[pads:len(wvls) - pads]
        if values.size and np.all(np.isfinite(values)) and wvls is not None and len(wvls) == values.size:
            j = int(np.argmax(values) if kind < 0 else np.argmin(values) if kind > 0 else np.argmax(np.abs(goal[sl] - values)))
            row["worst_value"] = sig(values[j] * unit)
            row["worst_at_nm"] = round(float(wvls[j]), 3)
        rows.append(row)
    return rows, bool(np.all(passed[counted]))


def sig(value, digits=6):
    """Round to significant figures, so tiny values (deep blocking) are not reported as 0."""
    return float(f"{float(value):.{digits}g}")


def crossing_ranges(x, values, level, above=True):
    """[start, end] ranges of x where values >= level (or <= level), with edges interpolated
    linearly between the points on either side of each crossing."""
    import numpy as np
    x, values = np.asarray(x, dtype=float), np.asarray(values, dtype=float)
    inside = values >= level if above else values <= level
    ranges, i = [], 0
    while i < len(x):
        if not inside[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(x) and inside[j + 1]:
            j += 1

        def edge(a, b):  # a outside, b inside
            return float(x[a] + (level - values[a]) * (x[b] - x[a]) / (values[b] - values[a])) if values[b] != values[a] else float(x[b])
        start = edge(i - 1, i) if i > 0 else float(x[i])
        end = edge(j + 1, j) if j + 1 < len(x) else float(x[j])
        ranges.append([round(start, 4), round(end, 4)])
        i = j + 1
    return ranges


def find_peak(x, values, near=None, start=None, stop=None):
    """Index of the peak: the highest point between start and stop, or with near, the local
    maximum closest to near (a band-pass resonance, not the higher ripple at a band edge)."""
    import numpy as np
    x, values = np.asarray(x, dtype=float), np.asarray(values, dtype=float)
    window = np.flatnonzero((x >= (x[0] if start is None else start)) & (x <= (x[-1] if stop is None else stop)))
    if not len(window):
        raise EngineError(f"no computed point between {start} and {stop}")
    if near is None:
        return int(window[np.argmax(values[window])])
    peaks = [i for i in window if (i == 0 or values[i] >= values[i - 1]) and (i == len(x) - 1 or values[i] >= values[i + 1])
             and 0 < i < len(x) - 1]
    if not peaks:  # monotonic in the window: its highest end
        return int(window[np.argmax(values[window])])
    return int(min(peaks, key=lambda i: (abs(x[i] - near), -values[i])))


def half_max_summary(x, values, options, axis="nm"):
    """FWHM of one peak (see find_peak). Edges are searched in the whole computed range; an edge that
    reaches its end is clipped, and then fwhm is null with fwhm_at_least the width seen."""
    i = find_peak(x, values, options.get("near"), options.get("from"), options.get("to"))
    half = values[i] / 2.0
    edges = next(r for r in crossing_ranges(x, values, half) if r[0] <= x[i] <= r[1])
    clipped = ([] if edges[0] > x[0] or values[0] < half else ["low"]) + \
              ([] if edges[1] < x[-1] or values[-1] < half else ["high"])
    out = {f"peak_{axis}": round(float(x[i]), 6), "peak_value": sig(values[i], 8), "half_max_edges": edges,
           f"half_max_center_{axis}": round((edges[0] + edges[1]) / 2, 6)}
    if clipped:
        out.update(fwhm=None, fwhm_at_least=round(edges[1] - edges[0], 6), half_max_edge_clipped=clipped,
                   fwhm_note=f"the half-maximum edge ({' and '.join(clipped)}) is beyond the computed range: widen it")
    else:
        out["fwhm"] = round(edges[1] - edges[0], 6)
    top = int(np.argmax(values))
    if options.get("near") is not None and values[top] > values[i] * (1 + 1e-6):
        # S6.3: the local peak nearest 663.5 nm was returned while the series maximum (0.856) was at 633.5 nm.
        out["peak_note"] = (f"the local peak nearest {float(options['near']):g} {axis}; the highest value in the range "
                            f"is {sig(values[top], 8)} at {round(float(x[top]), 6)} {axis}")
    return out


def with_margin(target_specs, margin):
    """Target specs with inequality limits tightened by margin (same units as the values: fractions
    for R/T/A, fs or fs^2 for GD/GDD), so an optimum does not sit exactly on the specification."""
    out = []
    for spec in target_specs:
        ineq = INEQUALITIES.get(str(spec.get("inequality", "equal")).lower())
        if not ineq or str(spec.get("kind", "")).endswith("_color"):
            out.append(spec)
            continue
        shift = -margin if ineq == "smaller" else margin
        clip = (lambda v: min(max(v, 0.0), 1.0)) if spec.get("kind") in ("R", "T", "A") else (lambda v: v)
        spec = dict(spec)
        if "value" in spec:
            spec["value"] = clip(float(spec["value"]) + shift)
        if spec.get("points"):
            spec["points"] = [[p[0], clip(float(p[1]) + shift), *p[2:]] for p in spec["points"]]
        out.append(spec)
    return out


def _refine_worst(design, spec, row, step):
    """Narrow notches can be thinner than the fine grid (scenario S5.10: 57.9 % on a 1.25 nm grid, 45.05 %
    in fact), so look for the true worst R/T/A within one fine step of the worst point, at step/25."""
    if spec.get("kind") not in ("R", "T", "A") or spec.get("points") or "worst_at_nm" not in row:
        return
    w = row["worst_at_nm"]
    # Stay inside the target's range (S5.11: "worst at 399.75 nm" for a 400-900 nm target).
    lo, hi = sorted((float(spec["from"]), float(spec["to"])))
    a, b = max(w - step, lo), min(w + step, hi)
    if b <= a:
        return
    r = spectra(design, [spec["kind"]], {"from": a, "to": b, "step": (b - a) / 50},
                angles=[float(spec.get("angle", 0.0))], polarizations=[spec.get("polarization", "unpolarized")],
                reverse=str(spec.get("direction", "normal")).lower() in ("reverse", "back"))
    values, wvls = np.asarray(r["series"][0]["values"]), r["wavelengths"]
    ineq, goal = INEQUALITIES.get(str(spec.get("inequality", "equal")).lower()), float(spec["value"])
    j = int(np.argmax(values) if ineq == "smaller" else np.argmin(values) if ineq == "larger"
            else np.argmax(np.abs(values - goal)))
    row["worst_value"], row["worst_at_nm"] = sig(values[j]), round(float(wvls[j]), 3)
    row["max_abs_deviation"] = max(row["max_abs_deviation"], sig(abs(values[j] - goal)))


def between_points_warning(design, target_specs, grid_rows=None, refine=4):
    """Optima sit on the limits, so a design that meets its targets on their wavelength grid can fail
    between grid points. Re-check spectral targets on a grid `refine` times finer. For a design that
    already fails (grid_rows: its target_report rows), warn when it is worse between the points than on
    them by more than a target's tolerance (scenario S4.10: worst R 98.5 % on a 5 nm grid, 58 % between)."""
    fine, changed = [], False
    for spec in target_specs:
        if "from" in spec and "to" in spec and "wavelength" not in spec:
            step = float(spec.get("step", max((float(spec["to"]) - float(spec["from"])) / 100.0, 0.1)))
            fine.append(dict(spec, step=step / refine))
            changed = True
        else:
            fine.append(spec)
    if not changed:
        return None
    rows, ok = target_report(_evaluator(build_filter(design), build_targets(fine, pad=True)), fine)
    if ok:
        return None
    for spec, row in zip(fine, rows):
        if row["pass_fraction"] < 1 and "from" in spec:
            _refine_worst(design, spec, row, float(spec["step"]))
    if grid_rows is not None and any(r["pass_fraction"] < 1 for r in grid_rows):
        worse = []
        for spec, grid, row in zip(target_specs, grid_rows, rows):
            if "worst_value" not in grid or "worst_value" not in row:
                continue
            ineq = INEQUALITIES.get(str(spec.get("inequality", "equal")).lower())
            excess = (row["worst_value"] - grid["worst_value"] if ineq == "smaller" else
                      grid["worst_value"] - row["worst_value"] if ineq == "larger" else
                      row["max_abs_deviation"] - grid["max_abs_deviation"])
            if excess > float(spec.get("tolerance", 0.01)):
                worse.append(f"target {row['target']} ({row['kind']}) worst {row['worst_value']} at "
                             f"{row['worst_at_nm']} nm (on the grid: {grid['worst_value']} at {grid['worst_at_nm']} nm)")
        if not worse:
            return None
        return (f"worse between the targets' grid points than on them ({refine}x finer grid): {'; '.join(worse)}. "
                "The reported worst values understate the miss; use a finer step.")
    worst = "; ".join(f"target {r['target']} ({r['kind']}) worst {r.get('worst_value')} at {r.get('worst_at_nm')} nm"
                      for r in rows if r["pass_fraction"] < 1)
    return (f"met on the targets' wavelength grid but not between its points ({refine}x finer grid): {worst}. "
            "Use a finer step, or optimize with options margin to keep away from the limits.")


def evaluate(design, target_specs):
    f = build_filter(design)
    targets = build_targets(target_specs, pad=True)
    warnings = material_warnings(design, target_wavelengths(target_specs))  # before computing: n = 0 divides by 0
    optimizer = _evaluator(f, targets)
    rows, all_pass = target_report(optimizer, target_specs)
    out = {"merit_chi2": float(optimizer.get_chi_2()), "all_targets_met": all_pass, "targets": rows,
           "nb_layers": f.get_nb_layers(), "total_thickness_nm": round(float(f.get_total_thickness()), 3)}
    between = between_points_warning(design, target_specs, rows)
    if between:
        warnings.append(between)
    if warnings:
        out["warnings"] = warnings
    return out


METHODS = {"refine": optimization_refinement, "needle": optimization_needles, "step": optimization_steps}


STALL_ITERATIONS = 10000  # iterations at one layer count ...
STALL_GAIN = 0.01  # ... that must lower the merit by at least this fraction, or the run has stalled
STUCK_ITERATIONS = 2000  # needle method: a free layer still under STALLED_LAYER_NM after this many iterations


class _Monitor:
    """Implements OpenFilters' parent.update() hook: enforces limits, detects
    add/remove cycles and stalls, remembers the best design seen, reports progress."""

    def __init__(self, optimizer, deadline, max_layers, max_total_thickness, progress, needles=False):
        self.optimizer, self.progress, self.needles = optimizer, progress, needles
        self.deadline = deadline
        self.max_layers, self.max_total_thickness = max_layers, max_total_thickness
        self.stop_reason, self.last_report = None, 0.0
        self.best_chi2, self.best_layers = float("inf"), None
        self.states, self.last_count = {}, None
        self.checkpoint = None  # (layer count, iteration, chi2) at the start of the current stall window
        self.count_start = None  # (layer count, first iteration seen at that count)

    def checkpoint_start(self, count, iteration):
        if self.count_start is None or self.count_start[0] != count or iteration < self.count_start[1]:
            self.count_start = (count, iteration)
        return self.count_start[1]

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
        # OpenFilters' refinement has no iteration limit (its GUI user presses Stop): with layers at its
        # 0 nm bound (fresh needles) Levenberg-Marquardt can crawl for minutes, each step a little
        # better, without meeting its "change too small" test. Stop when a window of STALL_ITERATIONS
        # at one layer count gains less than STALL_GAIN.
        iteration = o.get_iteration()
        if self.checkpoint is None or self.checkpoint[0] != count or iteration < self.checkpoint[1]:
            self.checkpoint = (count, iteration, chi2)
        elif iteration - self.checkpoint[1] >= STALL_ITERATIONS:
            if chi2 > self.checkpoint[2] * (1 - STALL_GAIN):
                self.stop_reason = self.stop_reason or "stalled"
            self.checkpoint = (count, iteration, chi2)
        # The direct sign of that crawl: a needle still under 1 nm long after it was inserted. The merit
        # window alone missed it on Linux and macOS (CI 2026-11-06: 31-60 s instead of 0.4 s), where the
        # crawl happened to gain a little more than 1 % per window. _continue_needles restarts it at 1 nm.
        if self.needles and iteration - self.checkpoint_start(count, iteration) >= STUCK_ITERATIONS and any(
                not isinstance(t, list) and t < STALLED_LAYER_NM and o.refine_thickness[i]
                for i, t in enumerate(o.front_thickness[:count])):
            self.stop_reason = self.stop_reason or "stalled"
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
    """Front layers of an optimizer's current state as design layers, with their flags (a fixed
    layer must stay fixed in the next round); None for graded layers, which are not tracked."""
    flags = {"refine_thickness": o.refine_thickness, "refine_index": o.refine_index, "preserve_ot": o.preserve_OT,
             "add_needles": o.add_needles_in_layer, "add_steps": o.add_steps_in_layer}
    layers = []
    for i in range(o.nb_front_layers):
        thickness = o.front_thickness[i]
        if isinstance(thickness, list):
            return None
        material = o.materials[o.front_layers[i]]
        layer = {"material": material.get_name(), "thickness": float(thickness)}
        if material.is_mixture():
            layer["index"] = float(o.front_index[i])
        defaults = _default_layer_flags(FRONT, material.is_mixture())
        layer.update({flag: bool(values[i]) for flag, values in flags.items() if bool(values[i]) != defaults[flag]})
        layers.append(layer)
    return layers


def _flat(thicknesses):
    for t in thicknesses:
        if isinstance(t, list):
            yield from t
        else:
            yield t


def _adjustable(layer):
    """A homogeneous layer whose thickness the optimizers may change (not graded, not fixed)."""
    return not layer.get("graded") and layer.get("refine_thickness", True)


def _thin_layers(layers, min_thickness):
    return [i for i, l in enumerate(layers) if _adjustable(l) and l["thickness"] < min_thickness]


def _remove_layer(layers, i, center_wavelength):
    """Remove layer i, giving its optical thickness to its neighbours
    (OpenFilters' rule), then merge identical neighbours."""
    layers = [dict(l) for l in layers]

    def n(layer):
        return layer["index"] if "index" in layer else _material(layer["material"]).get_index(center_wavelength)

    ot = layers[i]["thickness"] * n(layers[i])
    neighbours = [k for k in (i - 1, i + 1) if 0 <= k < len(layers) and _adjustable(layers[k])]
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
    (fewer layers are preferred). Returns (design, chi2, removed, iterations), removed being the
    removed layers in order, each with its material, thickness and position at the time."""
    def clamp(layers):
        return [dict(l, thickness=max(l["thickness"], floor)) if _adjustable(l) else l for l in layers]

    def refine(layers):
        d, chi2, _, it = _run("refine", dict(design, layers=clamp(layers)), targets, options, deadline, progress, floor)
        return d, chi2, it

    current, chi2, iterations = refine(design["layers"])
    removed = []
    while time.time() < deadline and len(current["layers"]) > 1:
        layers = current["layers"]
        # Removing a layer must not leave a fixed layer next to a free one of the same material (merged).
        stuck = [i for i, l in enumerate(layers) if _adjustable(l) and l["thickness"] <= floor * (1 + 1e-6)
                 and not _fixed_layer_merges(layers[max(i - 1, 0):i] + layers[i + 1:i + 2])]
        best = None
        for i in stuck:
            candidate, c_chi2, it = refine(_remove_layer(current["layers"], i, center_wavelength))
            iterations += it
            if best is None or c_chi2 < best[1]:
                best = (candidate, c_chi2, i)
        if best is None or best[1] > chi2 * (1 + 1e-3) + 1e-12:
            break
        i = best[2]
        removed.append({"position": i + 1, "material": layers[i]["material"], "thickness_nm": round(layers[i]["thickness"], 3)})
        current, chi2 = best[:2]
    return current, chi2, removed, iterations


DEADLINE_GRACE = 10.0  # s past the time limit before a stuck solver step is abandoned


class _Overrun(Exception):
    """Raised from inside a solver step that runs past the deadline."""


_GUARDED = {}


def _guarded(cls):
    """The optimizer class with a deadline check in calculate_values, which every Levenberg-Marquardt
    trial step calls. The time limit is otherwise only checked between iterations, and OpenFilters'
    pure-Python solver can loop forever inside one (seen with GDD targets)."""
    if cls not in _GUARDED:
        def calculate_values(self, *args, **kwargs):
            if getattr(self, "_deadline", None) and time.time() > self._deadline:
                raise _Overrun()
            return cls.calculate_values(self, *args, **kwargs)

        def cap_thicknesses(self):
            # OpenFilters leaves thicknesses unbounded above: a layer that is optically invisible (same
            # index as its neighbour, e.g. Ge on a Ge substrate) has no gradient and ran off to 4e16 nm.
            cap = getattr(self, "_max_thickness", None)
            if cap and getattr(self, "optimizer", None) is not None:
                for i, (kind, _) in enumerate(self.parameters):
                    if kind == THICKNESS:
                        self.parameter_max[i] = min(self.parameter_max[i], cap)
                self.optimizer.set_limits(self.parameter_min, self.parameter_max)

        def rebuilding(name):
            # The three places OpenFilters (re)creates its solver with fresh, unbounded limits.
            def method(self, *args, **kwargs):
                out = getattr(cls, name)(self, *args, **kwargs)
                cap_thicknesses(self)
                return out
            return method

        members = {"calculate_values": calculate_values}
        members.update({name: rebuilding(name) for name in ("prepare", "remove_thin_layers_", "add_parameters")
                        if hasattr(cls, name)})
        _GUARDED[cls] = type(cls.__name__, (cls,), members)
    return _GUARDED[cls]


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
    guarded = _guarded(METHODS[method])
    o = guarded.__new__(guarded)
    # Set before construction: OpenFilters' constructor already prepares the solver.
    thicknesses = [l["thickness"] for l in design.get("layers", []) if "thickness" in l and not l.get("graded")]
    o._max_thickness = float(options.get("max_layer_thickness") or max([20000.0] + [10 * t for t in thicknesses]))
    o.__init__(f, targets)
    o._deadline = deadline + DEADLINE_GRACE if deadline else None  # after construction: it evaluates once
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
    monitor = _Monitor(o, deadline, options.get("max_layers"), options.get("max_total_thickness"), progress,
                       needles=method == "needle")
    o.set_parent(monitor)
    try:
        o.go()
    except Exception as error:
        # OpenFilters fails when its thin-layer removal leaves no layer at all (C++ "length must
        # be positive", Python IndexError), or a solver step overran the deadline (_Overrun):
        # keep the best design seen before.
        stop = "time_limit" if isinstance(error, _Overrun) or isinstance(error.__cause__, _Overrun) else "openfilters_error"
        if info is not None and stop == "openfilters_error":
            info["error"] = f"{type(error).__name__}: {error}"
        if monitor.best_layers is None:
            if stop == "time_limit":
                return design, float(_evaluator(build_filter(design), targets).get_chi_2()), stop, o.get_iteration()
            raise EngineError(f"OpenFilters' {method} optimization failed: {error}") from error
        best = dict(design, layers=monitor.best_layers)
        return best, float(_evaluator(build_filter(best), targets).get_chi_2()), stop, o.get_iteration()
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


STOP_REASONS = {
    "converged": "the optimizer could not improve the merit any further (a local optimum)",
    "finished": "the method ran to its end",
    "time_limit": "the time limit was reached; the best design so far is returned",
    "cycling": "the needle/step method kept returning to the same designs, so it was stopped; the best design "
               "seen is returned (normal near an optimum)",
    "stalled": "the optimizer was stopped because it had stalled: its merit improved by less than 1 % over 10000 "
               "iterations, or a needle stayed under 1 nm for 2000 iterations (OpenFilters sets no iteration limit "
               "and would crawl on for minutes); the needle method restarts such a run with those layers at 1 nm, "
               "and this is where that stopped helping; the best design seen is returned",
    "max_layers": "the needle method's next step would have exceeded max_layers; the best design within the limit is "
                  "returned (each needle adds 2 layers and one step can insert several needles, so the result can "
                  "stop several layers below the limit)",
    "max_total_thickness": "the design grew past max_total_thickness; the best design within the limit is returned",
    "min_thickness_not_reached": "not every layer could be brought to min_thickness in the time available",
    "numerical_error": "OpenFilters' optimizer failed numerically (all thicknesses NaN); the best finite design "
                       "is returned",
    "openfilters_error": "OpenFilters' optimizer failed (see error_detail); the best design seen is returned",
}

EDGE_NEEDLE_NM = 1.0
MAX_CONTINUATIONS = 20


STALLED_LAYER_NM = 1.0  # after a stall, free layers thinner than this restart at this thickness


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
    numerical failure (without the 0 nm layers that trigger it), restart a run that ended
    with needles stuck near 0 nm with those layers at STALLED_LAYER_NM, and insert a thin
    layer at the best layer edge while that improves the merit (see _edge_needle).
    Returns (design, chi2, stop, iterations, edge needles inserted)."""
    iterations = inserted = 0
    restarted = False
    lift_failed = None  # the design whose lifted restart did not help (then go on to the edge needle)
    max_layers = options.get("max_layers")
    for _ in range(MAX_CONTINUATIONS):
        if time.time() >= deadline or chi2 <= 0.0:
            break
        stuck = [i for i, l in enumerate(current["layers"]) if _adjustable(l) and l["thickness"] < STALLED_LAYER_NM]
        kind = None
        if stop == "numerical_error" and not restarted:
            restarted, kind = True, "restart"
            layers = _drop_empty_layers(current["layers"])
        elif stop in ("stalled", "converged", "finished") and stuck and lift_failed is not current:
            # Needles inserted at OpenFilters' 0 nm bound can stay there: the solver either crawls
            # ("stalled", minutes) or calls it converged after one iteration with the needles unused.
            # Restarted from STALLED_LAYER_NM it typically converges in well under a second.
            kind = "lift"
            layers = [dict(l, thickness=STALLED_LAYER_NM) if i in stuck else dict(l)
                      for i, l in enumerate(current["layers"])]
        elif stop in ("converged", "finished") and info.get("edge_needle"):
            position, material = info["edge_needle"]
            if max_layers and len(current["layers"]) + 1 > max_layers:
                break
            kind = "edge"
            layers = [dict(l) for l in current["layers"]]
            layers.insert(position, {"material": material, "thickness": EDGE_NEEDLE_NM})
        else:
            break
        previous_info, info = info, {}
        start = dict(current, layers=layers)
        if kind == "lift":
            # Refine the lifted needles first: the needle method adds needles straight away, and at max_layers
            # it stopped before refining them (CI Linux 2026-11-06: 5 layers + 2 needles > 6, "0 iterations",
            # the needles never grew and the result collapsed to one layer).
            refined, r_chi2, _, its = _run("refine", start, targets, options, deadline, progress)
            iterations += its
            if r_chi2 < chi2 * (1 - 1e-4):
                start = refined
        candidate, c_chi2, c_stop, its = _run("needle", start, targets, options, deadline, progress, info=info)
        iterations += its
        if not c_chi2 < chi2 * (1 - 1e-4):
            if kind == "lift":
                lift_failed, info = current, previous_info  # try the edge needle on the unlifted design
                continue
            break
        inserted += kind == "edge"
        current, chi2, stop = candidate, c_chi2, c_stop
    return current, chi2, stop, iterations, inserted


def _unreachable_targets(design, target_specs):
    """T or R targets of 1 (and A targets of 0) cannot be met when a material absorbs: the optimizer
    then slowly thins the absorbing layers towards 0 nm, so a run cut by its time limit depends on CPU speed."""
    warnings = []
    for spec in target_specs:
        kind, value = spec.get("kind"), spec.get("value")
        inequality = str(spec.get("inequality", "equal")).strip()
        wants_one = kind in ("T", "R") and isinstance(value, (int, float)) and value >= 0.9999 and inequality != "<="
        wants_zero = kind == "A" and isinstance(value, (int, float)) and value <= 1e-4 and inequality != ">="
        wavelengths = target_wavelengths([spec])
        if not (wants_one or wants_zero) or not wavelengths:
            continue
        # Only absorption that matters at the target's tolerance (TiO2's k = 4e-6 does not).
        A = min(spectra(design, ["A"], sorted(set(wavelengths)), [float(spec.get("angle", 0.0))],
                        [spec.get("polarization", "unpolarized")])["series"][0]["values"])
        if A <= 0.1 * float(spec.get("tolerance", 0.01)):
            continue
        names = [name for name in _design_materials(design) if name != "void"
                 and max(-z.imag for z in material_N(name, wavelengths)) > 1e-6]
        absorbers = f"the design absorbs A = {A:.3g} there ({', '.join(names)})"
        if names:
            level = (f"{kind} = 1 cannot be met: {absorbers}, so {kind} <= 1 - A. Ask for a reachable level instead, "
                     f"e.g. {kind} >= {math.floor((1 - A) * 100) / 100:g}" if wants_one else
                     f"A = 0 cannot be met: {absorbers}. Ask for a reachable level instead, e.g. A <= "
                     f"{math.ceil(A * 100) / 100:g}")
            warnings.append(f"{level}. Chasing it, the optimizer slowly thins the absorbing layers towards 0 nm (with "
                            "min_thickness it then removes them), and if the time limit comes first the result depends "
                            "on how far it got. Give layers that must stay (e.g. electrodes) \"fixed\": true.")
    return warnings


def _fixed_layer_merges(layers):
    """OpenFilters' optimizers merge adjacent layers of the same material into one, which keeps the
    flags of the lower layer only: a fixed layer next to a free one of the same material is not kept."""
    warnings = []
    for i in range(len(layers) - 1):
        a, b = layers[i], layers[i + 1]
        if a["material"] == b["material"] and not a.get("graded") and not b.get("graded") \
                and a.get("index") == b.get("index") \
                and a.get("refine_thickness", True) != b.get("refine_thickness", True):
            warnings.append(f"layers {i + 1} and {i + 2} are both {a['material']}, one fixed and one not: the optimizer "
                            "merges them into one layer and the fixed thickness is lost. Put another material between "
                            "them, or fix or free both.")
    return warnings


def optimization_description(method, initial_chi2, chi2, all_pass, min_thickness, stop):
    """One line saying how a design was made, stored as the filter description in .ofp projects
    (the format has no fields for the optimizer settings)."""
    text = (f"Optimized with MCP for OpenFilters {__version__} on {time.strftime('%Y-%m-%d')}: {method} method, "
            f"merit chi2 {initial_chi2:.6g} -> {chi2:.6g}, all targets met: {'yes' if all_pass else 'no'}")
    if min_thickness:
        text += (f", minimum layer thickness {min_thickness:g} nm enforced by the connector "
                 "(MinimumThickness left at 0: OpenFilters' needle and step methods cycle with it)")
    return text + f", stop: {stop}."


def _no_steps_warning(design, center_wavelength):
    """The step method added nothing: say so, and which layers ended at a bound of their mixture's range."""
    at_bounds = []
    for i, layer in enumerate(design["layers"]):
        material = _material(layer["material"])
        if material.is_mixture() and layer.get("index") is not None:
            lo, hi = material.get_index_range(center_wavelength)
            for bound, word in ((lo, "lowest"), (hi, "highest")):
                if abs(layer["index"] - bound) <= 1e-4 * bound:
                    at_bounds.append(f"layer {i + 1} is at the {word} index of {layer['material']} ({bound:.4f})")
    text = "The step method added no steps: this is a local optimum of the starting design"
    if at_bounds:
        text += " (" + "; ".join(at_bounds) + ")"
    return (text + ", not necessarily a limit of the materials. Steps need room: start from a thicker mixture "
            "layer (several hundred nm) or a few layers, or use the needle method.")


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
    spec_targets = build_targets(target_specs, pad=True)
    margin = float(options.get("margin") or 0.0)
    targets = build_targets(with_margin(target_specs, margin), pad=True) if margin else spec_targets
    # Invisible layers are judged on the result, not on the start (a random start's Ge next to a Ge substrate
    # said nothing about the optimized design it was reported with).
    warnings = material_warnings(design, target_wavelengths(target_specs), invisible=False)
    start = time.time()
    deadline = start + float(options.get("time_limit", 300))
    design = design_from_filter(build_filter(design))
    warnings += _fixed_layer_merges(design["layers"]) + _unreachable_targets(design, target_specs)
    limit = options.get("max_total_thickness")
    total = sum(l["thickness"] for l in design["layers"])
    if limit and total >= 0.98 * float(limit):
        warnings.append(f"the start is {total:.1f} nm thick, at max_total_thickness ({float(limit):g} nm): the "
                        "optimizer stops as soon as the design grows past the limit (a stop, not a constraint), so it "
                        "may barely move. Start from a thinner design or raise the limit.")
    cw = design["center_wavelength"]
    initial = _evaluator(build_filter(design), spec_targets).get_chi_2()

    info = {}
    edge_needles = 0
    current, chi2, stop, iterations = _run(method, design, targets, options, deadline, progress, info=info)
    error_detail = info.get("error")
    if method == "needle":
        current, chi2, stop, its, edge_needles = _continue_needles(
            current, chi2, stop, info, targets, options, deadline, progress)
        iterations += its
    removed, rounds = [], 1
    if min_thickness:
        best = None
        for rounds in range(1, 6):
            clean, clean_chi2, round_removed, its = _enforce_min_thickness(
                current, targets, options, deadline, progress, min_thickness, cw)
            iterations += its
            if best is None or clean_chi2 < best[1] * (1 - 1e-3):
                best = (clean, clean_chi2, (best[2] if best else []) + round_removed)
            else:
                break  # another round did not improve the clean design
            if method == "refine" or time.time() >= deadline:
                break
            current, chi2, stop, it = _run(method, best[0], targets, options, deadline, progress)
            iterations += it
        current, chi2, removed = best
        if removed:
            names = "; ".join(f"{r['material']} ({r['thickness_nm']:g} nm, then layer {r['position']})" for r in removed)
            warnings.append(f"{len(removed)} layer(s) held at min_thickness were removed because the merit was no "
                            f"worse without them: {names}. Give a layer \"fixed\": true to keep it.")
        if _thin_layers(current["layers"], min_thickness * (1 - 1e-9)):
            stop = "min_thickness_not_reached"

    # 0 nm layers (left at OpenFilters' lower bound) are optically invisible: drop them.
    kept = _drop_empty_layers(current["layers"])
    if kept and len(kept) < len(current["layers"]):
        zeros = sum(1 for l in current["layers"] if not l.get("graded") and l["thickness"] < 1e-3)
        merged = len(current["layers"]) - zeros - len(kept)
        warnings.append(f"{zeros} layer(s) ended at 0 nm and were removed"
                        + (f", merging the {merged} pair(s) of identical layers they separated" if merged else "")
                        + f": {len(current['layers'])} layers before, {len(kept)} now ({len(design['layers'])} at "
                        "the start)")
    current = dict(current, layers=kept or current["layers"])
    wavelengths = target_wavelengths(target_specs)
    if wavelengths:
        warnings += _invisible_layers(current, (min(wavelengths) + max(wavelengths)) / 2)
    f = build_filter(current)
    final = _evaluator(f, spec_targets)
    rows, all_pass = target_report(final, target_specs)
    between = between_points_warning(current, target_specs, rows)
    if between:
        warnings.append(between)
    thicknesses = [l["thickness"] for l in current["layers"] if not l.get("graded")]
    if thicknesses and max(thicknesses) == 0:
        warnings.append("every layer went to 0 nm: from this start the bare substrate was better than any "
                        "coating the optimizer found; try another start")
    elif not min_thickness and thicknesses and min(thicknesses) < 1.0:
        warnings.append(f"the thinnest layer is {min(thicknesses):.3g} nm, below what can be deposited; pass "
                        "options min_thickness (e.g. 5 nm) to keep layers manufacturable")
    misses = [r["max_abs_deviation"] for r in rows if r["pass_fraction"] < 1]
    tols = [float(t.get("tolerance", 0.01)) for t in target_specs]  # same units as max_abs_deviation
    if misses and max(misses) <= 0.01 * min(tols):
        warnings.append(f"the targets are missed only by up to {max(misses):.3g}: the optimum sits on the limits. "
                        "Optimize with options margin (e.g. a tenth of the tolerance) to land inside them, which "
                        "also raises the yield")
    if method == "step" and not all_pass and len(current["layers"]) <= len(design["layers"]):
        warnings.append(_no_steps_warning(current, cw))
    if time.time() >= deadline and stop not in ("max_layers", "max_total_thickness", "min_thickness_not_reached"):
        stop = "time_limit"
    current = dict(current, description=optimization_description(
        method, initial, final.get_chi_2(), all_pass, min_thickness, stop))
    return {"method": method, "initial_chi2": float(initial), "merit_chi2": float(final.get_chi_2()),
            "all_targets_met": all_pass, "targets": rows, "iterations": iterations, "stop_reason": stop,
            "nb_layers": f.get_nb_layers(), "total_thickness_nm": round(float(f.get_total_thickness()), 3),
            "thinnest_layer_nm": round(min(thicknesses), 3) if thicknesses else None,
            "thin_layers_removed": len(removed), **({"removed_layers": removed} if removed else {}),
            "edge_needles_added": edge_needles,
            "rounds": rounds if min_thickness else 1,
            "elapsed_s": round(time.time() - start, 2), "stop_explanation": STOP_REASONS.get(stop, ""),
            **({"warnings": warnings} if warnings else {}), **({"error_detail": error_detail} if error_detail else {}),
            "design": current}


# ---------------------------------------------------------------------------
# Tolerance analysis
# ---------------------------------------------------------------------------

def tolerance_errors(thickness_error=None, index_error=None):
    """The error model of a tolerance analysis, validated: (kind, sigma, distribution, systematic, index sigma)."""
    thickness_error = dict(thickness_error or {"type": "relative", "sigma": 0.01})
    _check_keys(thickness_error, {"type", "sigma", "distribution", "systematic"}, "thickness_error")
    index_error = dict(index_error or {})
    _check_keys(index_error, {"sigma", "per_material"}, "index_error")
    kind = thickness_error.get("type", "relative")
    dist = thickness_error.get("distribution", "normal")
    if kind not in ("relative", "absolute"):
        raise EngineError('thickness_error type must be "relative" (sigma a fraction) or "absolute" (sigma in nm)')
    if dist not in ("normal", "uniform"):
        raise EngineError('thickness_error distribution must be "normal" or "uniform"')
    values = []
    for name, obj, key, default in (("thickness_error sigma", thickness_error, "sigma", 0.01),
                                    ("thickness_error systematic", thickness_error, "systematic", 0.0),
                                    ("index_error sigma", index_error, "sigma", 0.0)):
        try:
            value = float(obj.get(key, default))
        except (TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or (key == "sigma" and value < 0):
            raise EngineError(f"{name} must be a finite number{' >= 0' if key == 'sigma' else ''}")
        values.append(value)
    return kind, values[0], dist, values[1], values[2]


def check_tolerance(design, target_specs, thickness_error=None, index_error=None, **_):
    """Everything a tolerance analysis needs, checked before a job is created (a bad design or error
    model then fails at once instead of in every task). Returns the analysis' warnings."""
    tolerance_errors(thickness_error, index_error)
    build_targets(target_specs, pad=True)
    require_extrapolation_choice(design, target_wavelengths(target_specs), "This tolerance analysis")
    return material_warnings(design, target_wavelengths(target_specs)) + \
        _mixture_index_note(design_from_filter(build_filter(design)), index_error)


def _mixture_index_note(nominal, index_error):
    if not float((index_error or {}).get("sigma", 0.0) or 0.0):
        return []
    mixtures = sorted({l["material"] for key in ("layers", "back_layers") for l in nominal.get(key) or []
                       if _material(l["material"]).is_mixture()})
    return [f"index_error does not perturb mixtures ({', '.join(mixtures)}): only their thicknesses vary"] if mixtures else []


def tolerance(design, target_specs, n_trials=200, thickness_error=None, index_error=None, seed=0, keep_values=False):
    """Monte Carlo yield: perturb thicknesses (and optionally material
    indices) and count designs meeting all targets.

    thickness_error: {"type": "relative"|"absolute", "sigma": 0.01 (or nm),
                      "distribution": "normal"|"uniform", "systematic": 0.0}
    index_error: {"sigma": 0.005 (relative), "per_material": true}
    A graded layer is scaled as a whole (every step by one drawn factor: a deposition-rate error); an
    absolute error applies to its total thickness. Mixtures' indices are not perturbed.
    """
    kind, sigma, dist, systematic, idx_sigma = tolerance_errors(thickness_error, index_error)
    thickness_error = dict(thickness_error or {"type": "relative", "sigma": 0.01})
    rng = np.random.default_rng(seed)
    base = build_filter(design)
    nominal = design_from_filter(base)
    targets = build_targets(target_specs, pad=True)
    warnings = material_warnings(design, target_wavelengths(target_specs))  # refuses unphysical indices
    warnings += _mixture_index_note(nominal, index_error)
    nominal_eval = _evaluator(base, targets)
    _, nominal_pass = target_report(nominal_eval, target_specs)

    # Both faces are perturbed (independently drawn errors per layer).
    sides = [key for key in ("layers", "back_layers") if nominal.get(key)]
    materials_used = sorted({l["material"] for key in sides for l in nominal[key]})

    def total(layer):
        return sum(layer["graded"]["step_thicknesses"]) if layer.get("graded") else layer["thickness"]

    def perturbed(layer, new):
        new = max(float(new), 0.0)
        if not layer.get("graded"):
            return dict(layer, thickness=new)
        factor = new / total(layer) if total(layer) else 1.0
        steps = [d * factor for d in layer["graded"]["step_thicknesses"]]
        return dict(layer, thickness=sum(steps), graded=dict(layer["graded"], step_thicknesses=steps))

    def draw(size):
        if dist == "uniform":
            return rng.uniform(-sigma, sigma, size)
        return rng.normal(0.0, sigma, size)

    merits, passes, per_target_fail = [], 0, np.zeros(len(target_specs))
    for _ in range(int(n_trials)):
        trial = dict(nominal)
        for key in sides:
            d = np.array([total(l) for l in nominal[key]])
            e = draw(len(d))
            if kind == "relative":
                d = d * (1 + e + systematic)
            else:
                d = d + e + systematic * d
            trial[key] = [perturbed(l, t) for l, t in zip(nominal[key], d)]
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
    out = {**extra, "n_trials": int(n_trials), "passes": int(passes), "yield": passes / float(n_trials),
           "nominal_passes": nominal_pass,
           "nominal_chi2": float(nominal_eval.get_chi_2()),
           "chi2_median": float(np.median(merits)), "chi2_p90": float(np.percentile(merits, 90)),
           "target_failure_rate": [round(float(x) / n_trials, 4) for x in per_target_fail],
           "thickness_error": thickness_error, "index_error": index_error, "seed": seed}
    if warnings:
        out["warnings"] = warnings
    return out


RANDOM_ERROR_TYPES = {
    "R": of_data_holder.REFLECTION, "T": of_data_holder.TRANSMISSION, "A": of_data_holder.ABSORPTION,
    "R_phase": of_data_holder.REFLECTION_PHASE, "T_phase": of_data_holder.TRANSMISSION_PHASE,
    "R_GD": of_data_holder.REFLECTION_GD, "T_GD": of_data_holder.TRANSMISSION_GD,
    "R_GDD": of_data_holder.REFLECTION_GDD, "T_GDD": of_data_holder.TRANSMISSION_GDD,
    "color": of_data_holder.COLOR,
}
RANDOM_ERRORS_TIME_LIMIT = 50.0  # s: clients time out tool calls at about 60 s


def random_errors(design, quantities=("R", "T"), wavelengths=None, angle=0.0, polarization="s",
                  thickness_error=None, nb_tests=100, illuminant="CIE-D65", observer="CIE-1931", seed=0,
                  time_limit=RANDOM_ERRORS_TIME_LIMIT):
    """OpenFilters' Preproduction > Simulate random errors, run by OpenFilters' own class: every front layer
    thickness is drawn around its design value (relative: error = fraction x thickness; absolute: nm),
    uniform in +/- error or normal with sigma = error, clipped at 0; a graded layer shifts every step by
    one drawn error / number of steps. Returns the design curve and, per wavelength, the mean, standard
    deviation, minimum and maximum over the tests and every test; colour as Lab with the standard
    deviation and the worst case in Delta E 2000."""
    quantities = list(dict.fromkeys(quantities))
    unknown = [q for q in quantities if q not in RANDOM_ERROR_TYPES]
    if unknown or not quantities:
        raise EngineError(f"quantities must be among {', '.join(RANDOM_ERROR_TYPES)}")
    pol = polarization_value(polarization)
    if pol not in (S, P) and any(q not in ("R", "T", "A", "color") for q in quantities):
        raise EngineError("phase, GD and GDD need s or p polarization (as in OpenFilters)")
    angle = float(angle)
    if not 0 <= angle < 90:
        raise EngineError("angle must be in [0, 90)")
    nb_tests = int(nb_tests)
    if not 2 <= nb_tests <= 100000:
        raise EngineError("nb_tests must be between 2 and 100000")
    error = dict(thickness_error or {"type": "relative", "value": 0.01})
    _check_keys(error, {"type", "value", "sigma", "distribution"}, "thickness_error")
    if "sigma" in error:  # tolerance_analysis' name for it; the same +/- range for a uniform draw
        if "value" in error and error["value"] != error["sigma"]:
            raise EngineError('thickness_error: give "value" or "sigma", not both')
        error["value"] = error.pop("sigma")
    kind, dist = error.get("type", "relative"), error.get("distribution", "uniform")
    if kind not in ("relative", "absolute"):
        raise EngineError('thickness_error type must be "relative" (value a fraction, 0.01 = 1 %) or "absolute" (nm)')
    if dist not in ("uniform", "normal"):
        raise EngineError('thickness_error distribution must be "uniform" or "normal"')
    try:
        size = float(error.get("value", 0.01))
    except (TypeError, ValueError):
        size = math.nan
    if not math.isfinite(size) or size < 0:
        raise EngineError("thickness_error value must be a finite number >= 0")
    if "color" in quantities:
        if illuminant not in of_color.get_illuminant_names():
            raise EngineError(f"illuminant must be one of {', '.join(of_color.get_illuminant_names())}")
        if observer not in of_color.get_observer_names():
            raise EngineError(f"observer must be one of {', '.join(of_color.get_observer_names())}")

    # The GUI analyses the filter's own wavelengths: the design's wavelength_range, else OpenFilters' default.
    wvls = [float(w) for w in build_filter(design).get_wavelengths()] if wavelengths is None         else _wavelength_list(wavelengths)
    grid, pad = wvls, 0  # GD/GDD: one extra step at each end, as in spectra()
    if any(q in TIME_UNITS for q in quantities) and len(wvls) >= 3:
        steps = np.diff(wvls)
        if steps[0] > 0 and np.allclose(steps, steps[0], rtol=1e-6) and wvls[0] - steps[0] > 0:
            grid, pad = [wvls[0] - float(steps[0])] + list(wvls) + [wvls[-1] + float(steps[0])], 1
    warnings = material_warnings(design, wvls + ([380.0, 780.0] if "color" in quantities else []))
    f = build_filter(design, grid)
    if f.get_nb_layers(FRONT) == 0:
        raise EngineError("the design has no front layers to perturb")
    if design.get("back_layers") and design.get("consider_backside"):
        warnings.append("as in OpenFilters, only the front layers are perturbed; the back layers keep their "
                        "design thicknesses (tolerance_analysis perturbs both faces)")

    analyser = of_preproduction.random_errors(f)
    analyser.set_data_types([RANDOM_ERROR_TYPES[q] for q in quantities])
    analyser.set_angle(angle)
    analyser.set_polarization(pol)
    analyser.set_illuminant(illuminant)
    analyser.set_observer(observer)
    if kind == "relative":
        analyser.set_thickness_error_type(of_preproduction.RELATIVE_THICKNESS)
        analyser.set_relative_thickness_error(size)
    else:
        analyser.set_thickness_error_type(of_preproduction.PHYSICAL_THICKNESS)
        analyser.set_physical_thickness_error(size)
    analyser.set_distribution(of_preproduction.UNIFORM if dist == "uniform" else of_preproduction.NORMAL)
    analyser.set_nb_tests(nb_tests)
    random.seed(seed)  # OpenFilters draws from the random module
    timer = threading.Timer(float(time_limit), analyser.stop)
    started = time.time()
    timer.start()
    try:
        analyser.simulate()
    finally:
        timer.cancel()
    if not analyser.get_mean():
        done = max(int(round(analyser.get_progress() * (nb_tests + 1))) - 1, 0)
        raise EngineError(f"stopped after {time.time() - started:.0f} s with {done} of {nb_tests} tests done; "
                          f"use fewer tests (about {max(done * 9 // 10, 2)}) or fewer wavelengths")

    keep = slice(pad, pad + len(wvls))
    expected, mean, std = analyser.get_expected_result(), analyser.get_mean(), analyser.get_std_dev()
    lows, highs = analyser.get_min_max()
    results = analyser.get_results()
    out = {"wavelengths": list(wvls), "angle": angle, "polarization": polarization_name(pol),
           "thickness_error": {"type": kind, "value": size, "distribution": dist}, "nb_tests": nb_tests,
           "seed": seed, "series": [], "seconds": round(time.time() - started, 2)}
    for i, q in enumerate(quantities):
        if q == "color":
            item = {"quantity": "color", "illuminant": illuminant, "observer": observer}
            for j, side in enumerate(("R", "T")):
                design_Lab = list(expected[i][j].Lab())
                worst_Lab = list(highs[i][j].Lab())
                item[side] = {"design_Lab": design_Lab, "mean_Lab": list(mean[i][j].Lab()),
                              "std_dev_delta_E2000": float(std[i][j]), "worst_Lab": worst_Lab,
                              "worst_delta_E2000": float(of_color.Delta_E_2000(worst_Lab, design_Lab)),
                              "delta_E2000_tests": [float(of_color.Delta_E_2000(list(r[j].Lab()), design_Lab))
                                                    for r in results[i]]}
            out["series"].append(item)
            continue
        scale = TIME_UNITS.get(q, 1.0)

        def column(values):
            return np.asarray([values[k] for k in range(len(grid))], dtype=float)[keep] * scale
        out["series"].append({"quantity": q, "design": column(expected[i]), "mean": column(mean[i]),
                              "std_dev": column(std[i]), "min": column(lows[i]), "max": column(highs[i]),
                              "tests": [column(r) for r in results[i]]})
    return _with_warnings(out, warnings)


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


def worst_margin(optimizer, specs):
    """Smallest distance to a limit over all target points, in units of each point's tolerance:
    value - limit for ">=", limit - value for "<=", tolerance - |deviation| for "equal". Positive = met.
    Unlike the merit, it still changes when a design meets its inequality targets with room to spare."""
    calc = np.array(optimizer.all_calculated_values, dtype=float)
    goal = np.array(optimizer.all_target_values, dtype=float)
    tol = np.array(optimizer.all_tolerances, dtype=float)
    ineq = np.array(optimizer.all_inequalities)
    margin = np.where(ineq > 0, calc - goal, np.where(ineq < 0, goal - calc, tol - np.abs(calc - goal))) / tol
    starts = list(optimizer.target_starting_position) + [len(calc)]
    counted = np.zeros(len(calc), dtype=bool)
    for i, spec in enumerate(specs):
        pads = 1 if padded(spec) else 0
        counted[starts[i] + pads:starts[i + 1] - pads] = True
    values = margin[counted]
    return float(np.min(values)) if values.size and np.all(np.isfinite(values)) else float("nan")


def sensitivity(design, target_specs, relative_step=0.01):
    """Merit and worst-margin change when each layer thickness is changed by +/- relative_step
    (the worse of the two directions). A graded layer is scaled as a whole (every step)."""
    nominal = design_from_filter(build_filter(design))
    targets = build_targets(target_specs, pad=True)
    material_warnings(design, target_wavelengths(target_specs))  # refuses unphysical indices
    evaluated = _evaluator(build_filter(nominal), targets)
    base, base_margin = evaluated.get_chi_2(), worst_margin(evaluated, target_specs)
    rows = []
    for key, side in (("layers", "front"), ("back_layers", "back")):
        if key == "back_layers" and not nominal.get("consider_backside"):
            continue  # the back face does not enter the calculation
        for i, layer in enumerate(nominal.get(key) or []):
            chi2s, margins = [], []
            for sign in (1, -1):
                layers = [dict(l) for l in nominal[key]]
                factor = 1 + sign * relative_step
                if layer.get("graded"):
                    steps = [d * factor for d in layer["graded"]["step_thicknesses"]]
                    layers[i] = dict(layer, thickness=sum(steps), graded=dict(layer["graded"], step_thicknesses=steps))
                else:
                    layers[i]["thickness"] = layer["thickness"] * factor
                o = _evaluator(build_filter(dict(nominal, **{key: layers})), targets)
                chi2s.append(o.get_chi_2())
                margins.append(worst_margin(o, target_specs))
            row = {"side": side, "#": i + 1, "material": layer["material"], "thickness_nm": layer["thickness"],
                   "delta_chi2": sig(max(chi2s) - base), "delta_worst_margin": sig(min(margins) - base_margin)}
            if layer.get("graded"):
                row.update(graded=True, n_steps=len(layer["graded"]["step_thicknesses"]))
            rows.append(row)
    out = {"base_chi2": float(base), "base_worst_margin": sig(base_margin), "relative_step": relative_step,
           "layers": rows}
    # Rank by the merit, or by the margin when no layer changes the merit (targets met with room to spare).
    by_chi2 = any(r["delta_chi2"] > 0 for r in rows)
    ranked = sorted(rows, key=lambda r: -r["delta_chi2"] if by_chi2 else r["delta_worst_margin"])
    out["most_sensitive"] = [f"{r['side']} #{r['#']}" for r in ranked[:5]]
    if not by_chi2:
        out["note"] = ("No thickness change of this size changes the merit (the design meets its inequality "
                       "targets with room to spare), so layers are ranked by how much they reduce the worst margin.")
    return out


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
        unit = TIME_UNITS.get(short, 1.0)
        spec.update(wavelength=w[0], value=v[0] * unit, tolerance=d[0] * unit)
    elif t.get_kind() in of_targets.SPECTRUM_TARGETS:
        start, stop, step, w, v, d = t.get_points()
        unit = TIME_UNITS.get(short, 1.0)
        spec.update({"from": start, "to": stop, "step": step,
                     "points": [[a, b * unit, c * unit] for a, b, c in zip(w, v, d)]})
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


# Characters missing from Latin-1 (the encoding of .ofp files) that have a readable equivalent.
_LATIN1_EQUIVALENTS = {
    "–": "-", "—": "-", "−": "-", "‐": "-", "‘": "'", "’": "'", "“": '"',
    "”": '"', "…": "...", "•": "*", "≤": "<=", "≥": ">=", "≈": "~", "≠": "!=",
    "→": "->", "←": "<-", "∞": "inf", "∇": "nabla", "∂": "d", "√": "sqrt",
    "⁰": "0", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁺": "+", "⁻": "-", "₀": "0", "₁": "1", "₂": "2", "₃": "3", "μ": "µ",
    "Ω": "Ohm", "‰": " per mille", "€": "EUR",
}
_GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho final_sigma "
          "sigma tau upsilon phi chi psi omega").split()


def latin1_text(text):
    """Text that .ofp files (Latin-1) can hold: Greek letters spelled out (λ -> lambda), dashes,
    quotes and common symbols replaced, anything else '?'."""
    out = []
    for char in str(text):
        if ord(char) < 256:
            out.append(char)
        elif char in _LATIN1_EQUIVALENTS:
            out.append(_LATIN1_EQUIVALENTS[char])
        elif 0x391 <= ord(char) <= 0x3a9 and ord(char) != 0x3a2:
            out.append(_GREEK[ord(char) - 0x391].capitalize())
        elif 0x3b1 <= ord(char) <= 0x3c9:
            out.append(_GREEK[ord(char) - 0x3b1].replace("final_", ""))
        else:
            out.append("?")
    return "".join(out)


def save_project(path, designs, target_specs=None, comment=""):
    """Write an .ofp project. Designs without a wavelength_range get one covering the targets,
    so the GUI analyses the region that matters."""
    p = of_project.project(catalog())
    if comment:
        p.set_comment(latin1_text(comment))
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


def gui_extrapolation_notes(designs, target_specs=None):
    """The OpenFilters GUI always extrapolates tables (it has no table_extrapolation): say which saved
    designs it will show differently, over the range it analyses (the design's or the targets')."""
    notes = []
    targets_range = target_wavelength_range(target_specs)
    for i, design in enumerate(designs):
        if not isinstance(design, dict) or extrapolation_mode(design) == "openfilters":
            continue
        r = design.get("wavelength_range") or targets_range
        if not r:
            continue
        names = sorted(name for name, _, _ in outside_data(design, [float(r["from"]), float(r["to"])]))
        if names:
            notes.append(f"design {i + 1}: {', '.join(names)} are used beyond their data in its wavelength range; "
                         f"the connector uses table_extrapolation \"{extrapolation_mode(design)}\" there, but the "
                         "OpenFilters GUI extrapolates, so it will show different results there (table_extrapolation "
                         "is not stored in .ofp files)")
    return notes


def acceleration_status():
    from openfilters_core import abeles, moremath
    return {"cpp_core_abeles": abeles.get_abeles_dll_import_success(),
            "cpp_core_moremath": moremath.get_moremath_dll_import_success()}
