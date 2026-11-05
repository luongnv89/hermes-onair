"""``hermes onair …``: the plugin's CLI subtree (``ctx.register_cli_command``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import sidecar

PLUGIN_NAME = "onair"
COMMAND = "onair"
HELP = "On Air: free voices and two-host shows"
DESCRIPTION = (
    "On Air plugin commands.\n\n"
    "  setup          create the sidecar venv and install the pinned engine (needs uv)\n"
    "  setup --check  report the sidecar's state without changing anything"
)
USAGE = "usage: hermes onair setup [--check]"

GetConfig = Callable[..., Any]


def setup_parser(parser: argparse.ArgumentParser) -> None:
    """argparse ``setup_fn``: Hermes passes the ``hermes onair`` subparser."""
    actions = parser.add_subparsers(dest="onair_action", metavar="ACTION")
    setup = actions.add_parser(
        "setup",
        help="create or check the sidecar venv",
        description="Create the sidecar venv under the plugin's data dir with uv and "
        "install the pinned engine. Safe to run again: a good install is left alone.",
    )
    setup.add_argument(
        "--check",
        action="store_true",
        help="only report the sidecar's state (exit 0 when ready)",
    )


def resolve_data_dir() -> Path:
    """``$HERMES_HOME/plugin-data/onair``, through Hermes's public storage helper."""
    from plugins.plugin_storage import plugin_data_dir

    return Path(plugin_data_dir(PLUGIN_NAME))


def _setting(get_config: GetConfig, key: str, default: str) -> str:
    # PluginContext.get_config does not apply config_schema defaults.
    value = get_config(key, default)
    return value if isinstance(value, str) and value.strip() else default


def make_handler(get_config: GetConfig) -> Callable[[argparse.Namespace], int]:
    """argparse ``handler_fn``; settings are read at call time, not at register()."""

    def handle(args: argparse.Namespace) -> int:
        if getattr(args, "onair_action", None) != "setup":
            print(USAGE, file=sys.stderr)
            return 2
        data_dir = resolve_data_dir()
        uv_bin = _setting(get_config, "uv_bin", sidecar.DEFAULT_UV_BIN)
        if getattr(args, "check", False):
            return sidecar.check(data_dir, uv_bin=uv_bin)
        python_request = _setting(get_config, "sidecar_python", sidecar.DEFAULT_SIDECAR_PYTHON)
        return sidecar.setup(data_dir, uv_bin=uv_bin, python_request=python_request)

    return handle
