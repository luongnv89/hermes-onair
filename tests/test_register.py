"""register(ctx), the `hermes onair` CLI wiring and the `/onair` slash command."""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest

import onair
from onair import cli, commands, episode, personas, router, sidecar


class StrictContext:
    """Only what register() may touch, like Hermes's validate probe.

    get_config returns the passed default (PluginContext does not apply
    config_schema defaults); any other attribute raises AttributeError.
    """

    def __init__(self, config=None):
        self.cli_commands: list[dict] = []
        self.commands: list[dict] = []
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

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append({"name": name, "handler": handler, "description": description,
                              "args_hint": args_hint})

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


def test_register_adds_the_cli_command_the_slash_command_the_tool_and_the_tts_provider():
    ctx = StrictContext()
    onair.register(ctx)
    assert [c["name"] for c in ctx.cli_commands] == ["onair"]
    (slash,) = ctx.commands
    assert slash["name"] == "onair" and callable(slash["handler"])
    assert slash["args_hint"] == commands.ARGS_HINT and "voice" in slash["description"]
    assert ctx.tools == ["onair_episode"] and ctx.hooks == []
    assert callable(ctx.cli_commands[0]["handler_fn"])
    (provider,) = ctx.tts_providers
    assert isinstance(provider, router.OnAirTTS) and provider.name == "onair"


def test_register_touches_nothing_else():
    class Bare:
        def __init__(self):
            self.n = 0
            self.providers = []
            self.slash = []
            self.tools = []

        def register_cli_command(self, **kwargs):
            self.n += 1

        def register_command(self, name, handler, **kwargs):
            self.slash.append(name)

        def register_tts_provider(self, provider):
            self.providers.append(provider)

        def register_tool(self, name, toolset, schema, handler, **kwargs):
            self.tools.append(name)

        def get_config(self, key, default=None):
            raise AssertionError("settings are read when they are used, not at register()")

        @property
        def llm(self):
            raise AssertionError("ctx.llm is read when an episode is made, not at register()")

        def post_message(self, text):
            raise AssertionError("nothing is posted at register()")

        @property
        def profile_name(self):
            raise AssertionError("the profile is read per call, not at register()")

    ctx = Bare()
    onair.register(ctx)  # any other attribute access would raise AttributeError
    assert ctx.n == 1 and len(ctx.providers) == 1 and ctx.slash == ["onair"]
    assert ctx.tools == ["onair_episode"]


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


# ------------------------------------------------- /onair through register()


class ProfileContext(StrictContext):
    """A context whose ``profile_name`` is read live, like Hermes's PluginContext."""

    def __init__(self, name="custom", config=None):
        super().__init__(config)
        self.name = name
        self.reads = 0

    @property
    def profile_name(self):
        self.reads += 1
        return self.name


def test_the_slash_command_and_the_provider_read_the_profile_per_call(monkeypatch, tmp_path):
    root = tmp_path / "root"
    homes = {name: root / "profiles" / name for name in ("forge", "mail")}
    current = {"home": homes["forge"]}
    monkeypatch.setattr(cli, "resolve_data_dir",
                        lambda: current["home"] / "plugin-data" / "onair")
    ctx = ProfileContext("custom")
    onair.register(ctx)
    assert ctx.reads == 0  # nothing read at register()
    handler = ctx.commands[0]["handler"]
    forge = handler("voice")
    assert forge.startswith("Profile forge speaks as ") and ctx.reads == 1
    current["home"] = homes["mail"]  # the next call runs for another profile
    mail = handler("voice")
    assert mail.startswith("Profile mail speaks as ")
    claimed = {name: personas.read_record(personas.claim_path_of(home))["persona"]
               for name, home in homes.items()}
    assert claimed["forge"] != claimed["mail"]
    provider = ctx.tts_providers[0]
    assert provider._profile_name() == "custom" and ctx.reads == 3


class TaskContext(StrictContext):
    """A host with ``register_auxiliary_task`` (hermes_cli/plugins.py)."""

    def __init__(self, refuse=False):
        super().__init__()
        self.aux_tasks: list[tuple] = []
        self.refuse = refuse

    def register_auxiliary_task(self, key, *, display_name, description, defaults=None,
                                inherit_from=None):
        if self.refuse:
            raise ValueError("key taken")
        self.aux_tasks.append((key, display_name, description, defaults, inherit_from))


def test_register_adds_the_reasoning_off_script_task():
    ctx = TaskContext()
    onair.register(ctx)
    ((key, name, description, defaults, inherit),) = ctx.aux_tasks
    assert key == "onair_script" and name and description and inherit is None
    assert defaults == {"reasoning_effort": "none"}  # no provider or model: no override


@pytest.mark.parametrize("ctx_factory", [StrictContext, lambda: TaskContext(refuse=True)])
def test_register_still_loads_without_the_task(ctx_factory):
    ctx = ctx_factory()
    onair.register(ctx)
    assert ctx.tools == ["onair_episode"] and [c["name"] for c in ctx.commands] == ["onair"]


def test_the_host_binding_carries_the_task_only_when_registered(monkeypatch):
    seen = []
    real = episode.Host

    def spy(*args, **kwargs):
        host = real(*args, **kwargs)
        seen.append(host.script_task)
        return host

    monkeypatch.setattr(episode, "Host", spy)
    onair.register(TaskContext())
    onair.register(StrictContext())
    onair.register(TaskContext(refuse=True))
    assert seen == ["onair_script", None, None]
