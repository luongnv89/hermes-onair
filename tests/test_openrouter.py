"""The price gate (AGENTS.md rule 1), its 24 h cache, and the speech call (rule 2)."""

from __future__ import annotations

import json
import logging

import pytest

from onair import openrouter

from fakes import FREE, MP3_BYTES, FakeHttp, audio, model_list, status

FISH = "fish-audio/s2.1-pro-free:free"
HEYGEN = "heygen/heygen-voice-1-turbo:free"
KEY = "fake-test-key-0123456789abcdef"
NOW = 1_800_000_000.0
DAY = 24 * 3600


def catalog(tmp_path, http, now=NOW):
    return openrouter.Catalog(tmp_path, http=http, clock=lambda: now)


def write_cache(tmp_path, fetched_at, models=None):
    models = models if models is not None else {FISH: {"pricing": FREE}}
    (tmp_path / openrouter.CACHE_NAME).write_text(
        json.dumps({"fetched_at": fetched_at, "models": models})
    )


# ------------------------------------------------------------------ gate


@pytest.mark.parametrize(
    ("model", "entry"),
    [
        (FISH, {"pricing": {"prompt": "0", "completion": "0"}}),
        (FISH, {"pricing": {"prompt": 0, "completion": 0.0, "image": None}}),
        (FISH, {"pricing": {}}),  # a :free id with no listed price
        (FISH, {"pricing": None}),
        ("vendor/zero-priced", {"pricing": {"prompt": "0", "completion": "0.000"}}),
    ],
)
def test_gate_allows_listed_free_models(model, entry):
    assert openrouter.gate(model, {model: entry}) is None


@pytest.mark.parametrize(
    ("model", "entry", "why"),
    [
        # a :free id with a price
        (FISH, {"pricing": {"prompt": "0.0001", "completion": "0"}}, "non-zero"),
        (FISH, {"pricing": {"prompt": "0", "completion": "-1"}}, "non-zero"),  # variable price
        (FISH, {"pricing": {"prompt": "abc"}}, "non-zero"),  # unreadable counts as paid
        (FISH, {"pricing": {"prompt": True}}, "non-zero"),
        (FISH, {"pricing": {"prompt": ["0"]}}, "non-zero"),
        (FISH, {"pricing": {"prompt": "NaN"}}, "non-zero"),
        (FISH, {"pricing": "free"}, "unreadable"),
        ("vendor/paid", {"pricing": {"prompt": "0.00003", "completion": "0"}}, "non-zero"),
        ("vendor/unpriced", {"pricing": {}}, "no price"),
    ],
)
def test_gate_refuses_anything_paid_or_unknown(model, entry, why):
    reason = openrouter.gate(model, {model: entry})
    assert reason and why in reason


def test_gate_refuses_an_unlisted_model_even_with_a_free_id():
    assert "not in the public model list" in openrouter.gate("not/listed:free", {})


def test_parse_models_keeps_only_ids_and_prices():
    parsed = openrouter.parse_models(
        {"data": [{"id": FISH, "pricing": FREE, "name": "x"}, {"no": "id"}, "junk"]}
    )
    assert parsed == {FISH: {"pricing": FREE}}


@pytest.mark.parametrize("payload", [{}, {"data": {}}, [], None])
def test_parse_models_refuses_a_broken_list(payload):
    with pytest.raises(openrouter.OpenRouterError):
        openrouter.parse_models(payload)


# ----------------------------------------------------------------- cache


def test_list_is_fetched_without_a_key_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    http = FakeHttp()
    models = catalog(tmp_path, http).models()
    assert FISH in models and HEYGEN in models
    (call,) = http.calls
    assert call["method"] == "GET" and call["url"] == openrouter.MODELS_URL
    assert "Authorization" not in call["headers"]
    assert KEY not in json.dumps(call["headers"])
    record = json.loads((tmp_path / openrouter.CACHE_NAME).read_text())
    assert record["fetched_at"] == NOW and FISH in record["models"]
    assert KEY not in (tmp_path / openrouter.CACHE_NAME).read_text()


@pytest.mark.parametrize("age", [0, 1, DAY - 1, DAY])
def test_a_cache_up_to_24h_old_is_used_without_fetching(tmp_path, age):
    write_cache(tmp_path, NOW - age)
    http = FakeHttp(models=OSError("must not fetch"))
    assert catalog(tmp_path, http).models() == {FISH: {"pricing": FREE}}
    assert http.calls == []


@pytest.mark.parametrize("age", [DAY + 0.001, DAY + 1, 10 * DAY, -1])
def test_a_cache_older_than_24h_or_from_the_future_is_refetched(tmp_path, age):
    write_cache(tmp_path, NOW - age, models={"old/model": {"pricing": FREE}})
    http = FakeHttp()
    models = catalog(tmp_path, http).models()
    assert http.gets() == 1 and FISH in models and "old/model" not in models


def test_a_stale_cache_is_not_used_when_the_fetch_fails(tmp_path):
    write_cache(tmp_path, NOW - DAY - 1)
    cat = catalog(tmp_path, FakeHttp(models=OSError("down")))
    with pytest.raises(openrouter.OpenRouterError, match="unreachable"):
        cat.check(FISH)


def test_no_list_and_no_cache_refuses_every_model(tmp_path):
    cat = catalog(tmp_path, FakeHttp(models=TimeoutError("slow")))
    for model in (FISH, HEYGEN):
        with pytest.raises(openrouter.OpenRouterError):
            cat.check(model)
    assert not (tmp_path / openrouter.CACHE_NAME).exists()


@pytest.mark.parametrize(
    "answer",
    [
        openrouter.HttpResponse(503, {}, b""),
        openrouter.HttpResponse(200, {}, b"<html>"),
        openrouter.HttpResponse(200, {}, b'{"nodata": []}'),
    ],
)
def test_a_broken_list_answer_refuses_and_is_not_cached(tmp_path, answer):
    cat = catalog(tmp_path, FakeHttp(models=answer))
    with pytest.raises(openrouter.OpenRouterError):
        cat.check(FISH)
    assert not (tmp_path / openrouter.CACHE_NAME).exists()


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"fetched_at": "x", "models": {}}', '{"fetched_at": 1e999}',
     '{"fetched_at": true, "models": {}}', '{"fetched_at": 1800000000, "models": []}'],
)
def test_an_unreadable_cache_is_ignored(tmp_path, content):
    (tmp_path / openrouter.CACHE_NAME).write_text(content)
    http = FakeHttp()
    catalog(tmp_path, http).check(FISH)
    assert http.gets() == 1


def test_check_uses_the_cached_prices(tmp_path):
    write_cache(tmp_path, NOW - 60, models={FISH: {"pricing": {"prompt": "0.01"}}})
    with pytest.raises(openrouter.OpenRouterError, match="non-zero"):
        catalog(tmp_path, FakeHttp()).check(FISH)


# ---------------------------------------------------------------- speech


def speak(tmp_path, http, environ=None):
    environ = {"OPENROUTER_API_KEY": KEY} if environ is None else environ
    return openrouter.speech(
        FISH, "Hello.", "v1", "mp3", catalog=catalog(tmp_path, http), http=http, environ=environ
    )


def test_speech_sends_the_documented_request(tmp_path):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    assert speak(tmp_path, http).body == MP3_BYTES
    get, post = http.calls
    assert "Authorization" not in get["headers"]
    assert post["url"] == "https://openrouter.ai/api/v1/audio/speech"
    assert post["headers"]["Authorization"] == f"Bearer {KEY}"
    assert json.loads(post["body"]) == {
        "model": FISH, "input": "Hello.", "voice": "v1", "response_format": "mp3"
    }


@pytest.mark.parametrize("environ", [{}, {"OPENROUTER_API_KEY": ""}, {"OPENROUTER_API_KEY": "  "}])
def test_no_key_means_no_request_at_all(tmp_path, environ):
    http = FakeHttp()
    with pytest.raises(openrouter.OpenRouterError, match="OPENROUTER_API_KEY is not set"):
        speak(tmp_path, http, environ)
    assert http.calls == []


def test_a_refused_price_check_sends_no_speech_request(tmp_path):
    http = FakeHttp(models=model_list(**{FISH: {"prompt": "0.5"}}), speech={FISH: audio(MP3_BYTES)})
    with pytest.raises(openrouter.OpenRouterError, match="price check refused"):
        speak(tmp_path, http)
    assert http.posts() == []


@pytest.mark.parametrize("code", [400, 401, 402, 403, 404, 408, 429, 500, 502, 503, 504])
def test_any_error_status_raises(tmp_path, code):
    with pytest.raises(openrouter.OpenRouterError, match=f"HTTP {code}"):
        speak(tmp_path, FakeHttp(speech={FISH: status(code)}))


@pytest.mark.parametrize("error", [OSError("connection refused"), TimeoutError("timed out")])
def test_network_errors_raise(tmp_path, error):
    with pytest.raises(openrouter.OpenRouterError, match="network error"):
        speak(tmp_path, FakeHttp(speech={FISH: error}))


@pytest.mark.parametrize(
    "answer",
    [
        audio(b""),
        audio(b'{"error": "x"}', "application/json"),
        audio(b"oops", "text/plain"),
        audio(b'  {"looks": "json"}', "audio/mpeg"),
        audio(b"<html></html>", "audio/mpeg"),
    ],
)
def test_a_body_that_is_not_audio_raises(tmp_path, answer):
    with pytest.raises(openrouter.OpenRouterError):
        speak(tmp_path, FakeHttp(speech={FISH: answer}))


def test_the_key_never_reaches_an_error_or_a_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    echo = status(401, f"bad key {KEY} (Bearer {KEY})")
    with pytest.raises(openrouter.OpenRouterError) as info:
        speak(tmp_path, FakeHttp(speech={FISH: echo}))
    assert "HTTP 401" in str(info.value) and KEY not in str(info.value)
    with pytest.raises(openrouter.OpenRouterError) as info:
        speak(tmp_path, FakeHttp(speech={FISH: OSError(f"proxy said Bearer {KEY}")}))
    assert KEY not in str(info.value)
    assert KEY not in caplog.text


def test_scrub():
    assert openrouter.scrub(f"a {KEY} b", KEY) == "a [REDACTED] b"
    assert "sk-or-" not in openrouter.scrub("token " + "sk-" + "or-v1-abcdef0123456789")
    assert openrouter.scrub("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"


def test_api_key_reads_only_the_environment(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert openrouter.api_key() is None
    monkeypatch.setenv("OPENROUTER_API_KEY", f" {KEY} ")
    assert openrouter.api_key() == KEY


def test_the_real_transport_refuses_plain_http():
    with pytest.raises(OSError, match="https"):
        openrouter.urllib_http("GET", "http://openrouter.ai/x", headers={}, body=None, timeout=1)
