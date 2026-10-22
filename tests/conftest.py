import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
CORE = os.path.join(SRC, "openfilters_core")

if SRC not in sys.path:  # noqa: SIM102
    sys.path.insert(0, SRC)
sys.path.insert(0, HERE)
