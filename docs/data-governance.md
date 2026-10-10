# Data governance

**English** · [简体中文](zh-CN/data-governance.md)

## 1. What is stored

`session.data_dir` (default `data`) contains application data. The standard microphone pipeline
processes audio in memory for recognition and diarization; it does not archive raw microphone
recordings. Transcription status means transcription, not an audio-file recording.

| Data                                                                                           | Location                                               |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Session title, speakers, connection history, utterances, full-text index and utterance vectors | `<data_dir>/meetings.db`                               |
| Screenshot timestamps, descriptions and file references                                        | Database; images in `<data_dir>/sessions/<id>/frames/` |
| Running summaries (digests) and meeting reports                                                | Database                                               |
| Task goals, results, sources, progress events and outbound metadata                            | Database                                               |
| Task input copies, generated files and sandbox working files                                   | `<data_dir>/sessions/<id>/tasks/<label>/`              |
| Service/application logs                                                                       | `<data_dir>/logs/`                                     |

Downloaded exports are separate copies wherever the operator/browser saves them. Logs and task
results can also contain meeting content; they are not equivalent to the utterance table.
Configuration, environment secrets, models, runtime files and certificates are outside retention.

## 2. Where data is sent

The browser sends microphone audio and shared screenshots to the configured meeting server.
Whether inference stays on that server depends on **each actual endpoint**, not merely a mode
called local or whether process launch is enabled. Another machine on a LAN is still a destination
outside the meeting server. Review these settings before a meeting:

| Configuration / feature                       | Data received by the configured destination                                                                                                          |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `asr.base_url`                                | Audio windows plus recognition prompts/context; recognition and tokenization use the ASR service                                                     |
| `embedding.base_url`                          | Utterance text and semantic-recall queries                                                                                                           |
| `tts.base_url` (when enabled)                 | Text to be spoken, which can include meeting content and assistant/task results                                                                      |
| Active `realtime_llm` endpoint                | Meeting context: transcript, summaries, assistant/tool exchanges; images when vision is supported and used                                           |
| `screen.caption_provider`                     | Screenshots to the selected realtime or agent model for descriptions                                                                                 |
| `realtime.digest_provider`; `report.provider` | Transcript/context to the selected realtime or agent model; selected screenshot inputs when agent frame attachment and vision are enabled            |
| `agent.base_url`                              | Delegated goal, selected transcript, task/tool exchanges and selected screenshots when `agent.attach_frames` and `agent.supports_vision` are enabled |
| `agent.mcp_servers`                           | Tool parameters/results according to tools used by the agent                                                                                         |
| `agent.sandbox`                               | Task code and inputs, including copied screenshot files; network access is separately controlled by `network`                                        |

Speaker diarization uses the configured in-process native backend. `check`/`serve` and the browser
warn about some configured external flows; those reminders are not a complete destination audit.
Service providers, tools and network-enabled sandbox code may retain their own copies. Application
retention and manual deletion do not revoke data already sent elsewhere.

## 3. Retention and keep

All four `[retention]` periods default to `0`: existing data is retained indefinitely. Enable only
categories you intend to remove; zero is disabled, never immediate expiry. Values are strict
integers from 0 to 36500. `cleanup_interval_secs` defaults to 3600 and accepts 60–86400.

Each day is 86400 seconds. A pass samples server UTC Unix time once and expires anchors **strictly
older** than `now - days * 86400`; equality and future timestamps are retained.

| Period                | Clock anchor                                                                          | Removed data                                                        |
| --------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `transcript_days`     | Meeting inactivity: maximum of started, last-active and ended timestamps (if present) | Utterances, their vectors and full-text entries                     |
| `screenshots_days`    | Same meeting inactivity anchor                                                        | Frame rows, including descriptions, and the owned `frames/` subtree |
| `reports_days`        | Each terminal report's completion; each digest's creation                             | Terminal report rows and digest rows                                |
| `task_artifacts_days` | Each terminal task's completion; legacy terminal tasks without it use creation        | Owned task directories and `artifacts_json` lists, reset to `[]`    |

Interrupted meetings may expire without being ended. Live meetings and protected background work
are skipped: this includes queued/running tasks and still-active writes/finalization. New reports
and task files can outlive an old meeting because their clocks start later.

Use **Keep** in the meeting list or detail to exempt that meeting from all four automatic
categories. The flag persists across restart and updates only after a successful server response;
failed requests show an error. Keep can change during a live meeting. It cannot restore removed
files, and a cleanup claim already in progress can return a conflict; retry afterwards. Explicit
manual deletion ignores keep.

Automatic cleanup retains session metadata (including title, speakers, connection history and
keep), task goals/results/sources/events/outbound metadata, logs and exported files. Task input
copies can appear in goals, results or events even after task directories are cleaned. Expiring
transcripts does not erase screenshot descriptions, summaries, reports or task text: these are
separate copies with independent lifetimes or survive until full meeting deletion.

## 4. Complete deletion and recovery limits

Delete from the meeting list explicitly removes a whole meeting, including kept meetings. Busy
meetings or protected work return 409 without beginning deletion. After admission, the server
marks the meeting **pending deletion** before removing its owned files, vectors and database rows
(including related tasks/events, reports, digests, frames, speakers and connections).

A pending meeting still exposes metadata but its content/export, rename, keep, resume, end and new
work are blocked. The page shows **待删除** and preserves **重试删除** in the list. Keep cannot cancel
this state. A real file/database failure returns 500; some files may already be removed. The page
refreshes the list/detail rather than claiming success. Retry DELETE after repairing the server
problem; subsequent cleanup passes and restart also retry, even with every category disabled.
Only completed deletion returns success; retrying a fully removed id returns 404.

Cleanup removes only validated application-owned descendants, refusing unsafe links/path escapes.
Repair permissions, disk errors or refused path ownership rather than changing paths to force a
retry. File removal and database commit are not one atomic transaction. There is no undo and no
secure-erasure guarantee: SQLite pages/WAL, storage snapshots, backups, exported downloads,
external services and logs may still contain copies. Retention does not compact or sanitize those
copies. Operators must manage their backup/export/log lifetimes separately; restoring a backup can
restore deleted content and old keep flags.

For configuration see [Configuration](configuration.md#retention); for errors see
[Troubleshooting](troubleshooting.md#keep-cleanup-and-pending-deletion). The exact contract is in
[Interfaces §8.4](interfaces.md#84-retention-cleanup-and-complete-deletion).
