"""Make pure engine modules importable even when Home Assistant is not installed."""

import importlib.util
import sys
import types
from pathlib import Path

if importlib.util.find_spec("homeassistant") is None:
    _comp_dir = Path(__file__).resolve().parent.parent / "custom_components" / "hass_nilm"
    _pkg = types.ModuleType("custom_components.hass_nilm")
    _pkg.__path__ = [str(_comp_dir)]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules["custom_components.hass_nilm"] = _pkg
