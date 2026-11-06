"""AGENTS.md hard rules that a static scan can hold."""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

from onair import adapter

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".idd", "__pycache__", ".venv", "venv", "node_modules",
             ".ruff_cache", ".pytest_cache", ".mypy_cache"}
CODE_AND_CONFIG = {".py", ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini", ".json"}
HOST_MODULES = sorted(p for p in ROOT.glob("*.py"))
# The only non-stdlib imports the host side may make: Hermes's public storage
# helper and the TTS provider base class.
HERMES_IMPORTS = {"cli.py": {"plugins.plugin_storage"}, "router.py": {"agent.tts_provider"}}


def repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in CODE_AND_CONFIG:
                yield path


def imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level


def strings_outside_docstrings(tree: ast.AST):
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        is_text = isinstance(node, ast.Constant) and isinstance(node.value, str)
        if is_text and id(node) not in docstrings:
            yield node.value


def test_the_pin_is_written_once_in_code_and_config():
    pin = adapter.DROPCAST_PIN
    hits = []
    for path in repo_files():
        count = path.read_text(encoding="utf-8", errors="replace").count(pin)
        hits += [path.relative_to(ROOT).as_posix()] * count
    assert hits == ["adapter.py"]


def test_only_adapter_imports_dropcast():
    for path in repo_files():
        if path.suffix != ".py" or path.name == "adapter.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module, _ in imports(tree):
            assert module.split(".")[0] != "dropcast", path


def test_other_host_modules_never_name_dropcast_in_code():
    for path in HOST_MODULES:
        if path.name == "adapter.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for value in strings_outside_docstrings(tree):
            assert "dropcast" not in value.lower(), (path.name, value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert "dropcast" not in node.id.lower(), (path.name, node.id)


def test_host_side_imports_only_the_stdlib():
    assert {p.name for p in HOST_MODULES} >= {
        "__init__.py", "adapter.py", "cli.py", "sidecar.py", "router.py", "openrouter.py"
    }
    for path in HOST_MODULES:
        allowed = HERMES_IMPORTS.get(path.name, set())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module, level in imports(tree):
            if level or module == "__future__":
                continue
            top = module.split(".")[0]
            assert top in sys.stdlib_module_names or module in allowed, (path.name, module)


def test_no_openrouter_key_values_in_the_repo():
    # An OpenRouter key value (prefix split so this file does not match itself).
    key = re.compile("sk-" + r"or-(v1-)?[A-Za-z0-9]{20,}")
    for path in repo_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not key.search(text), path


def test_the_key_is_read_only_from_the_environment():
    # Rule 2: one reader of OPENROUTER_API_KEY, from os.environ; no other module names it in code.
    readers = []
    for path in HOST_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "OPENROUTER_API_KEY" in set(strings_outside_docstrings(tree)):
            readers.append(path.name)
    assert sorted(readers) == ["adapter.py", "openrouter.py"]  # adapter only scrubs it
    source = (ROOT / "openrouter.py").read_text(encoding="utf-8")
    assert "os.environ if environ is None else environ" in source


def test_only_the_router_registers_models_it_may_call():
    # Rule 1: the models the plugin calls are the two free ids, and every call goes
    # through openrouter.speech, which runs the price gate first.
    from onair import openrouter, router

    assert set(router.MODELS.values()) == {
        "fish-audio/s2.1-pro-free:free", "heygen/heygen-voice-1-turbo:free"
    }
    source = (ROOT / "openrouter.py").read_text(encoding="utf-8")
    speech = source[source.index("def speech("):]
    assert speech.index("catalog.check(model)") < speech.index("http(\"POST\"")
    assert openrouter.SPEECH_URL.startswith("https://")
