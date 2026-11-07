# On Air: build plan (Hermes plugin, Nous hackathon; submission due Nov 11 2026, 17:00 EST)

On Air gives every Hermes agent a free voice and a show:
1. a free voice router as a `tts.provider`;
2. a distinct voice for each profile;
3. `/onair` (plus an `onair_episode` agent tool) that turns a link or text into a two-host episode;
4. an "Agent Standup" show in which each bot reports in its own voice.

It is built on DropCast (`luongnv89/dropcast`, pinned to commit `821715c`). DropCast is called through its CLI JSON contract, which its owner (Echo) keeps compatible:
- `dropcast run INPUT --adapter headless --headless --root DIR` …, then `dropcast resume JOB`. Statuses: `needs_agent` | `needs_approval` | `rendering` | `delivered` | `stopped` | `failed`. At `needs_agent` the agent writes `script.json` and `summary.txt`, then resumes.
- Single steps: `python -m dropcast.ingest`, `python -m dropcast.engines select`, and `python -m dropcast.openrouter|edgetts render`.
- Python calls (`dropcast.run.start/resume`, `ingest.ingest`, `engines.select_engine`, `openrouter/edgetts.render_script`, `voices.resolve`) are best-effort and NOT declared public. Use them only through one adapter module.

Evidence: a feasibility spike ran on 2026-11-05 and is at `/home/adminuser/.hermes/profiles/forge/data/spikes/onair/spikes/` (`SUMMARY.md` and the per-spike READMEs). Measured results:
- OpenRouter has exactly 2 free speech models:
  - `fish-audio/s2.1-pro-free:free`: MP3, about 2–3 s per sentence, works for EN/FR/VI;
  - `heygen/heygen-voice-1-turbo:free`: headerless PCM at 44.1 kHz 16-bit mono, about 0.7–1.2 s, EN/FR/VI.
- edge-tts: about 1 s warm, 11 s on a cold first call. Its output is for private use only. Delivering it to the requesting user's own chat or session is fine. Never publish it: no public post, plugin catalog entry, demo, or release asset.
- `tts.provider: onair` through `ctx.register_tts_provider` works with Hermes's real `text_to_speech_tool`.
- `ctx.llm` resolves to a free model and returned HTTP 429 on 5 of 6 calls, and it ignores length targets.
- A slash command's `MEDIA:` reply works on the gateway. Desktop tool media renderers only claim AGENT-TOOL output.
- Hermes passes `synthesize()` a temporary output path and renames it afterwards.
- `ctx.profile_name` is "custom" outside `<root>/profiles/<name>`.

## Phase 1: foundation

### Task 1.1: Plugin scaffold, DropCast sidecar and CI
- Package layout: `plugin.yaml` (name `onair`; `python_runtime: external`; `config_schema`), `__init__.py` with `register(ctx)`, a stdlib-only host side, `tests/` (pytest, no network: block non-loopback sockets as DropCast's conftest does).
- `hermes onair setup` (a CLI command via `ctx.register_cli_command`) creates a sidecar venv under the plugin's `plugin_data_dir()` with uv and installs DropCast from `git+ssh://git@github.com/luongnv89/dropcast@821715c`. It is idempotent and prints a clear error when uv or the repo is unavailable.
- `adapter.py` is the ONLY module that knows DropCast, through the CLI JSON contract.
- CI is a GitHub Actions workflow on ubuntu-latest: ruff and pytest, plus `hermes plugins validate` only when that can run without the real install. Free minutes only.
- Acceptance:
  - `hermes plugins validate PATH` and `hermes plugins doctor PATH --ci` pass;
  - the tests pass offline;
  - setup works in an isolated `HERMES_HOME` OUTSIDE `~/.hermes` (the spike found that homes inside `~/.hermes` touch the real install).

### Task 1.2: Free voice router (`tts.provider: onair`)
Depends on: Task 1.1
- `OnAirTTS(TTSProvider)` with an engine chain per language:
  - EN: Fish free → HeyGen free → edge-tts;
  - other languages: HeyGen free → Fish free → edge-tts.
- Price gate: call an OpenRouter model only if it appears in the public model list (a copy up to 24 h old is fine) and either its id ends in `:free` or every listed price is zero. A non-zero listed price means paid, whatever the id. If there is no list and no cache, or the check fails, skip OpenRouter.
- Fall through to the next engine on 401, 402, 408, 429, 5xx, network errors, a missing key, or a failed price check. Never mix engines within one request.
- Convert HeyGen PCM to WAV/MP3 with ffmpeg. Write to the path Hermes gives (a temp path that gets renamed): no sidecar files keyed by that path.
- The key comes only from the `OPENROUTER_API_KEY` env var. Never log, print or store it.
- Acceptance:
  - tests with a fake HTTP layer for every fallback case, including "the model became paid";
  - a live smoke test marked `live`, off by default.

### Task 1.3: A distinct voice per profile
Depends on: Task 1.2
- Each profile gets a stable persona (a voice id per engine and language) from a pool. It is claimed on first use and stored in that profile's plugin data, so up to the pool size no two profiles share one (a plain hash collided on 3 of 8 real profile names).
- Resolve the profile per call (`ctx.profile_name`, falling back to the home directory name).
- `/onair voice` shows the profile's voice, `/onair voice list` lists the pool, and `/onair voice set <id>` changes it.
- Acceptance: on Luong's 9 profile names the personas are distinct and stable across restarts (tested).

## Phase 2: shows

### Task 2.1: `/onair` command and `onair_episode` tool
Depends on: Task 1.2, Task 1.3
- One shared pipeline:
  1. ingest the link or text (via DropCast);
  2. write the script with `ctx.llm`: one retry with backoff, a hard word cap of about 150 words per minute × the target length (default 1–2 min), and a template fallback when the LLM fails;
  3. finalize the script through DropCast's checks;
  4. render with the router's free engines;
  5. deliver the MP3 plus a WebVTT transcript.
- The command replies at once ("recording…") and delivers the episode when it's done, as a final reply ending in a `MEDIA:/abs/path.mp3` line. Check how a plugin command can post later (`ctx.post_message` handle edit, or `inject_message`) and document the choice.
- `onair_episode` is an agent tool with the same pipeline, so the agent can make an episode on request and Desktop can render it.
- Hosts are voiced by two personas. The script follows DropCast's `podcast-2host` format.
- Acceptance:
  - EN, FR and VI episodes render for $0 in under 90 s for a 1-minute episode on this box;
  - a failed LLM still yields an episode (template), with that said in the reply.

### Task 2.2: Agent Standup show
Depends on: Task 2.1
- `/onair standup [profiles…]` builds a short show in which each opted-in profile reports its last 24 h in ITS OWN voice persona, joined with DropCast's finish step (theme, loudness).
- Sources: each opted-in profile's recent session titles and summaries from its own `state.db`, read-only. Opt-in comes from a config list; no profile is read without being listed. A disclosure goes in `plugin.yaml`'s description.
- Optional cron blueprint (`ctx.register_automation_blueprint`, if available) for a morning standup delivered to a chat.
- Acceptance:
  - a 3-profile standup renders with 3 distinct voices;
  - a profile that isn't listed is never read (tested).

### Task 2.3: Desktop player (stretch; only if Phase 1 and Task 2.1 are done)
Depends on: Task 2.1
- `desktop/plugin.js`: a tool media renderer for `onair_episode` output, with `<audio>` and live VTT cues (current speaker and line), and a status-bar chip while an episode is recording.
- Acceptance: it loads in Hermes Desktop and `hermes plugins doctor` reports no stale-copy warning. Luong checks it manually on his machine, because this box has no Desktop.

## Phase 3: submission

### Task 3.1: Docs, catalog entry draft and demo script
Depends on: Task 2.1
- README: install, setup and config; disclosure; privacy; and a "Free means" section covering:
  - the free OpenRouter models are promotions that may end;
  - edge-tts is unofficial, and its output is for private use only. Delivering it to the requesting user's own chat or session is fine. Never publish it: no public post, plugin catalog entry, demo, or release asset;
  - the engines and the fallback order.
- A catalog card image, and a draft `plugin-catalog/onair.yaml` matching `~/.hermes/hermes-agent/plugin-catalog/` entries (name, repo, sha, description with disclosure, category voice, capabilities). Do NOT open the catalog PR.
- `docs/demo.md`: a 90 s demo script with shots: link → episode on Telegram; Agent Standup; Desktop player if done.
- Acceptance: a fresh install from the README works in an isolated `HERMES_HOME`.
