"""On Air: a Hermes plugin that gives every agent a free voice and a show.

``register()`` only registers; it does no I/O. The sidecar is installed by
``hermes onair setup``; ``tts.provider: onair`` routes Hermes's speech to the
free voice router (:mod:`router`) in the active profile's persona
(:mod:`personas`); ``/onair voice`` shows and changes it (:mod:`commands`).
``/onair <link|text>`` and the ``onair_episode`` agent tool (:mod:`tool`)
share one pipeline (:mod:`episode`) that turns a link or text into a
two-host episode.
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    # Imported here, not at module level: importing the package stays free of
    # side effects, and pytest can import this file on its own.
    from . import cli, commands, episode, router, tool, writer

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
    def llm():
        # ctx.llm is host-owned (the user's own model); read on use, never at register().
        return ctx.llm

    def post(text):
        return ctx.post_message(text)

    host = episode.Host(cli.resolve_data_dir, profile_name, ctx.get_config, llm,
                        script_task=_register_script_task(ctx, writer))
    ctx.register_command(
        commands.COMMAND,
        commands.make_handler(cli.resolve_data_dir, profile_name, ctx.get_config,
                              host=host, post=post),
        description=commands.DESCRIPTION,
        args_hint=commands.ARGS_HINT,
    )
    ctx.register_tool(
        name=tool.NAME,
        toolset=tool.TOOLSET,
        schema=tool.SCHEMA,
        handler=tool.make_handler(host),
        description=tool.DESCRIPTION,
        emoji=tool.EMOJI,
    )


def _register_script_task(ctx, writer) -> str | None:
    """Register the ``onair_script`` auxiliary task; its key, or None on an older host.

    Its defaults turn reasoning off and name no provider or model, so the call
    still runs on the user's main model (AGENTS.md rule 1: no override). The
    user's ``auxiliary.onair_script`` config block overrides them.
    """
    register = getattr(ctx, "register_auxiliary_task", None)
    if not callable(register):
        writer.note_no_task("this Hermes has no ctx.register_auxiliary_task")
        return None
    try:
        register(
            writer.SCRIPT_TASK,
            display_name=writer.SCRIPT_TASK_NAME,
            description=writer.SCRIPT_TASK_DESCRIPTION,
            defaults=dict(writer.SCRIPT_TASK_DEFAULTS),
        )
    except Exception as exc:  # a refused key must not stop the plugin from loading
        writer.note_no_task(f"registering {writer.SCRIPT_TASK} failed ({type(exc).__name__})")
        return None
    return writer.SCRIPT_TASK
