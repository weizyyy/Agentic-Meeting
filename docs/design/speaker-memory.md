# Design: remembering speakers across meetings

**English** · [简体中文](../zh-CN/design/speaker-memory.md)

Status: **proposed**, for [#25](https://github.com/weizyyy/Agentic-Meeting/issues/25). Nothing in
this document is implemented yet. When a part lands, its interfaces move into
[interfaces.md](../interfaces.md), [configuration.md](../configuration.md) and
[data-governance.md](../data-governance.md), and this document records only the reasoning.

## 1. Goal

Speakers are numbered anew in every meeting ("说话人 1", "说话人 2" …) and the host renames them
each time. With this feature the server recognizes voices it has been taught before and **suggests**
a name for a speaker; the host confirms or dismisses the suggestion. Nothing is renamed silently.

Requirements from the issue:

- A separate speaker-embedding model (§2), which is a new runtime dependency (§3).
- Enrollment from an existing meeting ("this speaker is …") and storage of voice profiles (§6).
- Matching early in a meeting with a confidence threshold, as a suggestion the host confirms (§7).
- Voice profiles are biometric data: opt-in, deletable per person, covered by the retention rules
  of [#11](https://github.com/weizyyy/Agentic-Meeting/issues/11), and never sent to remote
  services (§8).
- Off unless a model is configured; nothing is downloaded automatically (§10).

Out of scope: recognizing people who were never enrolled, speaker identification by anything other
than voice, and changing the streaming diarization itself.

## 2. Why diarization alone is not enough

The diarization backend (NeMo-Speech.cpp v0.2.0, a streaming Sortformer model, see
[interfaces.md §4.2](../interfaces.md)) assigns **slots in order of arrival**: slot 1 is whoever
spoke first in this stream. The slot number carries no identity across meetings, and the model
distinguishes a small fixed number of slots (four for the model tested so far). Its internal
speaker cache holds encoder frames, not voice embeddings trained for verification, and the C API
exposes only segments (`start_time`, `end_time`, `speaker`). The pinned NeMo-Speech.cpp release
has no speaker-embedding model or API.

So a second model is needed: a **speaker-embedding** (speaker verification) model that turns a few
seconds of one person's speech into a fixed-length vector whose cosine similarity is high for the
same person and low for different people, independent of the meeting.

## 3. Runtime dependency

| Option                                                                | What it brings                                                                                                                                                                                                                                                                                                                         | Assessment                                                                                                                       |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **A. `sherpa-onnx`** (Python package, Apache-2.0)                     | Speaker-embedding extractor with feature extraction built in, on ONNX Runtime bundled in the wheel; CPU wheels for CPython 3.12–3.14 on Windows x64, Linux x64/arm64 and macOS arm64 (a 2–4 MB wheel plus a 10–16 MB `sherpa-onnx-core` wheel). Loads 3D-Speaker (CAM++, ERes2Net), WeSpeaker and NeMo TitaNet models exported to ONNX | **Recommended.** One package, no PyTorch, no compiler. Mandarin-trained models (3D-Speaker) are available in the format it loads |
| B. `onnxruntime` + `kaldi-native-fbank`                               | The same models, with our own feature extraction and model metadata handling                                                                                                                                                                                                                                                           | Two packages and more code that must match each model's training front end exactly                                               |
| C. PyTorch stacks (SpeechBrain, pyannote, WeSpeaker's Python package) | Reference implementations                                                                                                                                                                                                                                                                                                              | Rejected: gigabytes of dependencies for a model of a few tens of megabytes                                                       |
| D. Wait for a speaker-embedding API in NeMo-Speech.cpp                | Same runtime as diarization                                                                                                                                                                                                                                                                                                            | Not available in the pinned release; can replace A later behind the same service interface (§5)                                  |

Decision (approved by the owner): add `sherpa-onnx` with an exact version pin as a new optional extra `speaker`
(`uv sync --extra speaker`), installed in the application container image. The base install does
not change. Model weights are placed by the user under `models/` like every other model;
`docs/runtimes.md` will list the models that were tested. Candidates for Mandarin group meetings are
the 3D-Speaker CAM++ and ERes2Net models trained on Chinese data (tens of megabytes, 16 kHz); the
default threshold (§7) is calibrated on the model that is chosen and recorded in
[benchmarks.md](../benchmarks.md).

## 4. Overview

```text
microphone audio ──► recorder ──► diarizer (slots) ──┐
        │                                            │ stable single-speaker spans
        └──► 30 s ring buffer ──────────────────────► clip cutter ──► speaker service ──► clip vectors
                                                                    (127.0.0.1 only)        │
captions (speaker_idx after host corrections) ─────────────────────────────────────────────┤
                                                                                            ▼
                                       per-meeting voiceprint of each speaker ──► match against profiles
                                                                                            │
                                         host: confirm / dismiss ◄── suggestion ◄───────────┘
                                         host: "remember this voice" ──► voice profile (+ consent)
```

## 5. Voice clips

**Speaker service.** A small process `agentic-meeting speaker-service` wraps the extractor and is
supervised like the other inference services (`launch.*`, health check, restart with backoff). It
accepts 16 kHz mono PCM and returns one L2-normalized vector. Running it out of process keeps a
native crash or a long computation away from the event loop and from transcription (hard rule 7),
and lets end-to-end tests replace it with a fake in `tests/e2e/inference.py`. Its address must be a
loopback address; configuration validation rejects anything else (§8).

**Cutting clips.** The recorder already feeds all audio to the diarizer in order. Alongside it, a
ring buffer keeps the last 30 seconds (about 1 MB). Every 2 seconds:

1. Take diarization segments up to `labeled_secs − 2 s`; the streaming model still revises the most
   recent seconds.
2. Keep the parts where exactly one slot is active, removing overlaps with other slots and the
   assistant's speaking periods (the same echo window as [interfaces.md §4.3](../interfaces.md)).
3. Trim 0.2 s at both ends, split into windows of at most 6 s and keep windows of at least 1.5 s.
4. Send each window to the speaker service (timeout 5 s, bounded queue; when the queue is full the
   oldest window is dropped).

Without diarization (`diarization.backend = "none"` or after it degraded) the windows are cut from
caption spans instead. That still lets the host enroll speakers they assigned by hand.

**Storage.** Each vector is stored as a voice clip of the session with its time span and slot. A
clip has no speaker of its own: its speaker is the `speaker_idx` of the ASR caption that covers more
than half of it, read at the time of use. Reassigning captions, merging speakers and late correction
therefore move clips along with the text, and the host's corrections are the ground truth. Clips
not covered by any caption (coughs, noise) are ignored.

**A speaker's voiceprint in a meeting** is the duration-weighted mean of its clips, normalized, and
its _clean speech_ is the total clip duration.

## 6. Voice profiles and enrollment

A voice profile is a named person with up to eight **samples**; a sample is one meeting's voiceprint
of that person. Profiles are global to the deployment (one data directory), not per meeting.

**Enrollment** is an explicit action by the host on a speaker of a live or ended meeting:
"Remember this voice…", with a name (prefilled from the speaker's display name, with
`session.members` offered as choices) and a required confirmation that the person agreed. It needs
at least `min_enroll_secs` of clean speech; below that the page says how much more is needed.
Enrolling into an existing profile adds a sample. The speaker is then linked to the profile.

**Updating.** When the host confirms a suggestion, the speaker is linked to the profile. When that
meeting ends, its voiceprint is added as a sample if `auto_update` is on, replacing an older sample
from the same meeting; the eight most recent meetings are kept. This adapts profiles to different
microphones and rooms without asking again.

**Model changes.** Each profile records a fingerprint of the model file (SHA-256 and vector
dimension). Profiles from another model are not matched and are shown as needing re-enrollment;
vectors are never converted between models.

## 7. Matching and suggestions

For each speaker of the meeting with a positive index that is not linked to a profile yet, once its
clean speech reaches `min_match_secs` and then at most every 5 seconds while it grows:

1. Score each compatible profile as the highest cosine similarity between the speaker's voiceprint
   and the profile's samples.
2. Suggest the best profile when its score is at least `match_threshold` and exceeds the second-best
   profile by at least `match_margin`.
3. Assign greedily by score so that one profile is suggested for at most one speaker, skip profiles
   already linked in this meeting and pairs the host dismissed.

The host page shows the suggestion next to the speaker ("可能是 张三"). **Confirm** renames the
speaker to the profile's name (the same broadcast as a manual rename) and links them; **Dismiss**
remembers the pair for this meeting. If a later evaluation no longer supports a pending suggestion,
it is withdrawn. Ended meetings get suggestions on request, computed from their stored clips, so a
past meeting can be named after enrollment.

Limitations stated in the user guide: the diarization model separates only a few slots, so a slot
that mixes two people matches poorly; short interjections are not enough to match; similar voices
can be confused, which is why nothing is applied without confirmation.

## 8. Privacy rules

Voice clips and profiles are biometric data.

- **Off by default.** With `speaker_memory.backend = "none"` no audio is embedded, no clips are
  stored and no endpoint or message appears.
- **Opt-in per person.** Matching uses only profiles created by explicit enrollment with the
  consent confirmation. While the feature is on, clips are computed for everyone who speaks, so that
  enrollment from an earlier meeting is possible; `store_session_voice = false` keeps them in memory
  for the live meeting only, and the recording notice says that voice features are processed.
- **Deletable per person.** Deleting a profile deletes its samples and, in every meeting, the clips
  attributed to speakers linked to it. Erasing the voice data of one meeting deletes its clips and
  keeps the transcript. Both are available in the page and from the command line.
- **Retention.** Clips belong to the transcript category of [interfaces.md §8.4](../interfaces.md):
  they expire with it and are removed by complete deletion. Profiles have their own period,
  `retention.voice_profiles_days`, measured from the last update or confirmed match; samples are
  copies and survive deletion of the meeting they came from until then.
- **Never sent anywhere.** The speaker service must listen on loopback; vectors and scores never
  appear in HTTP responses, exports, the LLM context, tool results, task inputs or logs. Profile
  names reach the LLM only after the host confirms them as a speaker's display name, exactly like a
  manual rename.

## 9. Access and identity

Per-meeting access and local accounts ([#22](https://github.com/weizyyy/Agentic-Meeting/issues/22))
are being settled separately; this feature follows whatever identity model lands there.

- With today's shared access password, everyone who is signed in may do everything, the same as
  renaming speakers today.
- With per-meeting roles, enrollment, confirming, dismissing and erasing a meeting's voice data
  require the host role in that meeting. Listing, renaming and deleting profiles are open to
  signed-in local accounts but not to guests who came through a share link or meeting password. A
  profile can be linked to a local account so that its owner can find and delete it.
- Suggestions are sent only to host pages, never to the event stream of following devices.

Several meetings at once ([#23](https://github.com/weizyyy/Agentic-Meeting/issues/23)) share one
speaker service; each meeting has its own clip queue and they are served in turn.

## 10. Interfaces to add

These go into the reference documents before the code (hard rule 9). Names are proposals.

**Configuration** `[speaker_memory]`:

| Key                   | Default                   | Meaning                                                               |
| --------------------- | ------------------------- | --------------------------------------------------------------------- |
| `backend`             | `"none"`                  | `"none"` or `"sherpa_onnx"`                                           |
| `base_url`            | `"http://127.0.0.1:8084"` | Speaker service; loopback only                                        |
| `min_match_secs`      | `10`                      | Clean speech before a speaker is matched                              |
| `min_enroll_secs`     | `20`                      | Clean speech needed for enrollment                                    |
| `match_threshold`     | calibrated (§3)           | Minimum cosine similarity for a suggestion                            |
| `match_margin`        | `0.08`                    | Required lead over the second-best profile                            |
| `auto_update`         | `true`                    | Add confirmed meetings as samples                                     |
| `store_session_voice` | `true`                    | Store clips with the meeting; `false` keeps them for the live meeting |
| `launch.*`            |                           | `enabled`, `model_path`, `num_threads`, `env`, `extra_args`           |

`[retention] voice_profiles_days`, default `0` (kept until deleted).

**Database** (additive migration): `voice_clips` (session, time span, slot, duration, vector, write
token; cascades with the session), `voice_profiles` (name, optional account, timestamps, model
fingerprint), `voice_profile_samples` (profile, source meeting set to null when that meeting is
deleted, duration, vector), `speakers.voice_profile_id`, and dismissed pairs per meeting.

**HTTP:** `GET /api/voice-profiles`, `PATCH`/`DELETE /api/voice-profiles/{id}`,
`POST`/`DELETE /api/speakers/{idx}/voice-profile` (enroll or link, unlink),
`GET /api/speaker-suggestions`, `POST /api/speaker-suggestions/accept` and `/dismiss`,
`DELETE /api/sessions/{id}/voice`. Responses carry names, durations, dates and a rounded score,
never vectors.

**Data channel:** `speaker_suggestion` and `speaker_suggestion_cleared`, to host pages only.

**Command line:** `agentic-meeting voice-profiles list | delete <id>` for the operator, with the
server stopped.

## 11. Failure handling

The speaker service is optional in the same way as embeddings. If it is not configured, fails to
start, times out or returns errors, clip windows are dropped, matching pauses and the page shows one
notice per outage; captions, diarization and storage carry on. A model fingerprint that differs from
stored profiles is a warning from `check` and `serve`, not a startup failure.

## 12. Tests

End-to-end scenarios in `tests/e2e/` with a fake speaker service that returns a deterministic vector
per recording, and captions assigned to speakers by hand (diarization is off in the test harness):

- enroll a speaker in one meeting, play the same recording in a second meeting, get a suggestion,
  confirm it and see the rename; dismiss it in a third;
- below `min_enroll_secs` enrollment is refused; with the feature off no endpoint, clip or message
  exists;
- deleting a profile removes its samples and linked clips; erasing a meeting's voice keeps its
  captions; transcript and profile retention remove what they should;
- a failing or slow speaker service does not delay captions.

A `@pytest.mark.gpu` test in `tests/test_real_services.py` runs the real model with real
diarization on the user's machine.

## 13. Delivery plan

1. This design.
2. Dependency, configuration, speaker service, clip capture and storage, erasure and retention of
   clips, data-governance documentation.
3. Profiles, enrollment, matching, suggestions, the page's controls and the user guide.
4. Profile retention, the command line, the container image and threshold calibration in
   benchmarks.md.

## 14. Decisions

Decided: the dependency in §3 (`sherpa-onnx` as an optional extra).

For review:

1. Clips stored with the meeting by default (`store_session_voice = true`) so that past meetings can
   be used for enrollment.
2. Confirmed matches update the profile automatically (`auto_update = true`).
