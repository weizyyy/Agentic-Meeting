# Prompts / 提示词

Plain-text prompts used by the models. Edit them freely; changes take effect after a restart.
The prompts are written in Chinese for meetings held mostly in Mandarin with English terms mixed in.

本目录下是各模型使用的提示词，均为纯文本，可以直接修改，重启应用后生效。
提示词以中文编写，面向以中文为主、夹杂英文术语的会议。

| File / 文件          | Used for / 用途                                                                                                | Variables / 变量                                                                     |
| -------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `realtime_system.md` | System prompt of the realtime LLM / 实时模型的系统提示词                                                       | `{assistant_name}`, `{task_section}`                                                 |
| `realtime_tasks.md`  | Section about the task tools, included when `agent.enabled = true` / 任务工具的说明，仅在启用后台 agent 时并入 | —                                                                                    |
| `digest.md`          | Running summary / 滚动纪要                                                                                     | `{previous_digest}`, `{new_transcript}`                                              |
| `screen_caption.md`  | Screenshot summary / 画面摘要                                                                                  | —                                                                                    |
| `agent_system.md`    | System prompt of the background agent / 后台 agent 的系统提示词                                                | —                                                                                    |
| `report.md`          | Post-meeting report / 会后报告                                                                                 | `{meeting_info}`, `{digest}`, `{material_name}`, `{material}`, `{tasks}`, `{frames}` |
| `report_section.md`  | Key points of one section of a long transcript / 长转录的分段要点                                              | `{part}`, `{total}`, `{span}`, `{transcript}`                                        |

## Notes

- Variables are substituted with Python's `str.format_map`. Write literal braces as `{{` and `}}`,
  as the JSON example in `agent_system.md` does.
- Prompts must not contain model names.
- `digest.md` produces a **cumulative** summary: the model receives the previous summary and the new
  transcript and returns the complete updated summary. The length limit in the prompt keeps the
  summary from growing without bound; earlier details are compressed over time and remain available
  through recall.
- In `report.md`, `{material}` is either the full transcript or, for long meetings, the key points
  of each section; `{material_name}` says which. The report heading is written by code. The page
  shows the report as pre-formatted text, so the prompt allows only `##` headings and `-` lists.
- `agent_system.md` asks for plain text without Markdown in `detail_md`, which the task panel shows
  verbatim.
- `screen_caption.md` defines the exact reply for a picture unrelated to the meeting. The code
  recognizes it by the constant `IRRELEVANT_CAPTION` in `screen/caption.py`; change both together.
- After editing `realtime_system.md`, run `scripts/eval_realtime_model.py` to check that tool
  selection has not shifted.

## 说明

- 变量通过 Python 的 `str.format_map` 代入。正文中的花括号需写成 `{{` 和 `}}`，
  `agent_system.md` 中的 JSON 示例即是如此。
- 提示词中不出现模型名。
- `digest.md` 生成的是**累积**纪要：模型收到此前的纪要和新增的转录，返回更新后的完整纪要。
  提示词中的字数上限使纪要不会无限增长；较早的细节会逐步压缩，仍可通过召回工具查到。
- `report.md` 中的 `{material}` 可能是完整的转录，也可能是长会议各段的要点，由 `{material_name}` 说明。
  报告的标题部分由代码生成。页面以保留格式的纯文本显示报告，因此提示词只允许使用 `##` 标题和 `-` 列表。
- `agent_system.md` 要求 `detail_md` 为不含 Markdown 标记的纯文本，任务面板会原样显示。
- `screen_caption.md` 规定了与会议无关的画面的固定回复。代码通过 `screen/caption.py` 中的常量
  `IRRELEVANT_CAPTION` 识别它，修改时两处需同步。
- 修改 `realtime_system.md` 之后，请运行 `scripts/eval_realtime_model.py`，确认工具选择没有发生偏移。
