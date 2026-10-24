"""Search the refractiveindex.info database and convert its entries to n,k tables.

The database (https://refractiveindex.info, M. N. Polyanskiy, public domain CC0) is read
on demand from its GitHub repository: nothing is bundled. The catalogue is cached in
the data folder; a page is downloaded only when the user imports it.
"""

import html
import os
import re
import time
import urllib.parse
import urllib.request

import numpy as np
import yaml

from . import settings
from .engine import EngineError

BASE = "https://raw.githubusercontent.com/polyanskiy/refractiveindex.info-database/main/database/"
CATALOG_MAX_AGE = 30 * 86400  # seconds
MAX_ROWS = 2000

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_entries_cache = {}


def _fetch(path):
    try:
        # Page ids can contain spaces ("Rodriguez-de Marcos"): percent-encode them for the URL.
        with urllib.request.urlopen(BASE + urllib.parse.quote(path, safe="/"), timeout=30) as response:
            return response.read().decode("utf-8")
    except Exception as error:  # URLError, HTTPError, timeout
        raise EngineError(f"cannot download {path} from the refractiveindex.info database: {error}") from error


def _plain(text):
    """Catalogue names carry HTML (Ta<sub>2</sub>O<sub>5</sub>)."""
    return html.unescape(re.sub(r"<[^>]+>", "", str(text))).strip()


def _catalog_path():
    folder = os.path.join(settings.home(), "cache")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "refractiveindex-catalog-nk.yml")


def entries(refresh=False):
    """Every page of the n,k catalogue, flattened: page id, shelf, book,
    material and source names, and the category (e.g. thin film or bulk)."""
    path = _catalog_path()
    stale = not os.path.exists(path) or time.time() - os.path.getmtime(path) > CATALOG_MAX_AGE
    if refresh or stale:
        try:
            text = _fetch("catalog-nk.yml")
        except EngineError:
            if not os.path.exists(path):
                raise
        else:  # offline: keep using the cached copy
            with open(path, "w", encoding="utf-8") as outfile:
                outfile.write(text)
    mtime = os.path.getmtime(path)
    if _entries_cache.get("mtime") != mtime:
        with open(path, encoding="utf-8") as infile:
            _entries_cache.update(mtime=mtime, entries=flatten(yaml.load(infile, Loader=_Loader)))
    return _entries_cache["entries"]


def flatten(tree):
    out = []
    for shelf in tree or []:
        if "SHELF" not in shelf:
            continue
        for book in shelf.get("content") or []:
            if "BOOK" not in book:
                continue
            category = ""
            for page in book.get("content") or []:
                if "DIVIDER" in page:
                    category = _plain(page["DIVIDER"])
                elif "data" in page:
                    out.append({"page": page["data"].removesuffix(".yml"), "shelf": shelf["SHELF"],
                                "book": str(book["BOOK"]), "material": _plain(book.get("name", book["BOOK"])),
                                "source": _plain(page.get("name", page.get("PAGE", ""))), "category": category})
    return out


def search(query, shelf=None, limit=25, refresh=False):
    tokens = query.lower().split()
    if not tokens:
        raise EngineError("give a material to search for, e.g. 'Ta2O5' or 'silicon nitride'")
    q = query.lower().strip()
    hits = []
    for e in entries(refresh):
        if shelf and e["shelf"] != shelf:
            continue
        text = " ".join((e["book"], e["material"], e["source"], e["category"], e["shelf"])).lower()
        if all(t in text for t in tokens):
            book = e["book"].lower()
            hits.append((0 if book == q else 1 if book.startswith(q) else 2, e))
    hits.sort(key=lambda hit: hit[0])  # stable: catalogue order within a rank
    return {"matches": len(hits), "results": [e for _, e in hits[:max(1, int(limit))]]}


def load_page(page):
    page = page.strip().removesuffix(".yml")
    if ".." in page or not re.fullmatch(r"[\w\-+.(),%/ ]+", page):
        raise EngineError(f"invalid page id {page!r}: use the `page` of a search_refractiveindex result")
    return yaml.load(_fetch(f"data/{page}.yml"), Loader=_Loader)


def formula_n(number, coefficients, wl):
    """Refractive index of the database's dispersion formulas 1-9 (wl in um),
    as in the database's own reader (database/tools/nkexplorer.py)."""
    C = list(coefficients) + [0.0] * (17 - len(coefficients))
    with np.errstate(all="ignore"):
        if number == 1:
            return np.sqrt(1 + C[0] + sum(C[i] / (1 - (C[i + 1] / wl) ** 2) for i in range(1, 17, 2)))
        if number == 2:
            return np.sqrt(1 + C[0] + sum(C[i] / (1 - C[i + 1] / wl ** 2) for i in range(1, 17, 2)))
        if number == 3:
            return np.sqrt(C[0] + sum(C[i] * wl ** C[i + 1] for i in range(1, 17, 2)))
        if number == 4:
            return np.sqrt(C[0] + C[1] * wl ** C[2] / (wl ** 2 - C[3] ** C[4]) + C[5] * wl ** C[6] / (wl ** 2 - C[7] ** C[8])
                           + sum(C[i] * wl ** C[i + 1] for i in range(9, 17, 2)))
        if number == 5:
            return C[0] + sum(C[i] * wl ** C[i + 1] for i in range(1, 11, 2))
        if number == 6:
            return 1 + C[0] + sum(C[i] / (C[i + 1] - wl ** -2.0) for i in range(1, 11, 2))
        if number == 7:
            return C[0] + C[1] / (wl ** 2 - 0.028) + C[2] / (wl ** 2 - 0.028) ** 2 + C[3] * wl ** 2 + C[4] * wl ** 4 + C[5] * wl ** 6
        if number == 8:
            tmp = C[0] + C[1] * wl ** 2 / (wl ** 2 - C[2]) + C[3] * wl ** 2
            return np.sqrt((2 * tmp + 1) / (1 - tmp))
        if number == 9:
            return np.sqrt(C[0] + C[1] / (wl ** 2 - C[2]) + C[3] * (wl - C[4]) / ((wl - C[4]) ** 2 + C[5]))
    raise EngineError(f"unknown dispersion formula {number}")


def to_table(doc, from_nm=None, to_nm=None):
    """Rows [wavelength_nm, n, k] from a database page, on the union of its n
    and k grids where both are defined (k = 0 when the page gives none)."""
    n = k = None
    for data in doc.get("DATA") or []:
        kind = str(data.get("type", "")).split()
        if kind[:1] == ["tabulated"]:
            rows = np.array([[float(x) for x in line.split()] for line in str(data["data"]).splitlines() if line.strip()])
            if kind[1] in ("n", "nk") and n is None:
                n = (rows[:, 0], rows[:, 1])
            if kind[1] == "nk" and k is None:
                k = (rows[:, 0], rows[:, 2])
            if kind[1] == "k" and k is None:
                k = (rows[:, 0], rows[:, 1])
        elif kind[:1] == ["formula"] and n is None:
            lo, hi = (float(x) for x in str(data["wavelength_range"]).split())
            wl = np.geomspace(lo, hi, 301) if hi / lo > 20 else np.linspace(lo, hi, 301)
            coefficients = [float(c) for c in str(data["coefficients"]).split()]
            n = (wl, formula_n(int(kind[1]), coefficients, wl))
    if n is None:
        raise EngineError("this entry gives no refractive index n (only k); choose another entry")
    wl_n, n_values = n
    order = np.argsort(wl_n)
    wl_n, n_values = wl_n[order], n_values[order]
    if k is None:
        grid, n_on, k_on = wl_n, n_values, np.zeros_like(wl_n)
    else:
        wl_k, k_values = k
        order = np.argsort(wl_k)
        wl_k, k_values = wl_k[order], k_values[order]
        lo, hi = max(wl_n[0], wl_k[0]), min(wl_n[-1], wl_k[-1])
        if lo >= hi:
            raise EngineError("the n and k data of this entry do not overlap in wavelength")
        grid = np.union1d(wl_n, wl_k)
        grid = grid[(grid >= lo) & (grid <= hi)]
        n_on, k_on = np.interp(grid, wl_n, n_values), np.interp(grid, wl_k, k_values)
    nm = grid * 1000.0
    keep = np.isfinite(n_on) & np.isfinite(k_on) & (n_on > 0)
    nm, n_on, k_on = nm[keep], n_on[keep], np.abs(k_on[keep])
    if from_nm is not None or to_nm is not None:
        # Keep one point beyond each end so the range is covered by interpolation.
        i0 = max(0, int(np.searchsorted(nm, from_nm, "right")) - 1) if from_nm is not None else 0
        i1 = min(len(nm), int(np.searchsorted(nm, to_nm, "left")) + 1) if to_nm is not None else len(nm)
        nm, n_on, k_on = nm[i0:i1], n_on[i0:i1], k_on[i0:i1]
    if len(nm) > MAX_ROWS:
        idx = np.unique(np.linspace(0, len(nm) - 1, MAX_ROWS).round().astype(int))
        nm, n_on, k_on = nm[idx], n_on[idx], k_on[idx]
    if len(nm) < 2:
        raise EngineError("fewer than 2 data points in the requested wavelength range")
    return [[round(float(w), 4), float(f"{a:.7g}"), float(f"{b:.7g}")] for w, a, b in zip(nm, n_on, k_on)]


def page_url(page):
    parts = page.split("/")
    return f"https://refractiveindex.info/?shelf={parts[0]}&book={parts[1]}&page={parts[-1]}"


def default_name(page):
    parts = page.split("/")
    return re.sub(r"[^\w\-.+]", "_", f"{parts[1]}_{parts[-1]}")


def references(doc):
    return _plain(re.sub(r"<br\s*/?>", "\n", str(doc.get("REFERENCES", "")))).strip()
