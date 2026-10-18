"""The protocol and pellet packages have no Home Assistant imports, so test them directly."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "burnerific"))
