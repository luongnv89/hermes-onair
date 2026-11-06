# hermes-onair
On Air: free voices and two-host audio shows for Hermes Agent (Hermes plugin)

> Status: early (v0.1.0). This release ships `hermes onair setup` and the free
> voice router (`tts.provider: onair`); personas and `/onair` come next.

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
  called only when it is listed, no listed price is non-zero, and its id ends
  in `:free` or every listed price is zero. Without a list or a fresh cache,
  OpenRouter is skipped.
- **The key** comes only from the `OPENROUTER_API_KEY` environment variable.
  Without it, On Air goes straight to edge-tts.
- **Fall-through.** An engine is skipped on a missing key, a refused or failed
  price check, any HTTP error (401, 402, 408, 429, 5xx, …), a network error or
  timeout, or an empty or invalid audio body.
- **HeyGen** answers raw PCM, which ffmpeg converts. Without ffmpeg, HeyGen is
  skipped.
- **edge-tts** renders in the sidecar (run `hermes onair setup` first). Its
  audio is for private listening only: never publish it.
- `tts.output_format` picks the file format (`mp3` by default; `wav`, `ogg`,
  `opus` and `flac` need ffmpeg). `tts.voice: <engine>:<voice>` (for example
  `edge:en-GB-RyanNeural`) sets one engine's voice.

Settings (`plugins.entries.onair.settings` in `config.yaml`):

| Key | Default | Meaning |
|-----|---------|---------|
| `uv_bin` | `""` | Path or name of uv; empty looks it up on `PATH`. |
| `sidecar_python` | `">=3.11"` | Python request for `uv venv --python`. |
| `ffmpeg_bin` | `""` | Path or name of ffmpeg; empty looks on `PATH`, then in `~/.hermes/tools/ffmpeg-*/bin/`. |
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
