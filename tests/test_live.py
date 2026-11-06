"""Live smoke tests: real OpenRouter calls. Deselected by default (AGENTS.md rule 5).

Run by hand, with the key in the environment only (never in a file):

    OPENROUTER_API_KEY=... pytest -m live tests/test_live.py -v -s

Each test goes through the price gate first, so a model that is no longer free
is skipped (the test fails, nothing paid is called). Audio lands in pytest's
tmp dir and is for private listening only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from onair import openrouter, router

pytestmark = pytest.mark.live

SENTENCE = "Welcome to On Air, a tiny test show recorded on a quiet afternoon."


@pytest.fixture
def key():
    value = openrouter.api_key()
    if value is None:
        pytest.skip("set OPENROUTER_API_KEY in the environment to run the live smoke test")
    return value


def ffmpeg_or_skip() -> str:
    found = router.find_ffmpeg()
    if not found:
        pytest.skip("ffmpeg not found (PATH or ~/.hermes/tools/ffmpeg-*)")
    return found


def duration(path: Path) -> float | None:
    ffmpeg = router.find_ffmpeg()
    ffprobe = shutil.which("ffprobe") or (
        str(Path(ffmpeg).with_name("ffprobe")) if ffmpeg else None
    )
    if not ffprobe or not Path(ffprobe).exists():
        return None
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def check_audio(path: Path) -> None:
    assert path.is_file() and path.stat().st_size > 1000
    seconds = duration(path)
    print(f"\n  {path} ({path.stat().st_size} bytes, {seconds} s)")
    if seconds is not None:
        assert 1.0 < seconds < 30.0


def test_live_english_sentence_through_the_router(tmp_path, key):
    """One EN sentence through the full chain: Fish free should speak it."""
    env = router.Environment(data_dir=tmp_path / "data", ffmpeg=router.find_ffmpeg())
    out = tmp_path / "en.mp3"
    engine = router.speak(SENTENCE, out, env=env, language="en", fmt="mp3")
    print(f"\n  attempts: {env.attempts}")
    assert key not in str(env.attempts)
    check_audio(out)
    assert engine == "fish", env.attempts


def test_live_fish(tmp_path, key):
    env = router.Environment(data_dir=tmp_path / "data", ffmpeg=router.find_ffmpeg())
    made = router.render_fish(env, SENTENCE, router.voice_for("fish", "en"), tmp_path, "mp3")
    check_audio(made)


def test_live_heygen(tmp_path, key):
    env = router.Environment(data_dir=tmp_path / "data", ffmpeg=ffmpeg_or_skip())
    made = router.render_heygen(env, SENTENCE, router.voice_for("heygen", "en"), tmp_path, "mp3")
    check_audio(made)
