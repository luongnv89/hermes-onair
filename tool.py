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

from . import episode

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
