"""The voice router: one engine per request, the free chain, and its fall-throughs."""

from __future__ import annotations

import errno
import http.client
import json
import logging
import os
import time
from pathlib import Path

import pytest

from onair import adapter, openrouter, router

from fakes import (
    FREE,
    MP3_BYTES,
    PCM_BYTES,
    FakeHttp,
    FakeRunner,
    audio,
    done,
    model_list,
    status,
    write_install,
)

FISH = router.FISH_MODEL
HEYGEN = router.HEYGEN_MODEL
KEY = "fake-test-key-0123456789abcdef"
FFMPEG = "/opt/ffmpeg/bin/ffmpeg"
EN = "Welcome to On Air, a tiny test show."
FR = "Bienvenue sur On Air, c'est une petite émission pour nous et pour vous."
EDGE_MP3 = b"ID3edge" + b"\xff\xfb\x90\x00" * 32
CONVERTED = b"converted-audio"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    return KEY


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "plugin-data" / "onair"
    write_install(path / "sidecar")  # an On Air sidecar (marker + pinned install)
    return path


def is_ffmpeg(argv):
    return argv[0] == FFMPEG


def is_edge(argv):
    return argv[1:4] == ["-I", "-m", adapter.EDGE_MODULE]


def ffmpeg_ok(argv):
    Path(argv[-1]).write_bytes(CONVERTED)
    return done()


def edge_ok(argv):
    job = Path(argv[argv.index("--out") + 1]).parent
    episode = job / "out" / "episode.mp3"
    episode.parent.mkdir(parents=True, exist_ok=True)
    episode.write_bytes(EDGE_MP3)
    return done(0, json.dumps({"rendered": True, "episode": str(episode)}, indent=2))


def runner(edge=edge_ok, ffmpeg=ffmpeg_ok):
    return FakeRunner().on(is_ffmpeg, ffmpeg).on(is_edge, edge)


def environment(data_dir, http, run=None, ffmpeg=FFMPEG):
    return router.Environment(data_dir=data_dir, ffmpeg=ffmpeg, http=http, runner=run or runner())


def speak(tmp_path, env, text=EN, language="en", fmt="mp3", **kw):
    out = tmp_path / "out" / ".tts_123.5f2a.mp3"  # Hermes's temporary name
    out.parent.mkdir(exist_ok=True)
    engine = router.speak(text, out, env=env, language=language, fmt=fmt, **kw)
    return engine, out


def tried(env):
    return [(a["engine"], bool(a["error"])) for a in env.attempts]


# ------------------------------------------------------------ happy paths


def test_english_speaks_with_fish_first(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    run = runner()
    env = environment(data_dir, http, run)
    engine, out = speak(tmp_path, env)
    assert engine == "fish" and out.read_bytes() == MP3_BYTES
    assert http.posts() == [FISH]
    assert run.calls == []  # MP3 asked, MP3 answered: no ffmpeg, no edge
    body = json.loads(http.calls[-1]["body"])
    assert body["voice"] == router.DEFAULT_VOICES["fish"]["en"]


def test_other_languages_speak_with_heygen_first(tmp_path, data_dir, key):
    http = FakeHttp(speech={HEYGEN: audio(PCM_BYTES, "audio/pcm;rate=44100;channels=1")})
    run = runner()
    engine, out = speak(tmp_path, environment(data_dir, http, run), text=FR, language="fr")
    assert engine == "heygen" and out.read_bytes() == CONVERTED
    assert http.posts() == [HEYGEN]
    assert json.loads(http.calls[-1]["body"])["voice"] == "nadine"


def test_heygen_pcm_is_converted_with_ffmpeg(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: status(429), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    run = runner()
    engine, out = speak(tmp_path, environment(data_dir, http, run), fmt="wav")
    assert engine == "heygen" and out.read_bytes() == CONVERTED
    (argv,) = run.argvs()
    i = argv.index("-i")
    assert argv[i - 6:i] == ["-f", "s16le", "-ar", "44100", "-ac", "1"]
    assert Path(argv[i + 1]).name == "heygen.pcm"
    assert argv[-3:-1] == ["-f", "wav"] and "pcm_s16le" in argv
    assert not Path(argv[i + 1]).exists()  # scratch is removed


def test_heygen_rate_comes_from_the_content_type_when_given(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(PCM_BYTES, "audio/pcm;rate=24000")})
    run = runner()
    speak(tmp_path, environment(data_dir, http, run))
    argv = run.argvs()[0]
    assert argv[argv.index("-ar") + 1] == "24000"


@pytest.mark.parametrize(("fmt", "codec"), [("mp3", "libmp3lame"), ("ogg", "libopus"),
                                            ("opus", "libopus"), ("flac", "flac")])
def test_pcm_codecs_per_format(tmp_path, data_dir, key, fmt, codec):
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    run = runner()
    speak(tmp_path, environment(data_dir, http, run), fmt=fmt)
    argv = run.argvs()[0]
    assert codec in argv and argv[-3:-1] == ["-f", fmt]


def test_fish_mp3_is_converted_only_when_another_format_is_asked(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    run = runner()
    engine, out = speak(tmp_path, environment(data_dir, http, run), fmt="ogg")
    assert engine == "fish" and out.read_bytes() == CONVERTED
    assert "libopus" in run.argvs()[0]


def test_without_ffmpeg_fish_writes_mp3_for_any_format(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    engine, out = speak(tmp_path, environment(data_dir, http, ffmpeg=None), fmt="wav")
    assert engine == "fish" and out.read_bytes() == MP3_BYTES


# ---------------------------------------------------------- fall-throughs


@pytest.mark.parametrize("code", [401, 402, 408, 429, 500, 502, 503, 504])
def test_fish_error_status_falls_through_to_heygen(tmp_path, data_dir, key, code):
    http = FakeHttp(speech={FISH: status(code), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    env = environment(data_dir, http)
    engine, out = speak(tmp_path, env)
    assert engine == "heygen" and out.read_bytes() == CONVERTED
    assert tried(env) == [("fish", True), ("heygen", False)]
    assert f"HTTP {code}" in env.attempts[0]["error"]


@pytest.mark.parametrize("code", [401, 402, 408, 429, 500, 503])
def test_both_free_models_failing_falls_through_to_edge(tmp_path, data_dir, key, code):
    http = FakeHttp(speech={FISH: status(code), HEYGEN: status(code)})
    env = environment(data_dir, http)
    engine, out = speak(tmp_path, env)
    assert engine == "edge" and out.read_bytes() == EDGE_MP3
    assert tried(env) == [("fish", True), ("heygen", True), ("edge", False)]


@pytest.mark.parametrize("error", [OSError("connection refused"), TimeoutError("timed out")])
def test_network_errors_and_timeouts_fall_through(tmp_path, data_dir, key, error):
    http = FakeHttp(speech={FISH: error, HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    env = environment(data_dir, http)
    assert speak(tmp_path, env)[0] == "heygen"
    assert "network error" in env.attempts[0]["error"]


def test_a_download_cut_off_mid_body_falls_through_to_edge(tmp_path, data_dir, key):
    cut = http.client.IncompleteRead(b"ID3", 4096)
    http_ = FakeHttp(speech={FISH: cut, HEYGEN: http.client.BadStatusLine("garbled")})
    env = environment(data_dir, http_)
    engine, out = speak(tmp_path, env)
    assert engine == "edge" and out.read_bytes() == EDGE_MP3
    assert tried(env) == [("fish", True), ("heygen", True), ("edge", False)]
    assert all("network error" in a["error"] for a in env.attempts[:2])


def test_a_model_list_cut_off_falls_through_to_edge(tmp_path, data_dir, key):
    cut = http.client.IncompleteRead(b'{"data": [')
    http_ = FakeHttp(models=cut, speech={FISH: audio(MP3_BYTES)})
    env = environment(data_dir, http_)
    assert speak(tmp_path, env)[0] == "edge"
    assert http_.posts() == []
    assert "model list unreachable" in env.attempts[0]["error"]


def test_a_missing_key_goes_straight_to_edge_with_no_request(tmp_path, data_dir):
    http = FakeHttp()
    env = environment(data_dir, http)
    engine, out = speak(tmp_path, env)
    assert engine == "edge" and out.read_bytes() == EDGE_MP3
    assert http.calls == []  # not even the model list
    assert all("OPENROUTER_API_KEY is not set" in a["error"] for a in env.attempts[:2])


def test_a_failed_price_check_calls_no_model(tmp_path, data_dir, key):
    http = FakeHttp(models=OSError("list down"), speech={FISH: audio(MP3_BYTES)})
    env = environment(data_dir, http)
    assert speak(tmp_path, env)[0] == "edge"
    assert http.posts() == []
    assert "model list unreachable" in env.attempts[0]["error"]


def test_the_model_became_paid_is_not_called(tmp_path, data_dir, key):
    # Fish now lists a price: the router must not call it, whatever its id says.
    http = FakeHttp(
        models=model_list(**{FISH: {"prompt": "0.000015", "completion": "0"}}),
        speech={FISH: audio(MP3_BYTES), HEYGEN: audio(PCM_BYTES, "audio/pcm")},
    )
    env = environment(data_dir, http)
    assert speak(tmp_path, env)[0] == "heygen"
    assert http.posts() == [HEYGEN]
    assert "non-zero price" in env.attempts[0]["error"]


def test_a_free_id_with_a_non_zero_price_is_not_called(tmp_path, data_dir, key):
    http = FakeHttp(
        models=model_list(**{HEYGEN: {"prompt": "0", "completion": "0.002"}}),
        speech={HEYGEN: audio(PCM_BYTES, "audio/pcm"), FISH: audio(MP3_BYTES)},
    )
    env = environment(data_dir, http)
    engine, _ = speak(tmp_path, env, text=FR, language="fr")
    assert engine == "fish" and http.posts() == [FISH]


def test_an_unlisted_model_is_not_called(tmp_path, data_dir, key):
    http = FakeHttp(models=model_list(**{FISH: None, HEYGEN: None}),
                    speech={FISH: audio(MP3_BYTES)})
    env = environment(data_dir, http)
    assert speak(tmp_path, env)[0] == "edge" and http.posts() == []


def test_a_cached_paid_price_is_honoured_without_fetching(tmp_path, data_dir, key):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / openrouter.CACHE_NAME).write_text(json.dumps({
        "fetched_at": time.time(),
        "models": {FISH: {"pricing": {"prompt": "1"}}, HEYGEN: {"pricing": FREE}},
    }))
    http = FakeHttp(models=OSError("must not fetch"), speech={HEYGEN: audio(PCM_BYTES)})
    env = environment(data_dir, http)
    assert speak(tmp_path, env)[0] == "heygen"
    assert http.gets() == 0 and http.posts() == [HEYGEN]


@pytest.mark.parametrize(
    "answer",
    [audio(b""), audio(b'{"error":{}}', "application/json"), audio(b"RIFF....WAVE", "audio/mpeg"),
     audio(b"plain words", "text/plain")],
)
def test_an_empty_or_invalid_fish_body_falls_through(tmp_path, data_dir, key, answer):
    http = FakeHttp(speech={FISH: answer, HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    assert speak(tmp_path, environment(data_dir, http))[0] == "heygen"


def test_heygen_pcm_starting_with_a_brace_is_spoken(tmp_path, data_dir, key):
    # Review r1, N5: valid PCM whose first byte is 0x7B is not taken for JSON.
    pcm = b"{\x00" + PCM_BYTES
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(pcm, "audio/pcm;rate=44100")})
    run = runner()
    assert speak(tmp_path, environment(data_dir, http, run))[0] == "heygen"


@pytest.mark.parametrize("body", [b'  {"looks": "json"}', b"<html></html>"])
def test_a_fish_error_page_typed_audio_mpeg_still_falls_through(tmp_path, data_dir, key, body):
    # The sniff is skipped for audio/*; Fish's MP3 check still refuses it.
    http = FakeHttp(speech={FISH: audio(body, "audio/mpeg"), HEYGEN: audio(PCM_BYTES)})
    env = environment(data_dir, http)
    assert speak(tmp_path, env)[0] == "heygen"
    assert "not MP3 audio" in env.attempts[0]["error"]


@pytest.mark.parametrize("body", [b"", b"\x00", b"\x00\x01\x02"])
def test_an_empty_or_odd_pcm_body_falls_through(tmp_path, data_dir, key, body):
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(body, "audio/pcm")})
    run = runner()
    assert speak(tmp_path, environment(data_dir, http, run))[0] == "edge"
    assert not any(is_ffmpeg(a) for a in run.argvs())


def test_without_ffmpeg_heygen_is_skipped_unasked(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    env = environment(data_dir, http, ffmpeg=None)
    assert speak(tmp_path, env)[0] == "edge"
    assert http.posts() == [FISH]  # HeyGen never called
    assert "ffmpeg not found" in env.attempts[1]["error"]


def test_a_failing_ffmpeg_falls_through(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    run = runner(ffmpeg=lambda argv: done(1, stderr="Invalid data"))
    env = environment(data_dir, http, run)
    assert speak(tmp_path, env)[0] == "edge"
    assert "ffmpeg failed (exit 1): Invalid data" in env.attempts[1]["error"]


def test_french_order_is_heygen_fish_edge(tmp_path, data_dir, key):
    http = FakeHttp(speech={HEYGEN: status(402), FISH: status(402)})
    env = environment(data_dir, http)
    assert speak(tmp_path, env, text=FR, language="fr")[0] == "edge"
    assert http.posts() == [HEYGEN, FISH]
    assert [a["engine"] for a in env.attempts] == ["heygen", "fish", "edge"]
    assert env.attempts[-1]["voice"] == "fr-FR-DeniseNeural"


# ---------------------------------------------------------- one engine only


def test_all_engines_failing_raises_and_writes_nothing(tmp_path, data_dir, key, caplog):
    caplog.set_level(logging.DEBUG)
    http = FakeHttp(speech={FISH: status(401, f"key {KEY} rejected"), HEYGEN: status(503)})
    run = runner(edge=lambda argv: done(1, json.dumps({"rendered": False, "reason": "offline"})))
    env = environment(data_dir, http, run)
    with pytest.raises(router.AllEnginesFailed) as info:
        speak(tmp_path, env)
    message = str(info.value)
    assert "fish: HTTP 401" in message and "heygen: HTTP 503" in message and "offline" in message
    assert KEY not in message and KEY not in caplog.text
    assert list((tmp_path / "out").iterdir()) == []


def test_a_failed_engine_never_leaves_partial_audio(tmp_path, data_dir, key):
    # HeyGen answers, then its conversion fails: the output must hold edge's audio only.
    def half(argv):
        Path(argv[-1]).write_bytes(b"partial")
        return done(1, stderr="broken pipe")

    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    engine, out = speak(tmp_path, environment(data_dir, http, runner(ffmpeg=half)))
    assert engine == "edge" and out.read_bytes() == EDGE_MP3


def test_the_sidecar_must_be_set_up_for_edge(tmp_path):
    http = FakeHttp()
    env = environment(tmp_path / "empty", http)
    with pytest.raises(router.AllEnginesFailed, match="hermes onair setup"):
        speak(tmp_path, env)


def test_a_foreign_sidecar_is_never_run(tmp_path):
    data = tmp_path / "data"
    write_install(data / "sidecar", commit=None)  # marker but no pinned install
    run = runner()
    with pytest.raises(router.AllEnginesFailed):
        speak(tmp_path, environment(data, FakeHttp(), run))
    assert run.calls == []


def test_edge_gets_the_text_voice_and_language(tmp_path, data_dir):
    seen = {}

    def edge(argv):
        job = Path(argv[argv.index("--out") + 1]).parent
        seen["script"] = json.loads((job / "script.json").read_text())
        seen["argv"] = argv
        return edge_ok(argv)

    speak(tmp_path, environment(data_dir, FakeHttp(), runner(edge=edge)), text=FR, language="fr")
    assert seen["script"]["language"] == "fr"
    assert seen["script"]["segments"][0]["text"] == FR
    assert "host=fr-FR-DeniseNeural" in seen["argv"]
    assert seen["argv"][0] == str(data_dir / "sidecar" / "bin" / "python")


def test_edge_mp3_is_converted_for_other_formats(tmp_path, data_dir):
    run = runner()
    engine, out = speak(tmp_path, environment(data_dir, FakeHttp(), run), fmt="flac")
    assert engine == "edge" and out.read_bytes() == CONVERTED
    assert [is_ffmpeg(a) for a in run.argvs()] == [False, True]


def test_an_unknown_format_becomes_mp3(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    run = runner()
    assert speak(tmp_path, environment(data_dir, http, run), fmt="aiff")[0] == "fish"
    assert run.calls == []


def test_empty_text_is_refused(tmp_path, data_dir):
    with pytest.raises(ValueError):
        speak(tmp_path, environment(data_dir, FakeHttp()), text="  ")


# ----------------------------------------------------------------- voices


def test_voice_overrides_from_settings(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    voices = {"fish": {"en": "my-fish-voice"}, "edge": {"*": "en-GB-RyanNeural"}}
    speak(tmp_path, environment(data_dir, http), voices=voices)
    assert json.loads(http.calls[-1]["body"])["voice"] == "my-fish-voice"


def test_a_pinned_tts_voice_applies_to_its_engine_only(tmp_path, data_dir, key):
    http = FakeHttp(speech={FISH: status(500), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    env = environment(data_dir, http)
    speak(tmp_path, env, pinned=("heygen", "tabitha"))
    voices = [json.loads(c["body"])["voice"] for c in http.calls if c["method"] == "POST"]
    assert voices == [router.DEFAULT_VOICES["fish"]["en"], "tabitha"]


@pytest.mark.parametrize(
    ("engine", "language", "overrides", "want"),
    [
        ("edge", "en", None, "en-US-AndrewNeural"),
        ("edge", "fr-CA", None, "fr-FR-DeniseNeural"),
        ("edge", "xx", None, "en-US-EmmaMultilingualNeural"),
        ("heygen", "vi", None, "nadine"),
        ("fish", "fr", None, "9a9cf47702da476aa4629e2506d4a857"),
        ("edge", "fr-ca", {"edge": {"fr-ca": "fr-CA-SylvieNeural"}}, "fr-CA-SylvieNeural"),
        ("edge", "fr", {"edge": {"fr": "  "}}, "fr-FR-DeniseNeural"),
        ("edge", "fr", {"edge": "oops"}, "fr-FR-DeniseNeural"),
        ("edge", "fr", "oops", "fr-FR-DeniseNeural"),
    ],
)
def test_voice_for(engine, language, overrides, want):
    assert router.voice_for(engine, language, overrides) == want


@pytest.mark.parametrize(
    ("value", "want"),
    [("edge:en-GB-RyanNeural", ("edge", "en-GB-RyanNeural")),
     ("HeyGen: tabitha", ("heygen", "tabitha")),
     ("lucian", None), ("other:x", None), ("fish:", None), (None, None)],
)
def test_pinned_voice(value, want):
    assert router.pinned_voice(value) == want


# --------------------------------------------------------------- language


@pytest.mark.parametrize(
    ("text", "want"),
    [(EN, "en"), (FR, "fr"),
     ("Chào mừng bạn đến với On Air, một chương trình thử nghiệm nhỏ.", "vi"),
     ("Le château est à côté de la forêt.", "fr")],
)
def test_detect_language(text, want):
    assert router.detect_language(text) == want


@pytest.mark.parametrize(
    ("setting", "want"),
    [("auto", "en"), (None, "en"), ("", "en"), ("FR", "fr"), ("vi", "vi"), ("en-GB", "en-gb"),
     ("not a tag!", "en")],
)
def test_resolve_language(setting, want):
    assert router.resolve_language(setting, EN) == want


def test_chain_per_language():
    assert router.chain_for("en") == ("fish", "heygen", "edge")
    assert router.chain_for("en-gb") == ("fish", "heygen", "edge")
    for language in ("fr", "vi", "de"):
        assert router.chain_for(language) == ("heygen", "fish", "edge")


# ----------------------------------------------------------------- ffmpeg


def test_find_ffmpeg_prefers_the_setting(tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_text("")
    exe.chmod(0o755)
    assert router.find_ffmpeg(str(exe), which=lambda n: None) == str(exe)
    assert router.find_ffmpeg("ff", which=lambda n: f"/bin/{n}") == "/bin/ff"


def test_find_ffmpeg_falls_back_to_path_then_hermes_tools(tmp_path):
    assert router.find_ffmpeg("", which=lambda n: "/usr/bin/ffmpeg") == "/usr/bin/ffmpeg"
    managed = tmp_path / ".hermes" / "tools" / "ffmpeg-9.0.1-linux-x64" / "bin" / "ffmpeg"
    managed.parent.mkdir(parents=True)
    managed.write_text("")
    managed.chmod(0o755)
    assert router.find_ffmpeg("", which=lambda n: None, home=tmp_path) == str(managed)
    assert router.find_ffmpeg("", which=lambda n: None, home=tmp_path / "none") is None


# --------------------------------------------------------------- provider


class Config:
    def __init__(self, **values):
        self.values = values

    def __call__(self, key, default=None):
        return self.values.get(key, default)


def provider(data_dir, http, run=None, **config):
    return router.OnAirTTS(
        Config(**config), lambda: data_dir, http=http, runner=run or runner(),
        which=lambda name: FFMPEG if name == "ffmpeg" else None,
    )


def test_the_provider_is_named_onair_and_is_a_tts_provider():
    from agent.tts_provider import TTSProvider

    tts = router.OnAirTTS(Config(), lambda: Path("/nonexistent"))
    assert isinstance(tts, TTSProvider)
    assert tts.name == "onair" and tts.display_name == "On Air (free voices)"
    assert [m["id"] for m in tts.list_models()][:2] == [FISH, HEYGEN]


def test_synthesize_writes_only_the_temp_path_hermes_gives(tmp_path, data_dir, key):
    folder = tmp_path / "audio_cache"
    folder.mkdir()
    temp = folder / ".tts_20261106_101500.0b1c2d3e.mp3"
    http = FakeHttp(speech={FISH: audio(MP3_BYTES)})
    tts = provider(data_dir, http)
    written = tts.synthesize(EN, str(temp), voice=None, model=None, speed=None, format="mp3",
                             unknown_extra="ignored")
    assert written == str(temp)
    assert temp.read_bytes() == MP3_BYTES
    assert sorted(p.name for p in folder.iterdir()) == [temp.name]  # nothing beside it
    os.replace(temp, folder / "tts_final.mp3")  # Hermes renames it afterwards
    assert sorted(p.name for p in folder.iterdir()) == ["tts_final.mp3"]


def test_synthesize_reads_settings_at_call_time(tmp_path, data_dir, key):
    http = FakeHttp(speech={HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    run = runner()
    config = Config()
    tts = router.OnAirTTS(config, lambda: data_dir, http=http, runner=run,
                          which=lambda name: name if name == FFMPEG else None)
    config.values.update(language="fr", ffmpeg_bin=FFMPEG, voices={"heygen": {"fr": "bram"}})
    out = tmp_path / "x.mp3"
    tts.synthesize(EN, str(out))
    assert http.posts() == [HEYGEN]
    assert json.loads(http.calls[-1]["body"])["voice"] == "bram"
    assert run.argvs()[0][0] == FFMPEG


def test_synthesize_raises_when_every_engine_fails(tmp_path):
    tts = provider(tmp_path / "nothing", FakeHttp())
    with pytest.raises(router.AllEnginesFailed):
        tts.synthesize(EN, str(tmp_path / "x.mp3"))
    assert not (tmp_path / "x.mp3").exists()


def test_a_broken_config_read_uses_defaults(tmp_path, data_dir):
    def broken(key, default=None):
        raise RuntimeError("config unavailable")

    tts = router.OnAirTTS(broken, lambda: data_dir, http=FakeHttp(), runner=runner(),
                          which=lambda n: None)
    out = tmp_path / "x.mp3"
    assert tts.synthesize(EN, str(out)) == str(out) and out.read_bytes() == EDGE_MP3


def test_is_available_without_network(tmp_path, data_dir, monkeypatch):
    http = FakeHttp()
    assert provider(data_dir, http).is_available() is True  # the sidecar is there
    assert provider(tmp_path / "none", http).is_available() is False
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert provider(tmp_path / "none", http).is_available() is True
    assert http.calls == []

    def boom():
        raise RuntimeError("no home")

    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert router.OnAirTTS(Config(), boom).is_available() is False


# ------------------------------------------- review r1, N1: atomic delivery


def failing_copy(error):
    def copy(src, dst, *args, **kwargs):
        dst.write(src.read(7))  # a partial write before the disk gives up
        raise error

    return copy


@pytest.mark.parametrize(
    "error",
    [OSError(errno.ENOSPC, "No space left on device"),
     PermissionError(errno.EACCES, "Permission denied")],
    ids=["ENOSPC", "EACCES"],
)
def test_a_failed_copy_stops_the_request_and_leaves_no_partial_file(
    tmp_path, data_dir, key, monkeypatch, error
):
    http = FakeHttp(speech={FISH: audio(MP3_BYTES), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    run = runner()
    env = environment(data_dir, http, run)
    monkeypatch.setattr(router.shutil, "copyfileobj", failing_copy(error))
    with pytest.raises(router.OutputWriteError, match=error.strerror):
        speak(tmp_path, env)
    assert http.posts() == [FISH]  # no fall-through: HeyGen and edge never ran
    assert run.calls == []
    assert [a["engine"] for a in env.attempts] == ["fish"]
    assert list((tmp_path / "out").iterdir()) == []  # neither output nor temp file


def test_an_existing_output_is_untouched_when_the_write_fails(tmp_path, data_dir, monkeypatch):
    out = tmp_path / "out" / "keep.mp3"
    out.parent.mkdir()
    out.write_bytes(b"earlier audio")
    monkeypatch.setattr(router.shutil, "copyfileobj",
                        failing_copy(OSError(errno.ENOSPC, "No space left on device")))
    with pytest.raises(router.OutputWriteError):
        router.speak(EN, out, env=environment(data_dir, FakeHttp()), language="en")
    assert out.read_bytes() == b"earlier audio"
    assert [p.name for p in out.parent.iterdir()] == ["keep.mp3"]


def test_a_failed_replace_removes_the_temp_file(tmp_path, data_dir, monkeypatch):
    def no_replace(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(router.os, "replace", no_replace)
    with pytest.raises(router.OutputWriteError, match="cross-device"):
        speak(tmp_path, environment(data_dir, FakeHttp()))
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_a_read_only_output_dir_stops_the_request(tmp_path, data_dir, key):
    folder = tmp_path / "locked"
    folder.mkdir()
    folder.chmod(0o500)
    http = FakeHttp(speech={FISH: audio(MP3_BYTES), HEYGEN: audio(PCM_BYTES, "audio/pcm")})
    try:
        with pytest.raises(router.OutputWriteError, match="Permission denied"):
            router.speak(EN, folder / ".tts.abc.mp3", env=environment(data_dir, http),
                         language="en")
        assert list(folder.iterdir()) == []
    finally:
        folder.chmod(0o700)
    assert http.posts() == [FISH]


def test_delivery_goes_through_a_temp_file_and_replace(tmp_path, data_dir, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src), Path(dst)))
        return real(src, dst)

    monkeypatch.setattr(router.os, "replace", spy)
    _, out = speak(tmp_path, environment(data_dir, FakeHttp()))
    ((src, dst),) = seen
    assert dst == out and src.parent == out.parent and src.name.endswith(".part")
    assert [p.name for p in out.parent.iterdir()] == [out.name]
