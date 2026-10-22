import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "src", "openfilters_core")

# OpenFilters uses flat imports; expose its directories (temporary until the
# core is converted to a proper package).
for sub in ["", "abeles", "color", "config", "materials", "modules", "moremath"]:
    path = os.path.join(CORE, sub)
    if path not in sys.path:
        sys.path.append(path)
sys.path.insert(0, HERE)
