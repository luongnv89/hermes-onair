"""The price gate (AGENTS.md rule 1), its 24 h cache, and the speech call (rule 2)."""

from __future__ import annotations

import http.client
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


@pytest.mark.parametrize(
    "error", [http.client.IncompleteRead(b""), http.client.LineTooLong("header line")]
)
def test_an_http_client_error_on_the_list_refuses(tmp_path, error):
    cat = catalog(tmp_path, FakeHttp(models=error))
    with pytest.raises(openrouter.OpenRouterError, match="unreachable"):
        cat.check(FISH)
    assert not (tmp_path / openrouter.CACHE_NAME).exists()


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


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection refused"),
        TimeoutError("timed out"),
        http.client.IncompleteRead(b"ID3"),  # body cut off mid-download
        http.client.BadStatusLine("garbled"),
    ],
)
def test_network_errors_raise(tmp_path, error):
    with pytest.raises(openrouter.OpenRouterError, match="network error"):
        speak(tmp_path, FakeHttp(speech={FISH: error}))


@pytest.mark.parametrize(
    "answer",
    [
        audio(b""),
        audio(b'{"error": "x"}', "application/json"),
        audio(b"oops", "text/plain"),
        # No audio/* type: the body is sniffed for an error document.
        audio(b'  {"looks": "json"}', ""),
        audio(b"<html></html>", "application/octet-stream"),
        audio(b'[{"error": 1}]', "binary/octet-stream"),
    ],
)
def test_a_body_that_is_not_audio_raises(tmp_path, answer):
    with pytest.raises(openrouter.OpenRouterError):
        speak(tmp_path, FakeHttp(speech={FISH: answer}))


@pytest.mark.parametrize("first", [b"{", b"[", b"<", b" {", b"\n<"])
def test_pcm_starting_like_json_is_audio_when_typed_audio(tmp_path, first):
    # Review r1, N5: the JSON/HTML sniff no longer runs on an audio/* answer.
    pcm = first + b"\x00" * (2001 - len(first))
    http = FakeHttp(speech={HEYGEN: audio(pcm, "audio/pcm;rate=44100;channels=1")})
    response = openrouter.speech(
        HEYGEN, "Hi.", "nadine", "pcm", catalog=catalog(tmp_path, http), http=http,
        environ={"OPENROUTER_API_KEY": KEY},
    )
    assert response.body == pcm


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


class _Response:
    status = 200
    headers: dict = {}

    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _limit):
        raise self.error


class _Opener:
    def __init__(self, open_error=None, read_error=None):
        self.open_error, self.read_error = open_error, read_error

    def open(self, request, timeout):
        if self.open_error is not None:
            raise self.open_error
        return _Response(self.read_error)


@pytest.mark.parametrize(
    "opener",
    [
        _Opener(open_error=http.client.BadStatusLine("garbled")),
        _Opener(open_error=http.client.LineTooLong("header line")),
        _Opener(open_error=ValueError("malformed")),
        _Opener(read_error=http.client.IncompleteRead(b"ID3", 4096)),
    ],
)
def test_the_real_transport_turns_http_client_errors_into_oserror(monkeypatch, opener):
    monkeypatch.setattr(openrouter, "_OPENER", opener)
    with pytest.raises(OSError, match="network error"):
        openrouter.urllib_http(
            "GET", openrouter.MODELS_URL, headers={}, body=None, timeout=1
        )


# ------------------------------------------------- review r1, B1: duplicates

PAID = {"prompt": "0.01", "completion": "0"}


@pytest.mark.parametrize("order", [(PAID, FREE), (FREE, PAID), (FREE, FREE)],
                         ids=["paid-then-free", "free-then-paid", "free-twice"])
def test_an_id_listed_more_than_once_is_refused_in_any_order(tmp_path, order):
    payload = {"data": [{"id": FISH, "pricing": dict(p)} for p in order]}
    models = openrouter.parse_models(payload)
    assert openrouter.gate(FISH, models) == f"{FISH} is listed more than once"
    http = FakeHttp(models=payload, speech={FISH: audio(MP3_BYTES)})
    with pytest.raises(openrouter.OpenRouterError, match="listed more than once"):
        speak(tmp_path, http)
    assert [c["method"] for c in http.calls] == ["GET"]  # no speech POST


def test_a_duplicate_stays_refused_from_the_cache(tmp_path):
    payload = {"data": [{"id": FISH, "pricing": dict(PAID)}, {"id": FISH, "pricing": dict(FREE)},
                        {"id": HEYGEN, "pricing": dict(FREE)}]}
    catalog(tmp_path, FakeHttp(models=payload)).models()  # fetched and cached
    cached = catalog(tmp_path, FakeHttp(models=OSError("must not fetch")))
    with pytest.raises(openrouter.OpenRouterError, match="listed more than once"):
        cached.check(FISH)
    cached.check(HEYGEN)  # the other model is unaffected


def test_a_cache_marking_a_duplicate_is_refused(tmp_path):
    write_cache(tmp_path, NOW - 60, models={FISH: {openrouter.DUPLICATE: True, "pricing": FREE}})
    with pytest.raises(openrouter.OpenRouterError, match="listed more than once"):
        catalog(tmp_path, FakeHttp(models=OSError("must not fetch"))).check(FISH)


@pytest.mark.parametrize(
    "text",
    [
        '{"data": [{"id": "@", "pricing": {"prompt": "0.01", "prompt": "0"}}]}',
        '{"data": [{"id": "@", "pricing": {"prompt": "0.01"}, "pricing": {"prompt": "0"}}]}',
        '{"data": [], "data": [{"id": "@", "pricing": {"prompt": "0"}}]}',
    ],
    ids=["price-key", "pricing-key", "data-key"],
)
def test_a_list_repeating_a_json_key_is_refused(tmp_path, text):
    body = text.replace("@", FISH).encode()
    answer = openrouter.HttpResponse(200, {"Content-Type": "application/json"}, body)
    with pytest.raises(openrouter.OpenRouterError, match="repeats a key"):
        catalog(tmp_path, FakeHttp(models=answer)).check(FISH)
    assert not (tmp_path / openrouter.CACHE_NAME).exists()


def test_a_cache_repeating_a_json_key_is_not_trusted(tmp_path):
    paid_then_free = (
        '{"fetched_at": NOW, "models": {"@": {"pricing": {"prompt": "0.01"}}, '
        '"@": {"pricing": {"prompt": "0"}}}}'
    )
    (tmp_path / openrouter.CACHE_NAME).write_text(
        paid_then_free.replace("@", FISH).replace("NOW", repr(NOW - 60))
    )
    with pytest.raises(openrouter.OpenRouterError, match="unreachable"):
        catalog(tmp_path, FakeHttp(models=OSError("down"))).check(FISH)
