"""One shared pipeline for ``/onair <link|text>`` and the ``onair_episode`` tool.

1. **Ingest** the link or text: the engine's ``run`` (through :mod:`adapter`,
   rule 4) ingests it and stops at the script step with a brief (format
   ``podcast-2host``, the target length, the language, no guests).
2. **Script**: :mod:`writer`, with ``ctx.llm`` (host-owned, one retry with
   backoff, a hard word cap) or, when that fails, the template.
3. **Checks**: the engine's own script checks (:func:`adapter.check_script`)
   on ``script.json``: an LLM script that fails them is replaced by the
   template; a script whose only problem is its length re-briefs the job to
   the script's own length (once), so a short source still gets an episode.
4. **Render** with ONE engine for the whole episode, the router's chain for
   the language (English: Fish, HeyGen, edge-tts; other languages: HeyGen,
   Fish, edge-tts). Fish and HeyGen are OpenRouter's free models, called per
   turn through :mod:`router` (the price gate runs before every call, rule
   1) and joined by the engine's finish step; any failed turn drops that
   engine and the next one renders the whole episode again. edge-tts is
   rendered by the engine itself on ``resume`` (private use only, rule 3).
5. **Deliver** the MP3 and its WebVTT transcript (``transcript.vtt``).

Two hosts, two personas: the profile's own persona (:mod:`personas`) hosts in
its own name and voice (the host of its voice type), and a second persona of
the other type, the next one in the pool, is the co-host. The show keeps the
engine's fixed name, opening and sign-off; only the hosts are renamed
(``show.hosts`` and ``speakers[].name``, which its checks accept). The job
folder the engine names must lie inside the episode's folder, and only the
newest ``keep_episodes`` episode folders are kept. Each job runs under its own
folder in the plugin data dir (``episodes/<stamp>``), and ``onair.json``
there records the engine, voices, script source, the model id ``ctx.llm``
returned and the timings.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import adapter, openrouter, personas, router, sidecar, writer

log = logging.getLogger("onair")

DEFAULT_MINUTES = 1.0
MIN_MINUTES = 1.0  # the show's fixed lines alone fill about half a minute
MAX_MINUTES = 5.0
EPISODES_DIR = "episodes"
RECORD_FILE = "onair.json"
#: Episode folders kept in the plugin data dir (setting ``keep_episodes``; 0 keeps all).
KEEP_EPISODES = 30
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_LANG = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", re.IGNORECASE)

Clock = Callable[[], float]


class EpisodeError(RuntimeError):
    """No episode: the reason is safe to show the user."""


@dataclass(frozen=True)
class Request:
    """What to record: a link (http/https only) or pasted text."""

    link: str | None = None
    text: str | None = None
    language: str | None = None  # a tag, or None to detect it from the source
    minutes: float = DEFAULT_MINUTES


def make_request(source: Any, language: Any = None, minutes: Any = None) -> Request:
    """A :class:`Request` from loose input. A lone http(s) URL is a link; anything else is text.

    Local paths are never links: a chat user must not make the plugin read files.
    """
    if not isinstance(source, str) or not source.strip():
        raise EpisodeError("give a link (http or https) or some text to talk about")
    value = source.strip()
    lang = None
    if isinstance(language, str) and language.strip() and language.strip().lower() != "auto":
        lang = language.strip().lower()
        if not _LANG.fullmatch(lang):
            raise EpisodeError(f"not a language tag: {language!r} (try en, fr or vi)")
    if minutes is None or minutes == "":
        length = DEFAULT_MINUTES
    else:
        try:
            length = float(minutes)
        except (TypeError, ValueError):
            raise EpisodeError(f"not a length in minutes: {minutes!r}") from None
        if not MIN_MINUTES <= length <= MAX_MINUTES:
            raise EpisodeError(f"the length must be {MIN_MINUTES:g} to {MAX_MINUTES:g} minutes")
    if _URL.fullmatch(value):
        return Request(link=value, language=lang, minutes=length)
    return Request(text=value, language=lang, minutes=length)


@dataclass
class Episode:
    """A finished episode and how it was made."""

    mp3: Path
    transcript: Path | None
    title: str
    language: str
    duration_s: float | None
    engine: str
    voices: dict[str, str]
    hosts: dict[str, str]  # host name (a persona's name) -> persona id
    script_source: str  # "llm" or "template"
    model: str | None  # the model id ctx.llm returned
    llm_error: str | None
    engines_skipped: list[dict[str, str]]
    timings: dict[str, float]
    job: Path
    private_use: bool
    profile: str
    notes: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("mp3", "transcript", "job"):
            data[key] = str(data[key]) if data[key] is not None else None
        return data


# ------------------------------------------------------------------- casting


def partner(persona: personas.Persona) -> personas.Persona:
    """The co-host: the next persona in the pool of the other voice type.

    One with a Fish voice is preferred when *persona* has one, so the
    episode can use Fish for both hosts.
    """
    start = personas.POOL.index(persona) if persona in personas.POOL else 0
    order = [personas.POOL[(start + i) % len(personas.POOL)] for i in range(1, len(personas.POOL))]
    other = [p for p in order if p.gender != persona.gender] or order
    if persona.fish:
        with_fish = [p for p in other if p.fish]
        if with_fish:
            return with_fish[0]
    return other[0]


def cast(persona: personas.Persona) -> dict[str, personas.Persona]:
    """Speaker id -> persona: each host gets the persona of its voice type."""
    second = partner(persona)
    cast_ = {}
    for sid, gender in adapter.HOST_GENDER.items():
        cast_[sid] = persona if persona.gender == gender else second
    if len({p.id for p in cast_.values()}) == 1:  # both personas of one type
        sids = list(cast_)
        cast_[sids[1]] = second
    return cast_


# -------------------------------------------------------------------- render


@dataclass
class Tools:
    """Every outside effect, injectable for tests."""

    runner: router.Runner = subprocess.run
    http: openrouter.Http = openrouter.urllib_http
    which: router.Which = shutil.which
    sleep: Callable[[float], None] = time.sleep
    clock: Clock = time.monotonic
    environ: Mapping[str, str] | None = None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def render_turns(
    engine: str,
    lines: list[tuple[str, str, str]],
    voices: Mapping[str, str],
    env: router.Environment,
    job: Path,
    venv: Path,
    tools: Tools,
    language: str = "en",
) -> adapter.Finished:
    """Every line ``(segment id, speaker id, text)`` with *engine*, then the finish step.

    *voices* maps speaker id -> that engine's voice. Fish and HeyGen go through
    the router (the price gate runs before every call, rule 1); edge-tts is
    rendered by the sidecar, one line at a time (private use only, rule 3).
    Raises :class:`router.EngineError` on any failure.
    """
    parts_dir = job / "audio" / "onair" / engine
    parts_dir.mkdir(parents=True, exist_ok=True)
    started = _now_iso()
    parts: list[tuple[Path, str, str]] = []
    for sid, speaker, text in lines:
        with tempfile.TemporaryDirectory(prefix="onair-turn-") as tmp:
            if engine == router.FISH:
                made = router.render_fish(env, text, voices[speaker], Path(tmp), "mp3")
            elif engine == router.HEYGEN:
                made = router.render_heygen(env, text, voices[speaker], Path(tmp), "mp3")
            else:
                made = router.render_edge(env, text, voices[speaker], language, Path(tmp), "mp3")
            target = parts_dir / f"{sid}.mp3"
            shutil.copyfile(made, target)
        parts.append((target, sid, text))
    edge = engine == router.EDGE
    try:
        return adapter.finish_parts(
            venv, job, parts, model="edge-tts" if edge else router.MODELS[engine],
            voices=voices, started=started, engine="edgetts" if edge else "openrouter",
            private_use=edge, runner=tools.runner, env=tools.environ,
        )
    except adapter.AdapterError as exc:
        raise router.EngineError(str(exc)) from None


def _render_edge(
    voices: dict[str, str],
    brief: adapter.Brief,
    job: Path,
    root: Path,
    venv: Path,
    tools: Tools,
    length_min: float | None,
) -> adapter.Finished:
    """The engine's own edge-tts render on ``resume`` (with the hosts' voices)."""
    path = adapter.write_voices(root / "onair-voices.json", brief, voices)
    result = adapter.resume(venv, str(job), root, voices=path, length_min=length_min,
                            runner=tools.runner, env=tools.environ)
    if result.status is not adapter.JobStatus.DELIVERED:
        errors = result.raw.get("errors")
        detail = "; ".join(errors) if isinstance(errors, list) and errors else result.message
        raise router.EngineError(f"the render stopped at {result.step} ({result.status.value}): "
                                 f"{detail[:300]}")
    return adapter.delivered(result)


# ------------------------------------------------------------------ pipeline


def _ingest(request: Request, venv: Path, root: Path, tools: Tools) -> adapter.JobResult:
    try:
        result = adapter.run(
            venv, root, link=request.link, text=request.text, fmt=adapter.EPISODE_FORMAT,
            length_min=request.minutes, language=request.language, guests=0,
            runner=tools.runner, env=tools.environ,
        )
    except adapter.AdapterError as exc:
        raise EpisodeError(f"could not read the source: {exc}") from None
    if result.status is not adapter.JobStatus.NEEDS_AGENT or result.step != "script":
        raise EpisodeError(f"could not read the source: {result.message[:300]}")
    return result


def _check(draft: writer.Draft, brief: adapter.Brief, source: adapter.Source, job: Path,
           venv: Path, tools: Tools) -> tuple[adapter.ScriptCheck, float | None]:
    """Write and check *draft*; on a length-only miss, retarget once to its own length.

    Returns the check and the new target length (None when the brief's stands).
    """
    adapter.write_script(job, adapter.script_document(draft, brief, source), draft.summary)
    verdict = adapter.check_script(venv, job, runner=tools.runner, env=tools.environ)
    if verdict.ok or not verdict.only_length or verdict.est_min is None:
        return verdict, None
    target = round(min(max(verdict.est_min, adapter.MIN_LENGTH_MIN), MAX_MINUTES * 1.5), 2)
    adapter.write_script(job, adapter.script_document(draft, brief, source, target),
                         draft.summary)
    return adapter.check_script(venv, job, runner=tools.runner, env=tools.environ), target


def make_episode(
    request: Request,
    *,
    data_dir: Path,
    profile: str,
    persona: personas.Persona,
    llm: Any,
    ffmpeg_setting: Any = "",
    tools: Tools | None = None,
    keep: int = KEEP_EPISODES,
    script_task: str | None = None,
) -> Episode:
    """Run the whole pipeline; raise :class:`EpisodeError` when no episode can be made."""
    tools = tools or Tools()
    clock = tools.clock
    t0 = clock()
    timings: dict[str, float] = {}
    data_dir = Path(data_dir)
    venv = sidecar.venv_dir(data_dir)
    if sidecar.ownership(venv) not in (sidecar.OWNED, sidecar.ADOPTABLE):
        raise EpisodeError("the sidecar is not set up: run `hermes onair setup` first")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    root = data_dir / EPISODES_DIR / stamp
    root.mkdir(parents=True, exist_ok=True)

    # 1. ingest
    result = _ingest(request, venv, root, tools)
    job = _job_inside(result.job, root)
    try:
        brief = adapter.read_brief(job)
        source = adapter.read_source(job)
    except adapter.AdapterError as exc:
        raise EpisodeError(f"could not read the brief: {exc}") from None
    # Two hosts, two personas: the show keeps its name; its hosts take the personas' names.
    hosts = cast(persona)
    brief = dataclasses.replace(
        brief, names={sid: hosts[sid].name if sid in hosts else name
                      for sid, name in brief.names.items()})
    timings["ingest_s"] = round(clock() - t0, 1)

    # 2. script: ctx.llm, then the template on failure
    t1 = clock()
    llm_error: str | None = None
    try:
        draft = writer.llm_draft(llm, brief, source, sleep=tools.sleep, task=script_task)
    except writer.ScriptFailed as exc:
        llm_error = str(exc)
        draft = None
    timings["script_s"] = round(clock() - t1, 1)

    # 3. the engine's script checks (LLM script first, the template if it fails)
    t2 = clock()
    model = draft.model if draft else None
    retarget: float | None = None
    verdict = None
    if draft is not None:
        try:
            verdict, retarget = _check(draft, brief, source, job, venv, tools)
        except adapter.AdapterError as exc:  # the check broke on the model's script
            llm_error = f"the checks could not run on the model's script: {exc}"[:300]
            draft = None
        else:
            if not verdict.ok:
                llm_error = ("the model's script failed the checks: "
                             + "; ".join(verdict.errors)[:300])
                draft = None
    if draft is None:
        try:
            draft = writer.template_draft(brief, source, reason=llm_error)
        except writer.ScriptFailed as exc:
            raise EpisodeError(f"no script: {exc}") from None
        try:
            verdict, retarget = _check(draft, brief, source, job, venv, tools)
        except adapter.AdapterError as exc:
            raise EpisodeError(str(exc)) from None
        if not verdict.ok:
            raise EpisodeError("the script failed the checks: " + "; ".join(verdict.errors)[:400])
    timings["checks_s"] = round(clock() - t2, 1)

    # 4. render: one engine for the whole episode
    t3 = clock()
    language = writer.primary(brief.language)
    home = router.active_hermes_home(data_dir)
    env = router.Environment(
        data_dir=data_dir,
        ffmpeg=router.find_ffmpeg(ffmpeg_setting, tools.which, environ={}, hermes_home=home),
        http=tools.http, runner=tools.runner, environ=tools.environ,
    )
    key = openrouter.api_key(tools.environ)
    try:
        lines = adapter.script_lines(job)
    except adapter.AdapterError as exc:
        raise EpisodeError(str(exc)) from None
    skipped: list[dict[str, str]] = []
    finished: adapter.Finished | None = None
    engine_used = ""
    voices: dict[str, str] = {}
    for engine in router.chain_for(language):
        voices = {sid: router.voice_for(engine, language, None, p) for sid, p in hosts.items()}
        missing = [hosts[sid].name for sid, v in voices.items() if v is None]
        if missing:
            skipped.append({"engine": engine, "reason": f"no {engine} voice for {missing[0]}"})
            continue
        try:
            if engine == router.EDGE:
                finished = _render_edge(voices, brief, job, root, venv, tools, retarget)
            elif key is None:
                raise router.EngineError("no OPENROUTER_API_KEY in the environment")
            else:
                finished = render_turns(engine, lines, voices, env, job, venv, tools, language)
        except (router.EngineError, adapter.AdapterError, OSError) as exc:
            reason = openrouter.scrub(str(exc), key)
            skipped.append({"engine": engine, "reason": reason[:300]})
            log.warning("episode: %s could not render (%s)", engine, reason)
            continue
        engine_used = engine
        break
    timings["render_s"] = round(clock() - t3, 1)
    if finished is None:
        detail = "; ".join(f"{s['engine']}: {s['reason']}" for s in skipped)
        raise EpisodeError(f"no free voice engine could render the episode ({detail[:500]})")
    timings["total_s"] = round(clock() - t0, 1)

    episode = Episode(
        mp3=finished.episode.resolve(),
        transcript=finished.transcript.resolve() if finished.transcript else None,
        title=draft.title,
        language=brief.language,
        duration_s=round(finished.duration_s, 1) if finished.duration_s else None,
        engine=engine_used,
        voices={brief.names[sid]: v for sid, v in voices.items()},
        hosts={brief.names[sid]: p.id for sid, p in hosts.items()},
        script_source=draft.source,
        model=model if draft.source == "llm" else None,
        llm_error=llm_error,
        engines_skipped=skipped,
        timings=timings,
        job=job,
        private_use=engine_used == router.EDGE,
        profile=profile,
        notes=list(draft.notes) + ([f"length set to {retarget} min"] if retarget else []),
    )
    try:
        (job / RECORD_FILE).write_text(json.dumps(episode.record(), indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    except OSError as exc:
        log.warning("episode: cannot write %s (%s)", RECORD_FILE, exc.strerror or exc)
    prune(data_dir / EPISODES_DIR, keep, current=root)
    return episode


_STAMP = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")


def prune(episodes: Path, keep: int, *, current: Path | None = None) -> list[Path]:
    """Remove the oldest episode folders past the newest *keep* (0 keeps every one).

    Only real folders named like an episode stamp are touched; never *current*,
    never a symbolic link. Returns what was removed; a failure is logged, not raised.
    """
    if keep <= 0:
        return []
    try:
        folders = sorted(
            (p for p in Path(episodes).iterdir()
             if _STAMP.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),
            key=lambda p: p.name,
        )
    except OSError:
        return []
    removed = []
    for folder in folders[:-keep] if len(folders) > keep else []:
        if current is not None and folder == current:
            continue
        try:
            shutil.rmtree(folder)
            removed.append(folder)
        except OSError as exc:
            log.warning("episode: cannot remove old episode %s (%s)", folder.name,
                        exc.strerror or exc)
    return removed


def _job_inside(job: str | None, root: Path) -> Path:
    """The job folder the engine named: an existing folder strictly inside *root*."""
    if not job:
        raise EpisodeError("the engine named no job folder")
    path = Path(job).resolve()
    base = Path(root).resolve()
    if not path.is_relative_to(base):
        raise EpisodeError("the engine named a job folder outside this episode's folder")
    if path == base:
        raise EpisodeError("the engine named the episode's own folder as its job folder")
    if not path.is_dir():
        raise EpisodeError("the engine named a job folder that does not exist")
    return path


# --------------------------------------------------------------------- reply

ENGINE_LABEL = {router.FISH: "Fish (OpenRouter, free)", router.HEYGEN: "HeyGen (OpenRouter, free)",
                router.EDGE: "edge-tts"}


def reply(episode: Episode) -> str:
    """The chat reply: what was made and how, ending with the ``MEDIA:`` line."""
    hosts = " & ".join(episode.hosts)
    length = f"{episode.duration_s:.0f} s" if episode.duration_s else "length unknown"
    lines = [f'On Air: "{episode.title}" ({length}, {episode.language}). '
             f"Hosts {hosts} (voice personas), profile {episode.profile}."]
    voices = ", ".join(f"{name} {voice}" for name, voice in episode.voices.items())
    voice_line = f"Voices: {ENGINE_LABEL.get(episode.engine, episode.engine)}: {voices}."
    if episode.engines_skipped:
        voice_line += " Skipped: " + "; ".join(
            f"{s['engine']} ({s['reason'][:120]})" for s in episode.engines_skipped) + "."
    lines.append(voice_line)
    if episode.script_source == "llm":
        lines.append(f"Script: written by your model ({episode.model or 'model id not reported'}).")
    else:
        lines.append("Script: built-in template, because the LLM script failed "
                     f"({(episode.llm_error or 'no reason given')[:300]}).")
    if episode.transcript:
        lines.append(f"Transcript: {episode.transcript}")
    t = episode.timings
    lines.append(f"Took {t.get('total_s', 0):.0f} s (ingest {t.get('ingest_s', 0):.0f} s, "
                 f"script {t.get('script_s', 0):.0f} s, checks {t.get('checks_s', 0):.0f} s, "
                 f"render {t.get('render_s', 0):.0f} s).")
    if episode.private_use:
        lines.append("Private listening only (edge-tts voices): do not publish this episode.")
    lines.append(f"MEDIA:{episode.mp3}")
    return "\n".join(lines)


def tool_result(episode: Episode) -> dict[str, Any]:
    """The ``onair_episode`` tool's JSON."""
    return {
        "ok": True,
        "media": f"MEDIA:{episode.mp3}",
        "mp3": str(episode.mp3),
        "transcript": str(episode.transcript) if episode.transcript else None,
        "title": episode.title,
        "language": episode.language,
        "duration_s": episode.duration_s,
        "engine": episode.engine,
        "engines_skipped": episode.engines_skipped,
        "voices": episode.voices,
        "hosts": episode.hosts,
        "script": episode.script_source,
        "model": episode.model,
        "llm_error": episode.llm_error,
        "timings": episode.timings,
        "private_use": episode.private_use,
        "note": ("Private listening only (edge-tts voices): do not publish."
                 if episode.private_use else None),
    }


# ---------------------------------------------------------------------- host


def _setting(get_config: Callable[..., Any] | None, key: str, default: Any) -> Any:
    if get_config is None:
        return default
    try:
        value = get_config(key, default)
    except Exception:  # a broken config read must not stop an episode
        return default
    return default if value is None else value


@dataclass
class Host:
    """What ``register()`` binds from Hermes; every value is read per call."""

    data_dir: Callable[[], Path]
    profile_name: Callable[[], Any] | None = None
    get_config: Callable[..., Any] | None = None
    llm: Callable[[], Any] | None = None
    tools: Tools | None = None
    script_task: str | None = None  # the registered auxiliary task, or None (main path)

    def setting(self, key: str, default: Any) -> Any:
        return _setting(self.get_config, key, default)

    def request(self, source: Any, language: Any = None, minutes: Any = None) -> Request:
        """A request, with the ``language`` and ``episode_minutes`` settings as defaults."""
        if language in (None, ""):
            language = self.setting("language", "auto")
        if minutes in (None, ""):
            minutes = self.setting("episode_minutes", DEFAULT_MINUTES)
        return make_request(source, language, minutes)

    def record(self, request: Request) -> Episode:
        directory = Path(self.data_dir())
        home = router.active_hermes_home(directory)
        claim = personas.safe_claim(directory, self.profile_name, home)
        persona = claim.persona if claim is not None else personas.POOL[0]
        profile = claim.profile if claim is not None else personas.profile_label(
            home, None, directory)
        try:
            llm = self.llm() if self.llm is not None else None
        except Exception as exc:  # no ctx.llm in this Hermes: the template speaks
            log.warning("episode: ctx.llm unavailable (%s)", type(exc).__name__)
            llm = None
        keep = self.setting("keep_episodes", KEEP_EPISODES)
        keep = keep if isinstance(keep, int) and not isinstance(keep, bool) else KEEP_EPISODES
        return make_episode(
            request, data_dir=directory, profile=profile, persona=persona, llm=llm,
            ffmpeg_setting=self.setting("ffmpeg_bin", ""), tools=self.tools, keep=keep,
            script_task=self.script_task,
        )
