"""On Air: a Hermes plugin that gives every agent a free voice and a show.

``register()`` only registers; it does no I/O. The sidecar is installed by
``hermes onair setup``; ``tts.provider: onair`` routes Hermes's speech to the
free voice router (:mod:`router`).
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    # Imported here, not at module level: importing the package stays free of
    # side effects, and pytest can import this file on its own.
    from . import cli, router

    ctx.register_cli_command(
        name=cli.COMMAND,
        help=cli.HELP,
        setup_fn=cli.setup_parser,
        handler_fn=cli.make_handler(ctx.get_config),
        description=cli.DESCRIPTION,
    )
    ctx.register_tts_provider(router.OnAirTTS(ctx.get_config, cli.resolve_data_dir))
