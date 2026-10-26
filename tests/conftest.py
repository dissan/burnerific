"""The protocol and pellet packages have no Home Assistant imports, so test them directly.

The Home Assistant tests in tests/ha need pytest-homeassistant-custom-component
(the devcontainer has it) and are skipped where it is not installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "burnerific"))

collect_ignore = [] if importlib.util.find_spec("pytest_homeassistant_custom_component") else ["ha"]
