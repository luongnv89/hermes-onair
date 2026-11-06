"""``/onair``: the plugin's in-session slash command (``ctx.register_command``).

Hermes runs a plugin slash command the same way on the CLI
(``cli.py`` ``_run_plugin_slash_command``) and on the gateway
(``gateway/run_inbound_hooks.py`` ``_hm_dispatch_plugin_command``): both look
the handler up with ``get_plugin_command_handler`` and call it through
``invoke_plugin_command(handler, raw_args, …)``, which passes only the
keywords the handler declares. This handler takes ``raw_args`` alone and
returns the reply text.

Subcommands (``/voice`` is a Hermes built-in, so they live under ``/onair``):

- ``/onair voice``: this profile's persona and its voices (claims one on first use);
- ``/onair voice list``: the pool and which profile holds each persona;
- ``/onair voice set <name> [--force]``: change this profile's persona.
  A persona another profile holds is refused unless ``--force``, which
  changes only this profile's claim (the two then share the voice).

The profile and its home are resolved on every call, as for speech.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import personas, router

COMMAND = "onair"
DESCRIPTION = "On Air: this profile's voice persona (voice, voice list, voice set <name>)"
ARGS_HINT = "voice [list | set <name> [--force]]"
USAGE = "usage: /onair voice | /onair voice list | /onair voice set <name> [--force]"

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
        return "none (Fish is skipped; English goes to HeyGen)"
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


def make_handler(
    data_dir: Callable[[], Path],
    profile_name: Callable[[], Any] | None = None,
    get_config: Callable[..., Any] | None = None,
) -> Callable[[str], str]:
    """The ``/onair`` handler, ``fn(raw_args) -> reply``; everything is read per call."""

    def handle(raw_args: str = "") -> str:
        args = (raw_args or "").split()
        if not args or args[0].lower() != "voice":
            return USAGE
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
