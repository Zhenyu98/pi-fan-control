"""Pytest path setup: expose src/ (runtime) and tools/ (research) as importable."""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ("src", "tools"):
    _path = os.path.join(_ROOT, _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)
