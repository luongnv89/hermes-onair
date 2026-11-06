"""On Air: a Hermes plugin that gives every agent a free voice and a show.

``register()`` only registers; it does no I/O. The sidecar is installed by
``hermes onair setup``; ``tts.provider: onair`` routes Hermes's speech to the
free voice router (:mod:`router`) in the active profile's persona
(:mod:`personas`); ``/onair voice`` shows and changes it (:mod:`commands`).
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    # Imported here, not at module level: importing the package stays free of
    # side effects, and pytest can import this file on its own.
    from . import cli, commands, router

    def profile_name():
        # Read per call, never at register(): it follows HERMES_HOME and any
        # per-task home override, so one process can serve several profiles.
        return ctx.profile_name

    ctx.register_cli_command(
        name=cli.COMMAND,
        help=cli.HELP,
        setup_fn=cli.setup_parser,
        handler_fn=cli.make_handler(ctx.get_config),
        description=cli.DESCRIPTION,
    )
    ctx.register_tts_provider(
        router.OnAirTTS(ctx.get_config, cli.resolve_data_dir, profile_name=profile_name)
    )
    ctx.register_command(
        commands.COMMAND,
        commands.make_handler(cli.resolve_data_dir, profile_name, ctx.get_config),
        description=commands.DESCRIPTION,
        args_hint=commands.ARGS_HINT,
    )
