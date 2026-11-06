"""plugin.yaml agrees with Hermes's manifest rules and with register()."""

from __future__ import annotations

from pathlib import Path

import yaml

import onair
from onair import router, sidecar

from test_register import StrictContext

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

# hermes_cli/plugins_manifest.py: the config_schema types Hermes knows.
KNOWN_TYPES = {"str", "string", "int", "integer", "float", "number", "bool", "boolean",
               "list", "array", "dict", "object", "secret"}


def test_required_fields():
    assert MANIFEST["name"] == "onair"
    assert MANIFEST["version"] == "0.1.0" == onair.__version__
    assert isinstance(MANIFEST["description"], str) and MANIFEST["description"].strip()


def test_external_python_runtime():
    assert MANIFEST["python_runtime"] == "external"
    assert "pip_dependencies" not in MANIFEST and "python_dependencies" not in MANIFEST


def test_config_schema_shape():
    schema = MANIFEST["config_schema"]
    assert isinstance(schema, dict) and schema
    for key, spec in schema.items():
        assert isinstance(spec, dict), key
        assert spec.get("type") in KNOWN_TYPES, key
        assert isinstance(spec.get("description"), str), key
        assert isinstance(spec.get("required", False), bool), key


def test_config_schema_matches_the_code():
    schema = MANIFEST["config_schema"]
    assert set(schema) == {"uv_bin", "sidecar_python", "ffmpeg_bin", "language", "voices"}
    assert schema["uv_bin"]["default"] == sidecar.DEFAULT_UV_BIN
    assert schema["sidecar_python"]["default"] == sidecar.DEFAULT_SIDECAR_PYTHON
    assert schema["ffmpeg_bin"]["default"] == router.DEFAULT_FFMPEG_BIN
    assert schema["language"]["default"] == router.DEFAULT_LANGUAGE
    assert schema["voices"] == {**schema["voices"], "type": "dict", "default": {}}
    source = "".join((ROOT / name).read_text(encoding="utf-8") for name in ("cli.py", "router.py"))
    for key in schema:
        assert f'"{key}"' in source, f"{key} is declared but never read"


def test_provides_match_what_register_registers():
    ctx = StrictContext()
    onair.register(ctx)
    assert MANIFEST.get("provides_tools", []) == ctx.tools == []
    assert MANIFEST.get("provides_hooks", []) == ctx.hooks == []
    assert [c["name"] for c in ctx.cli_commands] == ["onair"]
    # Hermes has no provides_* key for slash commands either; the description names it.
    assert [c["name"] for c in ctx.commands] == ["onair"]
    assert "/onair voice" in MANIFEST["description"]
    # Hermes has no provides_* key for TTS providers; the description names it.
    assert [p.name for p in ctx.tts_providers] == ["onair"]
    assert "tts.provider: onair" in MANIFEST["description"]
