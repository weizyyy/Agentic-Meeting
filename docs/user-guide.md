# User guide

**English** · [简体中文](zh-CN/user-guide.md)

How to use the web page during and after a meeting. The interface is available in Chinese and
English (see **Language** under [Meetings](#meetings)); button labels are quoted as they appear in
Chinese, followed by the English label.

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
| Firefox | 128             |
| Safari  | 16.4            |

These are the versions the page is built for (`client/src/browserSupport.ts`); Firefox needs 128
because the page's stylesheet (Tailwind CSS 4) relies on features it added. On macOS, installed Chrome,
Edge, Firefox and Safari have been checked by hand for microphone input, screen sharing, automatic
reconnection and ending a meeting.

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

| Action    | How                                                              | What happens                                                                     |
| --------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Start     | **开始新会议** (Start meeting)                                   | Creates a meeting and connects the microphone                                    |
| End       | **结束会议** (End meeting)                                       | Marks the meeting as ended and disconnects                                       |
| Interrupt | Close or refresh the page, lose the network                      | The meeting is kept and marked _interrupted_                                     |
| Resume    | **继续** (Resume) under the meeting title or in the meeting list | Reconnects to the same meeting                                                   |
| Browse    | Meeting list                                                     | View, keep, rename or delete past meetings; deleting asks for confirmation first |

**One connection at a time.** Starting or resuming a meeting in another tab or on another device
takes over the connection; the previous page is notified and switches to read-only.

**Resuming.** The timeline continues where it left off, and the gap is shown in the captions
(for example a **中断了 12 分钟** (Interrupted for 12 minutes) divider). Speaker names, screenshots and tasks are kept, and the assistant
remembers the earlier discussion through the running summary and the last ten minutes of transcript.
After a short network drop the page reconnects on its own, up to five attempts.

After a server restart, diarization has to learn the voices again, so a returning speaker may get a
new label. Use **合并到…** (Merge into…) on the speaker chip to merge the two.

**Following along.** Open the same address on another device to watch a live meeting without
taking it over. Captions, speaker changes, screenshots and tasks appear there as they happen, and
that device records nothing. Typed questions from it still reach the assistant; the answer appears
in the captions on every device and is never spoken aloud. **在这台设备上继续** (Continue on this
device) moves the connection there. While other devices are watching, the meeting page shows
**N 台设备在旁观** (N devices watching) next to the meeting's state; click it to see each device
(for example _iPhone · Safari_) and since when it has been watching.

If the host's connection is interrupted, leave the meeting open: following devices keep listening
for its return and show new captions automatically when the host resumes. Typed questions are
disabled during the interruption. Following stops when the meeting ends or is deleted, you choose
another meeting, start recording on this device, or close the meeting page.

**Microphone.** During a meeting, **麦克风** (Microphone) in the top bar mutes the microphone; it then
reads **已静音** (Muted) until you click it again. The arrow next to it picks the microphone and the
speaker, and the bars on the button move with the sound it picks up.

**Appearance.** **主题** (Theme) in the top bar switches between **跟随系统** (Follow system),
**浅色** (Light) and **深色** (Dark). The choice is kept in this browser and also applies to the
password page.

**Language.** The interface follows the browser's language preference: Chinese for a browser that
prefers Chinese, English otherwise. The language button in the top bar (**中文** or **EN**) switches
between **跟随浏览器** (Follow browser), **中文** and **English**; the choice is kept in this browser,
applies at once and does not interrupt a running meeting. Dates and numbers follow the chosen
language, and so do the notices and error messages the server sends and the reason a background
task or report failed. Captions, the assistant's answers and reports stay in the language they were
produced in.

**Presenting.** On a wide window, **演示模式** (Present) in the top bar turns the page into
a dark full-screen view for a meeting-room display: the meeting title, the last three captions in
large type, and the assistant's state and answer. The newest caption is always shown. Press `Esc` or
**退出演示** (Exit presentation) to go back; audio keeps playing while presenting. If the browser
does not allow full screen, the view fills the window instead.

**On a phone.** On a narrow screen the meeting is split into tabs: **字幕** (Captions, with the
report), **助理** (Assistant), **任务** (Tasks) and **画面** (Screen). A dot on the assistant tab
means there are notices, and the number on the tasks tab counts unfinished tasks. The browser's
**Add to Home Screen** (Safari) or **Install app** (Chrome) puts the page on the home screen, where
it opens without the browser's address bar.

**Recording on a phone.** A phone can hold the meeting like a computer, but phone systems stop the
microphone when the screen locks or the page goes to the background, so keep the page in front.
While recording, a yellow line under the tabs says so, and the page keeps the screen on if the
browser allows it; if it cannot, the line says so too and a longer auto-lock time helps. When the
microphone was stopped for three seconds or more, a notice says for how long; nothing said in that
time is in the captions. A long break can also drop the connection, which then reconnects on its
own as on a computer.

| Phone browser             | Keeps the screen on         | Locked screen or another app in front                  |
| ------------------------- | --------------------------- | ------------------------------------------------------ |
| Safari on iPhone and iPad | Yes                         | The microphone stops until the page is back in front   |
| Chrome on Android         | Yes                         | Chrome may keep recording, depending on power settings |
| Other browsers            | When they support Wake Lock | Assume the microphone stops                            |

On every phone the assistant answers typed questions in text and speaks spoken ones; if its voice
does not play, tap **打开助理声音** (Turn on assistant voice). Screen sharing is not available.

**Keyboard and screen readers.** Every control can be reached with `Tab` and shows a focus ring.
When no text box, menu or tab has focus, `↑` `↓` scroll the captions (or the report) and `←` `→`
pick the previous or next screenshot; `Esc` closes dialogs and presentation mode. Screen readers announce new notices and the answer to a typed question; spoken
answers are heard anyway, and captions are not read out so that they do not talk over the meeting.
Text meets the WCAG AA contrast ratio in both themes, and animations are skipped when the system
asks for reduced motion.

## Captions and speakers

Each caption starts with a line naming the speaker, the wall-clock time in the browser's time zone
and the elapsed meeting time (`+12:04`); the text follows below. The assistant's answers have a violet
background. While you scroll back through earlier captions, new ones do not pull the view down;
**跟随最新** (Follow latest) returns to the newest caption. Short pauses do not split a caption: consecutive segments from the same
speaker within two seconds are merged into one row.

Speakers appear as chips above the captions.

- **Rename** — click a chip and type a name, or click one of the names from `session.members`
  shown next to the input. Names already used by another speaker are left out. The new name
  applies to the whole meeting, including what the assistant sees.
- **Merge** — **合并到…** moves all of one speaker's captions to another speaker. This cannot be
  undone.
- **Reassign individual captions** — click the speaker-and-time line of a caption to select it, or drag across rows to select a range. The speaker chips turn into targets: click one
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
Messages sent while the assistant is busy are queued. A device that is only following the meeting
can ask too; its answer appears in the captions.

## Screen sharing

Click **共享屏幕** (Share screen) in the screen panel under the captions and choose a window or
display. A screenshot is taken whenever the picture changes, and once a minute otherwise. Thumbnails
appear on the timeline; scroll the strip with the mouse wheel, click a thumbnail to enlarge it, and
step through with `←` `→` or **上一张** (Previous) and **下一张** (Next). A short summary is
generated for each new picture and shown next to the panel's title for the framed thumbnail. That
is the latest one until you pick another with `←` `→` (when no text box or menu has focus) or by
viewing it enlarged; moving back to the last one follows new screenshots again.

Screenshots are uploaded only while a meeting is live. From another device this requires HTTPS.
Phone browsers cannot share the screen, so the button is missing there; screens shared from a
computer still appear on the phone.

## Background tasks

Ask for something that takes longer — "Nova, look up the citation count of this paper", "plot these
three numbers as a bar chart" — and the assistant acknowledges the request, hands it to the
background agent and reports the result when it is done, waiting for a pause in the conversation.
Tasks requested by typing are reported in text only.

An explicit request to hand work to the background agent also covers preparing a document from
the meeting; asking for an immediate summary is separate. The assistant confirms delegation only
after a task has been created. The **后台任务** (Background tasks) panel stays visible on desktop
even before the first task, so an empty panel means no task has been created for that meeting.

During the meeting, "note this in the minutes" or a correction gets a brief acknowledgement;
it does not create a background task to edit the eventual report. The running summary is context
for the assistant, while the report is generated afterwards. A separately requested document draft
is a background task; its result does not edit the application's meeting records.

The task panel shows progress, with an icon for each state (queued, running, done, failed,
cancelled). Open a task to see the detailed result, sources, generated files and
**exactly what was sent to the remote model**. Running tasks can be cancelled, and you can ask the
assistant how a task is going. When the window is short, the task panel gives up space first and
scrolls, so the assistant's answer always keeps at least four lines.

Running code requires the Docker sandbox; without it the agent can still search and read images.

## Report and export

**Report.** After a meeting has ended or been interrupted, switch to the **报告** (Report) tab and
click **生成报告** (Generate report). The report has six sections — summary, discussion points,
decisions, action items, background tasks and a screenshot index — shown section by section with
the action items as a checklist, and can be downloaded as Markdown or regenerated. On a wide window a
**会议资料** (Meeting materials) card next to the report counts the utterances, tasks and screenshots
and links back to the captions and to the full archive.

**Export.** **导出** (Export) next to the meeting title offers three downloads, also available while
the meeting is live:

| Item                      | Contents                                                                             |
| ------------------------- | ------------------------------------------------------------------------------------ |
| **转录** (Transcript)     | Markdown: running summary, captions and screen summaries in time order, task results |
| **JSON**                  | All structured data                                                                  |
| **完整包** (Full archive) | ZIP with the transcript, JSON, report, every screenshot and all task artifacts       |
