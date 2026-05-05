# ElevenLabs Realtime STT — research notes for structured-data capture

**Pipecat version:** `pipecat-ai==0.0.100`
**ElevenLabs model:** `scribe_v2_realtime` (Scribe v2 Realtime, ~150ms latency, 90+ languages)
**Sources verified:**
- ElevenLabs Realtime API: https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime
- Scribe v2 Realtime overview: https://elevenlabs.io/realtime-speech-to-text
- Pipecat ElevenLabs STT service: https://docs.pipecat.ai/server/services/stt/elevenlabs

> Headline: ElevenLabs Realtime STT is fast and supports keyterm biasing, but **the API does NOT specify whether numbers/dates come back as digits or words, and there is no smart-formatting flag** in the documented params. Our DOB / phone capture has to assume worst case (words) and normalize on the LLM side or via post-processing.

## 1. The Pipecat wrapper

`pipecat.services.elevenlabs.stt.ElevenLabsRealtimeSTTService` constructor:

| Param | Type | Default | Notes |
|---|---|---|---|
| `api_key` | str | required | |
| `base_url` | str | `api.elevenlabs.io` | |
| `model` | str | `scribe_v2_realtime` | **deprecated** — use `settings.model` |
| `sample_rate` | int | None | inherits pipeline rate |
| `settings` | `Settings` | None | runtime config (see below) |
| `commit_strategy` | `CommitStrategy` | `MANUAL` | `MANUAL` or `VAD` |
| `include_timestamps` | bool | False | per-word timing |
| `enable_logging` | bool | False | server-side logs at ElevenLabs |
| `include_language_detection` | bool | False | |
| `params` | `InputParams` | None | **deprecated** as of pipecat v0.0.105 — use `Settings` |
| `ttfs_p99_latency` | float | env-default | latency override |

`Settings` fields:

| Param | Type | Range / Default | What it does |
|---|---|---|---|
| `model` | str | None | transcription model id |
| `language` | `Language \| str` | `Language.EN` (HTTP) / None (realtime, auto-detect) | |
| `vad_silence_threshold_secs` | float | 0.3–3.0, default 1.5 | how long a silence before auto-commit |
| `vad_threshold` | float | 0.1–0.9, default 0.4 | VAD sensitivity |
| `min_speech_duration_ms` | int | 50–2000 | reject blips |
| `min_silence_duration_ms` | int | 50–2000 | reject brief pauses |

> **What our `bot.py` does today:** `ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)` — nothing else. So we get: `commit_strategy=MANUAL`, language auto-detect, no timestamps, default VAD thresholds. **For our use case we should at least set `language=Language.EN` to skip detection latency.**

## 2. Two commit strategies — important

- **`MANUAL`** (Pipecat default): the **client** (Pipecat) decides when an utterance is "done" and explicitly commits. Pipecat uses Silero VAD or the Smart Turn Analyzer (we have both) to gate this. Better when you have your own end-of-utterance logic — which we do.
- **`VAD`**: the **server** decides via its own VAD using `vad_silence_threshold_secs`. Simpler but ElevenLabs' VAD doesn't know about our Smart Turn analyzer.

**Recommendation:** keep `MANUAL` — our Smart Turn + Silero combo (in `bot.py`) is more sophisticated than the server-side VAD knobs.

In `MANUAL` mode, transcription frames are marked `TranscriptionFrame.finalized=True` when committed.

## 3. Number / date / email handling — the actual answer

The ElevenLabs realtime API docs (the source-of-truth page we fetched) **do not document any "smart formatting" flag**. The documented connection params are:

```
model_id, token, language_code, audio_format, commit_strategy,
include_timestamps, include_language_detection, keyterms,
no_verbatim, vad_silence_threshold_secs, vad_threshold,
min_speech_duration_ms, min_silence_duration_ms, enable_logging
```

There is no `smart_format`, `format_numbers`, or equivalent. The committed transcript output schema (`committed_transcript` / `committed_transcript_with_timestamps`) returns `text: string` and an optional `words[]` array — no structured number/date fields.

**Practical implication:** assume the model returns whatever Whisper-style transcription returns — typically a mix (cardinal numbers like "1992" but spelled-out shorter ones like "five"). We can't rely on a clean format. We must:
1. **Normalize on the LLM side** — make the schema parameter type explicit (e.g., `birthday: "YYYY-MM-DD format, convert from spoken"`) and let GPT-4 do the normalization. The patient_intake.py example does exactly this:
   > `"description": "The user's birthdate (convert to YYYY-MM-DD format)"`
2. **Confirm-back-to-user**: after capturing, have the bot read it back ("So that's January first, 1983, correct?"). This is what the Flows `verify` node pattern is for.

This is the single biggest reason to do the LLM normalization step rather than regex on the transcript.

## 4. Keyterms — vocabulary biasing

> "List of keyterms the model is biased towards."

From the realtime-overview page: up to **50 keyterms, 20 chars each**. Useful for:
- Clinic name (avoid "prosper" → "prospect")
- Common medication / condition names if we extend the EHR
- Provider names

Not exposed directly on `ElevenLabsRealtimeSTTService` constructor in the params we saw — would need to set via the underlying API or check if Settings has a `keyterms` field (the docs we fetched did **not** list it on the Settings dataclass; verify against the installed package source if we want to use it).

## 5. Latency

- 150ms first-result latency claim from ElevenLabs marketing.
- `ttfs_p99_latency` override exists on the Pipecat service for advanced metric overriding.
- Realtime auto-reconnects on new audio after disconnect.
- Sends silent keepalive chunks every 5s; 10s timeout.

## 6. VAD / Smart Turn interplay

Our `bot.py` sets:

```python
vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2))
TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())
```

- `stop_secs=0.2` is **aggressive** — only 200ms of silence before VAD says "done." For a caller reciting a phone number digit-by-digit ("five... four... one..."), this risks committing each digit as a separate utterance.
- The Smart Turn V3 model is supposed to mitigate this by understanding sentence-level completion, not just silence.
- **For DOB / phone capture specifically**, consider raising `stop_secs` to 0.5–0.8 transiently while in those nodes, OR rely on the prompt to have callers say the whole thing at once ("Please say your full date of birth in one sentence, including the year").

This is a **known voice-agent UX problem** — ElevenLabs' own keyterms feature won't save us here. The fix is at the prompting + VAD layer.

## 7. Reliability / errors

Server can send these error messages:
- `auth_error`, `quota_exceeded`, `rate_limited`, `commit_throttled`, `queue_overflow`, `resource_exhausted`, `session_time_limit_exceeded`, `chunk_size_exceeded`, `insufficient_audio_activity`, `transcriber_error`

Pipecat handles auto-reconnect on new audio. There's no documented Pipecat-side fallback to a different STT provider — that would be a larger architectural piece (multi-STT racing or failover, see SOLUTION.md if we get into it).

## 8. Frames emitted

Pipecat docs reference `TranscriptionFrame` (committed) and "interim" transcripts (presumably `InterimTranscriptionFrame`). Specific frame class names weren't listed verbatim in the docs we fetched — verify against the installed package source if needed for testing.

## 9. Actionable recommendations for our DOB / phone capture flow

Ranked by leverage:

1. **Use the Flows pattern of asking the LLM to normalize at the schema level.** Prompt the parameter description with "convert to YYYY-MM-DD" / "convert to E.164 format" / etc. This is the single highest-leverage tactic and the one the patient_intake example uses.
2. **Always confirm critical fields back to the user** before persisting. Use a verification node with `ContextStrategy.RESET_WITH_SUMMARY` to read back captured info before booking.
3. **Set `language=Language.EN`** explicitly on the Realtime service to skip auto-detection.
4. **Tune `stop_secs` per node** — keep it at 0.2 for natural conversation; raise to 0.6–0.8 for nodes capturing structured data where the caller is likely to pause mid-utterance.
5. **Spelling fallback** — when the caller's first attempt fails normalization (LLM returns "I didn't catch that"), the prompt should pivot to "Could you spell that out for me?" — letters tend to transcribe more reliably than numbers.
6. **Consider `keyterms`** for the clinic name and any vocabulary that consistently mistranscribes. Verify whether Pipecat's Settings exposes this; if not, we can fall back to LLM-side correction.
7. **Document this trade-off in SOLUTION.md** — the eval bonus section can specifically include automated tests where simulated callers say DOBs in different formats ("April third nineteen ninety two" vs "four three ninety two") and we measure capture accuracy.
