# hermes-onair
On Air: free voices and two-host audio shows for Hermes Agent (Hermes plugin)

> Status: early (v0.1.0). This release ships `hermes onair setup`, the free
> voice router (`tts.provider: onair`), a distinct voice per profile
> (`/onair voice`), two-host episodes (`/onair <link or text>` and the
> `onair_episode` agent tool) and the Agent Standup show (`/onair standup`
> and the `onair_standup` tool).

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
time it speaks (or runs `/onair voice`): the probe walks the pool from
`sha256(profile) % 10`, skips personas other profiles of the same Hermes root
hold, and takes a free persona with a Fish voice before one without, so up to
10 profiles never share a voice and, with 7 Fish voices, the first 7 all get
one. The claim is stored in that profile's `plugin-data/onair/persona.json`
and survives restarts.

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
  weston have none: Fish is skipped for them, and the next engine in the
  chain speaks in their own voice. A `voices` setting or `tts.voice:
  fish:<id>` brings Fish back for such a profile.
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
| `voices` | `{}` | Voice per engine and language, e.g. `{edge: {fr: fr-FR-HenriNeural}, heygen: {"*": tabitha}}`. Speech only: an episode's two hosts keep their personas' voices. |
| `episode_minutes` | `1.0` | Target length of an `/onair` episode (1 to 5 minutes) when the request names none. |
| `keep_episodes` | `30` | Episode folders kept under the plugin data dir; older ones are removed after each new episode (and, separately, each new standup). `0` keeps all. |
| `standup_profiles` | `[]` | Profiles the Agent Standup may read, e.g. `[default, coder, research]` (`default` is the root profile). Nothing is read for a profile that is not listed. |

## Episodes: `/onair <link or text>` and `onair_episode`

```text
/onair https://science.nasa.gov/moon/facts/
/onair --lang fr --minutes 2 <pasted text>
```

On Air replies at once ("recording…"), then about a minute later with the
episode: a two-host MP3 (DropCast's `podcast-2host` show), its WebVTT
transcript, the voices and timings, and a last line `MEDIA:/abs/path.mp3`
that the gateway sends as an audio attachment. The agent tool
`onair_episode` (`source`, `language`, `minutes`) runs the same pipeline and
returns JSON with the `MEDIA:` path, the transcript, the engine, the model
id and the timings.

1. **Ingest**: DropCast reads the link (http/https only; a local path is
   treated as text) or the text, and briefs the episode: the target length
   (`--minutes`, else the `episode_minutes` setting, default 1), the language
   (`--lang`, else the `language` setting, else detected from the source:
   en, fr and vi are supported), no guests.
2. **Script**: written by **your own configured model** through `ctx.llm`, so
   its cost follows your model choice; On Air passes no model override and
   records the model id the call returns. One retry with backoff (a
   Retry-After of up to 10 s is honoured); a hard cap of about 150 spoken
   words per minute; lines must quote the source, and a line with a link,
   web address, email or phone number is dropped. The source is passed as
   fenced data the model is told never to follow. The answer is read
   leniently (a fenced block, prose around it, a `<think>` block, a trailing
   comma); when none can be read, the reply says what came back. When
   the model fails (free models often answer HTTP 429), a built-in template
   built from sentences of the source is used instead (never a sentence
   with a link, web address, email or phone number), and the reply says so.
   The call runs through On Air's own auxiliary task, `onair_script` (see
   below), with reasoning off and a 40 s timeout per attempt.
3. **Checks**: DropCast's own script checks (show order, fixed lines,
   fact-check quotes, length). An LLM script that fails them is replaced by
   the template.
4. **Render**: one engine for the whole episode, never mixed: Fish (English
   first) or HeyGen, OpenRouter's free models behind the price gate, when
   `OPENROUTER_API_KEY` is set; otherwise edge-tts. Two hosts, two personas:
   your profile's persona hosts in its own name and voice, with the next
   persona of the other voice type as co-host. The show itself keeps
   DropCast's fixed name, opening and sign-off ("Build Wiz AI Show"): its
   script checks accept no other.
5. **Deliver**: `episode.mp3` and `transcript.vtt` under
   `$HERMES_HOME/plugin-data/onair/episodes/<stamp>/jobs/<job>/`, with
   `onair.json` (engine, voices, script source, model id, timings). The
   newest `keep_episodes` (30) episode folders are kept; older ones are removed.

Episodes voiced by edge-tts are for your private listening only: the reply
says so. Never publish them.

**The `onair_script` task: reasoning off.** On Air registers an auxiliary
LLM task, `onair_script` ("On Air episode script"), and makes the script call
through it. Its only default is `reasoning_effort: none`. It names no
provider or model, so the call still runs on your main model, exactly as
without it (no override). The setting stops a reasoning model from spending
the answer's token budget (4096) on thinking: free reasoning models otherwise
came back with an empty answer. Hermes turns it into `reasoning: {enabled:
false}` for the request (`thinking: disabled` on Claude). To change it, add an
`auxiliary.onair_script` block to `config.yaml`; your values override the
plugin's defaults:

```yaml
# config.yaml
auxiliary:
  onair_script:
    reasoning_effort: low   # or none (the default), medium, high
```

On a Hermes without `ctx.register_auxiliary_task`, On Air calls `ctx.llm`
without a task (your main model, its own reasoning default) and logs it once.

**How the reply travels.** The "recording…" note goes through
`ctx.post_message` where a session is bound (gateway chats, TUI/Desktop) and
is edited to "recorded in N s" at the end; on the classic CLI, where nothing
is bound, it is printed. The episode itself is the command's return value,
because only a command's reply carries `MEDIA:` attachments (`post_message`
sends text only, and `inject_message` queues a user turn for the agent).
The handler is synchronous, so the gateway runs it on its worker pool and
its event loop is never blocked.


## The Agent Standup: `/onair standup`

```text
/onair standup                 # every profile in standup_profiles
/onair standup coder research  # some of them (each must be listed)
```

Each listed profile reports its last 24 hours in a short segment (2 to 4
sentences) spoken in **its own persona's voice**, and DropCast's finish step
joins the segments with the show's theme and loudness. The reply follows
`/onair`: "recording…" at once, then the voices, timings and a last line
`MEDIA:/abs/path.mp3`, with `transcript.vtt` beside it under
`$HERMES_HOME/plugin-data/onair/standups/<stamp>/`. The agent tool
`onair_standup` (`profiles`, `scheduled`) runs the same show.

**Privacy: what is read, and what is not.**

- **Opt-in only.** Only profiles listed in `standup_profiles` are read. A
  profile named on the command line that is not listed is refused before
  anything is opened; so is every profile when the list is empty.
- **Read-only.** A profile's history is its own `state.db`
  (`$HERMES_ROOT/state.db` for `default`, `$HERMES_ROOT/profiles/<name>/state.db`
  otherwise), opened with SQLite's `mode=ro` URI (plus `immutable=1` for an
  idle WAL database, so SQLite creates no side files there) and
  `PRAGMA query_only`: it is never written, write-locked, or given new files. A `state.db`, a profile folder or a
  `profiles` folder that is a symbolic link is never followed.
- **Titles only.** Only the titles (and a summary, where the table has one)
  of sessions active in the last 24 hours are read: never a message, a
  prompt or a tool output.
- **Secrets scrubbed.** Anything that looks like a secret (API keys and
  tokens of the usual shapes, `password=…`-style values, private keys, URLs
  with a password, long hex or base64 runs) is replaced by "redacted" before
  the titles reach your model, the voices or any file, and again after the
  model writes the script. File paths, links and emails are dropped from
  titles too.
- The titles reach your model as fenced data it is told never to follow.

**Voices.** Each profile speaks as the persona it claimed (its own
`persona.json`, read only). A profile with no claim, or whose claim an
earlier segment already speaks with, gets a fixed persona for this show
only (picked from its name; nothing is written), so every voice is
distinct. One engine for the whole show, as for episodes: Fish or HeyGen
with `OPENROUTER_API_KEY`, else edge-tts; a failed segment drops that engine
and the next one renders the whole show again. The `voices` setting is not
applied (it would give every profile the same voice).

**Script.** One `ctx.llm` call on your own model (the `onair_script` task,
reasoning off) writes every segment; when it fails, a template says
"<Persona> for <profile>. <Profile> worked on: <titles>." A profile with no
session in the last 24 hours gets a one-line "quiet day" segment rather than
being skipped, so the show accounts for every listed profile; one whose
`state.db` is missing or unreadable gets a one-line "could not be read"
segment. The template and these lines are English.

**The morning standup blueprint.** On a Hermes with
`ctx.register_automation_blueprint`, On Air adds the `onair:standup`
Automation Blueprint (`/blueprint`, the dashboard gallery): weekdays at a
time you choose, delivered where you choose. A scheduled standup has no
requesting user, so it **never uses edge-tts**, whatever the target: only
OpenRouter's free voices (it needs `OPENROUTER_API_KEY`; without it the run
says why and sends no audio). The tool also detects a cron run itself.

Standups voiced by edge-tts are for your private listening only: the reply
says so. Never publish them.

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
