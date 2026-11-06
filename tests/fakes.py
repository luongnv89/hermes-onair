"""Test doubles: a scripted process runner and a direct_url.json writer."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from onair import adapter

#: The pinned commit as uv records it (full SHA), built from the one pin.
PINNED_SHA = adapter.DROPCAST_PIN + "ab1cb34d54913285597309403b892fe8b"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"


def write_install(
    venv: Path,
    commit: str | None = PINNED_SHA,
    *,
    python: bool = True,
    marker: bool = True,
    uv_made: bool = True,
) -> None:
    """Lay out a sidecar venv the way uv leaves it after a git install.

    ``marker`` adds the ``.onair-sidecar`` file setup writes after creating a
    venv; ``uv_made`` the ``uv = …`` line uv writes in ``pyvenv.cfg``.
    """
    venv.mkdir(parents=True, exist_ok=True)
    if not (venv / "pyvenv.cfg").exists():  # keep the one a real `uv venv` wrote
        cfg = "home = /usr/bin\n" + ("uv = 0.13.0\n" if uv_made else "")
        (venv / "pyvenv.cfg").write_text(cfg + "version_info = 3.12.3\n")
    if marker:
        (venv / ".onair-sidecar").write_text("Created by the hermes-onair plugin.\n")
    if python:
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("")
    if commit is not None:
        info = venv / "lib" / "python3.12" / "site-packages" / "dropcast-0.2.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        url = adapter.DROPCAST_REPO.removeprefix("git+")
        record = {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}}
        (info / "direct_url.json").write_text(json.dumps(record))
        (info / "INSTALLER").write_text("uv\n")


class FakeRunner:
    """Answers each call with the first matching rule; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._rules: list[tuple[Callable[[list[str]], bool], Callable[[list[str]], object]]] = []

    def on(self, match: Callable[[list[str]], bool], respond) -> FakeRunner:
        self._rules.append((match, respond if callable(respond) else (lambda argv, r=respond: r)))
        return self

    def __call__(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, **kwargs})
        for match, respond in self._rules:
            if match(argv):
                result = respond(argv)
                if isinstance(result, BaseException):
                    raise result
                return result
        raise AssertionError(f"unexpected command: {argv}")

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls]


def done(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- HTTP (the voice router's OpenRouter calls) ---

#: The public speech list as OpenRouter answers it (both free models, a paid twin).
FREE = {"prompt": "0", "completion": "0"}
MODEL_LIST = {
    "data": [
        {"id": "fish-audio/s2.1-pro-free:free", "pricing": dict(FREE)},
        {"id": "heygen/heygen-voice-1-turbo:free", "pricing": dict(FREE)},
        {"id": "heygen/heygen-voice-1-turbo", "pricing": {"prompt": "0.00003", "completion": "0"}},
    ]
}
MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64
PCM_BYTES = b"\x01\x00\xff\x7f" * 2000


def model_list(**pricing) -> dict:
    """MODEL_LIST with some models' pricing replaced (``{id: pricing}``; None = unlisted)."""
    data = []
    for entry in MODEL_LIST["data"]:
        if entry["id"] in pricing:
            if pricing[entry["id"]] is None:
                continue
            entry = {"id": entry["id"], "pricing": pricing[entry["id"]]}
        data.append(entry)
    return {"data": data}


class FakeHttp:
    """Answers GET /models and POST /audio/speech; records every call.

    ``speech[model]`` is a response, an exception, or a list of them used in
    turn. Unscripted models answer 500.
    """

    def __init__(self, models=None, speech=None) -> None:
        from onair import openrouter

        self.openrouter = openrouter
        self.models = MODEL_LIST if models is None else models
        self.speech = dict(speech or {})
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, body, timeout):
        call = {"method": method, "url": url, "headers": dict(headers), "body": body,
                "timeout": timeout}
        self.calls.append(call)
        response = self.openrouter.HttpResponse
        if method == "GET" and url == self.openrouter.MODELS_URL:
            if isinstance(self.models, BaseException):
                raise self.models
            if isinstance(self.models, response):
                return self.models
            return response(200, {"Content-Type": "application/json"},
                            json.dumps(self.models).encode())
        if method == "POST" and url == self.openrouter.SPEECH_URL:
            model = json.loads(body)["model"]
            answer = self.speech.get(model, response(500, {}, b""))
            if isinstance(answer, list):
                answer = answer.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        raise AssertionError(f"unexpected request: {method} {url}")

    def posts(self) -> list[str]:
        return [json.loads(c["body"])["model"] for c in self.calls if c["method"] == "POST"]

    def gets(self) -> int:
        return sum(1 for c in self.calls if c["method"] == "GET")


def audio(body: bytes, content_type: str = "audio/mpeg", status: int = 200):
    from onair import openrouter

    return openrouter.HttpResponse(status, {"Content-Type": content_type}, body)


def status(code: int, message: str = "nope"):
    from onair import openrouter

    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    return openrouter.HttpResponse(code, {"Content-Type": "application/json"}, body)
