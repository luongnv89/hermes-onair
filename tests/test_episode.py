"""The episode pipeline: `/onair <link|text>` and the `onair_episode` tool.

Every outside effect is faked: the engine's CLI (a scripted runner that
writes the job files the real one writes), ``ctx.llm`` and OpenRouter's HTTP.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from onair import adapter, commands, episode, personas, router, tool, writer

from fakes import MP3_BYTES, FakeHttp, FakeRunner, audio, done, status, write_install

KEY = "fake-test-key-0123456789abcdef"
MODEL = "vendor/free-writer:free"

EN = (
    "## Tides\n\n"
    "Tides are the regular rise and fall of the sea. They are caused mostly by the Moon's "
    "gravity pulling on the oceans. The Sun also plays a part, but its pull is a little less "
    "than half as strong. When the Sun and Moon line up, we get spring tides, which are higher "
    "than usual. When they pull at right angles, we get neap tides, which are weaker. Most "
    "coasts see 2 high tides and 2 low tides every day. Some bays funnel the water into very "
    "high tides. People have used tide tables for centuries to plan fishing trips."
)
FR = (
    "Les marées sont la montée et la descente régulières de la mer. Elles sont causées "
    "surtout par la gravité de la Lune qui attire les océans. Le Soleil joue aussi un rôle "
    "dans ce mouvement. Quand le Soleil et la Lune sont alignés, les marées sont plus hautes "
    "que d'habitude. La plupart des côtes connaissent deux marées hautes chaque jour."
)
VI = (
    "Thủy triều là sự dâng lên và rút xuống đều đặn của nước biển. Nguyên nhân chính là lực "
    "hấp dẫn của Mặt Trăng kéo các đại dương. Mặt Trời cũng góp một phần vào chuyển động này. "
    "Khi Mặt Trời và Mặt Trăng thẳng hàng, ta có triều cường cao hơn bình thường."
)
SHOW = {
    "en": ("Welcome to Build Wiz AI Show. Big AI ideas, made clear for builders.",
           "That's all for today on Build Wiz AI Show. Thanks for listening, and see you next "
           "time.", "Builder's Corner", (128, 172), 150),
    "fr": ("Bienvenue dans Build Wiz AI Show. Les grandes idées de l'IA, expliquées clairement "
           "à ceux qui construisent.", "C'est tout pour aujourd'hui dans Build Wiz AI Show. "
           "Merci de nous avoir écoutés, et à bientôt.", "Le coin des builders", (132, 178), 155),
    "vi": ("Chào mừng các bạn đến với Build Wiz AI Show. Những ý tưởng lớn về AI, giải thích "
           "dễ hiểu cho người làm sản phẩm.", "Tập hôm nay của Build Wiz AI Show đến đây là hết. "
           "Cảm ơn các bạn đã lắng nghe, hẹn gặp lại ở tập sau.", "Góc builder", (154, 207), 180),
}


def brief_data(language="en", target=1.0):
    opening, sign_off, corner, word_range, budget = SHOW[language.split("-")[0]]
    return {
        "version": 1, "format": "podcast-2host", "language": language, "target_min": target,
        "faithfulness": "factual", "structure": "show-v2",
        "length": {"target_min": target, "word_budget": budget, "word_range": list(word_range)},
        "show": {"name": "Build Wiz AI Show", "hosts": {"host_a": "Bob", "host_b": "Alice"},
                 "opening": opening, "sign_off": sign_off, "builders_corner": corner},
        "hosts": {"host_a": {"name": "Bob", "role": "host-curious"},
                  "host_b": {"name": "Alice", "role": "host-expert"}},
        "host_roles": {"explainer": "Alice", "asker": "Bob"},
    }


def result_json(status_, step, job, *, reply=None, extra=None):
    data = {"version": 1, "status": status_, "step": step, "job": str(job),
            "message": f"{status_} at {step}", "reply": reply,
            "next": None if status_ == "delivered" else f"dropcast resume {job}",
            "steps": {}, "request": {}, "notes": []}
    data.update(extra or {})
    return json.dumps(data)


class FakeEngine:
    """The engine's CLI as the pipeline drives it, with the files it writes."""

    def __init__(self, text=EN, language="en", title="pasted text", url=None, check=None):
        self.text, self.language, self.title, self.url = text, language, title, url
        self.check = check or (lambda n, script: (0, ""))
        self.checks: list[dict] = []
        self.runner = (
            FakeRunner()
            .on(lambda a: a[1:2] == ["run"], self._run)
            .on(lambda a: a[1:5] == ["-I", "-m", adapter.SCRIPT_MODULE, "finalize"], self._check)
            .on(lambda a: a[1:2] == ["resume"], self._resume)
            .on(lambda a: a[1:3] == ["-I", "-c"], self._finish)
        )
        self.job: Path | None = None

    def _run(self, argv):
        root = Path(argv[argv.index("--root") + 1])
        self.job = root / "jobs" / "2026-11-06_pasted-text"
        self.job.mkdir(parents=True)
        lang = argv[argv.index("--lang") + 1] if "--lang" in argv else self.language
        target = float(argv[argv.index("--length") + 1])
        (self.job / "brief.json").write_text(json.dumps(brief_data(lang, target)))
        source = {"title": self.title, "author": None, "url": self.url, "language": lang,
                  "text": self.text, "word_count": len(self.text.split())}
        (self.job / "source.json").write_text(json.dumps(source, ensure_ascii=False))
        return done(1, result_json("needs_agent", "script", self.job))

    def _check(self, argv):
        script = json.loads(Path(argv[5]).read_text())
        self.checks.append(script)
        words = sum(len(s["text"].split()) for s in script["segments"])
        script["est_min"] = round(words / 150, 2)
        Path(argv[5]).write_text(json.dumps(script))
        code, errors = self.check(len(self.checks), script)
        out = "".join(f"  {e}\n" for e in errors.splitlines()) if errors else ""
        return done(code, "", f"{argv[5]}: invalid\n{out}" if code else "")

    def _write_episode(self):
        (self.job / "episode.mp3").write_bytes(MP3_BYTES)
        (self.job / "transcript.vtt").write_text("WEBVTT\n")

    def _resume(self, argv):
        self.resume_argv = argv
        self._write_episode()
        reply = {"adapter": "headless", "text": "ok", "attachments": [],
                 "episode": str(self.job / "episode.mp3"),
                 "transcript": str(self.job / "transcript.vtt")}
        return done(0, result_json("delivered", "reply", self.job, reply=reply,
                                   extra={"delivery": {"duration_s": 61.5}}))

    def _finish(self, argv):
        self.finish_spec = json.loads(Path(argv[-1]).read_text())
        self._write_episode()
        return done(0, json.dumps({"ok": True, "episode": str(self.job / "episode.mp3"),
                                   "transcript": str(self.job / "transcript.vtt"),
                                   "duration_s": 58.0}))

    def calls(self, kind):
        index = {"run": 1, "resume": 1}
        return [a for a in self.runner.argvs() if a[index.get(kind, 1)] == kind]


class FakeLlm:
    """``ctx.llm``: answers from a list (an exception raises), records every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return types.SimpleNamespace(parsed=answer, text=json.dumps(answer), model=MODEL)


def good_llm_json():
    return {
        "teaser": "Why does the sea rise twice a day?",
        "thesis": {"text": "Tides come mostly from the Moon's pull on the oceans.",
                   "quote": "They are caused mostly by the Moon's gravity pulling on the oceans."},
        "points": [
            {"question": "And the Sun?", "answer": "The Sun helps, at a little less than half "
             "the Moon's strength.", "quote": "The Sun also plays a part, but its pull is a "
             "little less than half as strong."},
            {"question": "What about spring tides?", "answer": "When the Sun and Moon line up, "
             "tides run higher than usual.", "quote": "When the Sun and Moon line up, we get "
             "spring tides, which are higher than usual."},
        ],
        "counterpoint": {"question": "Is it the same everywhere?", "answer": "No, some bays "
                         "funnel water into very high tides.",
                         "quote": "Some bays funnel the water into very high tides."},
        "tip": {"text": "Check a tide table before your next trip, like fishers have for "
                "centuries.", "quote": "People have used tide tables for centuries to plan "
                "fishing trips."},
        "takeaway": "The Moon leads and the Sun helps.",
        "open_question": "How much does a bay's shape change the tide?",
        "summary": ["Tides follow the Moon.", "The Sun helps.", "Bays can amplify them."],
    }


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "plugin-data" / "onair"
    write_install(path / "sidecar")
    personas.write_record(path / personas.CLAIM_FILE, {"persona": "lena", "profile": "test"})
    return path


def host_for(data_dir, engine, llm=None, http=None, config=None, environ=None):
    tools = episode.Tools(runner=engine.runner, http=http or FakeHttp(), which=lambda _: None,
                          sleep=lambda s: None, environ=environ)
    settings = config or {}
    return episode.Host(lambda: data_dir, lambda: "test", lambda k, d=None: settings.get(k, d),
                        (lambda: llm), tools)


def record(data_dir, engine, source=EN, llm=None, **kw):
    host = host_for(data_dir, engine, llm=llm, **kw)
    return host.record(host.request(source))


# ---------------------------------------------------------------- requests


def test_a_lone_http_url_is_a_link_and_anything_else_is_text():
    assert episode.make_request("https://example.com/a").link == "https://example.com/a"
    assert episode.make_request("/etc/passwd").text == "/etc/passwd"  # never a local file
    assert episode.make_request("file:///etc/passwd").text is not None
    req = episode.make_request("Some text here", "FR", "2")
    assert (req.text, req.language, req.minutes) == ("Some text here", "fr", 2.0)
    assert episode.make_request("x", "auto").language is None


@pytest.mark.parametrize("args", [("",), ("  ",), ("t", "not a tag!"), ("t", None, "9"),
                                  ("t", None, "abc")])
def test_bad_requests_are_refused_with_a_reason(args):
    with pytest.raises(episode.EpisodeError):
        episode.make_request(*args)


def test_command_options_are_read_only_at_the_start():
    assert commands.parse_episode_args("--lang fr --minutes 2 Bonjour --lang en") == (
        "Bonjour --lang en", "fr", "2")
    assert commands.parse_episode_args("--min=1.5 hello") == ("hello", None, "1.5")
    assert commands.parse_episode_args("--other text") == ("--other text", None, None)


# ------------------------------------------------------------------ casting


@pytest.mark.parametrize("persona", personas.POOL)
def test_two_hosts_are_two_personas_of_their_voice_type(persona):
    hosts = episode.cast(persona)
    assert hosts["host_a"].gender == "man" and hosts["host_b"].gender == "woman"
    assert hosts["host_a"].id != hosts["host_b"].id
    assert persona in hosts.values()
    if persona.fish:
        assert all(p.fish for p in hosts.values())  # Fish can voice both hosts


# ------------------------------------------------------------------- writer


def brief(language="en", target=1.0, tmp_path=Path("/job")):
    data = brief_data(language, target)
    return adapter.Brief(
        job=tmp_path, language=language, target_min=target,
        word_range=tuple(data["length"]["word_range"]), word_budget=data["length"]["word_budget"],
        show="Build Wiz AI Show", opening=data["show"]["opening"],
        sign_off=data["show"]["sign_off"], corner=data["show"]["builders_corner"],
        names={"host_a": "Bob", "host_b": "Alice"},
        roles={"host_a": "host-curious", "host_b": "host-expert"},
        asker="host_a", explainer="host_b", structure="show-v2", faithfulness="factual")


def source(text=EN, title="pasted text", author=None):
    return adapter.Source(title=title, author=author, url=None, language=None, text=text,
                          word_count=len(text.split()))


def test_markdown_never_reaches_the_spoken_text():
    found = writer.sentences("## Moon Facts\n\n- **Bold** item that is long enough here.\n"
                             "The Moon is Earth's only natural satellite.")
    assert "The Moon is Earth's only natural satellite." in found
    assert not any("#" in s or "**" in s for s in found)


@pytest.mark.parametrize("language,text", [("en", EN), ("fr", FR), ("vi", VI)])
def test_the_template_follows_the_show_and_stays_under_the_cap(language, text):
    b = brief(language)
    draft = writer.template_draft(b, source(text), reason="429")
    parts = [t.part for t in draft.turns]
    assert parts == sorted(parts, key=writer.PARTS.index)  # the show's order
    assert set(parts) == set(writer.PARTS)
    assert draft.turns[1].text.startswith(b.opening) and draft.turns[1].host == writer.ASKER
    assert draft.turns[-1].text.endswith(b.sign_off)
    assert all(t.text.endswith("?") for t in draft.turns if t.part == "teaser")
    assert any(t.quote for t in draft.turns if t.part == "corner")
    for turn in draft.turns:
        if turn.quote:
            assert writer.in_source(turn.quote, text) and turn.quote in turn.text
    assert draft.words <= writer.word_cap(b) and draft.source == "template"
    assert draft.notes == ["template: 429"]


def test_the_word_cap_is_about_150_words_a_minute():
    assert 140 <= writer.word_cap(brief("en", 1.0)) <= 172
    assert writer.word_cap(brief("en", 2.0)) > writer.word_cap(brief("en", 1.0))


def test_the_llm_path_records_the_model_and_never_overrides_it():
    llm = FakeLlm(good_llm_json())
    draft = writer.llm_draft(llm, brief(), source())
    assert draft.source == "llm" and draft.model == MODEL
    (call,) = llm.calls
    assert not {"model", "provider", "agent_id", "profile"} & set(call)
    assert call["timeout"] == writer.LLM_TIMEOUT_S and call["json_schema"] == writer.SCHEMA
    assert all(t.quote is None or writer.in_source(t.quote, EN) for t in draft.turns)
    assert draft.words <= writer.word_cap(brief())


def test_the_llm_is_retried_once_with_backoff():
    slept = []
    llm = FakeLlm(RuntimeError("HTTP 429 fair-share"), good_llm_json())
    draft = writer.llm_draft(llm, brief(), source(), sleep=slept.append)
    assert draft.source == "llm" and len(llm.calls) == 2 and slept == [writer.LLM_BACKOFF_S]


def test_two_llm_failures_are_reported_once():
    llm = FakeLlm(RuntimeError("HTTP 429"), RuntimeError("HTTP 429"), good_llm_json())
    with pytest.raises(writer.ScriptFailed, match=r"RuntimeError: HTTP 429 \(both attempts\)"):
        writer.llm_draft(llm, brief(), source(), sleep=lambda s: None)
    assert len(llm.calls) == 2  # one retry, never a third call


def test_unbacked_quotes_and_numbers_are_dropped():
    data = good_llm_json()
    data["points"][0]["quote"] = "words that are not in the source at all"
    data["points"][1]["answer"] = "There are 3 spring tides a week."  # number not in its quote
    draft = writer.compose(data, brief(), source(), model=MODEL)
    texts = [t.text for t in draft.turns]
    assert "There are 3 spring tides a week." not in texts
    point = next(t for t in draft.turns if t.text.startswith("The Sun helps"))
    assert point.quote is None


def test_a_rambling_llm_script_is_cut_to_the_hard_cap():
    data = good_llm_json()
    long = " ".join(["The Moon pulls the oceans and makes the tides rise."] * 4)
    data["points"] = [dict(data["points"][0], answer=long) for _ in range(4)]
    draft = writer.compose(data, brief(), source(), model=MODEL)
    assert draft.words <= writer.word_cap(brief())
    assert any("cut" in n for n in draft.notes)
    data["tip"]["text"] = "Watch " + " and ".join(["the Moon"] * 80) + "."
    with pytest.raises(writer.ScriptFailed, match="word cap"):
        writer.compose(data, brief(), source(), model=MODEL)


def test_an_llm_answer_with_nothing_backed_fails():
    data = good_llm_json()
    data["thesis"]["quote"] = data["points"][0]["quote"] = data["points"][1]["quote"] = "nope"
    data["points"] = [dict(p, answer="Tides happen 7 times.") for p in data["points"]]
    data["thesis"]["text"] = "Tides happen 9 times."
    with pytest.raises(writer.ScriptFailed, match="no line backed"):
        writer.compose(data, brief(), source(), model=MODEL)


# ----------------------------------------------------------------- adapter


def test_run_and_resume_carry_the_brief_options(tmp_path):
    argv = adapter.build_run_argv(tmp_path, Path("/r"), text="x", fmt="podcast-2host",
                                  length_min=1, language="fr", guests=0)
    assert argv[-2:] == ["--text", "-"]
    for flag, value in (("--format", "podcast-2host"), ("--length", "1"), ("--lang", "fr"),
                        ("--guests", "0")):
        assert argv[argv.index(flag) + 1] == value
    assert "--no-publish" in argv and argv[argv.index("--engine") + 1] == "edgetts"
    resume = adapter.build_resume_argv(tmp_path, "/r/j", Path("/r"), voices=Path("/v.json"),
                                       length_min=0.8)
    assert resume[-2:] == ["--", "/r/j"] and resume[resume.index("--voices") + 1] == "/v.json"
    assert resume[resume.index("--length") + 1] == "0.8"


@pytest.mark.parametrize("kwargs", [{"fmt": "Bad Format"}, {"length_min": 99},
                                    {"length_min": True}, {"language": "x y"},
                                    {"guests": -1}])
def test_bad_brief_options_are_refused(tmp_path, kwargs):
    with pytest.raises(adapter.AdapterError):
        adapter.build_run_argv(tmp_path, Path("/r"), text="x", **kwargs)


def test_script_document_follows_the_contract(tmp_path):
    b = brief()
    draft = writer.template_draft(b, source())
    doc = adapter.script_document(draft, b, source())
    assert doc["format"] == "podcast-2host" and doc["structure"] == "show-v2"
    assert [s["id"] for s in doc["speakers"]] == ["host_a", "host_b"]
    assert doc["segments"][1]["speaker"] == "host_a"  # the asker opens
    sections = [s["section"] for s in doc["segments"]]
    assert sections[0] == "Teaser" and sections[-1] == "Outro" and "Builder's Corner" in sections
    ids = {s["id"] for s in doc["segments"]}
    assert doc["fact_check"] and all(c["segment"] in ids and c["ok"] for c in doc["fact_check"])
    assert adapter.script_document(draft, b, source(), 0.8)["target_min"] == 0.8


def test_voices_file_keys_each_host_by_name_and_language(tmp_path):
    path = adapter.write_voices(tmp_path / "v.json", brief("en-US"),
                                {"host_a": "en-US-GuyNeural", "host_b": "en-US-EmmaNeural"})
    roles = json.loads(path.read_text())["roles"]
    assert roles["host-bob/en-US"] == roles["host-bob/en"] == {"edgetts": "en-US-GuyNeural"}
    assert roles["host-alice/en"] == {"edgetts": "en-US-EmmaNeural"}
    with pytest.raises(adapter.AdapterError):
        adapter.write_voices(tmp_path / "v.json", brief(), {"host_a": "bad voice; rm"})


def test_check_script_reads_errors_and_the_estimate(tmp_path):
    (tmp_path / "script.json").write_text(json.dumps({"est_min": 1.4}))
    err = "x: invalid (1 error)\n  est_min 1.4 (204 spoken words) is not within ±15 %\n"
    run = FakeRunner().on(lambda a: True, done(1, "", err))
    verdict = adapter.check_script(tmp_path, tmp_path, runner=run)
    assert not verdict.ok and verdict.only_length and verdict.est_min == 1.4
    assert run.calls[0]["env"].get("OPENROUTER_API_KEY") is None
    run = FakeRunner().on(lambda a: True, done(2, "", "usage"))
    with pytest.raises(adapter.AdapterError, match="exit 2"):
        adapter.check_script(tmp_path, tmp_path, runner=run)


def test_finish_failures_are_adapter_errors(tmp_path):
    run = FakeRunner().on(lambda a: True, done(1, json.dumps({"ok": False, "error": "no ffmpeg"})))
    with pytest.raises(adapter.AdapterError, match="no ffmpeg"):
        adapter.finish_parts(tmp_path, tmp_path, [], model="m", voices={}, started="t",
                             runner=run)


# ---------------------------------------------------------------- pipeline


def test_a_text_episode_with_the_llm_is_voiced_by_edge_without_a_key(data_dir):
    engine = FakeEngine()
    made = record(data_dir, engine, llm=FakeLlm(good_llm_json()))
    assert made.script_source == "llm" and made.model == MODEL and made.llm_error is None
    assert made.engine == "edge" and made.private_use
    assert [s["engine"] for s in made.engines_skipped] == ["fish", "heygen"]
    assert made.mp3.is_file() and made.transcript.name == "transcript.vtt"
    assert made.hosts == {"Bob": "Theo", "Alice": "Lena"}  # profile persona + a partner
    voices = json.loads(Path(engine.resume_argv[engine.resume_argv.index("--voices") + 1])
                        .read_text())["roles"]
    assert voices["host-alice/en"] == {"edgetts": "en-US-EmmaNeural"}  # Lena's voice
    stored = json.loads((made.job / episode.RECORD_FILE).read_text())
    assert stored["model"] == MODEL and stored["engine"] == "edge"
    run = engine.runner.argvs()[0]
    assert run[run.index("--length") + 1] == "1" and run[run.index("--guests") + 1] == "0"
    text = episode.reply(made)
    assert text.splitlines()[-1] == f"MEDIA:{made.mp3}"
    assert MODEL in text and "Private listening only" in text


def test_a_failed_llm_still_yields_an_episode_and_the_reply_says_so(data_dir):
    engine = FakeEngine()
    llm = FakeLlm(RuntimeError("HTTP 429 fair-share"), RuntimeError("HTTP 429 fair-share"))
    made = record(data_dir, engine, llm=llm)
    assert made.script_source == "template" and made.model is None
    assert "429" in made.llm_error and len(llm.calls) == 2
    text = episode.reply(made)
    assert "built-in template, because the LLM script failed" in text and "429" in text
    assert text.splitlines()[-1].startswith("MEDIA:/")


def test_no_llm_at_all_uses_the_template(data_dir):
    made = record(data_dir, FakeEngine(), llm=None)
    assert made.script_source == "template" and "no ctx.llm" in made.llm_error


def test_an_llm_script_that_fails_the_checks_is_replaced_by_the_template(data_dir):
    engine = FakeEngine(check=lambda n, s: (1, "segments: missing section(s) Outro") if n == 1
                        else (0, ""))
    made = record(data_dir, engine, llm=FakeLlm(good_llm_json()))
    assert made.script_source == "template" and "failed the checks" in made.llm_error
    assert len(engine.checks) == 2


def test_a_length_only_miss_rebriefs_the_job_to_the_scripts_length(data_dir):
    def check(n, script):
        if script["target_min"] == 1.0:
            return 1, "est_min 0.6 (90 spoken words) is not within ±15 % of target_min 1"
        return 0, ""

    engine = FakeEngine(check=check)
    made = record(data_dir, engine)
    target = engine.checks[-1]["target_min"]
    assert target != 1.0 and engine.resume_argv[engine.resume_argv.index("--length") + 1] == \
        f"{target:g}"
    assert any("length set" in n for n in made.notes)


def test_a_script_that_cannot_pass_the_checks_is_an_error(data_dir):
    engine = FakeEngine(check=lambda n, s: (1, "fact_check: quote not found"))
    with pytest.raises(episode.EpisodeError, match="quote not found"):
        record(data_dir, engine)


@pytest.mark.parametrize("language,text,voice", [("fr", FR, "fr-CA-SylvieNeural"),
                                                 ("vi", VI, "de-DE-SeraphinaMultilingualNeural")])
def test_french_and_vietnamese_use_the_personas_voices(data_dir, language, text, voice):
    engine = FakeEngine(text=text, language=language)
    made = record(data_dir, engine, source=text)
    assert made.language == language and made.engine == "edge"
    voices = json.loads(Path(engine.resume_argv[engine.resume_argv.index("--voices") + 1])
                        .read_text())["roles"]
    assert voices[f"host-alice/{language}"] == {"edgetts": voice}
    assert SHOW[language][0] in json.dumps(engine.checks[-1], ensure_ascii=False)


def test_a_language_parameter_wins_over_detection(data_dir):
    engine = FakeEngine()
    host = host_for(data_dir, engine)
    host.record(host.request(EN, "fr"))
    run = engine.runner.argvs()[0]
    assert run[run.index("--lang") + 1] == "fr"


def test_with_a_key_fish_voices_every_turn_and_the_finish_step_joins_them(
        data_dir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    engine = FakeEngine()
    http = FakeHttp(speech={router.FISH_MODEL: audio(MP3_BYTES)})
    made = record(data_dir, engine, http=http)
    assert made.engine == "fish" and not made.private_use
    turns = len(engine.checks[-1]["segments"])
    assert http.posts() == [router.FISH_MODEL] * turns  # one engine for the whole episode
    assert len(engine.finish_spec["parts"]) == turns
    assert not engine.calls("resume")  # edge-tts never ran
    assert all(KEY not in json.dumps(c.get("env") or {}) for c in engine.runner.calls)
    assert "Private listening only" not in episode.reply(made)


def test_a_failed_turn_moves_the_whole_episode_to_the_next_engine(data_dir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    engine = FakeEngine()
    answers = [audio(MP3_BYTES), audio(MP3_BYTES), status(429)]
    http = FakeHttp(speech={router.FISH_MODEL: answers})
    made = record(data_dir, engine, http=http)
    # Fish failed on turn 3; HeyGen needs ffmpeg (none here); edge-tts renders it all.
    assert made.engine == "edge"
    assert [s["engine"] for s in made.engines_skipped] == ["fish", "heygen"]
    assert not hasattr(engine, "finish_spec")  # no Fish part reached the episode


def test_a_paid_model_is_never_called(data_dir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    paid = {"prompt": "0.001", "completion": "0"}
    from fakes import model_list

    http = FakeHttp(models=model_list(**{router.FISH_MODEL: paid, router.HEYGEN_MODEL: paid}))
    made = record(data_dir, FakeEngine(), http=http)
    assert http.posts() == [] and made.engine == "edge"


def test_no_sidecar_is_a_clear_error(tmp_path):
    engine = FakeEngine()
    with pytest.raises(episode.EpisodeError, match="hermes onair setup"):
        record(tmp_path / "plugin-data" / "onair", engine)
    assert engine.runner.calls == []


def test_an_ingest_failure_is_reported(data_dir):
    engine = FakeEngine()
    engine.runner = FakeRunner().on(lambda a: a[1:2] == ["run"], done(1, json.dumps({
        "version": 1, "status": "failed", "step": "ingest", "job": None,
        "message": "could not fetch the page", "reply": None, "next": None, "steps": {},
        "request": {}, "notes": []})))
    with pytest.raises(episode.EpisodeError, match="could not fetch the page"):
        record(data_dir, engine, source="https://example.com/x")


# ----------------------------------------------------------- command & tool


class Handle:
    def __init__(self, ok):
        self.ok = ok
        self.edits = []

    def edit(self, text):
        self.edits.append(text)
        return {"ok": True}


def test_the_command_posts_recording_at_once_then_replies_with_media(data_dir):
    engine = FakeEngine()
    posted, handle = [], Handle(True)

    def post(text):
        posted.append(text)
        return handle

    host = host_for(data_dir, engine)
    handler = commands.make_handler(lambda: data_dir, lambda: "test", None, host=host, post=post)
    reply = handler(EN)
    assert posted == [commands.RECORDING] and "recorded in" in handle.edits[0]
    assert reply.splitlines()[-1].startswith("MEDIA:/") and reply.endswith(".mp3")


def test_without_a_bound_session_the_note_is_printed(data_dir, capsys):
    host = host_for(data_dir, FakeEngine())
    handler = commands.make_handler(lambda: data_dir, lambda: "test", None, host=host,
                                    post=lambda text: Handle(False))
    assert handler(EN).splitlines()[-1].startswith("MEDIA:/")
    assert commands.RECORDING in capsys.readouterr().err


def test_command_errors_are_replies_not_exceptions(data_dir):
    engine = FakeEngine(check=lambda n, s: (1, "broken"))
    handle = Handle(True)
    host = host_for(data_dir, engine)
    handler = commands.make_handler(lambda: data_dir, None, None, host=host,
                                    post=lambda text: handle)
    assert handler(EN).startswith("On Air could not record this episode")
    assert handle.edits == ["🎙️ On Air: no episode this time."]
    assert "usage" in handler("--minutes 99 hello")


def test_voice_subcommands_still_answer(data_dir):
    host = host_for(data_dir, FakeEngine())
    handler = commands.make_handler(lambda: data_dir, lambda: "test", None, host=host)
    assert handler("voice list").startswith("Voice personas:")
    assert handler("") == commands.USAGE


def test_the_tool_returns_json_with_the_media_path(data_dir):
    handler = tool.make_handler(host_for(data_dir, FakeEngine()))
    data = json.loads(handler({"source": EN, "minutes": 1}, task_id="t1"))
    assert data["ok"] and data["media"] == f"MEDIA:{data['mp3']}"
    assert data["transcript"].endswith("transcript.vtt") and data["engine"] == "edge"
    assert set(data["timings"]) >= {"ingest_s", "script_s", "checks_s", "render_s", "total_s"}
    assert data["script"] == "template" and data["model"] is None
    bad = json.loads(handler({"source": ""}))
    assert bad == {"ok": False, "error": bad["error"]} and "link" in bad["error"]


def test_the_tool_schema_is_a_function_schema():
    assert tool.SCHEMA["name"] == tool.NAME == "onair_episode"
    params = tool.SCHEMA["parameters"]
    assert params["required"] == ["source"] and set(params["properties"]) == {
        "source", "language", "minutes"}


def test_sentences_are_not_cut_after_an_abbreviation():
    text = ("If you set a single green pea next to a U.S. nickel, you'd see the size. "
            "Dr. Smith agrees with it. The Moon has no rings.")
    assert writer.sentences(text, low=3) == [
        "If you set a single green pea next to a U.S. nickel, you'd see the size.",
        "Dr. Smith agrees with it.", "The Moon has no rings."]


def test_the_first_question_asks_for_the_idea_when_there_is_no_thesis():
    two = "The Moon has no rings at all today. The Moon has no moons of its own either."
    draft = writer.template_draft(brief(), source(two))
    main = [t for t in draft.turns if t.part == "main"]
    assert main[0].text == writer.PHRASES["en"]["first_q"]
