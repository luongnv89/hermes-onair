# On Air: 90-second demo script

The pitch in one line: **every Hermes agent gets a free voice and a show.**
The demo shows three things that a plain speech provider can't do. A link
becomes a two-host episode in a chat. Each profile has its own voice. The
agents hold a standup, each speaking in its own voice.

## Ground rules

- **Record on the owner's real Hermes, with the free OpenRouter voices.**
  Every audio clip in the video must be Fish S2.1 Pro or HeyGen voice 1
  turbo, from OpenRouter's free models.
- **edge-tts must not appear anywhere: not heard, not shown, not named in a
  reply on screen.** Its output is for private use only, and a demo is a
  publication (AGENTS.md rule 3). Before using a take, read the reply:
  - it must say `Voices: Fish (OpenRouter, free)` or
    `Voices: HeyGen (OpenRouter, free)`;
  - it must **not** contain `Private listening only (edge-tts voices)`;
  - its `Skipped:` part must not mention edge-tts.

  If any check fails, drop the take and record it again. Do not edit the
  text out.
- **The script must come from the LLM:** the reply must say
  `Script: written by your model (…)`. A free model may answer HTTP 429.
  Then the reply says `built-in template`, and that take is retaken.
- **Fish's terms.** DropCast records Fish's official voices as for
  personal, non-commercial use. The owner confirms that a hackathon demo
  fits those terms before publishing a Fish take. If it doesn't, use only
  takes whose reply says `Voices: HeyGen (OpenRouter, free)`.
- **No real session titles.** The standup speaks the titles of each
  profile's sessions from the last 24 hours. Before shot 9, check every
  title in those profiles, or record after a day of sessions made only for
  the demo. The scrubber removes secrets, paths, links and emails, but not
  project, client or people's names.
- The Desktop player was cut from v0.1.0, so it has no shot.
- There's no voice-over. The episodes speak for themselves, and the captions
  are on-screen text.

## Before recording (off camera)

```sh
hermes plugins install git@github.com:luongnv89/hermes-onair.git --enable
hermes onair setup --check                  # Result: OK
hermes config set tts.provider onair
# OPENROUTER_API_KEY is in the gateway's environment (never shown on screen)
hermes gateway restart
```

In `config.yaml`, under `plugins.entries.onair.settings`:

```yaml
standup_profiles: [default, coder, research]
```

- **Three profiles:** `default`, `coder` and `research`. Each one is
  connected to its own Telegram bot, or reachable in the same chat. Each has
  a few demo-safe sessions from the last 24 hours, so the standup has
  something to report (see the ground rules).
- **Personas:** run `/onair voice` once in each profile, so that each claims
  its persona. Check that the three personas all have a Fish voice (the
  first 7 claims always do). If not, use `/onair voice set <name>`.
- **The article:** pick a short, public, English article that a script can
  quote, for example `https://science.nasa.gov/moon/facts/`.
- **Rehearse each shot once.** Free models can be rate-limited, so leave
  time for retakes.
- **Hide the secrets.** No key, token, `.env` or `config.yaml` secret may
  be on screen. Telegram shows only the chat.

## Shot list (≤ 90 s)

| # | Time | Screen | Exact input | Expected on screen |
|---|------|--------|-------------|--------------------|
| 1 | 0:00–0:05 | The catalog card (`assets/catalog-card.png`) | — | "ON AIR · Every agent gets a free voice and a show." |
| 2 | 0:05–0:12 | Telegram, the `default` profile's chat | `/onair https://science.nasa.gov/moon/facts/` | The bot answers at once: "🎙️ On Air is recording your episode… (about a minute)". |
| 3 | 0:12–0:16 | Same chat, a jump cut over the wait | — | The note now reads "🎙️ On Air: recorded in N s." Caption: "about a minute later". |
| 4 | 0:16–0:35 | Same chat: the reply, then play the audio attachment (≈15 s) | Tap play | `On Air: "<title>" (… s, en). Hosts <A> & <B> (voice personas), profile default.` / `Voices: Fish (OpenRouter, free): …` / `Script: written by your model (<model id>).` and an audio attachment. Two different hosts talk about the Moon, quoting the page. Caption: "Free voices · your own model writes the script". |
| 5 | 0:35–0:43 | Telegram, the `default` chat | `/onair voice` | `Profile default speaks as <persona> (…)` and `Fish: <voice> (…), every language`. Crop to these two lines, so no edge-tts voice name is shown. |
| 6 | 0:43–0:52 | Telegram, the `coder` chat | `/onair voice` | `Profile coder speaks as <another persona> (…)` and its `Fish:` line, a different name and Fish voice. Crop to these two lines, as in shot 5. Caption: "Each profile, its own voice". |
| 7 | 0:52–0:58 | The `coder` chat | `/onair voice list` | The pool of 10, with `default`, `coder` and `research` each holding a different persona. Blur the `edge en <voice>` part of each row (it sits before the `->` holder column), so no edge-tts voice name is shown and the holders stay visible. |
| 8 | 0:58–1:02 | The `default` chat | `/onair standup` | "🎙️ On Air is recording the standup… (about a minute)". |
| 9 | 1:02–1:22 | Same chat, a jump cut, then play the attachment (≈18 s) | Tap play | `On Air standup: 3 profiles (… s, en). default as <A>, coder as <B>, research as <C>.` / `Voices: Fish (OpenRouter, free): …` / `Script: written by your model (…)`. Audio: "This is the On Air standup for <weekday, day month>: three agents report.", then three agents in three voices, each telling its own day. Caption: "Agent Standup: 3 agents, 3 voices". |
| 10 | 1:22–1:30 | The catalog card again, with the install line | — | Closing caption: **"On Air. Every Hermes agent gets a free voice and a show."** Below it: `hermes plugins install luongnv89/hermes-onair --enable`. |

**Total: 90 s.** Shots 4 and 9 carry the audio. If the cut runs long, trim
those clips, never the closing line.

## Fallbacks during the shoot

| Problem | Do this |
|---------|---------|
| The reply says `built-in template` (HTTP 429) | Wait a minute and run the same command again. Use only a take where the model wrote the script. |
| The reply mentions edge-tts, or Fish and HeyGen were skipped | Stop. Check `OPENROUTER_API_KEY` in the gateway's environment and that the free models are still listed, then retake. Never use that take. |
| A standup segment says "a quiet day" | That profile had no sessions today. Run a short session in it, then record again. |
| A recording takes over 90 s | Keep the jump cut. The on-screen "recorded in N s" stays honest. |
