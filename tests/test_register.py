"""register(ctx) and the `hermes onair` CLI wiring."""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest

import onair
from onair import cli, router, sidecar


class StrictContext:
    """Only what register() may touch, like Hermes's validate probe.

    get_config returns the passed default (PluginContext does not apply
    config_schema defaults); any other attribute raises AttributeError.
    """

    def __init__(self, config=None):
        self.cli_commands: list[dict] = []
        self.tools: list[str] = []
        self.hooks: list[str] = []
        self.tts_providers: list = []
        self._config = config or {}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands.append(
            {"name": name, "help": help, "setup_fn": setup_fn, "handler_fn": handler_fn,
             "description": description}
        )

    def register_tts_provider(self, provider):
        self.tts_providers.append(provider)

    def register_tool(self, name, *args, **kwargs):
        self.tools.append(name)

    def register_hook(self, name, *args, **kwargs):
        self.hooks.append(name)

    def get_config(self, key, default=None):
        return self._config.get(key, default)


def attach(ctx: StrictContext) -> argparse.ArgumentParser:
    """Wire the command the way hermes_cli.main does for plugin CLI commands."""
    root = argparse.ArgumentParser(prog="hermes")
    subparsers = root.add_subparsers(dest="command")
    (cmd,) = ctx.cli_commands
    plugin_parser = subparsers.add_parser(
        cmd["name"], help=cmd["help"], description=cmd["description"]
    )
    cmd["setup_fn"](plugin_parser)
    plugin_parser.set_defaults(func=cmd["handler_fn"])
    return root


@pytest.fixture
def calls(monkeypatch, tmp_path):
    seen: list[tuple[str, Path, dict]] = []
    monkeypatch.setattr(cli, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "setup", lambda d, **kw: seen.append(("setup", d, kw)) or 0)
    monkeypatch.setattr(sidecar, "check", lambda d, **kw: seen.append(("check", d, kw)) or 0)
    return seen


def test_register_adds_the_cli_command_and_the_tts_provider():
    ctx = StrictContext()
    onair.register(ctx)
    assert [c["name"] for c in ctx.cli_commands] == ["onair"]
    assert ctx.tools == [] and ctx.hooks == []
    assert callable(ctx.cli_commands[0]["handler_fn"])
    (provider,) = ctx.tts_providers
    assert isinstance(provider, router.OnAirTTS) and provider.name == "onair"


def test_register_touches_nothing_else():
    class Bare:
        def __init__(self):
            self.n = 0
            self.providers = []

        def register_cli_command(self, **kwargs):
            self.n += 1

        def register_tts_provider(self, provider):
            self.providers.append(provider)

        def get_config(self, key, default=None):
            raise AssertionError("settings are read when they are used, not at register()")

    ctx = Bare()
    onair.register(ctx)  # any other attribute access would raise AttributeError
    assert ctx.n == 1 and len(ctx.providers) == 1


def test_the_provider_reads_settings_through_the_context(tmp_path, monkeypatch):
    ctx = StrictContext({"language": "fr", "ffmpeg_bin": "", "voices": {}})
    monkeypatch.setattr(cli, "resolve_data_dir", lambda: tmp_path)
    onair.register(ctx)
    (provider,) = ctx.tts_providers
    assert provider._setting("language", "auto") == "fr"
    assert provider._data_dir() == tmp_path


def test_setup_uses_defaults_when_nothing_is_configured(calls, tmp_path):
    ctx = StrictContext()
    onair.register(ctx)
    args = attach(ctx).parse_args(["onair", "setup"])
    assert args.func(args) == 0
    assert calls == [("setup", tmp_path, {"uv_bin": "", "python_request": ">=3.11"})]


@pytest.mark.parametrize("value", [None, "", "  ", 3])
def test_blank_or_odd_settings_fall_back_to_defaults(calls, value):
    ctx = StrictContext({"uv_bin": value, "sidecar_python": value})
    onair.register(ctx)
    args = attach(ctx).parse_args(["onair", "setup"])
    args.func(args)
    assert calls[0][2] == {"uv_bin": "", "python_request": ">=3.11"}


def test_setup_reads_configured_settings(calls):
    ctx = StrictContext({"uv_bin": "/opt/uv", "sidecar_python": "3.12"})
    onair.register(ctx)
    args = attach(ctx).parse_args(["onair", "setup"])
    args.func(args)
    assert calls[0][2] == {"uv_bin": "/opt/uv", "python_request": "3.12"}


def test_setup_check_only_reports(calls, tmp_path):
    ctx = StrictContext()
    onair.register(ctx)
    args = attach(ctx).parse_args(["onair", "setup", "--check"])
    assert args.func(args) == 0
    assert calls == [("check", tmp_path, {"uv_bin": ""})]


def test_no_action_prints_usage(calls, capsys):
    ctx = StrictContext()
    onair.register(ctx)
    args = attach(ctx).parse_args(["onair"])
    assert args.func(args) == 2
    assert "usage: hermes onair setup [--check]" in capsys.readouterr().err
    assert calls == []


def test_data_dir_comes_from_hermes_plugin_storage(monkeypatch, tmp_path):
    seen = []
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: seen.append(name) or tmp_path / name
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    assert cli.resolve_data_dir() == tmp_path / "onair"
    assert seen == ["onair"]
