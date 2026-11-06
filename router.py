"""``tts.provider: onair``: the free voice router.

One request is spoken by exactly one engine, the first in its language's
chain that works:

- English: Fish free -> HeyGen free -> edge-tts;
- any other language: HeyGen free -> Fish free -> edge-tts.

Fish and HeyGen are OpenRouter's free speech models. Each is called only
after the price gate in :mod:`openrouter` allows it (AGENTS.md rule 1). An
engine is skipped and the next one tried on a missing key, a refused or
failed price check, HTTP 401/402/408/429/5xx (any non-200), a network
error or timeout, an empty or invalid audio body, or a requested format
other than MP3 with no ffmpeg to make it (MP3 bytes are never written
under another format's name). HeyGen answers raw PCM
(44.1 kHz, 16-bit, mono), which ffmpeg converts; without ffmpeg HeyGen is
skipped. edge-tts runs in the sidecar through :mod:`adapter` (rule 4); its
audio is for private use only (rule 3).

The audio is written to ``output_path`` and nowhere else: scratch work goes
to a temporary directory that is removed afterwards, and the result reaches
``output_path`` through a temp file beside it and ``os.replace``, so a failed
engine or a failed write leaves ``output_path`` untouched. A write error
stops the request instead of trying the next engine. Hermes passes a temporary path there and
renames it, so nothing may be keyed by that path.

Voices: one default per engine and language, overridable with the plugin
setting ``voices`` (``{engine: {language or "*": voice}}``) or, for one
engine, with ``tts.voice: <engine>:<voice>``. Per-profile personas come later.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.tts_provider import TTSProvider

from . import adapter, openrouter, sidecar

log = logging.getLogger("onair")

PROVIDER_NAME = "onair"

FISH = "fish"
HEYGEN = "heygen"
EDGE = "edge"
ENGINES = (FISH, HEYGEN, EDGE)

FISH_MODEL = "fish-audio/s2.1-pro-free:free"
HEYGEN_MODEL = "heygen/heygen-voice-1-turbo:free"
MODELS = {FISH: FISH_MODEL, HEYGEN: HEYGEN_MODEL}

CHAIN_EN = (FISH, HEYGEN, EDGE)
CHAIN_OTHER = (HEYGEN, FISH, EDGE)

#: HeyGen's raw PCM, measured: signed 16-bit little-endian, mono, 44.1 kHz.
HEYGEN_PCM_RATE = 44100
HEYGEN_PCM_CHANNELS = 1

FORMATS = ("mp3", "wav", "ogg", "opus", "flac")
DEFAULT_FORMAT = "mp3"
FFMPEG_TIMEOUT_S = 120

DEFAULT_LANGUAGE = "auto"
DEFAULT_FFMPEG_BIN = ""

#: Default voice per engine and language ("*" = any other language). The EN
#: voices are men's, the others women's, so a fallback keeps the voice type.
DEFAULT_VOICES: dict[str, dict[str, str]] = {
    FISH: {
        "en": "536d3a5e000945adb7038665781a4aca",  # Ethan
        "*": "9a9cf47702da476aa4629e2506d4a857",  # Hannah (measured in fr and vi)
    },
    HEYGEN: {"en": "lucian", "*": "nadine"},
    EDGE: {
        "en": "en-US-AndrewNeural",
        "fr": "fr-FR-DeniseNeural",
        "de": "de-DE-KatjaNeural",
        "es": "es-ES-ElviraNeural",
        "it": "it-IT-ElsaNeural",
        "pt": "pt-BR-FranciscaNeural",
        "nl": "nl-NL-ColetteNeural",
        "ja": "ja-JP-NanamiNeural",
        "zh": "zh-CN-XiaoxiaoNeural",
        "vi": "vi-VN-HoaiMyNeural",
        "*": "en-US-EmmaMultilingualNeural",
    },
}

_LANG = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})*")
# Letters only Vietnamese uses (â, ê, ô are shared with French).
_VI_CHARS = frozenset("ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ")
_FR_WORDS = frozenset(
    [
        "le", "la", "les", "des", "une", "est", "et", "pas", "que", "qui", "dans", "pour",
        "avec", "sur", "nous", "vous", "c'est", "du", "au", "aux", "ce", "cette", "je", "il",
        "elle", "sont",
    ]
)

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]
Which = Callable[[str], "str | None"]


class EngineError(Exception):
    """One engine could not speak this request; the router tries the next one."""


class AllEnginesFailed(RuntimeError):
    """No engine could speak the request; the message lists each engine's reason."""


class OutputWriteError(RuntimeError):
    """An engine spoke, but its audio could not be written to ``output_path``.

    Not an engine failure: the next engine would hit the same disk, so the
    request stops here and ``output_path`` is left as it was.
    """


# ------------------------------------------------------------------ settings


def detect_language(text: str) -> str:
    """``vi``, ``fr`` or ``en`` from the text (a light heuristic)."""
    low = text.lower()
    if sum(ch in _VI_CHARS for ch in low) >= 2:
        return "vi"
    words = [w.strip(".,;:!?«»\"'()") for w in low.split()]
    hits = sum(w in _FR_WORDS for w in words)
    accents = sum(ch in "éèêàçùœ" for ch in low)
    if words and (hits / len(words) > 0.12 or accents >= 3):
        return "fr"
    return "en"


def resolve_language(setting: Any, text: str) -> str:
    """The configured language tag (lowercased), or the detected one for ``auto``/invalid."""
    value = setting.strip().lower() if isinstance(setting, str) else ""
    if value and value != "auto" and _LANG.fullmatch(value):
        return value
    return detect_language(text)


def chain_for(language: str) -> tuple[str, ...]:
    return CHAIN_EN if language.split("-", 1)[0] == "en" else CHAIN_OTHER


def voice_for(engine: str, language: str, overrides: Any = None) -> str:
    """The voice for *engine* in *language*: plugin setting first, then the default."""
    base = language.split("-", 1)[0]
    for table in (overrides if isinstance(overrides, Mapping) else {}, DEFAULT_VOICES):
        voices = table.get(engine)
        if not isinstance(voices, Mapping):
            continue
        for key in (language, base, "*"):
            value = voices.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return DEFAULT_VOICES[engine]["*"]


def pinned_voice(voice: Any) -> tuple[str, str] | None:
    """``(engine, voice)`` from a ``tts.voice`` of the form ``<engine>:<voice>``."""
    if not isinstance(voice, str):
        return None
    engine, sep, name = voice.partition(":")
    engine, name = engine.strip().lower(), name.strip()
    return (engine, name) if sep and engine in ENGINES and name else None


def find_ffmpeg(
    configured: Any = DEFAULT_FFMPEG_BIN, which: Which = shutil.which, home: Path | None = None
) -> str | None:
    """ffmpeg: the ``ffmpeg_bin`` setting, else PATH, else Hermes's managed copy.

    The setting is a path only when it holds a path separator or starts with
    ``~``; a bare name is looked up on PATH, never in the current directory.
    """
    if isinstance(configured, str) and configured.strip():
        value = configured.strip()
        if os.sep in value or (os.altsep and os.altsep in value) or value.startswith("~"):
            path = Path(os.path.abspath(os.path.expanduser(value)))  # "./ffmpeg" stays a path
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return which(value)
    found = which("ffmpeg")
    if found:
        return found
    tools = (home or Path.home()) / ".hermes" / "tools"
    for candidate in sorted(tools.glob("ffmpeg-*/bin/ffmpeg"), reverse=True):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# ------------------------------------------------------------------- engines


@dataclass
class Environment:
    """What one request may use. Every outside effect is injected."""

    data_dir: Path
    ffmpeg: str | None
    http: openrouter.Http = openrouter.urllib_http
    runner: Runner = subprocess.run
    catalog: openrouter.Catalog | None = None
    environ: Mapping[str, str] | None = None
    attempts: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.catalog is None:
            self.catalog = openrouter.Catalog(self.data_dir, http=self.http)


def ffmpeg_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """ffmpeg's environment: ours without the key and the other paid-service names.

    Reuses the adapter's scrub list (``adapter.SCRUBBED_ENV``), which holds
    ``OPENROUTER_API_KEY``; ffmpeg needs none of them (rule 2, defence in depth).
    """
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if k not in adapter.SCRUBBED_ENV}


def _ffmpeg(env: Environment, args: list[str], out: Path) -> None:
    if not env.ffmpeg:
        raise EngineError("ffmpeg not found")
    argv = [env.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args, str(out)]
    try:
        proc = env.runner(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=FFMPEG_TIMEOUT_S, check=False, env=ffmpeg_env(env.environ),
        )
    except subprocess.TimeoutExpired:
        raise EngineError(f"ffmpeg did not finish within {FFMPEG_TIMEOUT_S} s") from None
    except OSError as exc:
        raise EngineError(f"ffmpeg could not start: {exc.strerror or exc}") from None
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise EngineError(f"ffmpeg failed (exit {proc.returncode}): {tail[0][:200]}")


def _codec(fmt: str) -> list[str]:
    return {
        "mp3": ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
        "wav": ["-c:a", "pcm_s16le", "-f", "wav"],
        "ogg": ["-c:a", "libopus", "-b:a", "64k", "-f", "ogg"],
        "opus": ["-c:a", "libopus", "-b:a", "64k", "-f", "opus"],
        "flac": ["-c:a", "flac", "-f", "flac"],
    }[fmt]


def pcm_to(env: Environment, pcm: Path, out: Path, fmt: str, rate: int = HEYGEN_PCM_RATE) -> None:
    """Headerless s16le mono PCM at *rate* -> *out* in *fmt* (ffmpeg)."""
    args = ["-f", "s16le", "-ar", str(rate), "-ac", str(HEYGEN_PCM_CHANNELS), "-i", str(pcm)]
    _ffmpeg(env, [*args, *_codec(fmt)], out)


def _can_make(env: Environment, fmt: str) -> None:
    """Raise EngineError when *fmt* needs ffmpeg and there is none (never mislabel MP3)."""
    if fmt != "mp3" and not env.ffmpeg:
        raise EngineError(f"ffmpeg not found (needed to make {fmt})")


def mp3_to(env: Environment, mp3: Path, out: Path, fmt: str) -> None:
    """An MP3 -> *out* in *fmt*: copied for mp3, converted by ffmpeg otherwise."""
    if fmt == "mp3":
        shutil.copyfile(mp3, out)
        return
    _can_make(env, fmt)
    _ffmpeg(env, ["-i", str(mp3), *_codec(fmt)], out)


def _is_mp3(data: bytes) -> bool:
    return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def _pcm_rate(content_type: str) -> int:
    match = re.search(r"rate=(\d{4,6})", content_type or "")
    return int(match.group(1)) if match else HEYGEN_PCM_RATE


def render_fish(env: Environment, text: str, voice: str, work: Path, fmt: str) -> Path:
    _can_make(env, fmt)  # checked first: never spend a request we cannot convert
    response = _call(env, FISH, text, voice, "mp3")
    if not _is_mp3(response.body):
        raise EngineError("the answer is not MP3 audio")
    raw = work / "fish.mp3"
    raw.write_bytes(response.body)
    out = work / f"fish.out.{fmt}"
    mp3_to(env, raw, out, fmt)
    return out


def render_heygen(env: Environment, text: str, voice: str, work: Path, fmt: str) -> Path:
    if not env.ffmpeg:  # checked first: never spend a request we cannot convert
        raise EngineError("ffmpeg not found (needed for HeyGen's raw PCM)")
    response = _call(env, HEYGEN, text, voice, "pcm")
    audio = response.body
    if len(audio) < 2 or len(audio) % 2:
        raise EngineError("the answer is not 16-bit PCM audio")
    raw = work / "heygen.pcm"
    raw.write_bytes(audio)
    out = work / f"heygen.out.{fmt}"
    pcm_to(env, raw, out, fmt, _pcm_rate(response.header("Content-Type")))
    return out


def render_edge(
    env: Environment, text: str, voice: str, language: str, work: Path, fmt: str
) -> Path:
    _can_make(env, fmt)
    venv = sidecar.venv_dir(env.data_dir)
    if sidecar.ownership(venv) not in (sidecar.OWNED, sidecar.ADOPTABLE):
        raise EngineError("the sidecar is not set up; run `hermes onair setup`")
    job = work / "edge"
    job.mkdir()
    try:
        mp3 = adapter.edge_render(
            venv, text, voice, language, job, runner=env.runner, env=env.environ
        )
    except adapter.AdapterError as exc:
        raise EngineError(str(exc)) from None
    out = work / f"edge.out.{fmt}"
    mp3_to(env, mp3, out, fmt)
    return out


def deliver(made: Path, out: Path) -> None:
    """Put *made* at *out* atomically: a temp file beside *out*, then ``os.replace``.

    On any error the temp file is removed, *out* is untouched, and
    :class:`OutputWriteError` is raised.
    """
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".part", dir=out.parent)
        with os.fdopen(fd, "wb") as dst, open(made, "rb") as src:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, out)
        tmp = None
    except OSError as exc:
        raise OutputWriteError(
            f"cannot write the audio to {out}: {exc.strerror or type(exc).__name__}"
        ) from None
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _call(env: Environment, engine: str, text: str, voice: str, response_format: str):
    try:
        return openrouter.speech(
            MODELS[engine], text, voice, response_format,
            catalog=env.catalog, http=env.http, environ=env.environ,
        )
    except openrouter.OpenRouterError as exc:
        raise EngineError(str(exc)) from None


def speak(
    text: str,
    output_path: str | Path,
    *,
    env: Environment,
    language: str,
    fmt: str = DEFAULT_FORMAT,
    voices: Any = None,
    pinned: tuple[str, str] | None = None,
) -> str:
    """Speak *text* into *output_path* with the first engine that works; return the engine.

    Raises :class:`AllEnginesFailed` when none does, and :class:`OutputWriteError`
    (without trying another engine) when the audio cannot be written; in both
    cases *output_path* is untouched.
    """
    if not text or not text.strip():
        raise ValueError("nothing to speak: empty text")
    fmt = fmt if fmt in FORMATS else DEFAULT_FORMAT
    key = openrouter.api_key(env.environ)
    out = Path(output_path)
    for engine in chain_for(language):
        voice = voice_for(engine, language, voices)
        if pinned and pinned[0] == engine:
            voice = pinned[1]
        with tempfile.TemporaryDirectory(prefix="onair-") as tmp:
            work = Path(tmp)
            try:
                if engine == FISH:
                    made = render_fish(env, text, voice, work, fmt)
                elif engine == HEYGEN:
                    made = render_heygen(env, text, voice, work, fmt)
                else:
                    made = render_edge(env, text, voice, language, work, fmt)
                if made.stat().st_size == 0:
                    raise EngineError("empty audio")
            except (EngineError, OSError) as exc:
                reason = openrouter.scrub(str(exc), key)
                env.attempts.append({"engine": engine, "voice": voice, "error": reason})
                log.warning("%s could not speak (%s); trying the next engine", engine, reason)
                continue
            try:
                deliver(made, out)  # a write error stops the request: no next engine
            except OutputWriteError as exc:
                env.attempts.append({"engine": engine, "voice": voice, "error": str(exc)})
                raise
        env.attempts.append({"engine": engine, "voice": voice, "error": ""})
        log.info("spoke %d chars with %s (%s, %s)", len(text), engine, voice, language)
        return engine
    detail = "; ".join(f"{a['engine']}: {a['error']}" for a in env.attempts)
    raise AllEnginesFailed(f"no free voice engine could speak this: {detail}")


# ------------------------------------------------------------------ provider

GetConfig = Callable[..., Any]


class OnAirTTS(TTSProvider):
    """The ``onair`` TTS provider. Settings are read on every call."""

    def __init__(
        self,
        get_config: GetConfig,
        data_dir: Callable[[], Path],
        *,
        http: openrouter.Http = openrouter.urllib_http,
        runner: Runner = subprocess.run,
        which: Which = shutil.which,
    ) -> None:
        self._get_config = get_config
        self._data_dir = data_dir
        self._http = http
        self._runner = runner
        self._which = which

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "On Air (free voices)"

    def _setting(self, key: str, default: Any) -> Any:
        try:
            value = self._get_config(key, default)
        except Exception:  # a broken config read must not break speech
            return default
        return default if value is None else value

    def is_available(self) -> bool:
        """True when some engine could run: a key for OpenRouter, or the sidecar. No network."""
        if openrouter.api_key() is not None:
            return True
        try:
            venv = sidecar.venv_dir(self._data_dir())
            return sidecar.ownership(venv) in (sidecar.OWNED, sidecar.ADOPTABLE)
        except Exception:
            return False

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": FISH_MODEL, "display": "Fish Audio S2.1 Pro (free)"},
            {"id": HEYGEN_MODEL, "display": "HeyGen Voice 1 Turbo (free)"},
            {"id": "edge-tts", "display": "edge-tts (private use)"},
        ]

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
        format: str = DEFAULT_FORMAT,  # the TTSProvider signature's name
        **extra: Any,
    ) -> str:
        fmt = (format or DEFAULT_FORMAT).strip().lower()
        env = Environment(
            data_dir=Path(self._data_dir()),
            ffmpeg=find_ffmpeg(self._setting("ffmpeg_bin", DEFAULT_FFMPEG_BIN), self._which),
            http=self._http,
            runner=self._runner,
        )
        language = resolve_language(self._setting("language", DEFAULT_LANGUAGE), text)
        speak(
            text, output_path, env=env, language=language, fmt=fmt,
            voices=self._setting("voices", {}), pinned=pinned_voice(voice),
        )
        return str(output_path)
