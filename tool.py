"""``onair_episode``: the agent tool behind the same pipeline as ``/onair``.

The agent calls it to make an episode on request; it returns a JSON string
with the ``MEDIA:`` path (Desktop's tool media renderers claim agent-tool
output, so the episode can get a player there), the transcript path, the
engine and voices, the script source and the model id ``ctx.llm`` returned,
and the timings. It blocks for about a minute: tool handlers run on the
agent's worker thread, never on the gateway's event loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from . import episode, standup

log = logging.getLogger("onair")

NAME = "onair_episode"
TOOLSET = "onair"
EMOJI = "🎙️"
DESCRIPTION = (
    "Turn a web link or a pasted text into a short two-host audio episode (MP3 plus a "
    "WebVTT transcript) with free voices only. Takes about a minute. Returns JSON with "
    "'media' (a MEDIA:/path line to show the user), 'transcript', 'engine', 'model' and "
    "'timings'. Episodes voiced by edge-tts are for the user's private listening only: "
    "never publish them."
)
SCHEMA: dict[str, Any] = {
    "name": NAME,
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "An http(s) link to a page, or the text itself (pasted text).",
            },
            "language": {
                "type": "string",
                "description": "Episode language: en, fr or vi (any BCP 47 tag), or auto "
                               "(default) to detect it from the source.",
            },
            "minutes": {
                "type": "number",
                "description": "Target length in minutes, 1 to 5 (default 1).",
                "minimum": episode.MIN_MINUTES,
                "maximum": episode.MAX_MINUTES,
            },
        },
        "required": ["source"],
        "additionalProperties": False,
    },
}


def make_handler(host: episode.Host) -> Callable[..., str]:
    """``handler(args, **kwargs) -> JSON string`` for ``ctx.register_tool``."""

    def handle(args: dict[str, Any] | None = None, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            request = host.request(args.get("source"), args.get("language"),
                                   args.get("minutes"))
            made = host.record(request)
        except episode.EpisodeError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        except Exception as exc:  # the agent gets a result, never a traceback
            log.exception("onair_episode failed")
            return json.dumps({"ok": False, "error": f"internal error: {type(exc).__name__}"})
        return json.dumps(episode.tool_result(made), ensure_ascii=False)

    return handle


STANDUP_NAME = "onair_standup"
STANDUP_DESCRIPTION = (
    "Record the Agent Standup: each opted-in Hermes profile (the plugin's standup_profiles "
    "setting) reports its last 24 hours in its own voice, from its own session titles, read "
    "read-only. Free voices only. Takes about a minute. Returns JSON with 'reply' (the text "
    "to send, ending in a MEDIA:/path line), 'media', 'segments' and 'engine'. Set "
    "'scheduled' to true when no user asked for it (a cron run): only free OpenRouter "
    "voices are used then, never edge-tts. Standups voiced by edge-tts are for the "
    "requesting user's private listening only: never publish them."
)
STANDUP_SCHEMA: dict[str, Any] = {
    "name": STANDUP_NAME,
    "description": STANDUP_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "profiles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Profile names to include (each must be opted in); empty or "
                               "absent: every opted-in profile.",
            },
            "scheduled": {
                "type": "boolean",
                "description": "True for a scheduled run with no requesting user (no "
                               "edge-tts). A Hermes cron run is detected either way.",
            },
        },
        "additionalProperties": False,
    },
}


def make_standup_handler(host: episode.Host) -> Callable[..., str]:
    """``handler(args, **kwargs) -> JSON string`` for the ``onair_standup`` tool."""

    def handle(args: dict[str, Any] | None = None, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        raw = args.get("profiles")
        names = [n for n in raw if isinstance(n, str)] if isinstance(raw, list) else []
        try:
            made = standup.run(host, names, scheduled=args.get("scheduled") is True)
        except (standup.StandupError, episode.EpisodeError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        except Exception as exc:  # the agent gets a result, never a traceback
            log.exception("onair_standup failed")
            return json.dumps({"ok": False, "error": f"internal error: {type(exc).__name__}"})
        return json.dumps(standup.tool_result(made), ensure_ascii=False)

    return handle
