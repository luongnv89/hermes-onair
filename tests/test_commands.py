"""``/onair voice``: show, list and set this profile's persona (issue #5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onair import commands, personas


class Profiles:
    """A Hermes root with profiles; ``use(name)`` picks the profile of the next call."""

    def __init__(self, tmp_path: Path, names=("default", "forge", "mail")):
        self.root = tmp_path / "root"
        self.current = names[0]
        for name in names:
            self.data(name).mkdir(parents=True, exist_ok=True)

    def home(self, name: str) -> Path:
        return self.root if name == "default" else self.root / "profiles" / name

    def data(self, name: str) -> Path:
        return self.home(name) / "plugin-data" / "onair"

    def ctx_name(self) -> str:
        return "custom" if self.current != "default" else "default"

    def handler(self, config=None):
        values = config or {}
        return commands.make_handler(lambda: self.data(self.current), self.ctx_name,
                                     lambda key, default=None: values.get(key, default))

    def run(self, name: str, args: str, config=None) -> str:
        self.current = name
        return self.handler(config)(args)

    def stored(self, name: str) -> str:
        return json.loads((self.data(name) / "persona.json").read_text())["persona"]


@pytest.fixture
def profiles(tmp_path):
    return Profiles(tmp_path)


@pytest.mark.parametrize("args", ["", "   ", "episode https://x", "voice nope",
                                  "voice set", "voice set a b", "voice set bob --yes",
                                  "voice list extra"])
def test_anything_else_prints_the_usage(profiles, args):
    assert profiles.run("forge", args) == commands.USAGE


def test_voice_shows_the_profiles_persona_and_claims_it(profiles):
    reply = profiles.run("forge", "voice")
    persona = personas.BY_ID[profiles.stored("forge")]
    assert reply.splitlines()[0] == (
        f"Profile forge speaks as {persona.name} ({persona.gender}'s voice; "
        f"just claimed on first use)."
    )
    assert f"  HeyGen:   {persona.heygen}, every language" in reply
    edge = " · ".join(f"{lang} {persona.edge[lang]}" for lang in ("en", "fr", "vi"))
    assert f"  edge-tts: {edge}" in reply
    again = profiles.run("forge", "VOICE")
    assert "speaks as " + persona.name in again and "; claimed since 20" in again


def test_voice_shows_fish_or_says_it_is_skipped(profiles):
    profiles.run("forge", "voice set maya")
    assert "  Fish:     Laura (e3cd384158934cc9a01029cd7d278634), every language" \
        in profiles.run("forge", "voice")
    profiles.run("forge", "voice set bram")
    assert "  Fish:     none (Fish is skipped; English goes to HeyGen), every language" \
        in profiles.run("forge", "voice")


def test_voice_names_the_settings_that_override_the_persona(profiles):
    profiles.run("forge", "voice set theo")
    reply = profiles.run("forge", "voice", {"voices": {"heygen": {"*": "nadine"},
                                                       "edge": {"fr": "fr-FR-HenriNeural"}}})
    assert "The `voices` setting overrides:" in reply
    assert "  HeyGen en: nadine (from settings)" in reply
    assert "  edge-tts fr: fr-FR-HenriNeural (from settings)" in reply
    assert "edge-tts en" not in reply
    assert "overrides" not in profiles.run("forge", "voice", {"voices": {}})


def test_list_shows_the_pool_and_who_holds_what(profiles):
    profiles.run("default", "voice")
    profiles.run("mail", "voice")
    reply = profiles.run("forge", "voice list")
    lines = reply.splitlines()
    assert lines[0] == "Voice personas: 10 (2 claimed in this Hermes root)."
    assert len([line for line in lines if line.startswith("- ")]) == len(personas.POOL)
    by_id = {line.split()[1]: line for line in lines if line.startswith("- ")}
    assert by_id[profiles.stored("default")].endswith("->  default")
    assert by_id[profiles.stored("mail")].endswith("->  mail")
    assert sum(line.endswith("->  free") for line in by_id.values()) == 8
    assert "3 personas have none" in lines[-1]
    assert not (profiles.data("forge") / "persona.json").exists()  # list never claims
    profiles.run("forge", "voice")
    assert by_id_line(profiles.run("forge", "voice list"), profiles.stored("forge")) \
        .endswith("->  forge (this profile)")


def by_id_line(reply: str, persona_id: str) -> str:
    return next(line for line in reply.splitlines() if line.startswith(f"- {persona_id} "))


def test_set_changes_this_profile_only(profiles):
    profiles.run("mail", "voice")
    free = next(p for p in personas.POOL if p.id != profiles.stored("mail"))
    reply = profiles.run("forge", f"voice set {free.name}")
    assert reply.splitlines()[0] == f"Profile forge now speaks as {free.name}."
    assert profiles.stored("forge") == free.id
    assert f"speaks as {free.name} ({free.gender}'s voice; set since" in \
        profiles.run("forge", "voice")


def test_set_refuses_another_profiles_persona_without_force(profiles):
    profiles.run("mail", "voice")
    theirs = personas.BY_ID[profiles.stored("mail")]
    before = profiles.run("forge", "voice")
    reply = profiles.run("forge", f"voice set {theirs.id}")
    assert reply == (f"Not changed: {theirs.name} is claimed by mail; pick a free persona "
                     f"(/onair voice list) or add --force to share it.")
    assert profiles.run("forge", "voice").splitlines()[0] != before  # status line only differs
    assert profiles.stored("forge") != theirs.id


def test_set_force_shares_and_says_so(profiles):
    profiles.run("mail", "voice")
    theirs = personas.BY_ID[profiles.stored("mail")]
    mail_file = (profiles.data("mail") / "persona.json").read_bytes()
    reply = profiles.run("forge", f"voice set {theirs.id} --force")
    assert reply.splitlines()[0] == \
        f"Profile forge now speaks as {theirs.name}. Shared with mail (--force)."
    assert profiles.stored("forge") == theirs.id
    assert (profiles.data("mail") / "persona.json").read_bytes() == mail_file
    assert "voice; set with --force (shared) since " in profiles.run("forge", "voice")


def test_set_an_unknown_persona(profiles):
    assert profiles.run("forge", "voice set zed").startswith(
        "Not changed: no persona called 'zed'; choose one of: bob, alice, maya")


def test_set_reports_a_write_error(profiles, monkeypatch):
    monkeypatch.setattr(personas, "write_record",
                        lambda *a: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    reply = profiles.run("forge", "voice set bob")
    assert reply.startswith("Not changed: cannot write ") and "(Permission denied)." in reply


def test_a_broken_profile_name_falls_back_to_the_home(profiles):
    def boom():
        raise RuntimeError("no profile")

    handler = commands.make_handler(lambda: profiles.data("forge"), boom)
    assert handler("voice").startswith("Profile forge speaks as ")


def test_the_default_profile_is_named_default(profiles):
    assert profiles.run("default", "voice").startswith("Profile default speaks as ")


def test_the_handler_takes_only_raw_args():
    # Hermes's invoke_plugin_command passes keywords only when the handler declares them.
    import inspect

    params = inspect.signature(commands.make_handler(lambda: Path("/x"))).parameters
    assert list(params) == ["raw_args"]
