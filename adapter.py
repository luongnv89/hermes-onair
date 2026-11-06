"""The one module that knows DropCast (AGENTS.md rule 4).

Everything about DropCast lives here: the pinned install spec, how to tell
whether the sidecar holds the pinned commit, and the CLI JSON contract
(``dropcast run … --adapter headless --headless --root DIR`` then
``dropcast resume JOB``). Other modules only pass around the argv lists and
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
    """``status`` of a DropCast result (``schemas/result.schema.json``)."""

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
    def resumable(self) -> bool:
        """True when ``dropcast resume`` can continue a job left in this status."""
        return self in RESUMABLE

    @property
    def needs_input(self) -> bool:
        """True when the caller must act (write a script, answer) and resume."""
        return self in NEEDS_INPUT


TERMINAL: frozenset[JobStatus] = frozenset({JobStatus.DELIVERED, JobStatus.STOPPED})
RESUMABLE: frozenset[JobStatus] = frozenset(
    {
        JobStatus.NEEDS_AGENT,
        JobStatus.NEEDS_APPROVAL,
        JobStatus.RENDERING,
        JobStatus.FAILED,
        JobStatus.STOPPED,
    }
)
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
        """True when this result's job folder can be continued with :func:`resume`.

        A failed ingest has no job folder (``job`` is null): its ``next`` is a
        fresh ``dropcast run``, not a resume.
        """
        return self.status.resumable and self.job is not None


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
            return commit
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


def build_run_argv(
    venv: Path, root: Path, *, link: str | None = None, text: str | None = None
) -> list[str]:
    """``dropcast run`` for one *link* (or file) or pasted *text*, headless, under *root*.

    Pasted text never goes in argv (Linux caps one argument at 128 KiB): the
    argv says ``--text -`` and :func:`run` pipes the text to DropCast's stdin,
    the way DropCast reads pasted text from a pipe. A *link* of ``-`` (stdin)
    is refused.
    """
    if (link is None) == (text is None):
        raise AdapterError("give exactly one of link or text")
    value = link if link is not None else text
    if not value or not value.strip() or (link is not None and link.strip() == "-"):
        raise AdapterError("nothing to run: empty source")
    argv = [str(dropcast_executable(venv)), "run", *_headless(root)]
    if text is not None:
        return [*argv, "--text", "-"]
    return [*argv, "--", link]


def build_resume_argv(venv: Path, job: str, root: Path, answer: str | None = None) -> list[str]:
    """``dropcast resume JOB``, headless and unpaid; *answer* is ``no`` or ``free``."""
    if not job or not job.strip():
        raise AdapterError("nothing to resume: empty job")
    if answer is not None and answer not in _ALLOWED_ANSWERS:
        raise AdapterError(f"answer {answer!r} is not allowed (only: no, free)")
    argv = [str(dropcast_executable(venv)), "resume", *_headless(root)]
    if answer is not None:
        argv += ["--answer", answer]
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
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> JobResult:
    """Start a DropCast job. Exit 0 (delivered) and 1 (any other status) parse."""
    return _call(build_run_argv(venv, root, link=link, text=text), runner, env, stdin_text=text)


def resume(
    venv: Path,
    job: str,
    root: Path,
    answer: str | None = None,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> JobResult:
    """Continue a DropCast job after the caller acted on its last status."""
    return _call(build_resume_argv(venv, job, root, answer), runner, env)
