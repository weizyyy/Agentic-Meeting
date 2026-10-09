# Benchmarks

**English** · [简体中文](zh-CN/benchmarks.md)

Numbers measured with real model weights. Each section states its conditions. Results depend on
hardware, models and runtime versions; [runtimes.md §6](runtimes.md) explains how to repeat the
measurements.

- [Test environment](#test-environment)
- [Full-meeting replay](#full-meeting-replay)
- [Screenshots, tools, tasks and reports](#screenshots-tools-tasks-and-reports)
- [Tool selection of the realtime LLM](#tool-selection-of-the-realtime-llm)
- [Failure injection](#failure-injection)
- [Component benchmarks](#component-benchmarks)
- [Loopback connections on Windows](#loopback-connections-on-windows)
- [Low microphone level](#low-microphone-level)

## Test environment

| | |
|---|---|
| OS | Windows 11 |
| GPUs | RTX 2080 Ti 22 GB (CUDA0 in llama.cpp numbering), RTX 3060 Laptop 6 GB (CUDA1) |
| Runtimes | llama.cpp v0.6.0 (b11429, CUDA 13 prebuilt), NeMo-Speech.cpp v0.2.0, qwentts.cpp @51512f1 (built with CUDA 13.0 and MSVC 14.44) |
| Local models | ASR: Confucius4-R2T2 with audio projector; diarization: Nemotron-3-Diarization q8; TTS: Qwen3-TTS custom-voice Q8; embeddings: 0.6B Q8 |
| Realtime and agent LLM | An OpenAI-compatible endpoint on another machine in the LAN (`realtime_llm.mode = "openai_api"`); reasoning off for the realtime model, medium effort for the agent model |

The [component benchmarks](#component-benchmarks) were taken with each service running alone (ASR
and diarization on the 6 GB card, ASR at Q8, TTS 0.6B). The full-meeting results were taken with all
services running together on the 22 GB card (ASR at f16, TTS 1.7B).

## Full-meeting replay

**Material.** A 50 min 29 s recording of a real panel discussion: a host and several guests taking
turns, with accents, filler words and applause.

**Method.** `scripts/soak.py` streams the recording in real time over WebRTC into the complete
application — input gain, VAD, ASR, diarization, storage, and the realtime LLM and TTS for answers.
Every seven minutes it calls the assistant with synthesized speech and asks one typed question.

### Transcription

| Metric | Result |
|---|---|
| Stored captions | 209 captions, 12,294 characters; 59 characters on average, 233 at most (after merging adjacent segments) |
| Speakers | 8 separated — the limit of the diarization model; about 9 people speak in the recording. Five very short captions were left unattributed |
| Caption lag (audio already sent minus the caption's end time, at finalization) | median 0.70 s, p95 0.78 s, max 2.06 s; stable throughout |
| Running summaries | 21, one every five minutes; the last one about 2,400 characters |
| Context compaction | Once, at minute 31: an estimated 25,900 → 6,900 tokens (budget 24,000) |

### Response latency

Measured from the end of the spoken request, including the 0.2 s end-of-speech wait:

| Metric | Result |
|---|---|
| Wake word → first text | median 1.5 s (0.95–2.8 s) |
| Wake word → first audio | median 2.5 s (1.7–3.7 s) |
| Typed question → first text | median 2.1 s (0.8–3.0 s); most typed questions triggered a tool call first |

Breakdown from the server log for an answer without tool calls:

| Stage | Time |
|---|---|
| End-of-speech wait (`turn.vad_stop_secs`) | 0.20 s |
| Final transcription | 0.10–0.13 s |
| End-of-turn model | 0.03–0.08 s |
| LLM time to first token | 0.45–1.0 s, growing with meeting length: this endpoint reprocesses the whole context on each request, and a warm-up request itself takes 0.7–1.2 s |
| Accumulating the first sentence | 0.3–0.5 s |
| TTS first packet | 0.24–0.36 s |
| Total | 1.4–2.3 s |

These figures are for the realtime LLM behind a generic endpoint. The one-second target in
[architecture.md](architecture.md#design-goals) applies to the llama.cpp deployment mode, which has
not been benchmarked yet.

### Wake-word reliability

In the first run the assistant missed three of eight spoken requests, and after one answer it also
reacted to other participants. Both behaviors were traced and changed:

| Observation | Cause | Change |
|---|---|---|
| The name was missing from the transcript or truncated (`ova`, `No one`, 诺亚) | In "name, pause, request" the name lasts a few hundred milliseconds. VAD often triggered only on the request, and just 0.3–0.5 s of earlier audio was passed to ASR | `asr.preroll_ms` default raised from 300 to 1500 ms |
| After answering, the assistant responded to other people's speech | Pipecat's wake strategy stays awake until `wake_timeout_secs` expires | The assistant returns to idle as soon as it has answered (`wire_wake_sleep`) |

Controlled comparison with the same synthesized requests, one every 33 seconds:

| Pre-roll | Woken |
|---|---|
| 500 ms | 9 of 14; 6 of 11 in a second run |
| 1500 ms | 11 of 12 (the name was transcribed as an unrelated word once) |

The same audio sent directly to the ASR backend, bypassing VAD, was transcribed correctly six times
out of six, which confirms that the losses came from the missing audio onset rather than from the
ASR model. Occasional misspellings of the name (`novel`, 诺亚) can be registered in
`session.wake_aliases`.

### Resources over 50 minutes

| | Start → end |
|---|---|
| Application process memory | 320 MB → 512 MB; 504 MB after five minutes, flat afterwards |
| TTS process memory | 2.7 GB, constant |
| ASR and embedding `llama-server` processes | 4.9 GB → **14.5 GB**, rising during the first 15 minutes |
| GPU memory, all services on the 22 GB card | 14.6 GB → 14.8 GB |

The 8 GB increase is `llama-server`'s host-memory prompt cache (`--cache-ram`, 8,192 MiB by
default). ASR requests never share a prefix, so the cache is useless there. The ASR and embedding
servers are now started with `--cache-ram 0`, and the two processes stay at about 5 GB under the same
load.

### Retest with the final configuration (33 minutes)

After the changes above (1500 ms pre-roll, `--cache-ram 0`, idle after answering) the replay was
repeated. It was planned for two hours and stopped manually at minute 33, so **a continuous two-hour
run has not been verified**; the longest continuous run remains the 54-minute one above.

| Metric | Result |
|---|---|
| Spoken requests | 4 of 4 answered |
| Typed questions | 3 of 3 answered; first text after 2.0–2.1 s |
| Caption lag | medians of 0.70–0.72 s per ten-minute window, max 1.8 s |
| Application process memory | 325 MB → 508 MB (minute 10) → 511 MB (minute 30) |
| `llama-server` processes | 4.9 GB → 5.1 GB, then constant |
| TTS process memory / GPU memory | 2.7 GB constant / 14.6–15.1 GB |

Time to first text for spoken requests was 4.0–5.7 s in this run. In the one request examined in
detail, the end-of-turn model judged the synthesized question unfinished and the pipeline waited
for its three-second fallback; the LLM's own time to first token was 1.2 s. With the longer pre-roll
the name is recognized as a segment of its own, so the request is evaluated as a separate turn.
Whether natural speech behaves the same way has not been measured. Setting `turn.smart_turn = false`
replaces the model with a fixed 0.6 s pause.

## Screenshots, tools, tasks and reports

Measured in live meetings with synthetic slides: a title with a few lines of text, or a three-column
table of 21 figures.

| Item | Result |
|---|---|
| Change detection | Two white slides that differ only in text differ by 0.9–1.3 % when averaged over the whole thumbnail — below the 4 % threshold, so the second slide inherited the first one's summary. Taking the most-changed block instead yields 9–14 % (0.1 % for the same picture recompressed), and each slide received its own summary |
| `look_at_screen` | A question about the current slide used the latest screenshot only. A question about two figures on an earlier table, absent from its summary, made the model request that screenshot by id and answer correctly. A question comparing two tables made it fetch the second one. A question already answered by a summary caused no extra lookup |
| Recall | Questions about a guest's remarks from more than 40 minutes earlier, and "who mentioned …", were answered correctly with the speaker identified |
| Background task, web research | "Look up the founding year and size of an institution": 192 s, with sources; progress questions were answered meanwhile |
| Background task, sandbox | "Mean and standard deviation of four numbers": 41 s, correct |
| Background task with attached screenshots (`agent.attach_frames`) | All three screenshots attached; the agent read six figures from the images and produced a grouped bar chart in the sandbox in 47 s |
| Running summary and report with attached screenshots | 15 s and 20 s with three images each; the summary quoted figures that appear only in the images |
| 40 images at 1920×1080 in one request | 28 s; the count and three spot-checked slides were correct |
| Post-meeting report | A 60-minute meeting (about 16k input tokens): 38 s with medium reasoning effort, 13 s with reasoning off; all six sections present |
| Export | Markdown transcript 66 KB, JSON 252 KB, ZIP with transcript, JSON, report, screenshot and task artifacts |

On the endpoint used here (reported context length 262,144 tokens, at most 999 images per request)
an image costs about one token per 1,024 pixels: roughly 880 for 1280×720 and 2,040 for 1920×1080.

Two issues found during these tests:

- With reasoning enabled, the report model spent its entire 4,096-token output budget on reasoning
  and returned an empty report. `agent.generation_max_tokens` (default 16,384) now sets a wider
  budget for direct generation.
- The agent wrote literal newlines inside JSON strings in its final answer, which strict parsing
  rejected. The result parser now accepts them.

## Tool selection of the realtime LLM

`scripts/eval_realtime_model.py` runs 16 fixed cases — a few transcript lines followed by a request —
three times each.

| Metric | Result |
|---|---|
| Time to first token | median 0.38 s, max 0.49 s |
| Correct tool when one is expected | 33 / 33 |
| No tool when none is expected | 15 / 15 |
| Length of direct answers | median 40 characters, max 85 |

Prompt changes can shift this balance noticeably. Adding one sentence that told the model to call
tools without a spoken preamble made it reach for a tool in 7 of the 15 cases that need none, and
the sentence was removed. Run the script after editing `config/prompts/realtime_system.md`.

## Failure injection

Each row of the failure table in [architecture.md §9](architecture.md) was provoked on the running
system.

| Injected failure | Observed |
|---|---|
| Realtime LLM pointed at an unreachable address | Transcription continued; no answers to spoken or typed requests; the page showed "助理暂不可用：连不上服务" |
| TTS pointed at an unreachable address | Text answers continued with the notice "语音不可用"; the answers were stored and remained in the context — the assistant could repeat its previous answer when asked |
| Diarization model replaced by an invalid file | Notice at connection time; captions without speakers; transcription and answers unaffected |
| Embedding server killed mid-meeting | Recall kept answering through keyword search |
| ASR server killed mid-meeting | Notice after 10 s; once the server was restarted with `services up`, captions resumed within 12 s |
| `serve --with-services` killed forcibly | All three inference processes were ended by the operating system. Before the job-object change they survived and kept the ports and GPU memory |
| Application killed and restarted, meeting resumed | The timeline continued, the context was rebuilt from the summary and recent transcript, and speakers were renumbered with a notice |

## Component benchmarks

Except for the upstream sample audio, the speech in this section was synthesized, with different
voices playing different participants. It is much cleaner than a real meeting: these numbers show
that the components work, that timestamps line up and that there is headroom, not real-world
accuracy.

### Speech synthesis

| | GPU build | CPU build |
|---|---|---|
| Real-time factor | 0.19 | about 1.1 |
| Time to first byte, streaming | 0.08 s (0.24 s for the first request) | about 0.2 s (about 1 s for the first) |
| Output | 24 kHz, 16-bit, mono PCM in chunks | same |

### Streaming ASR

Rolling window, prefix continuation, a token budget proportional to new audio at each step, and a
rollback of one token (interfaces.md §3.2). Step size 320 ms.

| Metric | Result |
|---|---|
| Upstream sample (6.7 s, Mandarin) | Identical to one-shot recognition except for one comma |
| Simulated meeting (90.5 s, 10 sentences, 4 voices, one 28 s utterance) | Character error rate 0.3 % (1 of 326) |
| Time per step | median 0.09 s, max 0.18 s with the full 16 s window |
| Finalized text behind audio | median 0.09 s, max 0.24 s, excluding VAD and diarization delays |
| Server start | Ready in about 3 s |
| First request | About 17 s and 9 s on the first two uses of a machine, 0.2 s after every later restart — presumably the GPU driver compiling and caching kernels. Expect it once after changing machine, driver or runtime version |

Two findings are built into the algorithm:

- **A generous generation budget with character-level rollback gives poor output.** Allowing 32
  tokens per step and holding back four characters dropped characters, inserted punctuation and
  truncated sentences. The model is trained to emit only stable content and runs ahead when given
  room, and character-level rollback can split a token. Following the upstream server's method
  resolved it.
- **Hotwords can be echoed at the start of a segment.** With very short or near-silent audio the
  model sometimes outputs the hotword list from the system message verbatim, which can also trigger
  a false wake-up. Provisional output that begins with two or more consecutive hotwords, starting
  from the first, is discarded.

### Wake-word matching

Four candidate names (Jarvis, Nova, Friday, Echo), seven sentences each — five that should wake the
assistant, with the name at the start, middle and end, and two that should not — in four voices, sent
directly to the ASR backend:

| Strategy | Correct |
|---|---|
| Pipecat's `WakePhraseUserTurnStartStrategy` | 5–6 of 7 per name; misses the name when it touches a Chinese character |
| This project's `WakeWordUserTurnStartStrategy` (corrected word boundary) | 7 of 7 per name |

### Speaker diarization

| Metric | Result |
|---|---|
| Load time | 0.23 s; up to 8 speakers; 10 ms output resolution |
| Speed | Real-time factor 0.02 (1.8 s for 90.5 s of audio) |
| Labels behind audio (low-latency preset, 320 ms chunks) | median 0.64 s, max 1.12 s |
| Sentence attribution in the simulated meeting | 9 of 10; the miss was a voice's first short question, merged into another speaker |

### Embeddings and vector search

| Metric | Result |
|---|---|
| Output dimension | 1024 |
| Query embedding time on CPU | median about 120 ms; 0.8 s for a batch of 8 sentences |
| Retrieval (8 captions, 6 questions) | Top-1 hit for 5, top-3 for the remaining one, including an English full name matching a Chinese abbreviation |

### GPU memory per service

| Service | Memory |
|---|---|
| ASR (Q8, context 8192, after a full 16 s window) | about 3.8 GB |
| Diarization | about 0.2 GB |
| TTS (0.6B) | about 2.4 GB |
| Embeddings with `gpu_layers = "0"` only | Still a few hundred MB on every GPU |
| Embeddings with `--device none` | 0 |

The figures above are for each service running alone. With all local services running together:

| Configuration | Memory |
|---|---|
| ASR Q8, TTS 0.6B | slightly more than 10 GB |
| ASR f16, TTS 1.7B (the full-meeting replay above) | 14.6–15.1 GB |

## Loopback connections on Windows

| | |
|---|---|
| Connecting to a loopback port nobody listens on | 2.07 s until "connection refused" with a raw socket; 2.27 s with httpx to `127.0.0.1`, 2.33 s to `localhost`. Windows retries loopback connections instead of refusing at once |
| Effect | Each health probe of a service that is still starting wastes about 2 s; probing four services in sequence adds about 8 s |
| Mitigation | Loopback probes use a 0.5 s connect timeout (`LOOPBACK_CONNECT_TIMEOUT_SECS` in `services/supervisor.py`), and the probes before startup run concurrently |
| Result | `services status` returns within 0.7 s when all four services are down |

Linux and macOS refuse immediately; the mitigation is harmless there and is not platform-specific.

## Low microphone level

### Symptom

With a physical microphone, speech is either not recognized at all or arrives in fragments of a few
words per sentence. A recording made with the same microphone is barely audible at normal playback
volume but intelligible when turned up: the captured level itself is low. Synthesized speech and
sample audio are loud and do not expose the problem.

### Cause: the VAD volume gate

Pipecat's `VADParams.min_volume` defaults to 0.6. A frame counts as speech only if
`confidence ≥ 0.7` **and** `smoothed volume ≥ min_volume`. The volume is the BS.1770 loudness of the
last 0.4 s, mapped linearly from −110…−10 LUFS to 0…1 and exponentially smoothed, so **0.6
corresponds to about −50 LUFS and every 6 dB lowers the value by about 0.06**. When the input is too
quiet only the loudest syllables pass the gate, speech-started and speech-stopped events fragment,
and ASR — which receives audio only during detected speech — gets a few words per sentence.

`scripts/mic_check.py` reproduces the measurement: an 11-second sample of natural speech
(`third_party/NeMo-Speech.cpp/test_files/asr/wav/test/jfk.wav`, RMS −16.9 dBFS) is attenuated by
various amounts and fed to Pipecat's VAD in 20 ms steps, counting the share of steps classified as
speech and the number of speech starts.

| Attenuation | RMS (dBFS) | Gate 0.6: speech ratio / starts | Gate off: speech ratio / starts | Peak smoothed volume |
|---|---|---|---|---|
| 0 dB | −16.9 | 0.44 / 6 | 0.46 / 6 | 0.99 |
| −12 dB | −28.9 | 0.45 / 8 | 0.45 / 8 | 0.87 |
| −24 dB | −40.9 | 0.40 / 8 | 0.43 / 10 | 0.75 |
| −30 dB | −46.9 | 0.27 / 9 | 0.38 / 7 | 0.69 |
| −36 dB | −52.9 | **0.07 / 1** | 0.40 / 9 | 0.63 |

The ratio is below 1 because the sample contains pauses; compare rows rather than absolute values.

- At −36 dB (RMS around −53 dBFS) the default gate discards almost the whole utterance. With the
  gate off, the VAD model's own confidence is nearly independent of level.
- The remedy has two layers: raise the level first — system input volume, microphone boost, or the
  server-side automatic gain in `[audio]` — and keep the gate adjustable through
  `turn.vad_min_volume`.

### Not measured

- Whether the browser's own automatic gain control takes effect on a given device. The Pipecat
  client requests the microphone without audio constraints (pipecat-notes.md §12).
- How well the ASR model itself tolerates low-level audio when the gate is open and no gain is
  applied.
