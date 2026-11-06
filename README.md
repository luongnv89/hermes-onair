# hermes-onair
On Air: free voices and two-host audio shows for Hermes Agent (Hermes plugin)

> Status: early (v0.1.0). This release ships `hermes onair setup`, the free
> voice router (`tts.provider: onair`) and a distinct voice per profile
> (`/onair voice`); shows (`/onair` episodes) come next.

## Install

On Air is a directory plugin: the repo root holds `plugin.yaml` and
`__init__.py`. The host side uses only the Python standard library
(`python_runtime: external`), so Hermes installs nothing for it.

```sh
git clone git@github.com:luongnv89/hermes-onair.git
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
ln -s "$PWD/hermes-onair" "${HERMES_HOME:-$HOME/.hermes}/plugins/onair"
hermes plugins enable onair
```

## Setup

On Air drives [DropCast](https://github.com/luongnv89/dropcast) in a sidecar
virtualenv, kept apart from Hermes's own environment. It needs
[uv](https://docs.astral.sh/uv/) and SSH access to the DropCast repo.

```sh
hermes onair setup           # create the sidecar and install the pinned DropCast
hermes onair setup --check   # report the sidecar's state; exit 0 when ready
```

- The sidecar lives in `$HERMES_HOME/plugin-data/onair/sidecar`.
- DropCast is installed over `git+ssh` at the commit pinned in `adapter.py`
  (`DROPCAST_PIN`). Setup is safe to run again: a sidecar already at the pin is
  left alone, and one at another commit is reinstalled.
- When uv is missing or the repository cannot be fetched, setup says so and
  exits non-zero.
- Setup only writes to an absent path or its own sidecar: a real directory
  (not a symlink) directly inside the data dir, made by uv (`uv = …` in
  `pyvenv.cfg`), where uv installed DropCast from the DropCast repository at
  the pinned commit (PEP 610 `direct_url.json`). It also needs the
  `.onair-sidecar` marker setup writes right after `uv venv`. A marker alone
  is never enough. Only such a venv is ever recreated (`uv venv --clear`).
  Anything else is left untouched, and setup asks you to move it aside.
  Setup never deletes anything itself.
- A sidecar made by v0.1.0 has no marker. Setup adopts it, by writing the
  marker, when it meets every rule above and has a Python. `--check` never
  writes; it reports such a sidecar as adoptable.
- Move the sidecar aside (`mv …/sidecar …/sidecar.old`) and run setup again
  when setup refuses it: after an install that never finished (for example,
  the repository was unreachable), or after a pin change, since the old
  sidecar no longer matches the pin.

## Free voices: `tts.provider: onair`

```yaml
# config.yaml
tts:
  provider: onair
```

Hermes's `text_to_speech` then speaks through On Air. Each request is spoken by
exactly one engine, the first in its language's chain that works:

| Language | Chain |
|----------|-------|
| English | Fish (`fish-audio/s2.1-pro-free:free`) → HeyGen (`heygen/heygen-voice-1-turbo:free`) → edge-tts |
| Any other | HeyGen → Fish → edge-tts |

- **Free only.** Before an OpenRouter model is called, On Air reads the public
  speech model list (no key sent), cached for up to 24 h in
  `$HERMES_HOME/plugin-data/onair/openrouter-speech-models.json`. A model is
  called only when it is listed exactly once, no listed price is non-zero,
  and its id ends in `:free` or every listed price is zero. Without a list or
  a fresh cache, OpenRouter is skipped.
- **The key** comes only from the `OPENROUTER_API_KEY` environment variable.
  Without it, On Air goes straight to edge-tts.
- **Fall-through.** An engine is skipped on a missing key, a refused or failed
  price check, any HTTP error (401, 402, 408, 429, 5xx, …), a network error or
  timeout, an empty or invalid audio body, or a non-MP3 format with no ffmpeg.
  The audio reaches the output path through a temp file and a rename; if it
  cannot be written (disk full, no permission), the request stops there
  instead of trying the next engine.
- **HeyGen** answers raw PCM, which ffmpeg converts. Without ffmpeg, HeyGen is
  skipped.
- **edge-tts** renders in the sidecar (run `hermes onair setup` first). Its
  audio is for private listening only: never publish it.
- `tts.output_format` picks the file format (`mp3` by default; `wav`, `ogg`,
  `opus` and `flac` need ffmpeg, and fail without it: MP3 is never written
  under another format's name). `tts.voice: <engine>:<voice>` (for example
  `edge:en-GB-RyanNeural`) sets one engine's voice.
- **Voices**, first match wins: `tts.voice` for its engine, then the `voices`
  setting, then the profile's persona (below), then the built-in defaults.

## A voice per profile: `/onair voice`

Each profile speaks as its own **persona**: a named character with one voice
per engine and, for edge-tts, per language. A profile claims one the first
time it speaks (or runs `/onair voice`): the probe starts at
`sha256(profile) % 10` and skips personas other profiles of the same Hermes
root hold, so up to 10 profiles never share a voice. The claim is stored in
that profile's `plugin-data/onair/persona.json` and survives restarts.

```text
/onair voice                       this profile's persona and voices
/onair voice list                  the pool, and which profile holds each persona
/onair voice set <name> [--force]  change it; --force shares a persona another
                                   profile holds (only this profile's claim changes)
```

| Persona | Fish | HeyGen | edge en | edge fr | edge vi |
|---------|------|--------|---------|---------|---------|
| bob (man) | Ethan | lucian | en-US-AndrewNeural | fr-FR-HenriNeural | vi-VN-NamMinhNeural |
| alice (woman) | Hannah | tabitha | en-US-AvaNeural | fr-FR-DeniseNeural | vi-VN-HoaiMyNeural |
| maya (woman) | Laura | meredith | en-GB-SoniaNeural | fr-CH-ArianeNeural | pt-BR-ThalitaMultilingualNeural |
| sam (man) | Adrian | gareth | en-US-GuyNeural | fr-BE-GerardNeural | en-US-BrianMultilingualNeural |
| lena (woman) | Sarah | noelle | en-US-EmmaNeural | fr-CA-SylvieNeural | de-DE-SeraphinaMultilingualNeural |
| theo (man) | Jordan | vincent | en-GB-RyanNeural | fr-CA-ThierryNeural | en-AU-WilliamMultilingualNeural |
| nadine (woman) | Selene | nadine | en-US-AriaNeural | fr-FR-VivienneMultilingualNeural | fr-FR-VivienneMultilingualNeural |
| bram (man) | — | bram | en-US-ChristopherNeural | fr-FR-RemyMultilingualNeural | fr-FR-RemyMultilingualNeural |
| sabrina (woman) | — | sabrina | en-US-JennyNeural | fr-FR-EloiseNeural | en-US-AvaMultilingualNeural |
| weston (man) | — | weston | en-US-EricNeural | fr-CA-AntoineNeural | de-DE-FlorianMultilingualNeural |

- **HeyGen**: the 10 voices `heygen/heygen-voice-1-turbo:free` lists in
  OpenRouter's public model list; one per persona.
- **Fish**: the 7 Fish Official voices DropCast records with their terms
  (personal, non-commercial use). There are no more, so bram, sabrina and
  weston have none: Fish is skipped for them and English goes to HeyGen in
  their own voice. A `voices` setting or `tts.voice: fish:<id>` brings Fish
  back for such a profile.
- **edge-tts** has only 2 native Vietnamese voices; the other personas speak
  Vietnamese with Microsoft multilingual voices. Languages other than en, fr
  and vi use the defaults.
- Every voice is a stock voice of a free engine; nothing is cloned.
- **Sharing.** Claims are found by reading the other profiles' claim files
  (`<root>/plugin-data/onair/persona.json` and
  `<root>/profiles/*/plugin-data/onair/persona.json`); no other profile's file
  is ever written. Two profiles claiming at once are serialized by an `flock`
  on the root directory (no lock file).
- **The profile** is resolved on every call from the active Hermes home (the
  same home as the plugin data): `<root>/profiles/<name>` is `<name>`;
  otherwise `ctx.profile_name`, and, when that says `custom`, the home
  directory's name.
- A claim file names its profile. A profile cloned with
  `hermes profile create --clone-all` (which copies `plugin-data`) claims its
  own persona the first time it speaks; a renamed profile keeps its persona
  while no other profile holds it.
- An 11th profile shares the least-used persona.

Settings (`plugins.entries.onair.settings` in `config.yaml`):

| Key | Default | Meaning |
|-----|---------|---------|
| `uv_bin` | `""` | Path or name of uv; empty looks it up on `PATH`. |
| `sidecar_python` | `">=3.11"` | Python request for `uv venv --python`. |
| `ffmpeg_bin` | `""` | A path (with `/` or a leading `~`) or a name looked up on `PATH`; empty looks on `PATH`, then for the newest `ffmpeg-*/bin/ffmpeg` in `$HERMES_HOME/tools`, then `~/.hermes/tools`. |
| `language` | `auto` | Language of the text (`en`, `fr`, `vi`, …); `auto` detects en/fr/vi. |
| `voices` | `{}` | Voice per engine and language, e.g. `{edge: {fr: fr-FR-HenriNeural}, heygen: {"*": tabitha}}`. |

## Develop

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
pytest -q            # offline: network, DNS and new processes are blocked
pytest -m live       # by hand only: tests that reach real services
```

The live smoke test speaks one English sentence through Fish and HeyGen (after
the price gate). It reads the key from the environment only:

```sh
OPENROUTER_API_KEY=... pytest -m live tests/test_live.py -v -s
# every Fish and HeyGen voice of the persona pool (7 + 10 free requests)
OPENROUTER_API_KEY=... pytest -m live tests/test_live.py -v -s -k persona
```

- Tests marked `real_uv` may start only the `uv` on `PATH`, through
  `subprocess`, with `env=real_uv_env` (offline). They are skipped when uv is
  absent.
- Read `AGENTS.md` first: its hard rules (free engines only, the key only from
  the environment, edge-tts output for private use, DropCast only through
  `adapter.py`, an offline test run) override everything else.
- Try the plugin in an isolated `HERMES_HOME` outside `~/.hermes`, for example
  `HERMES_HOME=/tmp/onair-home`. Homes inside `~/.hermes` share the real
  install, and `hermes plugins validate --install-deps` would rebuild it.
- Before a PR, also run `hermes plugins validate .` and
  `hermes plugins doctor . --ci` in that isolated home.
- CI runs ruff and the offline tests on Python 3.11 and 3.13
  (`.github/workflows/ci.yml`).
