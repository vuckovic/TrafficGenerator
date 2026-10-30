# SPDX-License-Identifier: MIT
"""tgen-controller: scenario engine, live feed and history for the TrafficGenerator (docs/08)."""
import sys
from pathlib import Path

__version__ = "0.1.0"

REPO = Path(__file__).resolve().parents[2]
# the planner, analysis, DUT adapters and inventory live in tools/tgen
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))
