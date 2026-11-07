"""``/onair``: the plugin's in-session slash command (``ctx.register_command``).

Hermes runs a plugin slash command the same way on the CLI
(``cli.py`` ``_run_plugin_slash_command``) and on the gateway
(``gateway/run_inbound_hooks.py`` ``_hm_dispatch_plugin_command``): both look
the handler up with ``get_plugin_command_handler`` and call it through
``invoke_plugin_command(handler, raw_args, …)``, which passes only the
keywords the handler declares. This handler takes ``raw_args`` alone and
returns the reply text.

``/onair <link|text>`` records a two-host episode (:mod:`episode`) and
answers twice: at once with "recording…", then with the episode, a final
reply that ends in a ``MEDIA:/abs/path.mp3`` line.

How the two messages travel (documented choice):

- The "recording…" note goes out through ``ctx.post_message``: it posts into
  the conversation bound to the command (gateway chats and local
  TUI/Desktop sessions) and returns a handle whose ``edit`` later turns it
  into "recorded in N s". Where nothing is bound (the classic CLI REPL, or a
  process with no live gateway) the post is refused with a result, never an
  exception, and the note is printed instead (the CLI prints the handler's
  output in the user's own terminal).
- The episode is the handler's **return value**. Every surface sends a
  command's reply through its normal path, which turns the ``MEDIA:`` line
  into an audio attachment on the gateway (``extract_media``) and prints it
  on the CLI. ``post_message`` sends text only (no attachments), and
  ``inject_message`` queues a *user* turn for the agent (and on the gateway
  needs ``allow_gateway_injection``), so neither can deliver the file.
- Long work never blocks the gateway's event loop: this handler is
  synchronous, and the gateway runs synchronous plugin handlers on its
  worker pool with the session context carried
  (``gateway/run_inbound_hooks.py`` ``_hm_run_plugin_handler``). An async
  handler would run on the loop, and on the CLI an async handler's result
  is awaited for at most 30 s, too short for an episode.

Voice subcommands (``/voice`` is a Hermes built-in, so they live under ``/onair``):

- ``/onair voice``: this profile's persona and its voices (claims one on first use);
- ``/onair voice list``: the pool and which profile holds each persona;
- ``/onair voice set <name> [--force]``: change this profile's persona.
  A persona another profile holds is refused unless ``--force``, which
  changes only this profile's claim (the two then share the voice).

``/onair standup [profiles…]`` records the Agent Standup show (:mod:`standup`):
each opted-in profile (the ``standup_profiles`` setting) reports its last 24 h
in its own voice. A profile that is not listed is refused before anything is
read. ``standup`` alone is the command; so is ``standup`` followed by words
that each name a listed profile or an existing profile folder (an unlisted
one is then refused). Anything else after ``standup`` ("standup is great")
is a text to record as an episode, as before this command existed.

The profile and its home are resolved on every call, as for speech.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import episode, personas, router, standup

log = logging.getLogger("onair")

COMMAND = "onair"
DESCRIPTION = (
    "On Air: turn a link or text into a two-host audio episode; "
    "standup [profiles] for the agents' daily standup; "
    "voice, voice list, voice set <name> for this profile's voice"
)
ARGS_HINT = "<link|text> | standup [profiles…] | voice [list | set <name> [--force]]"
USAGE = (
    "usage: /onair [--lang en|fr|vi] [--minutes N] <link or text> | "
    "/onair standup [profile …] | /onair voice | "
    "/onair voice list | /onair voice set <name> [--force]"
)
RECORDING = "🎙️ On Air is recording your episode… (about a minute)"
RECORDING_STANDUP = "🎙️ On Air is recording the standup… (about a minute)"

_ENGINE_LABEL = {"fish": "Fish", "heygen": "HeyGen", "edge": "edge-tts"}


def _setting(get_config: Callable[..., Any] | None, key: str, default: Any) -> Any:
    if get_config is None:
        return default
    try:
        value = get_config(key, default)
    except Exception:
        return default
    return default if value is None else value


def _fish_label(persona: personas.Persona) -> str:
    if persona.fish is None:
        return "none (skipped unless a setting names one)"
    return f"{persona.fish} ({persona.fish_id})"


def describe(persona: personas.Persona) -> list[str]:
    """The persona's voices, one engine per line."""
    edge = " · ".join(f"{lang} {persona.edge[lang]}" for lang in personas.LANGUAGES)
    return [
        f"  Fish:     {_fish_label(persona)}, every language",
        f"  HeyGen:   {persona.heygen}, every language",
        f"  edge-tts: {edge}",
    ]


def _overrides(persona: personas.Persona, voices: Any) -> list[str]:
    """Lines for every engine/language where the ``voices`` setting wins."""
    lines = []
    for engine in router.ENGINES:
        for lang in personas.LANGUAGES:
            mine = router.voice_for(engine, lang, None, persona)
            used = router.voice_for(engine, lang, voices, persona)
            if used != mine:
                lines.append(f"  {_ENGINE_LABEL[engine]} {lang}: {used} (from settings)")
    return lines


def show(claim: personas.Claim, voices: Any = None) -> str:
    persona = claim.persona
    since = f" since {claim.claimed_at}" if claim.claimed_at else ""
    made = {"set": "set", "set --force": "set with --force (shared)"}.get(claim.how, "claimed")
    first = "kept after a rename" if claim.how == "adopted" else "just claimed on first use"
    how = {"claimed": first, "stored": f"{made}{since}",
           "set": f"{made}{since}", "unstored": "not stored: the claim could not be written"}
    lines = [
        f"Profile {claim.profile} speaks as {persona.name} ({persona.gender}'s voice; "
        f"{how.get(claim.status, claim.status)}).",
        *describe(persona),
    ]
    extra = _overrides(persona, voices)
    if extra:
        lines += ["The `voices` setting overrides:", *extra]
    lines.append("Change it with /onair voice set <name>; see the pool with /onair voice list.")
    return "\n".join(lines)


def listing(held: dict[str, list[str]], profile: str) -> str:
    taken = sum(1 for p in personas.POOL if held.get(p.id))
    lines = [f"Voice personas: {len(personas.POOL)} ({taken} claimed in this Hermes root)."]
    for persona in personas.POOL:
        holders = [f"{h} (this profile)" if h == profile else h for h in held.get(persona.id, [])]
        who = ", ".join(holders) if holders else "free"
        fish = persona.fish or "-"
        lines.append(
            f"- {persona.id:<8} {persona.gender:<5}  Fish {fish:<7} HeyGen {persona.heygen:<9}"
            f" edge en {persona.edge['en']}  ->  {who}"
        )
    lines.append("Fish has 7 free voices with documented terms, so 3 personas have none "
                 "(Fish is skipped for them).")
    return "\n".join(lines)


def parse_episode_args(raw_args: str) -> tuple[str, str | None, str | None]:
    """``(source, language, minutes)`` from ``[--lang L] [--minutes N] <link or text>``.

    Options are read only at the start, so a text may contain anything after them.
    """
    rest = (raw_args or "").strip()
    language = minutes = None
    while rest.startswith("--"):
        head, _, tail = rest.partition(" ")
        flag, _, inline = head.partition("=")
        if flag not in ("--lang", "--minutes", "--min"):
            break
        value, _, after = (inline, "", tail) if inline else tail.strip().partition(" ")
        if not value:
            raise episode.EpisodeError(f"{flag} needs a value")
        if flag == "--lang":
            language = value
        else:
            minutes = value
        rest = after.strip()
    return rest, language, minutes


def _is_voice_command(args: list[str]) -> bool:
    """``voice``, ``voice list``, or ``voice set <name> [--force]``; anything longer is
    a text to record ("voice set the stage for…" is an episode, not a command)."""
    if not args or args[0].lower() != "voice":
        return False
    rest = [a.lower() for a in args[1:]]
    if rest in ([], ["list"]):
        return True
    if rest[:1] == ["set"]:
        return len(rest) == 2 or (len(rest) == 3 and rest[2].startswith("--"))
    return False


def _is_standup_command(args: list[str], host: episode.Host | None = None) -> bool:
    """``standup`` alone, or followed by words that all name profiles.

    A word names a profile when it is listed in ``standup_profiles`` or names
    an existing profile folder (:func:`standup.names_profiles`, a ``stat``
    only). Otherwise the words are an episode text.
    """
    if not args or args[0].lower() != "standup":
        return False
    if len(args) == 1:
        return True
    return host is not None and standup.names_profiles(host, args[1:])


class _Announcer:
    """The "recording…" note: ``post_message`` where a session is bound, else printed."""

    def __init__(self, post: Callable[[str], Any] | None) -> None:
        self._post = post
        self._handle: Any = None

    def start(self, text: str) -> None:
        handle = None
        if self._post is not None:
            try:
                handle = self._post(text)
            except Exception:  # post_message never raises; a stand-in might
                handle = None
        if handle is not None and getattr(handle, "ok", False):
            self._handle = handle
            return
        print(text, file=sys.stderr, flush=True)

    def finish(self, text: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.edit(text)
        except Exception:
            log.debug("onair: could not edit the recording note", exc_info=True)


def episode_reply(host: episode.Host, raw_args: str,
                  post: Callable[[str], Any] | None = None) -> str:
    """Record an episode for ``/onair <link|text>``; the reply ends with ``MEDIA:``."""
    try:
        source, language, minutes = parse_episode_args(raw_args)
        request = host.request(source, language, minutes)
    except episode.EpisodeError as exc:
        return f"On Air: {exc}.\n{USAGE}"
    note = _Announcer(post)
    note.start(RECORDING)
    try:
        made = host.record(request)
    except episode.EpisodeError as exc:
        note.finish("🎙️ On Air: no episode this time.")
        return f"On Air could not record this episode: {exc}"
    except Exception as exc:
        log.exception("onair: episode failed")
        note.finish("🎙️ On Air: no episode this time.")
        return f"On Air could not record this episode (internal error: {type(exc).__name__})."
    note.finish(f"🎙️ On Air: recorded in {made.timings.get('total_s', 0):.0f} s.")
    return episode.reply(made)


def standup_reply(host: episode.Host, names: list[str],
                  post: Callable[[str], Any] | None = None) -> str:
    """Record the standup for ``/onair standup [profiles…]``; the reply ends with ``MEDIA:``.

    The opt-in check runs before the "recording…" note and before any read.
    """
    try:
        selected = standup.select(names, standup.opted_in(host.setting(standup.SETTING, [])))
    except standup.StandupError as exc:
        return f"On Air standup: {exc}."
    note = _Announcer(post)
    note.start(RECORDING_STANDUP)
    try:
        made = standup.run(host, selected)
    except (standup.StandupError, episode.EpisodeError) as exc:
        note.finish("🎙️ On Air: no standup this time.")
        return f"On Air could not record the standup: {exc}"
    except Exception as exc:
        log.exception("onair: standup failed")
        note.finish("🎙️ On Air: no standup this time.")
        return f"On Air could not record the standup (internal error: {type(exc).__name__})."
    note.finish(f"🎙️ On Air: standup recorded in {made.timings.get('total_s', 0):.0f} s.")
    return standup.reply(made)


def make_handler(
    data_dir: Callable[[], Path],
    profile_name: Callable[[], Any] | None = None,
    get_config: Callable[..., Any] | None = None,
    *,
    host: episode.Host | None = None,
    post: Callable[[str], Any] | None = None,
) -> Callable[[str], str]:
    """The ``/onair`` handler, ``fn(raw_args) -> reply``; everything is read per call.

    *host* binds the episode pipeline (None: only the voice subcommands);
    *post* is ``ctx.post_message``.
    """

    def handle(raw_args: str = "") -> str:
        args = (raw_args or "").split()
        if not args:
            return USAGE
        if host is not None and _is_standup_command(args, host):
            return standup_reply(host, args[1:], post)
        if not _is_voice_command(args):
            if host is None:
                return USAGE
            return episode_reply(host, raw_args, post)
        directory = Path(data_dir())
        home = router.active_hermes_home(directory)
        try:
            ctx_name = profile_name() if profile_name is not None else None
        except Exception:
            ctx_name = None
        profile = personas.profile_label(home, ctx_name, directory)
        rest = [a.lower() if i == 0 else a for i, a in enumerate(args[1:])]
        if not rest:
            claim = personas.claim(directory, profile, home)
            return show(claim, _setting(get_config, "voices", {}))
        if rest == ["list"]:
            return listing(personas.all_claims(home, directory, profile), profile)
        if rest[0] == "set" and len(rest) >= 2:
            flags = [a for a in rest[1:] if a.startswith("--")]
            names = [a for a in rest[1:] if not a.startswith("--")]
            if len(names) != 1 or any(f != "--force" for f in flags):
                return USAGE
            try:
                claim = personas.set_claim(directory, profile, home, names[0],
                                           force="--force" in flags)
            except personas.ClaimRefused as exc:
                return f"Not changed: {exc}."
            except OSError as exc:
                return f"Not changed: cannot write {directory / personas.CLAIM_FILE} " \
                       f"({exc.strerror or type(exc).__name__})."
            shared = personas.held_by_siblings(home).get(claim.persona.id, [])
            note = f" Shared with {', '.join(shared)} (--force)." if shared else ""
            return f"Profile {profile} now speaks as {claim.persona.name}.{note}\n" + \
                "\n".join(describe(claim.persona))
        return USAGE

    return handle
