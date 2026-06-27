"""Make the runtime modules in ../src importable when a tool is run directly.

The research/dev tools in this folder depend on the runtime library in src/
(fan_control_core, fan_control_io, ...). When a tool is launched as
``python3 tools/<name>.py`` the interpreter only puts tools/ on sys.path, so
importing it first inserts the sibling src/ directory.
"""
from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
