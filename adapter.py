"""The one module that knows DropCast (AGENTS.md rule 4).

Everything about DropCast lives here: the pinned install spec, how to tell
whether the sidecar holds the pinned commit, the CLI JSON contract
(``dropcast run … --adapter headless --headless --root DIR`` then
``dropcast resume JOB``), and the single-text edge-tts render the voice
router falls back to (``python -m dropcast.edgetts render``, see
:func:`edge_render`). Other modules only pass around the argv lists and
results this module returns.

DropCast's Python internals are not public, so this module only drives the
``dropcast`` executable inside the sidecar venv and parses the JSON it prints.

Free and private by construction (rules 1 and 3): every run/resume call is
headless with a zero budget, uses the free edge-tts engine, never publishes,
and its child environment drops the keys and knobs that could make DropCast
spend money or publish.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# --- The pin: the only place the DropCast source and commit are written. ---
DROPCAST_REPO = "git+ssh://git@github.com/luongnv89/dropcast"
DROPCAST_PIN = "821715c"
PACKAGE = "dropcast"
NAME = "DropCast"
EXTRAS: tuple[str, ...] = ("edgetts",)

_FULL_SHA = re.compile(r"[0-9a-f]{40}")

#: Environment names DropCast reads that could spend money, reach a paid
#: service, publish, or override the headless/zero-budget flags. They never
#: reach a DropCast child process. (DropCast reads other names too, such as
#: its theme, whisper model and auto-resume; none of them can spend or
#: publish while the engine is fixed to edge-tts.)
SCRUBBED_ENV: frozenset[str] = frozenset(
    {
        "OPENROUTER_API_KEY",
        "ELEVENLABS_API_KEY",
        "TAVILY_API_KEY",
        "TINYFISH_API_KEY",
        "VOICECAST_HOME",
        "DROPCAST_PUBLISH_CMD",
        "DROPCAST_APPROVE_BUDGET",
        "DROPCAST_APPROVE_BUDGET_USD",
        "DROPCAST_ADAPTER",
        "DROPCAST_HEADLESS",
        "DROPCAST_HOME",
    }
)

#: Flags added to every run and resume: no publishing, nothing paid.
_SAFE_FLAGS: tuple[str, ...] = (
    "--no-publish",
    "--approve-budget",
    "0",
    "--approve-budget-usd",
    "0",
    "--engine",
    "edgetts",
)

#: ``--answer yes`` approves a paid render, so it is never sent.
_ALLOWED_ANSWERS = frozenset({"no", "free"})

RUN_TIMEOUT_S = 600
SMOKE_TIMEOUT_S = 60

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class AdapterError(RuntimeError):
    """DropCast could not be called, or answered outside its JSON contract."""


class JobStatus(StrEnum):
    """``status`` of a DropCast result (``schemas/result.schema.json``).

    There is deliberately no status-level "resumable": whether a job can go
    on depends on the result, not its status. Every non-delivered status can
    carry a ``dropcast resume`` command, yet a stopped run may need a budget
    and a failed ingest's ``next`` starts over with ``dropcast run``. Use
    :attr:`JobResult.resumable` / :attr:`JobResult.resume_blocker`.
    """

    NEEDS_AGENT = "needs_agent"
    NEEDS_APPROVAL = "needs_approval"
    RENDERING = "rendering"
    DELIVERED = "delivered"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        """True when DropCast ended the job: delivered, or stopped.

        A stopped job spent nothing (declined, or no free route) and moves on
        only after a new decision. A failed job is NOT terminal: DropCast keeps
        it for a re-run (``run.py`` builds its ``next`` as ``dropcast resume``).
        """
        return self in TERMINAL

    @property
    def needs_input(self) -> bool:
        """True when the caller must act (write a script, answer) and resume."""
        return self in NEEDS_INPUT


TERMINAL: frozenset[JobStatus] = frozenset({JobStatus.DELIVERED, JobStatus.STOPPED})
NEEDS_INPUT: frozenset[JobStatus] = frozenset(
    {JobStatus.NEEDS_AGENT, JobStatus.NEEDS_APPROVAL}
)

#: ``required`` of ``schemas/result.schema.json`` (result version 1).
RESULT_VERSION = 1
_REQUIRED_KEYS = (
    "version", "status", "step", "job", "message", "reply", "next", "steps", "request", "notes",
)
_STEPS = frozenset({"ingest", "brief", "script", "engine", "approval", "render", "reply"})


@dataclass(frozen=True)
class JobResult:
    """One DropCast result, parsed. ``raw`` keeps the full object."""

    status: JobStatus
    step: str
    job: str | None
    message: str
    reply: Mapping[str, Any] | None
    next: str | None
    raw: Mapping[str, Any] = field(repr=False)

    @property
    def resumable(self) -> bool:
        """True when :func:`resume` can move this job on (see :attr:`resume_blocker`)."""
        return self.resume_blocker is None

    @property
    def resume_blocker(self) -> str | None:
        """Why :func:`resume` cannot move this job on, or None when it can.

        A result is resumable only when it has a job folder, its ``next`` is a
        ``dropcast resume`` command, and that resume needs nothing this plugin
        refuses (``--answer yes``, a budget flag, an engine other than
        edgetts). The ``next`` shapes at the pinned commit (``run.py``):

        - needs_approval: ``dropcast resume JOB --answer yes|no|free``: resumable
          with ``answer="free"`` or ``"no"``;
        - stopped after a declined approval: ``dropcast resume JOB``, with the
          message naming ``--answer yes`` "(or --answer free)": resumable with
          ``answer="free"``;
        - a headless stop (the free engine cannot be used, ``approval.status``
          not ``declined``): ``dropcast resume JOB``, but only a budget, a
          shorter length or installing the free engine would render: blocked;
        - failed at a step: ``dropcast resume JOB``: resumable (a re-run);
        - failed ingest found on resume: ``job`` set but ``next`` is the
          original ``dropcast run …``: blocked (it starts over).
        """
        if self.status is JobStatus.DELIVERED:
            return "the job is delivered"
        if self.job is None:
            return "no job folder: DropCast must start over (`dropcast run`)"
        if not self.next:
            return "DropCast offers no next command"
        try:
            words = shlex.split(self.next)
        except ValueError:
            return "next is not a command line"
        if words[:2] != ["dropcast", "resume"]:
            return f"next starts over instead of resuming: {' '.join(words[:2])}"
        problem = _forbidden_resume_flags(words[2:])
        if problem:
            return problem
        if self.status is JobStatus.STOPPED:
            approval = self.raw.get("approval")
            decided = approval.get("status") if isinstance(approval, dict) else None
            if decided != "declined":
                return (
                    "headless run stopped: the free engine cannot be used, and only a "
                    "paid budget or a change to the job would render it"
                )
        return None


def _forbidden_resume_flags(args: list[str]) -> str | None:
    """What in a ``dropcast resume`` argument list this plugin will not send."""
    i = 0
    while i < len(args):
        flag, _, inline = args[i].partition("=")
        takes_value = flag in ("--answer", "--engine") and not inline
        value = inline or (args[i + 1] if takes_value and i + 1 < len(args) else "")
        if flag in ("--approve-budget", "--approve-budget-usd"):
            return f"the resume needs a budget ({flag}): paid renders are refused"
        if flag == "--answer" and not set(value.split("|")) & _ALLOWED_ANSWERS:
            return f"the resume needs --answer {value or '?'}: only no or free are sent"
        if flag == "--engine" and value != "edgetts":
            return f"the resume needs --engine {value or '?'}: only edgetts is used"
        i += 2 if takes_value else 1
    return None


# --- Install surface (used by ``hermes onair setup``). ---


def install_spec() -> str:
    """The PEP 508 requirement for the pinned DropCast, with its extras."""
    extras = f"[{','.join(EXTRAS)}]" if EXTRAS else ""
    return f"{PACKAGE}{extras} @ {DROPCAST_REPO}@{DROPCAST_PIN}"


def install_args() -> list[str]:
    """Arguments for ``uv pip install``: always (re)install the pinned commit."""
    return ["--reinstall-package", PACKAGE, install_spec()]


def dropcast_executable(venv: Path) -> Path:
    """The ``dropcast`` console script inside the sidecar venv."""
    return Path(venv) / "bin" / PACKAGE


def smoke_argv(venv: Path) -> list[str]:
    """A command that exits 0 when the sidecar's DropCast can start."""
    return [str(dropcast_executable(venv)), "--help"]



def smoke(venv: Path, runner: Runner = subprocess.run) -> tuple[bool, str]:
    """Start the sidecar's DropCast once (``--help``): ``(ok, detail)``. Never raises."""
    try:
        proc = runner(
            smoke_argv(venv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SMOKE_TIMEOUT_S,
            env=child_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"no answer within {SMOKE_TIMEOUT_S} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not start: {exc.__class__.__name__}"
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}"
    return True, "ok"


def installed_commit(venv: Path) -> str | None:
    """The full commit SHA DropCast was installed from, or None.

    Reads PEP 610 ``direct_url.json`` from the venv's ``dropcast-*.dist-info``.
    Only a 40-hex git commit from the DropCast repository counts. Anything
    else (another URL, a local path install, a missing or broken file) is
    None, so the caller reinstalls.
    """
    record = _install_record(venv)
    return record[0] if record else None


def pinned_install(venv: Path) -> bool:
    """True when uv installed DropCast in *venv* from the repo at the pinned commit.

    The evidence ``hermes onair setup`` needs before it treats a venv as its
    own: PEP 610 ``direct_url.json`` names the DropCast repository with a
    ``commit_id`` starting with the pin, and the dist-info's ``INSTALLER``
    says ``uv``. Structural evidence, not proof: anyone who can write the
    plugin's data dir can copy a real install.
    """
    record = _install_record(venv)
    if record is None or not pin_matches(record[0]):
        return False
    installer = record[1] / "INSTALLER"
    try:
        return not installer.is_symlink() and installer.read_text(encoding="utf-8").strip() == "uv"
    except OSError:
        return False


def _install_record(venv: Path) -> tuple[str, Path] | None:
    """(commit, dist-info dir) of DropCast's PEP 610 git record in *venv*, or None."""
    pattern = f"lib/python*/site-packages/{PACKAGE}-*.dist-info/direct_url.json"
    for record in sorted(Path(venv).glob(pattern)):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not _is_repo_url(data.get("url")):
            continue
        vcs = data.get("vcs_info")
        commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
        if isinstance(commit, str) and _FULL_SHA.fullmatch(commit):
            return commit, record.parent
    return None


def _is_repo_url(url: object) -> bool:
    """True for the DropCast repo URL as PEP 610 records it (no ``git+``)."""
    if not isinstance(url, str):
        return False
    url = url.removeprefix("git+").rstrip("/").removesuffix(".git")
    return url == DROPCAST_REPO.removeprefix("git+")


def pin_matches(commit: str | None) -> bool:
    """True when *commit* (a full SHA) is the pinned commit."""
    if not commit or _FULL_SHA.fullmatch(commit) is None:
        return False
    return commit.startswith(DROPCAST_PIN)


# --- CLI JSON contract. ---


def parse_result(stdout: str) -> JobResult:
    """Parse the JSON result ``dropcast run``/``resume`` prints on stdout."""
    text = (stdout or "").strip()
    if not text:
        raise AdapterError("DropCast printed no result")
    try:
        data = json.loads(text)
    except ValueError:
        # Tolerate stray lines before the result: decode from the last line
        # that opens a top-level object.
        data = None
        lines = text.splitlines()
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("{"):
                try:
                    data = json.loads("\n".join(lines[i:]))
                except ValueError:
                    continue
                break
        if data is None:
            raise AdapterError("DropCast result is not JSON") from None
    if not isinstance(data, dict):
        raise AdapterError("DropCast result is not a JSON object")
    _check_result(data)
    return JobResult(
        status=JobStatus(data["status"]),
        step=data["step"],
        job=data["job"],
        message=data["message"],
        reply=data["reply"],
        next=data["next"],
        raw=data,
    )


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _check_result(data: Mapping[str, Any]) -> None:
    """Hold *data* to ``schemas/result.schema.json`` (version 1) where it matters."""
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise AdapterError(f"DropCast result lacks: {', '.join(missing)}")
    if type(data["version"]) is not int or data["version"] != RESULT_VERSION:
        raise AdapterError(f"unsupported DropCast result version: {data['version']!r}")
    if data["status"] not in {s.value for s in JobStatus}:
        raise AdapterError(f"unknown DropCast status: {data['status']!r}")
    if data["step"] not in _STEPS:
        raise AdapterError(f"unknown DropCast step: {data['step']!r}")
    for key in ("job", "next"):
        if data[key] is not None and not isinstance(data[key], str):
            raise AdapterError(f"DropCast result has a malformed {key}")
    if not _is_text(data["message"]):
        raise AdapterError("DropCast result has a malformed message")
    reply = data["reply"]
    if reply is not None:
        attachments = reply.get("attachments") if isinstance(reply, dict) else None
        if (
            not isinstance(reply, dict)
            or not _is_text(reply.get("adapter"))
            or not _is_text(reply.get("text"))
            or not isinstance(attachments, list)
            or not all(_is_text(item) for item in attachments)
        ):
            raise AdapterError("DropCast result has a malformed reply")
    if not isinstance(data["steps"], dict) or not isinstance(data["request"], dict):
        raise AdapterError("DropCast result has malformed steps or request")
    if not isinstance(data["notes"], list) or not all(isinstance(n, str) for n in data["notes"]):
        raise AdapterError("DropCast result has malformed notes")


def _headless(root: Path) -> list[str]:
    return ["--adapter", "headless", "--headless", "--root", str(root), *_SAFE_FLAGS]


#: The shortest and longest episode a request may ask for, in minutes.
MIN_LENGTH_MIN = 0.2
MAX_LENGTH_MIN = 15.0


def _brief_flags(
    fmt: str | None, length_min: float | None, language: str | None, guests: int | None
) -> list[str]:
    """``--format``/``--length``/``--lang``/``--guests`` for a run or resume (checked)."""
    flags: list[str] = []
    if fmt is not None:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fmt):
            raise AdapterError(f"not a format name: {fmt!r}")
        flags += ["--format", fmt]
    if length_min is not None:
        if isinstance(length_min, bool) or not isinstance(length_min, int | float):
            raise AdapterError(f"not a length in minutes: {length_min!r}")
        if not MIN_LENGTH_MIN <= float(length_min) <= MAX_LENGTH_MIN:
            raise AdapterError(
                f"length {length_min} min is outside {MIN_LENGTH_MIN}-{MAX_LENGTH_MIN} min"
            )
        flags += ["--length", f"{float(length_min):g}"]
    if language is not None:
        if not _LANG_TAG.fullmatch(language):
            raise AdapterError(f"not a language tag: {language!r}")
        flags += ["--lang", language]
    if guests is not None:
        if isinstance(guests, bool) or not isinstance(guests, int) or guests < 0:
            raise AdapterError(f"not a guest count: {guests!r}")
        flags += ["--guests", str(guests)]
    return flags


def build_run_argv(
    venv: Path,
    root: Path,
    *,
    link: str | None = None,
    text: str | None = None,
    fmt: str | None = None,
    length_min: float | None = None,
    language: str | None = None,
    guests: int | None = None,
) -> list[str]:
    """``dropcast run`` for one *link* (or file) or pasted *text*, headless, under *root*.

    Pasted text never goes in argv (Linux caps one argument at 128 KiB): the
    argv says ``--text -`` and :func:`run` pipes the text to DropCast's stdin,
    the way DropCast reads pasted text from a pipe. A *link* of ``-`` (stdin)
    is refused. *fmt*, *length_min*, *language* and *guests* shape the brief
    (``--format``, ``--length``, ``--lang``, ``--guests``); None leaves
    DropCast's default.
    """
    if (link is None) == (text is None):
        raise AdapterError("give exactly one of link or text")
    value = link if link is not None else text
    if not value or not value.strip() or (link is not None and link.strip() == "-"):
        raise AdapterError("nothing to run: empty source")
    argv = [str(dropcast_executable(venv)), "run", *_headless(root),
            *_brief_flags(fmt, length_min, language, guests)]
    if text is not None:
        return [*argv, "--text", "-"]
    return [*argv, "--", link]


def build_resume_argv(
    venv: Path,
    job: str,
    root: Path,
    answer: str | None = None,
    *,
    voices: Path | None = None,
    length_min: float | None = None,
) -> list[str]:
    """``dropcast resume JOB``, headless and unpaid; *answer* is ``no`` or ``free``.

    *voices* is a ``voices.json`` for the render (``--voices``); *length_min*
    re-briefs the job to a new target length (``--length``).
    """
    if not job or not job.strip():
        raise AdapterError("nothing to resume: empty job")
    if answer is not None and answer not in _ALLOWED_ANSWERS:
        raise AdapterError(f"answer {answer!r} is not allowed (only: no, free)")
    argv = [str(dropcast_executable(venv)), "resume", *_headless(root)]
    if answer is not None:
        argv += ["--answer", answer]
    argv += _brief_flags(None, length_min, None, None)
    if voices is not None:
        argv += ["--voices", str(voices)]
    return [*argv, "--", job]


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a DropCast child: scrubbed, publishing off."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in SCRUBBED_ENV}
    env["DROPCAST_PUBLISH"] = "0"
    return env


def _call(
    argv: list[str],
    runner: Runner,
    env: Mapping[str, str] | None,
    stdin_text: str | None = None,
) -> JobResult:
    # Exactly one of stdin/input: a pipe carrying the text, or no stdin at all.
    feed = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    try:
        proc = runner(
            argv,
            **feed,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            env=child_env(env),
            check=False,
        )
    except FileNotFoundError:
        raise AdapterError("DropCast is not installed; run `hermes onair setup`") from None
    except subprocess.TimeoutExpired:
        raise AdapterError(f"DropCast did not answer within {RUN_TIMEOUT_S} s") from None
    except OSError as exc:  # PermissionError, E2BIG, …
        reason = exc.strerror or exc.__class__.__name__
        raise AdapterError(f"DropCast could not start: {reason}") from None
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no detail"]
        raise AdapterError(f"DropCast failed (exit {proc.returncode}): {detail[0]}")
    return parse_result(proc.stdout)


def run(
    venv: Path,
    root: Path,
    *,
    link: str | None = None,
    text: str | None = None,
    fmt: str | None = None,
    length_min: float | None = None,
    language: str | None = None,
    guests: int | None = None,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> JobResult:
    """Start a DropCast job. Exit 0 (delivered) and 1 (any other status) parse."""
    argv = build_run_argv(venv, root, link=link, text=text, fmt=fmt, length_min=length_min,
                          language=language, guests=guests)
    return _call(argv, runner, env, stdin_text=text)


def resume(
    venv: Path,
    job: str,
    root: Path,
    answer: str | None = None,
    *,
    voices: Path | None = None,
    length_min: float | None = None,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> JobResult:
    """Continue a DropCast job after the caller acted on its last status."""
    argv = build_resume_argv(venv, job, root, answer, voices=voices, length_min=length_min)
    return _call(argv, runner, env)


# --- Single-text edge-tts render (the voice router's last engine). ---
#
# DropCast at the pin has no "speak this one text" command. The closest public
# entry is the documented module CLI ``python -m dropcast.edgetts render SCRIPT
# VOICES --out JOB [--voice SPEAKER=VOICE]`` (see its module docstring), which
# prints one JSON object (exit 0 rendered, 1 not rendered, 2 usage error). This
# section writes a one-segment script.json and an empty voices.json, pins the
# one speaker's voice with ``--voice``, and reads ``episode`` from the JSON.

EDGE_TIMEOUT_S = 180
EDGE_MODULE = f"{PACKAGE}.edgetts"
_EDGE_SPEAKER = "host"
_LANG_TAG = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*")
_EDGE_VOICE = re.compile(r"[A-Za-z]{2,3}-[A-Za-z0-9-]+")
#: Show music is off for a single spoken text (DropCast's ``music.THEME_ENV``).
_NO_THEME = {"DROPCAST_THEME": "off"}


def edge_render_argv(venv: Path, job: Path, voice: str) -> list[str]:
    """``python -m dropcast.edgetts render`` for the files :func:`edge_render` writes."""
    if not _EDGE_VOICE.fullmatch(voice or ""):
        raise AdapterError(f"not an edge-tts voice name: {voice!r}")
    python = Path(venv) / "bin" / "python"
    job = Path(job)
    return [
        str(python), "-I", "-m", EDGE_MODULE, "render",
        str(job / "script.json"), str(job / "voices.json"),
        "--out", str(job / "out"),
        "--voice", f"{_EDGE_SPEAKER}={voice}",
        "--timeout", str(EDGE_TIMEOUT_S - 30),
    ]


def edge_script(text: str, language: str) -> dict[str, Any]:
    """A one-speaker, one-segment script.json (schema-valid at the pin)."""
    words = len(text.split())
    minutes = max(round(words / 150, 3), 0.01)
    return {
        "title": "On Air",
        "format": "solo-explainer",
        "language": language if _LANG_TAG.fullmatch(language or "") else "en",
        "target_min": minutes,
        "est_min": minutes,
        "speakers": [{"id": _EDGE_SPEAKER, "name": "Host"}],
        "segments": [{"id": "s1", "speaker": _EDGE_SPEAKER, "text": text}],
    }


def edge_render(
    venv: Path,
    text: str,
    voice: str,
    language: str,
    job: Path,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Speak *text* with edge-tts *voice* through the sidecar; return the MP3 it wrote.

    *job* is an empty scratch folder the caller owns and removes; the MP3 is
    inside it. Raises :class:`AdapterError` on any failure. Free by
    construction: edge-tts needs no key, the child env is scrubbed, and the
    output is private use only (AGENTS.md rule 3).
    """
    if not text or not text.strip():
        raise AdapterError("nothing to speak: empty text")
    job = Path(job)
    argv = edge_render_argv(venv, job, voice)
    try:
        (job / "script.json").write_text(
            json.dumps(edge_script(text, language), ensure_ascii=False), encoding="utf-8"
        )
        (job / "voices.json").write_text('{"roles": {}}', encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"cannot write the edge-tts job: {exc.strerror or exc}") from None
    child = child_env(env)
    child.update(_NO_THEME)
    try:
        proc = runner(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=EDGE_TIMEOUT_S,
            env=child,
            check=False,
        )
    except FileNotFoundError:
        raise AdapterError("the sidecar is not installed; run `hermes onair setup`") from None
    except subprocess.TimeoutExpired:
        raise AdapterError(f"edge-tts did not answer within {EDGE_TIMEOUT_S} s") from None
    except OSError as exc:
        raise AdapterError(f"edge-tts could not start: {exc.strerror or exc}") from None
    data = _last_json_object(proc.stdout)
    if proc.returncode != 0 or not data or data.get("rendered") is not True:
        reason = data.get("reason") if data else None
        if not isinstance(reason, str) or not reason:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no detail"]
            reason = tail[0]
        raise AdapterError(f"edge-tts render failed (exit {proc.returncode}): {reason[:300]}")
    episode = data.get("episode")
    if not isinstance(episode, str) or not episode:
        raise AdapterError("edge-tts result names no episode")
    path = Path(episode)
    try:
        inside = path.resolve().is_relative_to(job.resolve())
    except (OSError, RuntimeError):
        inside = False
    if not inside or path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise AdapterError("edge-tts episode is missing, empty, or outside the job folder")
    return path


def _last_json_object(stdout: str | None) -> dict[str, Any] | None:
    """The JSON object a module CLI printed (indented, maybe after stray lines)."""
    text = (stdout or "").strip()
    lines = text.splitlines()
    for i in range(len(lines)):
        if lines[i].startswith("{"):
            try:
                data = json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
            return data if isinstance(data, dict) else None
    return None


# --- Episodes: the brief, the script contract, voices and the finish step. ---
#
# ``/onair`` drives one job: ``run`` stops at ``needs_agent`` (step script);
# the plugin reads the brief and the ingested source, writes ``script.json``
# and ``summary.txt`` from a neutral draft (:func:`write_script`), has
# DropCast check it (:func:`check_script`, the module CLI ``python -m
# dropcast.script finalize``, the same checks the script step runs), then
# either resumes the job so DropCast voices it with edge-tts (``--voices``,
# :func:`write_voices`), or renders the turns itself with a free OpenRouter
# engine and hands them to DropCast's finish step (:func:`finish_parts`).

EPISODE_FORMAT = "podcast-2host"
SCRIPT_MODULE = f"{PACKAGE}.script"
CHECK_TIMEOUT_S = 120
FINISH_TIMEOUT_S = 300

#: Neutral part names -> DropCast's ``show-v2`` section names, in their order.
SECTIONS: dict[str, str] = {
    "teaser": "Teaser",
    "intro": "Intro",
    "main": "Main",
    "counter": "Counterpoints",
    "corner": "Builder's Corner",
    "takeaway": "Takeaways",
    "question": "Open questions",
    "outro": "Outro",
}
#: DropCast's hosts by speaker id, with the voice type each one keeps (its brand).
HOST_GENDER: dict[str, str] = {"host_a": "man", "host_b": "woman"}
_ASKER_ROLE = "host-curious"
_EXPLAINER_ROLE = "host-expert"
_LENGTH_ERROR = re.compile(r"^est_min .* is not within ")


@dataclass(frozen=True)
class Brief:
    """What the script step asks for, read from the job's ``brief.json``."""

    job: Path
    language: str
    target_min: float
    word_range: tuple[int, int]
    word_budget: int
    show: str
    opening: str
    sign_off: str
    corner: str
    names: Mapping[str, str]  # speaker id -> host name
    roles: Mapping[str, str]  # speaker id -> role
    asker: str  # speaker id of the host who asks (says the opening line)
    explainer: str  # speaker id of the host who explains
    structure: str | None
    faithfulness: str


@dataclass(frozen=True)
class Source:
    """The ingested source (``source.json``): what the script may quote."""

    title: str
    author: str | None
    url: str | None
    language: str | None
    text: str
    word_count: int


@dataclass(frozen=True)
class ScriptCheck:
    """DropCast's verdict on ``script.json``: errors, and the estimate it computed."""

    ok: bool
    errors: tuple[str, ...]
    est_min: float | None

    @property
    def only_length(self) -> bool:
        """True when the length band is the only problem (fixable by re-briefing)."""
        return bool(self.errors) and all(_LENGTH_ERROR.match(e) for e in self.errors)


def _job_json(job: Path, name: str) -> dict[str, Any]:
    try:
        data = json.loads((Path(job) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdapterError(f"cannot read {name}: {getattr(exc, 'strerror', None) or exc}") from None
    if not isinstance(data, dict):
        raise AdapterError(f"{name} is not a JSON object")
    return data


def read_brief(job: str | Path) -> Brief:
    """The job's ``brief.json`` (written by ``run`` before it stops at the script step)."""
    job = Path(job)
    data = _job_json(job, "brief.json")
    try:
        length = data["length"]
        show = data["show"]
        hosts = data["hosts"]
        names = {sid: str(h["name"]) for sid, h in hosts.items()}
        roles = {sid: str(h["role"]) for sid, h in hosts.items()}
        low, high = (int(n) for n in length["word_range"])
        brief = Brief(
            job=job,
            language=str(data["language"]),
            target_min=float(data["target_min"]),
            word_range=(low, high),
            word_budget=int(length["word_budget"]),
            show=str(show["name"]),
            opening=str(show["opening"]),
            sign_off=str(show["sign_off"]),
            corner=str(show.get("builders_corner") or SECTIONS["corner"]),
            names=names,
            roles=roles,
            asker=next(sid for sid, role in roles.items() if role == _ASKER_ROLE),
            explainer=next(sid for sid, role in roles.items() if role == _EXPLAINER_ROLE),
            structure=data.get("structure"),
            faithfulness=str(data.get("faithfulness") or "factual"),
        )
    except (KeyError, TypeError, ValueError, StopIteration, AttributeError):
        raise AdapterError("brief.json is not a two-host brief this plugin can follow") from None
    if data.get("format") != EPISODE_FORMAT:
        raise AdapterError(f"the brief asks for {data.get('format')!r}, not {EPISODE_FORMAT}")
    return brief


def read_source(job: str | Path) -> Source:
    """The job's ``source.json``: title, author, link, language and text."""
    data = _job_json(Path(job), "source.json")
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise AdapterError("the source has no text")

    def opt(key: str) -> str | None:
        value = data.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    count = data.get("word_count")
    return Source(
        title=opt("title") or "",
        author=opt("author"),
        url=opt("url"),
        language=opt("language"),
        text=text,
        word_count=count if isinstance(count, int) else len(text.split()),
    )


def script_document(draft: Any, brief: Brief, source: Source,
                    target_min: float | None = None) -> dict[str, Any]:
    """``script.json`` (``podcast-2host``, ``show-v2``) for a neutral *draft*.

    *draft* has ``title`` and ``turns``; each turn has ``host`` (``"asker"`` or
    ``"explainer"``), ``part`` (a key of :data:`SECTIONS`), ``text`` and
    ``quote`` (words copied from the source that back the line, or None).
    Every quoted turn gets an ``ok`` fact-check entry. *target_min* replaces
    the brief's target (after a re-brief to the script's own length).
    """
    speakers = []
    for sid in sorted(brief.names):
        speakers.append({"id": sid, "name": brief.names[sid], "role": brief.roles[sid]})
    segments: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for index, turn in enumerate(draft.turns, 1):
        if turn.part not in SECTIONS:
            raise AdapterError(f"unknown script part: {turn.part!r}")
        sid = f"s{index:03d}"
        speaker = brief.asker if turn.host == "asker" else brief.explainer
        segments.append({"id": sid, "speaker": speaker, "section": SECTIONS[turn.part],
                         "text": turn.text, "cues": []})
        if turn.quote:
            checks.append({"claim": turn.text, "source_quote": turn.quote, "ok": True,
                           "segment": sid})
    target = float(target_min if target_min is not None else brief.target_min)
    document: dict[str, Any] = {
        "title": draft.title or source.title or "On Air",
        "format": EPISODE_FORMAT,
        "show": {"name": brief.show, "hosts": dict(brief.names)},
        "language": brief.language,
        "target_min": target,
        "est_min": target,
        "speakers": speakers,
        "segments": segments,
        "sources": [source.url or source.title or "pasted text"],
        "faithfulness": brief.faithfulness,
        "fact_check": checks,
    }
    if brief.structure:
        document["structure"] = brief.structure
    return document


#: The format a standup's ``script.json`` names. DropCast has no spec for it, so
#: the finish step treats it as a show (the show's theme, no story mood cues).
STANDUP_FORMAT = "onair-standup"


def segments_document(title: str, language: str, speakers: Mapping[str, str],
                      lines: list[tuple[str, str, str]]) -> dict[str, Any]:
    """A ``script.json`` for the finish step from ``(segment id, speaker id, text)`` lines.

    *speakers* maps speaker id -> display name (the transcript's voice tags).
    Only the finish step reads it (the theme, the transcript), never ``run``.
    """
    words = sum(len(text.split()) for _sid, _speaker, text in lines)
    minutes = max(round(words / 150, 2), 0.01)
    return {
        "title": title,
        "format": STANDUP_FORMAT,
        "language": language if _LANG_TAG.fullmatch(language or "") else "en",
        "target_min": minutes,
        "est_min": minutes,
        "speakers": [{"id": sid, "name": name} for sid, name in speakers.items()],
        "segments": [{"id": sid, "speaker": speaker, "text": text, "cues": []}
                     for sid, speaker, text in lines],
    }


def write_script(job: str | Path, document: Mapping[str, Any], summary: list[str]) -> None:
    """Write ``script.json`` and ``summary.txt`` (three short lines) into the job."""
    job = Path(job)
    lines = [" ".join(str(line).split())[:200] for line in summary][:3]
    try:
        (job / "script.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (job / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"cannot write the script: {exc.strerror or exc}") from None


def check_script_argv(venv: Path, job: Path) -> list[str]:
    """``python -m dropcast.script finalize`` on the job's script, against its source."""
    python = Path(venv) / "bin" / "python"
    return [str(python), "-I", "-m", SCRIPT_MODULE, "finalize",
            str(Path(job) / "script.json"), "--source", str(Path(job) / "source.json")]


def check_script(
    venv: Path,
    job: str | Path,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> ScriptCheck:
    """Run DropCast's script checks on ``script.json`` (it is polished in place).

    Exit 0 passes; exit 1 lists the problems, one per indented line (on
    stdout or stderr). The estimate is read back from the rewritten
    ``script.json``.
    """
    job = Path(job)
    try:
        proc = runner(
            check_script_argv(venv, job), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=CHECK_TIMEOUT_S, env=child_env(env), check=False,
        )
    except FileNotFoundError:
        raise AdapterError("the sidecar is not installed; run `hermes onair setup`") from None
    except subprocess.TimeoutExpired:
        raise AdapterError(f"the script check did not answer within {CHECK_TIMEOUT_S} s") from None
    except OSError as exc:
        raise AdapterError(f"the script check could not start: {exc.strerror or exc}") from None
    if proc.returncode not in (0, 1):
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no detail"]
        raise AdapterError(f"the script check failed (exit {proc.returncode}): {tail[0][:300]}")
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    errors = tuple(line.strip() for line in output.splitlines() if line.startswith("  "))
    if proc.returncode == 1 and not errors:
        errors = ("the script check failed without a reason",)
    est = None
    try:
        value = json.loads((job / "script.json").read_text(encoding="utf-8")).get("est_min")
        est = float(value) if isinstance(value, int | float) else None
    except (OSError, ValueError, AttributeError):
        est = None
    return ScriptCheck(ok=proc.returncode == 0, errors=errors, est_min=est)


def script_lines(job: str | Path) -> list[tuple[str, str, str]]:
    """``(segment id, speaker id, text)`` of the checked (polished) ``script.json``."""
    data = _job_json(Path(job), "script.json")
    lines = []
    for seg in data.get("segments") or []:
        if not isinstance(seg, dict):
            raise AdapterError("script.json has a malformed segment")
        sid, speaker, text = seg.get("id"), seg.get("speaker"), seg.get("text")
        if not (_is_text(sid) and _is_text(speaker) and _is_text(text)):
            raise AdapterError("script.json has a malformed segment")
        lines.append((sid, speaker, text))
    if not lines:
        raise AdapterError("script.json has no segments")
    return lines


def write_voices(path: Path, brief: Brief, voices: Mapping[str, str]) -> Path:
    """A ``voices.json`` giving each host (speaker id -> edge-tts voice) its voice.

    Keys are ``host-<name>/<lang>`` (how the engine voices Bob and Alice) and
    ``<role>/<lang>`` (how it voices a host with any other name).
    """
    roles: dict[str, Any] = {}
    primary = brief.language.split("-", 1)[0].lower()
    for sid, voice in voices.items():
        if not _EDGE_VOICE.fullmatch(voice or ""):
            raise AdapterError(f"not an edge-tts voice name: {voice!r}")
        # A host named Bob or Alice is voiced by name; any other host by its role.
        for key in (f"host-{brief.names[sid].lower()}", brief.roles[sid]):
            for lang in dict.fromkeys((brief.language, primary)):
                roles[f"{key}/{lang}"] = {"edgetts": voice}
    try:
        Path(path).write_text(json.dumps({"roles": roles}, indent=1), encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"cannot write voices.json: {exc.strerror or exc}") from None
    return Path(path)


@dataclass(frozen=True)
class Finished:
    """A finished episode: the MP3, its WebVTT transcript and its length."""

    episode: Path
    transcript: Path | None
    duration_s: float | None


def _finished(episode: str, transcript: Any, duration: Any) -> Finished:
    has_vtt = _is_text(transcript) and Path(transcript).is_file()
    return Finished(
        episode=Path(episode),
        transcript=Path(transcript) if has_vtt else None,
        duration_s=float(duration) if isinstance(duration, int | float) else None,
    )


def delivered(result: JobResult) -> Finished:
    """The episode a ``delivered`` result names."""
    if result.status is not JobStatus.DELIVERED:
        raise AdapterError(f"the job is {result.status.value}, not delivered")
    found: dict[str, Any] = {}
    for holder in (result.raw.get("delivery"), result.reply):
        if isinstance(holder, Mapping):
            for key in ("episode", "transcript", "duration_s"):
                if found.get(key) is None:
                    found[key] = holder.get(key)
    episode = found.get("episode")
    if not _is_text(episode) or not Path(episode).is_file():
        raise AdapterError("the delivered result names no episode file")
    transcript = found.get("transcript")
    duration = found.get("duration_s")
    return _finished(episode, transcript, duration)


# The finish step has no CLI at the pin (only ``measure`` and ``report``), so
# this one internal call runs inside the sidecar's Python. It reads a spec
# file (job, parts, engine record), joins the parts with DropCast's finish
# (theme, loudness, transcript.vtt, run.json) and prints one JSON object.
_FINISH_PY = """
import json, sys
from pathlib import Path
from dropcast import finish
spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
job = Path(spec["job"])
script = json.loads((job / "script.json").read_text(encoding="utf-8"))
parts = [finish.Part(Path(p["path"]), (finish.Piece(p["segment"], p["text"]),))
         for p in spec["parts"]]
run = {"engine": spec.get("engine", "openrouter"), "model": spec["model"],
       "voices": spec["voices"],
       "chunks": [{"id": p["segment"], "chars": len(p["text"]), "status": "done"}
                  for p in spec["parts"]],
       "credits_estimated": None, "credits_used": None, "cost_estimated_usd": 0.0,
       "private_use": bool(spec.get("private_use")), "cues_dropped": 0, "duration_s": None,
       "started": spec["started"], "finished": None, "errors": []}
try:
    done = finish.finish_episode(parts, job, script=script, fmt="mp3", run=run,
                                 check_speech=False)
except finish.FinishError as exc:
    run["errors"].append(str(exc))
    finish.write_report(job, run, script.get("title"))
    print(json.dumps({"ok": False, "error": str(exc)}))
    sys.exit(1)
run["duration_s"] = done.duration_s
finish.write_report(job, run, script.get("title"))
print(json.dumps({"ok": True, "episode": str(done.episode),
                  "transcript": str(done.transcript), "duration_s": done.duration_s}))
"""


def finish_argv(venv: Path, spec: Path) -> list[str]:
    return [str(Path(venv) / "bin" / "python"), "-I", "-c", _FINISH_PY, str(spec)]


def finish_parts(
    venv: Path,
    job: str | Path,
    parts: list[tuple[Path, str, str]],
    *,
    model: str,
    voices: Mapping[str, str],
    started: str,
    engine: str = "openrouter",
    private_use: bool = False,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> Finished:
    """Join rendered *parts* ``(mp3 path, segment id, text)`` with DropCast's finish step.

    Writes ``episode.mp3``, ``transcript.vtt`` and ``run.json`` into *job*;
    ``run.json`` records *engine* and *private_use* (True for edge-tts parts).
    """
    job = Path(job)
    spec = job / "onair-finish.json"
    record = {
        "job": str(job), "model": model, "voices": dict(voices), "started": started,
        "engine": engine, "private_use": bool(private_use),
        "parts": [{"path": str(p), "segment": s, "text": t} for p, s, t in parts],
    }
    try:
        spec.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"cannot write the finish spec: {exc.strerror or exc}") from None
    try:
        proc = runner(
            finish_argv(venv, spec), stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=FINISH_TIMEOUT_S, env=child_env(env), check=False,
        )
    except FileNotFoundError:
        raise AdapterError("the sidecar is not installed; run `hermes onair setup`") from None
    except subprocess.TimeoutExpired:
        raise AdapterError(f"the finish step did not answer within {FINISH_TIMEOUT_S} s") from None
    except OSError as exc:
        raise AdapterError(f"the finish step could not start: {exc.strerror or exc}") from None
    data = _last_json_object(proc.stdout)
    if proc.returncode != 0 or not data or data.get("ok") is not True:
        reason = data.get("error") if data else None
        if not isinstance(reason, str) or not reason:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no detail"]
            reason = tail[0]
        raise AdapterError(f"the finish step failed (exit {proc.returncode}): {reason[:300]}")
    episode = data.get("episode")
    if not _is_text(episode) or not Path(episode).is_file():
        raise AdapterError("the finish step names no episode file")
    transcript = data.get("transcript")
    duration = data.get("duration_s")
    return _finished(episode, transcript, duration)
