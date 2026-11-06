"""OpenRouter for the voice router: the price gate (AGENTS.md rule 1) and one speech call.

Price gate. Before any model is called, the public speech model list
(``GET /models?output_modalities=speech``, sent WITHOUT a key) is read, from a
cache in the plugin data dir when that copy is at most 24 h old. A model may
be called only when it is listed exactly once, no listed price is non-zero,
and either its id ends in ``:free`` or every listed price is zero. A price
that is not a plain number counts as non-zero. A list (or cache) that repeats
a JSON key is not trusted. No list, no fresh cache, a broken answer:
OpenRouter is skipped.

Key (rule 2). ``OPENROUTER_API_KEY`` is read from the environment at call
time, sent only in the speech request's ``Authorization`` header, and never
logged, stored, or put in an error: every message that can carry server text
goes through :func:`scrub`.

Every network call goes through an injected :data:`Http` callable, so tests
fake the HTTP layer and never open a socket (rule 5).
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import Path
from typing import Any

API_BASE = "https://openrouter.ai/api/v1"
MODELS_URL = f"{API_BASE}/models?output_modalities=speech"
SPEECH_URL = f"{API_BASE}/audio/speech"
KEY_ENV = "OPENROUTER_API_KEY"

CACHE_NAME = "openrouter-speech-models.json"
CACHE_TTL_S = 24 * 3600
LIST_TIMEOUT_S = 20
SPEECH_TIMEOUT_S = 120
MAX_LIST_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 64 * 1024 * 1024


class OpenRouterError(Exception):
    """OpenRouter cannot be used for this request; the router tries the next engine."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return ""


#: ``http(method, url, headers=…, body=…, timeout=…) -> HttpResponse``. Any HTTP
#: status is a response; a network failure or timeout raises ``OSError``.
Http = Callable[..., HttpResponse]
Clock = Callable[[], float]

#: What a transport may raise for a network failure. ``http.client`` errors
#: (a body cut off mid-download, a garbled status line) are not ``OSError``;
#: callers treat them the same, so any injected transport falls through too.
NETWORK_ERRORS = (OSError, HTTPException)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not something to follow (never re-send the key)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def urllib_http(
    method: str, url: str, *, headers: Mapping[str, str], body: bytes | None, timeout: float,
    limit: int = MAX_AUDIO_BYTES,
) -> HttpResponse:
    """The real transport: urllib, no redirects, a bounded body."""
    if not url.startswith("https://"):
        raise OSError("only https URLs are used")
    # https only (checked above): S310's file:/custom-scheme concern cannot apply.
    request = urllib.request.Request(  # noqa: S310
        url, data=body, method=method, headers=dict(headers)
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
            return HttpResponse(response.status, dict(response.headers.items()), data)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(4096)
        except (OSError, HTTPException, ValueError):
            data = b""
        return HttpResponse(exc.code, dict(exc.headers.items()) if exc.headers else {}, data)
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else type(exc.reason).__name__
        raise OSError(f"network error: {reason}") from None
    except (HTTPException, ValueError) as exc:
        # IncompleteRead, BadStatusLine, LineTooLong, a malformed answer: not OSError.
        raise OSError(f"network error: {type(exc).__name__}") from None


def api_key(environ: Mapping[str, str] | None = None) -> str | None:
    """``OPENROUTER_API_KEY`` from the environment, or None. Never from a file."""
    value = (os.environ if environ is None else environ).get(KEY_ENV, "")
    value = value.strip()
    return value or None


_BEARER = re.compile(r"(?i)bearer\s+\S+")
_KEYLIKE = re.compile(r"sk-or-[A-Za-z0-9_-]{8,}")


def scrub(text: str, key: str | None = None) -> str:
    """*text* with the key (and anything that looks like one) replaced."""
    text = str(text)
    if key:
        text = text.replace(key, "[REDACTED]")
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _KEYLIKE.sub("[REDACTED]", text)


# --------------------------------------------------------------- price gate


def _price(value: Any) -> Decimal | None:
    """A listed price as a Decimal, or None when it is not a plain finite number."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() else None


#: Set on an id the list names more than once: which copy is right is unknown,
#: so the id is refused whatever its prices and whatever their order.
DUPLICATE = "listed_more_than_once"


def gate(model: str, catalog: Mapping[str, Any]) -> str | None:
    """Why *model* must not be called, or None when rule 1 allows the call."""
    entry = catalog.get(model)
    if not isinstance(entry, Mapping):
        return f"{model} is not in the public model list"
    if entry.get(DUPLICATE) is not None:
        return f"{model} is listed more than once"
    pricing = entry.get("pricing", {})
    if pricing is None:
        pricing = {}
    if not isinstance(pricing, Mapping):
        return f"{model} has an unreadable price list"
    listed = [(name, value) for name, value in pricing.items() if value is not None]
    for name, value in listed:
        price = _price(value)
        if price is None or price != 0:
            return f"{model} lists a non-zero price ({name}={value!r}): paid"
    if model.endswith(":free"):
        return None
    if listed:
        return None  # every listed price is zero
    return f"{model} lists no price and is not a :free id"


def parse_models(payload: Any) -> dict[str, dict[str, Any]]:
    """``{id: {"pricing": …}}`` from the model list answer; raises on a broken answer.

    An id that appears more than once becomes ``{DUPLICATE: True}`` (no
    pricing kept), so :func:`gate` refuses it in any order, from the cache too.
    """
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        raise OpenRouterError("the model list has no 'data' array")
    models: dict[str, dict[str, Any]] = {}
    for entry in data:
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
            model = entry["id"]
            if model in models:
                models[model] = {DUPLICATE: True}
            else:
                models[model] = {"pricing": entry.get("pricing")}
    return models


def _no_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json`` object hook: a key given twice in one object makes the document invalid.

    Plain ``json.loads`` keeps the last value, so ``{"prompt": "0.01",
    "prompt": "0"}`` would read as free.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"repeated key {key!r}")
        result[key] = value
    return result


def _loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_repeated_keys)


@dataclass
class Catalog:
    """The public speech model list, cached in *cache_dir* for up to 24 h."""

    cache_dir: Path
    http: Http = urllib_http
    clock: Clock = time.time
    ttl_s: float = CACHE_TTL_S

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir) / CACHE_NAME

    def _cached(self) -> dict[str, dict[str, Any]] | None:
        """The cached list when it is at most ``ttl_s`` old, else None."""
        path = self.cache_path
        try:
            if path.is_symlink() or not path.is_file():
                return None
            record = _loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # unreadable, or a repeated key: not a copy to trust
        if not isinstance(record, Mapping):
            return None
        fetched = record.get("fetched_at")
        if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
            return None
        if not math.isfinite(fetched):
            return None
        age = self.clock() - fetched
        if age < 0 or age > self.ttl_s:
            return None  # stale, or from the future: not a copy rule 1 accepts
        models = record.get("models")
        if not isinstance(models, Mapping):
            return None
        return {k: v for k, v in models.items() if isinstance(k, str) and isinstance(v, Mapping)}

    def _fetch(self) -> dict[str, dict[str, Any]]:
        try:
            response = self.http(
                "GET", MODELS_URL, headers={"Accept": "application/json"}, body=None,
                timeout=LIST_TIMEOUT_S,
            )
        except NETWORK_ERRORS as exc:
            raise OpenRouterError(f"model list unreachable: {scrub(exc)}") from None
        if response.status != 200:
            raise OpenRouterError(f"model list answered HTTP {response.status}")
        if len(response.body) > MAX_LIST_BYTES:
            raise OpenRouterError("model list is too large")
        try:
            payload = _loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OpenRouterError("model list is not JSON, or repeats a key") from None
        return parse_models(payload)

    def _store(self, models: Mapping[str, Any]) -> None:
        path = self.cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps({"fetched_at": self.clock(), "models": models}), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError:
            pass  # a cache that cannot be written only costs a fetch next time

    def models(self) -> dict[str, dict[str, Any]]:
        """The list: the fresh cache, else a fetch (stored). Raises OpenRouterError."""
        cached = self._cached()
        if cached is not None:
            return cached
        models = self._fetch()
        self._store(models)
        return models

    def check(self, model: str) -> None:
        """Raise OpenRouterError unless rule 1 allows calling *model* now."""
        reason = gate(model, self.models())
        if reason:
            raise OpenRouterError(f"price check refused: {reason}")


# ------------------------------------------------------------------- speech

_JSONISH = re.compile(rb"^\s*[\[{<]")


def speech(
    model: str,
    text: str,
    voice: str,
    response_format: str,
    *,
    catalog: Catalog,
    http: Http = urllib_http,
    environ: Mapping[str, str] | None = None,
) -> HttpResponse:
    """One ``POST /audio/speech``; returns the audio response or raises OpenRouterError.

    Order: key present, price gate passes, then the call. Any non-200 status
    (401, 402, 408, 429, 5xx, …), a network error, a timeout or a body that
    is not audio raises, so the router tries the next engine.
    """
    key = api_key(environ)
    if key is None:
        raise OpenRouterError(f"{KEY_ENV} is not set")
    catalog.check(model)
    body = json.dumps(
        {"model": model, "input": text, "voice": voice, "response_format": response_format}
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "audio/mpeg" if response_format == "mp3" else "audio/pcm",
    }
    try:
        response = http("POST", SPEECH_URL, headers=headers, body=body, timeout=SPEECH_TIMEOUT_S)
    except NETWORK_ERRORS as exc:
        raise OpenRouterError(f"network error: {scrub(exc, key)}") from None
    if response.status != 200:
        raise OpenRouterError(f"HTTP {response.status}{_detail(response, key)}")
    ctype = response.header("Content-Type").lower()
    audio = response.body
    if not audio:
        raise OpenRouterError("empty audio body")
    if len(audio) > MAX_AUDIO_BYTES:
        raise OpenRouterError("audio body is too large")
    if "json" in ctype or ctype.startswith("text/") or _JSONISH.match(audio[:64]):
        raise OpenRouterError(f"no audio in the answer (Content-Type {ctype or 'none'!r})")
    return response


def _detail(response: HttpResponse, key: str) -> str:
    """A short, scrubbed reason from an error body (OpenRouter's ``error.message``)."""
    try:
        payload = json.loads(response.body.decode("utf-8", "replace"))
        message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
    except (ValueError, AttributeError):
        message = None
    if not isinstance(message, str) or not message.strip():
        return ""
    return f": {scrub(' '.join(message.split()), key)[:200]}"
