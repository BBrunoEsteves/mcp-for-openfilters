"""PNG figures returned alongside tool results."""

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MAX_SERIES = len(SERIES)

UNITS = {"R": "Reflectance", "T": "Transmittance", "A": "Absorptance", "OD": "Optical density", "R_phase": "Phase (deg)",
         "T_phase": "Phase (deg)", "R_GD": "GD (fs)", "T_GD": "GD (fs)", "R_GDD": "GDD (fs²)", "T_GDD": "GDD (fs²)"}


def quantity_label(quantities):
    """Axis label naming what is drawn: "Reflectance", "Reflectance / Transmittance", "GD (fs)"; never
    the generic "R / T / A"."""
    return " / ".join(dict.fromkeys(UNITS.get(q, q) for q in quantities))


def polarization_label(name, short=False):
    """"s-polarized", "unpolarized", "polarization angle 30°" (short: s, p, unpolarized, ψ 30°) from the
    engine's polarization names ("s", "p", "unpolarized", "30deg")."""
    if name in ("s", "p"):
        return name if short else f"{name}-polarized"
    if name == "unpolarized":
        return name
    angle = str(name).replace("deg", "°")
    return f"ψ {angle}" if short else f"polarization angle {angle}"


def _figure(title, xlabel, ylabel):
    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=110)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.set_xlabel(xlabel, color=INK_2, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    return fig, ax


def _photometric_axis(ax, values):
    """Percent axis, zoomed in when everything is small (e.g. AR coatings)
    so that low-reflectance detail is visible."""
    top = float(np.nanmax(values)) if len(values) else 1.0
    if top < 0.5:
        ax.set_ylim(0.0, max(top * 1.15, 1e-3))
    else:
        ax.set_ylim(-0.02, 1.02)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))


def _png(fig, ax, n_series, legend_title=None):
    if n_series >= 1 or legend_title:
        legend = ax.legend(fontsize=8, frameon=False, labelcolor=INK_2, title=legend_title, title_fontsize=7)
        if legend_title:
            legend.get_title().set_color(INK_2)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buffer.getvalue()


def spectra(result, title="Spectrum"):
    """One axis per unit family: photometric quantities share an axis; other
    quantities get their own figure (never a dual axis). The axis names what is drawn
    (e.g. "Reflectance" for R alone, "R, T" for both) and every curve is in the legend."""
    groups = {}
    for s in result["series"]:
        groups.setdefault(UNITS[s["quantity"]] if s["quantity"] not in "RTA" else "R/T/A", []).append(s)
    images = []
    for ylabel, series in groups.items():
        if ylabel == "R/T/A":
            ylabel_shown = quantity_label(s["quantity"] for s in series)
        else:
            ylabel_shown = ylabel
        fig, ax = _figure(title, "Wavelength (nm)", ylabel_shown)
        # Colour by quantity and angle, line style by polarization: s and p at 7 angles fit in 7 colours,
        # and coincident curves (s and p at 0°) stay visible.
        colours = list(dict.fromkeys((s["quantity"], s["angle"]) for s in series))[:MAX_SERIES]
        drawn = [s for s in series if (s["quantity"], s["angle"]) in colours]
        for s in drawn:
            label = f"{s['quantity']} {s['angle']:g}° {polarization_label(s['polarization'], short=True)}"
            style = {"unpolarized": "-", "s": "--", "p": "-."}.get(s["polarization"], ":")
            ax.plot(result["wavelengths"], s["values"], color=SERIES[colours.index((s["quantity"], s["angle"]))],
                    linewidth=1.8, linestyle=style, label=label)
        if ylabel == "R/T/A":
            _photometric_axis(ax, np.concatenate([np.asarray(x["values"]) for x in drawn]))
        note = f"{len(series) - len(drawn)} more series not drawn" if len(drawn) < len(series) else None
        images.append(_png(fig, ax, len(drawn), legend_title=note))
    return images


def curves(x, ys, title, xlabel, ylabel, boundaries=None):
    """ys: {label: values}. Optional vertical layer boundaries."""
    fig, ax = _figure(title, xlabel, ylabel)
    for b in boundaries or []:
        ax.axvline(b, color=GRID, linewidth=0.8, zorder=0)
    for i, (label, values) in enumerate(list(ys.items())[:MAX_SERIES]):
        ax.plot(x[:len(values)], values, color=SERIES[i], linewidth=1.8, label=label)
    return _png(fig, ax, min(len(ys), MAX_SERIES))


def index_profile(segments, substrate_n, medium_n, center_wavelength, title="Index profile"):
    """Step plot of n at the center wavelength versus distance from the substrate. segments:
    [[thickness, n], ...] for every homogeneous layer and every step of a graded layer; the substrate
    and the incident medium are drawn as short dashed stretches at both ends."""
    fig, ax = _figure(title, "Distance from substrate (nm)", f"n at {center_wavelength:g} nm")
    total = sum(d for d, _ in segments)
    edge = max(total * 0.06, 1.0)
    xs, ns, depth = [], [], 0.0
    for d, n in segments:
        xs += [depth, depth + d]
        ns += [n, n]
        depth += d
    ax.plot([-edge, 0], [substrate_n] * 2, color=INK_2, linewidth=1.4, linestyle="--", label="substrate")
    ax.plot(xs, ns, color=SERIES[0], linewidth=1.6, label="coating")
    ax.plot([total, total + edge], [medium_n] * 2, color=SERIES[1], linewidth=1.4, linestyle="--",
            label="incident medium")
    ax.set_xlim(-edge, total + edge)
    return _png(fig, ax, 3)


def design_vs_targets(wavelengths, curves, targets, title, ylabel=None, photometric=True):
    """Spectra of one or more designs with their targets drawn on top. curves: [(label, quantity, values,
    is_reference)]; targets: target dicts of the plotted quantities. photometric=False for phase, GD
    and GDD (axis scaled to the data instead of 0-1). ylabel: default names the plotted quantities."""
    fig, ax = _figure(title, "Wavelength (nm)", ylabel or quantity_label(c[1] for c in curves))
    quantities, designs = [], []
    for label, quantity, values, reference in curves:
        if quantity not in quantities:
            quantities.append(quantity)
        # One colour per design (labels are "<quantity> <design>"), one line style per quantity.
        design = label.split(" ", 1)[-1]
        if design not in designs:
            designs.append(design)
        color = SERIES[designs.index(design) % MAX_SERIES]
        style = "--" if reference else ("-", "-.", ":")[quantities.index(quantity) % 3]
        ax.plot(wavelengths, values, color=color, linewidth=1.0 if reference else 1.8,
                linestyle=style, alpha=0.6 if reference else 1.0, label=label)
    for target in targets:
        if target.get("kind") not in quantities:
            continue
        color = SERIES[quantities.index(target["kind"]) % MAX_SERIES]
        marker = {">=": "^", "larger": "^", "min": "^", ">": "^",
                  "<=": "v", "smaller": "v", "max": "v", "<": "v"}.get(str(target.get("inequality", "")).lower(), "o")
        if "wavelength" in target:
            ax.plot([target["wavelength"]], [target["value"]], marker=marker, color=INK, markersize=8, linestyle="none")
        elif "points" in target:
            pts = target["points"]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=INK, linewidth=2.5, alpha=0.5)
        elif "from" in target:
            ax.plot([target["from"], target["to"]], [target["value"]] * 2, color=INK, linewidth=2.5, alpha=0.5)
            ax.plot([(target["from"] + target["to"]) / 2], [target["value"]], marker=marker, color=INK,
                    markersize=7, linestyle="none")
    target_values = [t.get("value", 0.0) for t in targets if t.get("kind") in quantities and "value" in t]
    target_values += [p[1] for t in targets if t.get("kind") in quantities for p in t.get("points", [])]
    if photometric:
        _photometric_axis(ax, np.concatenate([np.asarray(c[2]) for c in curves] + [np.asarray(target_values, dtype=float)]))
    note = "grey bars: targets (▲ ≥, ▼ ≤)"
    if any(c[3] for c in curves):
        note += "\ndashed: starting design"
    return _png(fig, ax, len(curves), legend_title=note)


def _draw_targets(ax, targets):
    """Targets as grey bars / markers (▲ ≥, ▼ ≤), as in design_vs_targets."""
    for target in targets:
        marker = {">=": "^", "larger": "^", "min": "^", ">": "^",
                  "<=": "v", "smaller": "v", "max": "v", "<": "v"}.get(str(target.get("inequality", "")).lower(), "o")
        if "wavelength" in target:
            ax.plot([target["wavelength"]], [target["value"]], marker=marker, color=INK, markersize=8, linestyle="none")
        elif "points" in target:
            pts = target["points"]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=INK, linewidth=2.5, alpha=0.5)
        elif "from" in target:
            ax.plot([target["from"], target["to"]], [target["value"]] * 2, color=INK, linewidth=2.5, alpha=0.5)


def random_errors(wavelengths, series, show, targets=(), title="Random errors"):
    """One figure per quantity, drawing what OpenFilters' random error dialog shows: every test, the
    mean, mean +/- k standard deviations, the worst case (minimum and maximum over the tests), the
    design and its targets. show: {"all_results", "mean", "std_devs" (k or None), "worst_case", "design",
    "targets"}."""
    images = []
    for s in series:
        q = s["quantity"]
        fig, ax = _figure(f"{title}\n{UNITS[q]} ({q}), random thickness errors", "Wavelength (nm)", UNITS[q])
        drawn = 0
        if show.get("all_results"):
            for i, values in enumerate(s["tests"]):
                ax.plot(wavelengths, values, color=GRID, linewidth=0.6, label="tests" if i == 0 else None)
            drawn += 1
        if show.get("worst_case"):
            ax.fill_between(wavelengths, s["min"], s["max"], color=SERIES[1], alpha=0.18, linewidth=0,
                            label="worst case (min-max)")
            drawn += 1
        k = show.get("std_devs")
        if k:
            ax.plot(wavelengths, s["mean"] + k * s["std_dev"], color=SERIES[2], linewidth=1.2, linestyle="--",
                    label=f"mean ± {k:g} σ")
            ax.plot(wavelengths, s["mean"] - k * s["std_dev"], color=SERIES[2], linewidth=1.2, linestyle="--")
            drawn += 1
        if show.get("mean"):
            ax.plot(wavelengths, s["mean"], color=SERIES[2], linewidth=1.6, label="mean")
            drawn += 1
        if show.get("design", True):
            ax.plot(wavelengths, s["design"], color=SERIES[0], linewidth=1.8, label="design")
            drawn += 1
        shown_targets = [t for t in targets if t.get("kind") == q] if show.get("targets") else []
        _draw_targets(ax, shown_targets)
        if q in ("R", "T", "A"):
            values = [s["design"], s["min"], s["max"]] + ([s["mean"] + k * s["std_dev"]] if k else [])
            values += [np.asarray([t["value"] for t in shown_targets if "value" in t], dtype=float)]
            _photometric_axis(ax, np.concatenate([np.asarray(v, dtype=float) for v in values]))
        images.append(_png(fig, ax, drawn, legend_title="grey: targets (▲ ≥, ▼ ≤)" if shown_targets else None))
    return images


def monitoring_map(deposited, wavelengths, matrix, boundaries, title, label="Signal"):
    """Spectroscopic (broadband) monitoring: the signal at every wavelength (y) versus deposited
    thickness (x), with the layer ends marked."""
    fig, ax = _figure(title, "Deposited thickness (nm)", "Wavelength (nm)")
    ax.grid(False)
    mesh = ax.pcolormesh(np.asarray(deposited), np.asarray(wavelengths), np.asarray(matrix), shading="auto",
                         cmap="viridis", vmin=0.0, vmax=max(1e-9, float(np.nanmax(matrix))), rasterized=True)
    for b in boundaries:
        ax.axvline(b, color="white", linewidth=0.5, alpha=0.6)
    bar = fig.colorbar(mesh, ax=ax)
    bar.set_label(label, color=INK_2, fontsize=9)
    bar.ax.tick_params(colors=INK_2, labelsize=8)
    return _png(fig, ax, 0)
