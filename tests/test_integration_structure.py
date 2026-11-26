"""Structural tests for Hass-NILM manifest, HACS metadata, engine purity, and translations."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "custom_components" / "hass_nilm"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_and_hacs() -> None:
    """Validate manifest.json key ordering, zero requirements, and hacs.json."""
    manifest = _load_json(COMP / "manifest.json")
    hacs = _load_json(ROOT / "hacs.json")

    keys = list(manifest.keys())
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert manifest["domain"] == "hass_nilm"
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == []
    assert hacs["name"] == "NILM Appliance Disaggregator"


def test_engine_has_zero_homeassistant_imports() -> None:
    """Enforce architectural invariant: engine/ must never import homeassistant."""
    engine_dir = COMP / "engine"
    py_files = list(engine_dir.glob("*.py"))
    assert len(py_files) >= 5

    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("homeassistant"), py_file.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("homeassistant"), py_file.name


def test_translations_parity() -> None:
    """Ensure strings.json, en.json, and es.json have identical key structures."""
    strings = _load_json(COMP / "strings.json")
    en = _load_json(COMP / "translations" / "en.json")
    es = _load_json(COMP / "translations" / "es.json")

    assert strings == en
    assert set(strings.keys()) == set(es.keys())
    for platform in ("sensor", "binary_sensor"):
        assert set(strings["entity"][platform].keys()) == set(es["entity"][platform].keys())
    for step_name in ("init", "line_settings", "add_appliance", "remove_appliance"):
        assert set(strings["options"]["step"][step_name].keys()) == set(
            es["options"]["step"][step_name].keys()
        )
    assert set(strings["options"]["error"].keys()) == set(es["options"]["error"].keys())


def test_entity_translation_keys_exist() -> None:
    """Every translation_key in sensor.py and binary_sensor.py exists in strings.json."""
    strings = _load_json(COMP / "strings.json")
    for fname, plat in (("sensor.py", "sensor"), ("binary_sensor.py", "binary_sensor")):
        src = (COMP / fname).read_text(encoding="utf-8")
        keys = set(re.findall(r'translation_key\s*=\s*"([^"]+)"', src))
        assert keys <= set(strings["entity"][plat].keys())
