"""PNG figures returned alongside tool results."""

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MAX_SERIES = len(SERIES)

UNITS = {"R": "Reflectance", "T": "Transmittance", "A": "Absorptance", "R_phase": "Phase (deg)",
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


def _png(fig, ax, n_series):
    if n_series >= 2:
        ax.legend(fontsize=8, frameon=False, labelcolor=INK_2)
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
            style = {"s": "-", "p": "--", "unpolarized": ":"}.get(s["polarization"], "-.")
            ax.plot(result["wavelengths"], s["values"], color=SERIES[i], linewidth=1.8, linestyle=style, label=label)
        if ylabel == "R/T/A":
            ax.set_ylim(-0.02, 1.02)
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
