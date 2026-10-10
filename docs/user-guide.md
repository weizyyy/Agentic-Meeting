# User guide

**English** · [简体中文](zh-CN/user-guide.md)

How to use the web page during and after a meeting. The interface is in Chinese; button labels are
quoted as they appear, with an English gloss.

- [Supported browsers](#supported-browsers)
- [Meetings](#meetings)
- [Captions and speakers](#captions-and-speakers)
- [Talking to the assistant](#talking-to-the-assistant)
- [Screen sharing](#screen-sharing)
- [Background tasks](#background-tasks)
- [Report and export](#report-and-export)

## Supported browsers

Use a desktop browser no older than:

| Browser | Minimum version |
| ------- | --------------- |
| Chrome  | 111             |
| Edge    | 111             |
| Firefox | 114             |
| Safari  | 16.4            |

These are the versions the page is built for (`client/src/browserSupport.ts`). The automated browser
tests run the current Chromium, Firefox and WebKit engines on Linux; Chrome, Edge and Safari on
Windows and macOS have not yet been checked by hand.

When a browser cannot hold a meeting, the page says why instead of failing on connect:

- **Older than the minimum.** The page shows **页面没能启动** (The page could not start) with the
  list above, instead of staying blank.
- **Opened over plain `http://` from another machine.** A red line under the top bar says the page
  was not opened over HTTPS or `localhost`. **开始新会议** (Start meeting) is greyed out and
  **继续** (Resume) only repeats the reason. Past meetings can still be read. See
  [Access from other devices](getting-started.md#access-from-other-devices).
- **A recent browser with WebRTC, microphone capture or Web Audio turned off** by a setting, a
  policy or an extension. The same line names what is missing.
- **Autoplay blocked.** See [Talking to the assistant](#talking-to-the-assistant).

## Meetings

The page shows the most recent meeting as soon as it loads — no connection is needed to browse.
If the server has an access password, the page asks for it first; **退出登录** (Log out) in the top
bar is shown when no meeting is running on this page. If the login expires during a meeting, a
password prompt covers the page and the meeting continues underneath.

| Action    | How                                                 | What happens                                  |
| --------- | --------------------------------------------------- | --------------------------------------------- |
| Start     | **开始新会议** (Start meeting)                      | Creates a meeting and connects the microphone |
| End       | **结束会议** (End meeting)                          | Marks the meeting as ended and disconnects    |
| Interrupt | Close or refresh the page, lose the network         | The meeting is kept and marked _interrupted_  |
| Resume    | **继续** (Resume) in the banner or the meeting list | Reconnects to the same meeting                |
| Browse    | Meeting list                                        | View, rename or delete past meetings          |

**One connection at a time.** Starting or resuming a meeting in another tab or on another device
takes over the connection; the previous page is notified and switches to read-only.

**Resuming.** The timeline continues where it left off, and the gap is shown in the captions
(for example "— 中断了 12 分钟 —"). Speaker names, screenshots and tasks are kept, and the assistant
remembers the earlier discussion through the running summary and the last ten minutes of transcript.
After a short network drop the page reconnects on its own, up to five attempts.

After a server restart, diarization has to learn the voices again, so a returning speaker may get a
new label. Use **合并到…** (Merge into…) on the speaker chip to merge the two.

**Following along.** Open the same address on another device to watch a live meeting read-only;
the view refreshes every three seconds. **在这台设备上继续** (Continue on this device) moves the
connection there.

## Captions and speakers

Each caption row shows two times: the wall-clock time in the browser's time zone, and below it the
elapsed meeting time. Short pauses do not split a caption: consecutive segments from the same
speaker within two seconds are merged into one row.

Speakers appear as chips above the captions.

- **Rename** — click a chip and type a name, or click one of the names from `session.members`
  shown next to the input. Names already used by another speaker are left out. The new name
  applies to the whole meeting, including what the assistant sees.
- **Merge** — **合并到…** moves all of one speaker's captions to another speaker. This cannot be
  undone.
- **Reassign individual captions** — click the time-and-speaker block at the left of a row to
  select it, or drag across rows to select a range. The speaker chips turn into targets: click one
  to assign the selected captions to that speaker, or choose **＋ 新说话人** (New speaker) and type
  a name or pick one from `session.members`. Press `Esc` to cancel. The text area of a row is not
  part of the selection, so you can still select and copy text.

## Talking to the assistant

**By voice.** Say the assistant's name followed by your request — "Nova, summarize the last ten
minutes". It answers in speech and text, then goes back to listening for its name, so follow-up
questions need the name again. You can interrupt it by speaking while it talks.

If the browser's autoplay policy blocks the assistant's voice, a yellow line says
**浏览器拦下了助理的声音** (The browser blocked the assistant's voice). Click **打开助理声音** (Turn on
the assistant's voice) once to hear it.

Things it can do directly:

| Request                       | Example                                                          |
| ----------------------------- | ---------------------------------------------------------------- |
| Summarize                     | "Nova, give me three takeaways so far."                          |
| Recall                        | "Nova, what did Dr. Wang say about the dataset split?"           |
| Read the screen               | "Nova, what is on the y-axis of this chart?"                     |
| Look back at an earlier slide | "Nova, on the budget slide from before, which year was highest?" |

**By typing.** The text box at the bottom of the assistant panel accepts questions without the wake
word (`Enter` to send, `Shift+Enter` for a new line). Typed questions are answered in text only.
Messages sent while the assistant is busy are queued.

## Screen sharing

Click **共享屏幕** (Share screen) in the screen panel and choose a window or display. A screenshot is
taken whenever the picture changes, and once a minute otherwise. Thumbnails appear on the timeline;
scroll the strip with the mouse wheel, click a thumbnail to enlarge it, and use `←` `→` to step
through. A short summary is generated for each new picture.

Screenshots are uploaded only while a meeting is live. From another device this requires HTTPS.

## Background tasks

Ask for something that takes longer — "Nova, look up the citation count of this paper", "plot these
three numbers as a bar chart" — and the assistant acknowledges the request, hands it to the
background agent and reports the result when it is done, waiting for a pause in the conversation.
Tasks requested by typing are reported in text only.

The task panel shows progress. Open a task to see the detailed result, sources, generated files and
**exactly what was sent to the remote model**. Running tasks can be cancelled, and you can ask the
assistant how a task is going. When the window is short, the task panel gives up space first and
scrolls, so the assistant's answer always keeps at least four lines.

Running code requires the Docker sandbox; without it the agent can still search and read images.

## Report and export

**Report.** After a meeting has ended or been interrupted, switch to the **报告** (Report) tab and
click **生成报告** (Generate report). The report has six sections — summary, discussion points,
decisions, action items, background tasks and a screenshot index — and can be downloaded as
Markdown or regenerated.

**Export.** The meeting banner offers three downloads, also available while the meeting is live:

| Link                      | Contents                                                                             |
| ------------------------- | ------------------------------------------------------------------------------------ |
| **转录** (Transcript)     | Markdown: running summary, captions and screen summaries in time order, task results |
| **JSON**                  | All structured data                                                                  |
| **完整包** (Full archive) | ZIP with the transcript, JSON, report, every screenshot and all task artifacts       |
