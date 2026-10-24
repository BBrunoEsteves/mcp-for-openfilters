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
    if n_series >= 2 or legend_title:
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
    quantities get their own figure (never a dual axis)."""
    groups = {}
    for s in result["series"]:
        groups.setdefault(UNITS[s["quantity"]] if s["quantity"] not in "RTA" else "R/T/A", []).append(s)
    images = []
    for ylabel, series in groups.items():
        fig, ax = _figure(title, "Wavelength (nm)", ylabel)
        for i, s in enumerate(series[:MAX_SERIES]):
            label = f"{s['quantity']} {s['angle']:g}° {s['polarization']}"
            # Line style by polarization keeps coincident curves (e.g. s and p at 0°) visible.
            style = {"unpolarized": "-", "s": "--", "p": "-."}.get(s["polarization"], ":")
            ax.plot(result["wavelengths"], s["values"], color=SERIES[i], linewidth=1.8, linestyle=style, label=label)
        if ylabel == "R/T/A":
            _photometric_axis(ax, np.concatenate([np.asarray(x["values"]) for x in series[:MAX_SERIES]]))
        if len(series) > MAX_SERIES:
            ax.text(0.99, 0.01, f"{len(series) - MAX_SERIES} more series not drawn", transform=ax.transAxes,
                    ha="right", va="bottom", fontsize=7, color=INK_2)
        images.append(_png(fig, ax, min(len(series), MAX_SERIES)))
    return images


def curves(x, ys, title, xlabel, ylabel, boundaries=None):
    """ys: {label: values}. Optional vertical layer boundaries."""
    fig, ax = _figure(title, xlabel, ylabel)
    for b in boundaries or []:
        ax.axvline(b, color=GRID, linewidth=0.8, zorder=0)
    for i, (label, values) in enumerate(list(ys.items())[:MAX_SERIES]):
        ax.plot(x[:len(values)], values, color=SERIES[i], linewidth=1.8, label=label)
    return _png(fig, ax, min(len(ys), MAX_SERIES))


def index_profile(design_rows, title="Index profile"):
    """Step plot of n at the center wavelength versus depth from the substrate."""
    fig, ax = _figure(title, "Distance from substrate (nm)", "n at center wavelength")
    depth = 0.0
    xs, ns = [], []
    for row in design_rows:
        if "n@cw" not in row:
            continue
        xs += [depth, depth + row["thickness_nm"]]
        ns += [row["n@cw"], row["n@cw"]]
        depth += row["thickness_nm"]
    ax.plot(xs, ns, color=SERIES[0], linewidth=1.8)
    return _png(fig, ax, 1)


def design_vs_targets(wavelengths, curves, targets, title, ylabel="R / T / A", photometric=True):
    """Spectra of one or more designs with their targets drawn on top. curves: [(label, quantity, values,
    is_reference)]; targets: target dicts of the plotted quantities. photometric=False for phase, GD
    and GDD (axis scaled to the data instead of 0-1)."""
    fig, ax = _figure(title, "Wavelength (nm)", ylabel)
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
