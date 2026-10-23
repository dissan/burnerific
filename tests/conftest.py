"""The protocol and pellet packages have no Home Assistant imports, so test them directly.

The Home Assistant tests in tests/ha need pytest-homeassistant-custom-component
(the devcontainer has it) and are skipped where it is not installed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Appended, not prepended: the integration has a calendar.py that must not shadow the stdlib module.
sys.path.append(str(Path(__file__).resolve().parents[1] / "custom_components" / "burnerific"))

collect_ignore = [] if importlib.util.find_spec("pytest_homeassistant_custom_component") else ["ha"]
