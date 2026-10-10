# 接口参考

[English](../interfaces.md) · **简体中文**

本文定义模块之间、服务端与浏览器之间、应用与推理服务之间交换的数据格式。实现以本文为准；
接口需要调整时，先修改本文。

- [1. 配置](#1-配置)
- [2. 数据库](#2-数据库)
- [3. 流式识别](#3-流式识别)
- [4. 说话人区分](#4-说话人区分)
- [5. HTTP 接口](#5-http-接口)
- [6. 数据通道消息](#6-数据通道消息)
- [7. 实时模型的工具](#7-实时模型的工具)
- [8. 后台任务](#8-后台任务)
- [9. 推理服务的命令行](#9-推理服务的命令行)

## 1. 配置

唯一的事实来源是 [`src/agentic_meeting/config.py`](../../src/agentic_meeting/config.py) 里的
pydantic 模型；模板是 [`config/config.example.toml`](../../config/config.example.toml)。
各配置项的说明见 [configuration.md](configuration.md)。

| 配置段         | 内容                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session`      | 助理名字（即唤醒词，必须是英文单词）、别名（`wake_aliases`：识别把名字写成的其他拼写，或听成的至少两个汉字；只用来唤醒，不作为识别热词）、识别热词、成员名单、数据目录                                                                                                                                                                                                                                                    |
| `server`       | 监听地址与端口、HTTPS 证书、ICE 服务器、访问口令与登录有效期                                                                                                                                                                                                                                                                                                                                                              |
| `realtime_llm` | 接入方式 `mode`，以及两种方式各自的一整套设置：`[realtime_llm.llama_server]`（地址、模型名字段、是否识图、思考开关、采样参数、槽位分工、启动参数）和 `[realtime_llm.openai_api]`（地址、密钥变量名、模型名、是否识图、是否认识 developer 角色、是否预热、附加请求字段、采样参数）                                                                                                                                         |
| `asr`          | 识别后端、提示词格式档案、步长与窗口、每步不定稿的 token 数、句首回补时长（`preroll_ms`，默认 1500 毫秒）、启动参数                                                                                                                                                                                                                                                                                                       |
| `diarization`  | 后端、动态库路径、权重路径、显卡序号、分段阈值                                                                                                                                                                                                                                                                                                                                                                            |
| `tts`          | 地址、模型名字段、音色、语言、启动参数                                                                                                                                                                                                                                                                                                                                                                                    |
| `embedding`    | 地址、模型名字段、向量维度、查询前缀（`query_prefix`，召回时加在查询前面的任务指令，默认空）、相关度门槛（`min_similarity`，余弦相似度，默认 0.4，0 = 不设）、启动参数                                                                                                                                                                                                                                                    |
| `audio`        | 入口收音增强：自动增益开关、起始增益、增益上限、目标电平、静音线、电平日志间隔（`audio/gain.py`，architecture.md §4）                                                                                                                                                                                                                                                                                                     |
| `turn`         | 静音阈值、语音检测的音量门限（`vad_min_volume`，默认 0.6 ≈ −50 LUFS，0 = 关闭）、轮次结束模型开关、唤醒窗口（`wake_timeout_secs`，默认 30 秒，必须大于 0；`single_activation` 下助理一答完就回到待唤醒状态，它是「从叫名字到答完」的上限，也是助理说话时能用声音打断的时间）、是否每轮都要叫名字                                                                                                                          |
| `realtime`     | 上下文预算、压缩保留时长、纪要间隔、纪要由谁写（`digest_provider`：`realtime_llm` 或 `agent_llm`，默认前者）、预热间隔、允许直连的 MCP 工具                                                                                                                                                                                                                                                                               |
| `screen`       | 截图间隔与阈值、是否生成摘要、摘要由谁生成                                                                                                                                                                                                                                                                                                                                                                                |
| `transcript`   | 发言怎么分条：同一个人两段之间停顿不超过 `merge_gap_secs`（默认 2 秒，0 = 不并）就并成一条；一条已有 `merge_soft_chars`（40）个字并且停在句末时另起一条；一条最多 `merge_max_chars`（200）个字。整段可以不写                                                                                                                                                                                                              |
| `report`       | 会后报告由谁写（`provider`：`realtime_llm` 或 `agent_llm`，默认前者）、一次请求最多放多少字的转录（`max_input_chars`，默认 8000，超过就分段提要点再合并）。整段可以不写                                                                                                                                                                                                                                                   |
| `agent`        | 远端模型地址与密钥变量名、每个请求都并入的字段（`extra_body`，一般用来指定思考的强度，如 `{ reasoning_effort = "medium" }`；后台任务和直接生成都带）、直接生成（会后报告、滚动纪要、画面摘要）时的输出上限（`generation_max_tokens`，默认 16384；思考也算在里面）、要不要把截图原图也交给它（`attach_frames`，默认否；`max_attached_frames`，默认 40，规则见 architecture.md §5.3）、MCP 服务器列表、沙箱设置、并发与超时 |

规则：

- 未知字段一律报错（`extra="forbid"`）。新增配置项时同步修改 `config.py`、配置模板、
  [configuration.md](configuration.md)，并补充测试。
- 配置里只写密钥的**环境变量名**（字段名以 `_env` 结尾），值用 `config.secret()` 读取。
- 相对路径一律用 `AppConfig.resolve()` 解析（相对仓库根目录）。
- 业务代码不按实时模型的接入方式分支。`RealtimeLLMConfig` 提供以下成员：

  | 成员                                   | 含义                                                                                       |
  | -------------------------------------- | ------------------------------------------------------------------------------------------ |
  | `active`                               | 当前选中的那一套设置（`base_url`、`api_key_env`、`model`、`supports_vision`、`sampling`…） |
  | `managed`                              | 是否由进程管理器启动实时模型服务（只有 `llama_server` 方式且 `launch.enabled` 时为真）     |
  | `has_health_endpoint`                  | 只读探测是否使用地址源的专用 `/health` 端点，与进程由谁启动无关                            |
  | `request_extra_body(background=False)` | 每次请求要放进 `extra_body` 的字段，已按接入方式合并好                                     |
  | `cache_warm`                           | 是否做缓存预热                                                                             |
  | `supports_developer_role`              | 服务端是否认识 `developer` 角色                                                            |

  只有进程管理器（要不要生成启动命令）需要直接看 `mode` 和 `llama_server.launch`。

  `llama_server` 的 `has_health_endpoint` 为真，包括外部启动的服务。其他方式使用选中的
  `active.base_url` 加 `/models`，即使地址为回环地址也一样；任意 HTTP 响应仅表示可达。
  此属性不新增配置字段或 HTTP 字段。

- `config.check_warnings(cfg)` 返回不妨碍启动、但运维者应当知情的事项（目前是数据外发）。
  `check` 与 `serve` 都会打印这些事项，浏览器连接后各收到一条 `notice` 消息。
- **模型相关的字符串不进代码**。识别模型的对话模板、前缀写法、输出标记放在
  `config/asr_profiles/*.toml`（由 `load_asr_profile()` 读取）；各类提示词放在 `config/prompts/*.md`。
  `tests/test_config.py` 里有一条测试会扫描 `src/`，发现模型名即失败。

## 2. 数据库

结构定义在 [`src/agentic_meeting/store/schema.sql`](../../src/agentic_meeting/store/schema.sql)。
数据库文件：`<data_dir>/meetings.db`；截图：`<data_dir>/sessions/<会话id>/frames/<序号>.webp`；
任务工作目录：`<data_dir>/sessions/<会话id>/tasks/<任务id>/`；日志：`<data_dir>/logs/`。

### 2.1 连接初始化

每个连接建立后执行：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
```

并加载 sqlite-vec 扩展：

```python
import sqlite_vec
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
```

用 aiosqlite 时，上面三行要放进 `await db._execute(...)` 能到达的同一线程里执行——最简单的写法是
`await db.enable_load_extension(True)`、`await db.load_extension(sqlite_vec.loadable_path())`。

向量表在建库时按配置的维度创建，行号等于 `utterances.id`：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS utterances_vec USING vec0(embedding float[<dimensions>]);
```

数据库中已有的向量表维度与配置不一致时，启动会报错并给出说明；向量表不会被自动删除。

### 2.2 向量写入与查询

```python
import struct
blob = struct.pack(f"{len(vec)}f", *vec)                      # float32 小端
conn.execute("INSERT INTO utterances_vec(rowid, embedding) VALUES (?, ?)", (utt_id, blob))
rows = conn.execute(
    "SELECT rowid, distance FROM utterances_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
    (query_blob, k),
).fetchall()
```

嵌入是异步回填的：发言落库时 `embedded = 0`，后台任务批量取未嵌入的发言、调嵌入服务、写向量表、
置 `embedded = 1`。嵌入服务不可用时跳过，不影响落库。

### 2.3 全文检索

`utterances_fts` 用 trigram 分词：

- 查询词 ≥ 3 个字符：`SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?`，参数用双引号包住
  （`'"验证集"'`），避免查询词里的符号被当成 FTS 语法。
- 查询词只有 1–2 个字符：trigram 查不到，退回 `text LIKE '%词%'`（加上会话与时间条件缩小范围）。

### 2.4 召回查询的语义

`recall`（见 §7）的实现顺序：

1. 先用结构化条件过滤：会话、说话人（按显示名或编号）、时间范围。
2. 有查询词时，在过滤结果里做关键词检索（§2.3）与向量检索（§2.2）并合并去重；
   没有查询词时按时间顺序返回。
3. 返回条数有上限（默认 20），每条带 `t_start`、说话人显示名、文本。

实现细节（`store/db.py`、`store/embeddings.py`）：

- 向量检索的过滤用 `rowid IN (子查询)`：先按会话、说话人、时间筛出发言编号，
  再在其中取最近的 `k` 条，所以不会越过会话边界，也不需要改向量表的结构。只查已经回填的发言（`embedded = 1`）。
- 向量一路最多贡献 `VECTOR_TOP_K = 8` 条，并且有相关度门槛：余弦相似度低于 `embedding.min_similarity`（默认 0.4，经验值）
  的不算命中。向量在客户端统一归一化成单位长度，门槛换算成向量表里的欧氏距离上限 `sqrt(2 − 2s)`。合并：关键词结果按从新到旧、
  向量结果按从近到远**轮流取**到 `limit` 条，去重后按时间升序返回——哪一路都不会被另一路挤掉。
- 查询词的嵌入限时 0.3 秒（实测 CPU 上约 120 毫秒），超时或嵌入服务不可用就只用关键词结果。
  查询前面加配置的 `embedding.query_prefix`（被检索的发言不加）。
- 回填：每 3 秒一批（16 条），有积压时连续处理；嵌入服务出错时退避重试（最长 60 秒），一次故障只记一行日志。
  返回的向量维度与配置不符是配置问题，记一条错误后停止回填（重试没有用）。
- `vec0` 不支持 `INSERT OR REPLACE`，重写向量时先删再插。

### 2.5 会话、连接与报告

会话的生命周期见 [architecture.md](architecture.md) §3.1。落到数据库上：

- `sessions.last_active_at`：最近一次有连接挂上、断开或写入发言的时间，会议列表按它倒序。
- `session_connections`：每次连接一行——`connected_at` / `disconnected_at`（Unix 秒）、`t_from` / `t_to`（会话时间轴秒）。
  用来显示「中断了多久」，也是会议实际时长（各段 `t_to − t_from` 之和）的来源。
- 会话**状态不存列**：`ended_at` 非空 = 已结束；为空时，应用内存里有活动连接 = 进行中，否则 = 已中断。
- `digests`（滚动纪要，`pipeline/digest.py`）：`text` 是**累积**的——每一份都覆盖从会议开头到当时，压缩上下文时只取最新一份；
  `t_from` / `t_to` 只记这一次新纳入的那一段，相邻两份首尾相接。`last_utterance_id` 记这份纪要已经纳入到哪条发言：
  「新发言」按编号算而不是按时间（助理的话一轮结束才落库，开始时间可能早于上一份纪要的终点）。旧库没有这一列，
  打开时由 `Store._migrate` 补上。每 `realtime.digest_interval_minutes` 生成一次（没有新发言不调模型）；
  一次最多纳入 300 条发言，积压多时分几轮追上；连接断开或会议结束后在后台再补一次（限时 120 秒，不阻塞「结束会议」的响应）。
  喂给模型的「新增转录」里按时间夹着这一段的画面摘要行。
- `reports`（会后报告）：`id, session_id, created_at, status('running'|'done'|'failed'), provider, text_md, error`；
  同一会话可以有多份，页面显示最近一份，导出取最近一份 `done` 的。服务启动时把遗留的 `running` 标为 `failed`。
- `speakers.idx` 的保留值：`0` 未知、`-1` 助理、`-2` 键入的文字（默认显示名「文字输入」，可改名）；说话人区分的输出从 `1` 起。
- `utterances.source`：`'asr'`（语音识别）、`'assistant'`（助理自己的话）、`'text'`（浏览器输入框键入）。
- `tasks.modality`：`'voice'` / `'text'`，委托任务的那一轮是语音还是文字，任务完成后的播报据此决定出不出声。

## 3. 流式识别

### 3.1 后端接口

定义在 [`src/agentic_meeting/asr/base.py`](../../src/agentic_meeting/asr/base.py)（`StreamingASR` 协议）；
输出类型 `ASRDelta` 在 [`types.py`](../../src/agentic_meeting/types.py)。

### 3.2 llama-server 后端的算法

实现在 `src/agentic_meeting/asr/llama_server.py`。下面的每条规则都对应一种效果更差的简化写法，
相关的实测见 [benchmarks.md](benchmarks.md#流式识别)。

算法沿用识别模型上游的流式实现：

- `third_party/Confucius4-R2T2/r2t2_llama/llama_native_backend.py`：`LlamaServerClient.generate`（请求怎么拼）。
- `third_party/Confucius4-R2T2/r2t2/r2t2_asr.py`：`streaming_transcribe_no_reset`（滚动窗口、按 token 回退）。
- `third_party/Confucius4-R2T2/ws_server.py`：每步的 token 预算（搜 `max_new_tokens`）、复读检测。

**状态**（每路音频流一份）：

| 变量          | 含义                                                                          |
| ------------- | ----------------------------------------------------------------------------- |
| `pending`     | 已收到、还没凑够一步的音频（float32，16 kHz）                                 |
| `window`      | 当前音频窗口                                                                  |
| `steps`       | 列表，元素为 `(本步新增采样数, 本步新定稿文字)`，与 `window` 里的音频一一对应 |
| `prefix_text` | `steps` 里所有定稿文字的拼接，即当前窗口对应的已定稿文字                      |
| `unstable`    | 上一步留下的未定稿尾巴（只用于显示，下一步会重新生成）                        |
| `busy`        | 是否有请求在途（同一时刻只允许一个请求）                                      |

**一步的流程**（当 `pending` 攒够 `chunk_ms`，且没有请求在途时触发）：

1. **取音频**。从 `pending` 取出已攒的音频作为本步新增（落后时自动合并多块，上限 3 倍步长，多的留到下一步），
   追加到 `window`。
2. **滑动窗口**。若 `window` 超过 `window_secs`：从 `steps` 头部依次弹出，直到弹出的采样数累计 ≥
   `window_drop_secs` 对应的采样数；`window` 砍掉**同样多**的采样（必须按弹出的实际累计值砍，
   保证音频与文字对齐）。
3. **算 token 预算**。这一步最多让模型生成几个 token：
   `base = max(1, 本步新增采样数 // 1280)`（每 80 毫秒音频 1 个 token）；
   上一个定稿字符是汉字时翻倍（段首还没有定稿文字时，识别语言是中文就翻倍）；
   再与 `max(4, 2 * base)` 和 `asr.max_new_tokens` 取最小。
   不能使用固定的大预算：模型按「只输出已稳定的内容」训练，预算宽松时会抢先输出，甚至照抄热词。
4. **请求**（§3.3）。assistant 前缀 = 档案里的 `assistant_prefix`（代入语言）+ `prefix_text`；
   `max_tokens` = 上一步算出的预算。
5. **清洗续写文字** `cont`：去掉 Unicode 替换字符（U+FFFD）；在档案的 `cut_markers` 任一字符处截断；
   若识别语言是中文，去掉相邻汉字之间的空格（保留英文单词间的空格）。
6. **照抄热词的防护**（只在 `prefix_text` 为空，即段首时检查）：若 `cont` 以「从第一个热词起、
   按顺序连续两个以上热词」开头（不区分大小写，热词之间允许空白和顿号逗号），说明模型在念 system 里的热词表。
   非收尾的步：整步作废——不定稿、不显示，`steps.append((本步采样数, ""))` 后直接返回空增量。
   收尾的步：只去掉照抄的那一段，其余照常处理。
7. **按 token 回退，确定定稿位置**。`full = prefix_text + cont`。调 `POST {asr.base_url}/tokenize`
   （请求体 `{"content": full, "add_special": false, "with_pieces": true}`）拿到 `full` 的 token 切分，
   从末尾去掉 `unfixed_tokens` 个 token，剩下的拼起来就是可定稿的部分。三个细节：
   - 每个 token 的 `piece` 可能是字符串，也可能是字节数组（一个汉字被拆成两个 token 时）。
     统一转成字节后再拼接解码；解码失败说明切在了字符中间，继续往前退一个 token。
   - 切点落在英文单词或数字中间时继续往前退（**一个英文单词不能被拆进两条增量**，唤醒匹配依赖这一点）。
   - 定稿位置不能小于 `len(prefix_text)`（已定稿的不回改）；拼回的文本与 `full` 不一致时本步不定稿。

   **必须按 token 回退，不能按字符。** 按字符会把一个 token 切成两半（例如把「现在」切成「现」），
   模型接着半个词续写时会丢字、乱加标点。

8. **产出**。`stable_new = full[len(prefix_text):定稿位置]`，`unstable = full[定稿位置:]`；
   `steps.append((本步采样数, stable_new))`；发出
   `ASRDelta(stable_text=stable_new, unstable_text=unstable, audio_end_secs=调用方传入的最新值)`。

**收尾 `flush()`**：等在途请求结束；把 `pending` 里剩下的音频全部并入窗口，再做一步，这一步的 token 预算
放开到 `asr.max_new_tokens`、回退 0 个 token（全部定稿）；发出带 `segment_end=True` 的增量；
清空 `window`、`steps`、`unstable`。没有任何音频时也要发一条空的 `segment_end` 增量。

**启动 `start()`**：先发一次预热请求（1 秒静音，前缀为空）并等它返回。通常只需 0.2 秒；
在一台机器上最初几次使用时可能需要十几秒（见 benchmarks.md），不预热的话会议开头的识别会全部积压。

**异常与保护**：

- 请求失败（连接失败、超时、非 2xx、返回格式不对，聊天请求和分词请求都算）：本步整体回滚——
  取走的音频放回 `pending` 最前面（失败期间新到的排在后面）、`window` 和 `steps` 恢复原样——然后按
  0.1、0.2、0.4… 秒（上限 2 秒）退避重试，重试时连同新到的音频一起送。连续失败 5 次后，`deltas()` 在取完
  已有增量之后抛出 `ASRBackendError`，后端停止处理音频（`push_audio` / `flush` 此后静默忽略，**不抛异常**，
  音频计时由调用方负责）。失败只经 `deltas()` 一条通道报告。成功一次即清零计数。
  调用方可以再调一次 `start()` 让同一个实例重新开始：清空全部内部状态、重新预热。
- 复读保护：若 `cont` 里同一个短片段（1–6 个字符）连续重复 6 次以上，视为幻觉——丢弃 `cont`，
  清空 `window`、`steps`、`unstable`。官方更完整的检测见 `ws_server.py` 的 `detect_hallucination`。
- 追不上实时有两道阈值：`pending` 积压超过 5 秒时记录告警（每次进入积压状态告警一次）；
  超过 `window_secs` 时丢掉最早的，只保留最近 `window_secs` 的音频。
- `ASRDelta.audio_end_secs` 取**发起该步请求时**调用方给的最新值。积压时（`pending` 里还留着音频）
  它会比这一步实际吃到的位置靠前。
- 没有新文字（续写为空）的步不调分词接口，结果与调用了完全相同。

可调的三个配置项及其取舍：`asr.chunk_ms`（步长；越小字幕越跟手、GPU 占用越高）、
`asr.unfixed_tokens`（每步留作不定稿的 token 数；默认 1，与官方一致；调大更稳但字幕更慢）、
`asr.window_secs` / `asr.window_drop_secs`（音频窗口；与官方一致，一般不动）。

### 3.3 请求格式

`POST {asr.base_url}/v1/chat/completions`（`llama-server` 需带音频投影文件启动）：

```json
{
  "messages": [
    {"role": "system", "content": "<热词串，可为空字符串>"},
    {"role": "user", "content": [
      {"type": "input_audio", "input_audio": {"data": "<WAV 的 base64>", "format": "wav"}}
    ]},
    {"role": "assistant", "content": "<assistant 前缀>"}
  ],
  "temperature": 0.0,
  "max_tokens": 8,
  "stream": false
}
```

- 音频：把 `window`（float32）写成 16 kHz 单声道 16 位 WAV（`soundfile.write(buf, data, 16000, format="WAV", subtype="PCM_16")`）。
- `max_tokens`：流式的每一步用 §3.2 第 3 步算出的预算（通常 4–8）；只有收尾那一步用 `asr.max_new_tokens`。
- 热词串：`session.hotwords` 加上助理的名字，按档案中的模板与连接符拼接。唤醒词的别名不包含在内。
- 响应的 `choices[0].message.content` **包含前缀本身**；去掉前缀后，剩下的才是续写内容。
- **对话模板在启动识别服务时指定，不随请求发送。** 当前锁定的 `llama-server`（v0.6.0）
  不读取请求体中的 `chat_template` 字段。因此进程管理器把档案中的 `chat_template` 写成文件，用
  `--chat-template-file` 传给识别用的 `llama-server`（见 §9）。
- 最后一条消息是 assistant 时，`llama-server` 默认会接着它续写（启动参数 `--prefill-assistant`
  默认开启），前缀续写依赖这一行为。
- `system` 内容可以是空字符串。

分词接口：`POST {asr.base_url}/tokenize`，请求体 `{"content": "<文本>", "add_special": false, "with_pieces": true}`，
响应 `{"tokens": [{"id": 1234, "piece": "现在"}, ...]}`，其中 `piece` 是字符串，或在不是完整 UTF-8 时是字节值的数组。
这是本机调用，耗时在毫秒级。

### 3.4 识别服务（Pipecat 侧）

`StreamingASRService` 继承 Pipecat 的 `STTService`，职责：

| 输入         | 动作                                                                                                                                                                                                                                             |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 音频帧       | 累加采样计数；没在说话时写入预留缓冲（保留最近 `preroll_ms`，停止说话时清空，所以只含上一段之后的音频），说话中调用后端 `push_audio`；原样放行。回补要够长：「名字，（停一下）要求」里的名字很短，语音检测常到后半句才触发，回补不到名字就唤不醒 |
| 开始说话事件 | 进入「说话中」；先把预留缓冲里的音频**作为一整块**送给后端（`audio_end_secs` = 此刻累计采样数 / 16000），随后清空缓冲                                                                                                                            |
| 停止说话事件 | 调用后端 `flush()`（等它返回，收尾的增量已进队列），然后退出「说话中」                                                                                                                                                                           |
| 后端增量     | 转成转录帧向下游推送（规则见下）                                                                                                                                                                                                                 |

转录帧的规则（括号中的原因对应 Pipecat 1.12.0 的行为）：

1. 每个增量先推一条**临时转录帧**，文本 = 本段已定稿文字 + 未定稿尾巴（供字幕与轮次判定使用）。
2. `stable_text` 去掉首尾空白后非空时推一条**转录帧**，文本 = `stable_text` 原样
   （唤醒策略只看转录帧；每步都推，唤醒才够快）。
3. 段落收尾的那条转录帧设 `finalized=True`
   （轮次结束策略收到 `finalized=True` 的转录后可立即结束轮次，否则要等超时）。
4. **绝不推送文本为空或只有空白的转录帧**（轮次结束策略会记住最后一条转录的文本，为空时不结束轮次；
   模型偶尔会单独吐出一个空格）。只有空白的 `stable_text` 并入下一条非空增量的开头。
   收尾时若没有任何新文字，就不推转录帧。
5. 收尾之后、下一次开始说话之前，不再推临时转录帧
   （临时转录帧会把「转录已定稿」的状态重置）。
6. 每条转录帧构造后设 `frame.includes_inter_frame_spaces = True`
   （否则用户侧聚合器会在各条转录之间各加一个空格，一句中文会变成「这个问题 我们问 一下」）。
7. 两种帧的 `result` 字段都放原始的 `ASRDelta`，`timestamp` 用 Pipecat 的 `time_now_iso8601()`，
   `user_id` 留空字符串（说话人由会议记录器判定，不在这里填）。
8. 构造时传 `ttfs_p99_latency=0.5`，作为没收到 `finalized` 转录时的兜底等待。

**时间轴**：采样计数在 Pipecat 基类可能提前返回的路径（被 `STTMuteFrame` 静音、重连中、服务被标为不可用）
**之前**做，计数永不中断，与会议记录器看到的是同一把尺子（architecture.md §3）。

**后端的启动与失败**：

- 后端在**后台任务**里启动，不阻塞 `StartFrame`：第一次预热可能需要十几秒。启动完成之前到的音频只计数、下传，不送后端。
- `deltas()` 抛 `ASRBackendError`（或后端启动失败）时：停止送音频，向浏览器推一条
  `{"type": "notice", "level": "warn", "text": "识别服务暂时不可用，正在重试"}`，丢掉当前识别段的
  半截状态，然后按 1、2、4…秒（上限 30 秒）退避，对**同一个后端实例**反复 `start()`；成功后推一条
  `level: "info"`、`text: "识别服务已恢复"`。音频计时和音频下传全程不受影响。失败和恢复各提示一次，
  重试过程中不重复提示。
- 处理单条增量时出错只记日志、跳过这一条，不让消费任务退出。
- 这段时间里到的音频不会补识别（只是字幕断了一截，转录计时没有断）。

## 4. 说话人区分

### 4.1 后端接口

定义在 [`src/agentic_meeting/diar/base.py`](../../src/agentic_meeting/diar/base.py)（`Diarizer` 协议）。

### 4.2 动态库绑定

[`diar/nemo_ctypes.py`](../../src/agentic_meeting/diar/nemo_ctypes.py) 包含同步的 `CtypesBinding`
和异步的 `NemoDiarizer`（实现 `Diarizer` 协议，所有底层调用在同一个单线程池里）。工厂 `diar.build_diarizer(cfg, on_notice=…)`：
`backend = "none"` 返回 `NullDiarizer`；动态库或模型加载失败时记录错误、通过 `on_notice` 提醒用户并降级为 `NullDiarizer`。
`segments(since_secs)` 只返回**结束时间晚于** `since_secs` 的分段，按开始时间排序。另有 `finish()`（流结束时让模型标注完最后几帧）
和 `labeled_secs()`（已标注到的时刻）。

C 头文件：`third_party/NeMo-Speech.cpp/include/nemo_speech/diar.h`；用法示例：
`third_party/NeMo-Speech.cpp/examples/diarize_file.cpp`。预编译包里的动态库：
`nemo_speech_asr_c`（Windows 为 `bin/nemo_speech_asr_c.dll`；Linux 与 macOS 上预期位于 `lib/` 下）。

Windows 上加载前必须 `os.add_dll_directory(<bin 目录>)`，否则找不到它依赖的其他动态库。

结构体（字段顺序与类型必须与头文件一致）：

```python
import ctypes as C

class DiarModelConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),               # 必须 = sizeof(本结构体)
        ("model_path", C.c_char_p),         # UTF-8 编码的权重路径
        ("gpu", C.c_int32),                 # 显卡序号；-1 = CPU
        ("preset", C.c_char_p),             # None = 模型默认的低延迟档
        ("chunk_frames", C.c_int32),        # 以下 5 个：<= 0 表示沿用预设
        ("right_context_frames", C.c_int32),
        ("left_context_frames", C.c_int32), # 注意：这个要填 -1 才是沿用预设，0 是有效取值
        ("fifo_frames", C.c_int32),
        ("spkcache_frames", C.c_int32),
        ("update_period_frames", C.c_int32),
    ]

class DiarSegmentationConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("onset", C.c_float), ("offset", C.c_float),
        ("pad_onset_sec", C.c_double), ("pad_offset_sec", C.c_double),
        ("min_gap_sec", C.c_double), ("min_duration_sec", C.c_double),
    ]                                       # 各字段 <= 0 表示用库默认值

class DiarSegment(C.Structure):
    _fields_ = [("start_time", C.c_double), ("end_time", C.c_double), ("speaker", C.c_int32)]
```

函数（返回 `int` 的都是状态码，`0` 表示成功；失败时调 `nemo_speech_asr_last_error()` 取错误文字，
它返回 `const char*`，且是**线程局部**的，必须在出错的同一线程里立刻读）：

| 函数                                 | 参数                                                                                        | 返回     |
| ------------------------------------ | ------------------------------------------------------------------------------------------- | -------- |
| `nemo_speech_diar_create`            | `(DiarModelConfig*, void**)`                                                                | 状态码   |
| `nemo_speech_diar_destroy`           | `(void* model)`                                                                             | 无       |
| `nemo_speech_diar_num_speakers`      | `(void* model)`                                                                             | `int32`  |
| `nemo_speech_diar_seconds_per_frame` | `(void* model)`                                                                             | `double` |
| `nemo_speech_diar_stream_open`       | `(void* model, void** stream)`                                                              | 状态码   |
| `nemo_speech_diar_stream_push_f32`   | `(void* stream, float*, size_t n, int32 sample_rate)`                                       | 状态码   |
| `nemo_speech_diar_stream_finish`     | `(void* stream)`                                                                            | 状态码   |
| `nemo_speech_diar_stream_close`      | `(void* stream)`                                                                            | 无       |
| `nemo_speech_diar_segments`          | `(void* stream, DiarSegmentationConfig*, DiarSegment* out, size_t capacity, size_t* count)` | 状态码   |

每个函数都需要设置 `argtypes` 和 `restype`，否则 64 位指针会被截断。

`nemo_speech_diar_segments` 是两段式调用：先传 `out=None, capacity=0` 取得条数，再按条数分配数组调一次。
说话人编号从 1 开始。

线程约束：同一条流不能并发调用；`push` 是阻塞的 GPU 计算。实现时用一个**只有一个工作线程**的
`ThreadPoolExecutor`，所有底层调用都 `loop.run_in_executor(这个线程池, ...)`。

长会议：库会在约 20 分钟后把较早的逐帧概率压缩成最终分段（`segments` 仍然返回它们），
内存不会无限增长。

`gpu` 序号与 llama.cpp 的 CUDA 编号一致，即 `gpu = 0` 对应 `llama-server --list-devices` 里的
CUDA0。它**不是** `nvidia-smi` 的序号——两者的排序规则不同，可能正好相反。

### 4.3 文字归属与发言切分（`diar/fusion.py`）

输入：按到达顺序的 `ASRDelta`；可随时查询的说话人分段。输出：字幕消息、定稿的 `Utterance`。

1. **每个增量的时间区间**：`[上一个增量的 audio_end_secs, 本增量的 audio_end_secs]`，再整体向前平移一个
   识别延迟估计（常量，0.2 秒）。段内第一个增量的起点取「开始说话」时刻。
2. **归属**：在该区间内，统计每个说话人的累计发声时长，取最长者；区间内没有任何分段时记为
   `SPEAKER_UNKNOWN`，沿用本段上一个增量的说话人。
3. **切分**：段落收尾时结束当前发言；段内相邻两个非空增量归属到不同说话人、且新说话人已持续
   ≥ 0.8 秒时，在此处切开（防止抖动造成碎片）。
4. **落库时机**：发言结束时立即落库（说话人取当时结论）。说话人区分的标注比音频晚约 0.6–1.1 秒，
   所以一句话刚结束时，它最后一秒左右的归属可能还没出来——按已有的部分判定，交给下一条的事后更正兜底。
5. **事后更正**：落库后 5 秒内每隔 1 秒重新计算该发言的归属；若变化，更新数据库并发
   `utterance_update` 消息。超过 5 秒不再更正。
6. **助理语音的处理**：与助理说话时段（从「助理开始说话」到「助理停止说话」后 0.3 秒）重叠超过一半的
   增量直接丢弃，不落库、不进上下文。助理自己的发言由助理侧聚合器的事件落库（`source="assistant"`）。

7. **相邻片段并成一条**：语音检测按停顿切段，而停顿阈值（`turn.vad_stop_secs`）很短——为的是应答快——
   所以一句话中间换口气就会被切成几段，每条记录连一句话都不全。落库时把新定稿的一段并进上一条发言（`should_merge`）：
   - 条件：上一条也是语音识别来的、中间没有隔着助理的话或键入的文字；同一个说话人，间隔（新一段的起点 − 上一条的终点）
     不超过 `transcript.merge_gap_secs`；有一方说话人还是「未知」时间隔要不超过 0.8 秒（别人很快插的一句短话，说话人区分
     往往还没给结论，不能因此并进前一个人的话里）。
   - 什么时候不再并：上一条已有 `merge_soft_chars` 个字并且停在句末（`。！？!?…`）；或上一条已到 `merge_max_chars`。
   - 怎么并：数据库里那一行的文字接上新的一段（中文之间不加空格，两边都是英文或数字时加一个）、结束时间往后、向量清掉重算；
     上一条当时说话人未知而这一段有结论的，整条补上。那条发言已经被滚动纪要纳入时不并（否则纪要看不到新加的部分），照常另存一条。
   - 页面：发一条 `utterance`，`id` 是**原来那条**发言的、`segment_id` 是新片段那行实时字幕的，`text` 是并好的全文——
     页面把灰色的那行收掉，把原来那行的文字换掉。
   - 实时模型的上下文不回改：每个片段照旧各追加一行。压缩或继续会议时从数据库重建，那时就是并好的一行了。
   - 事后更正（第 5 条）按这条发言**当时**的时间范围算，并进新片段之后范围跟着变长。

`TranscriptAssembler` 的实现细节：

- 「沿用上一个增量的说话人」和「未知」的处理：区间里没有分段时沿用本段上一个增量的说话人（包括正在累计的待定说话人）；
  **未知从不触发切分**——发言开头说话人区分还没有结论（标注比音频晚约 1 秒）时，先记为未知，第一个有结论的说话人直接补上，
  不会因为「未知 → 3 号」切成两条。
- 切分用「待定区」实现：换人后，新说话人的增量先挂在待定区，字幕仍显示在原发言那一行；他**连续**说满 0.8 秒就在他开口的那个增量处切开
  （待定区的文字归新发言，`t_start` 取待定区第一个增量的区间起点）；没说满又回到原说话人，或换成了第三个人，待定区的文字并回原发言。
  只有空白的增量不参与归属，跟着最近的去处。
- 平手（两个说话人累计时长相同）取编号小的。区间长度为 0 时用覆盖那个时刻的分段。
- 字幕行 `segment_id`：每关闭一行（换人切开或段落收尾）加一。收尾时这一行没有定稿文字、但屏幕上显示过灰色的临时字幕，
  就发一条文字全空的 `CaptionUpdate` 让界面清掉。
- 上一段还没收尾时「开始说话」就到了（系统帧会插队，见 pipecat-notes.md §10）：记下来，等上一段关闭后再用。
- 被丢弃的回声增量不延长发言的结束时间；若它恰好是段落收尾，仍然关闭已有的发言。

## 5. HTTP 接口

除信令外，全部是 JSON；业务错误返回 `{"error": "<中文说明>"}` 和合适的状态码。
开发时前端跑在 5173 端口并把 `/api` 代理到应用（见 `client/vite.config.ts`）。
就绪检查的 503 响应保留 §5.8 定义的状态快照。配置了访问口令时，中间件保护 `/api`
及其下路径，§5.7 的公开认证接口除外。§5.8 的三个运维 GET 位于 `/api` 之外，保持匿名。

### 5.1 会话与对时

| 方法与路径                                           | 请求               | 响应                                                                                                                         |
| ---------------------------------------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/time`                                      | —                  | `{"server_time": <Unix 秒，浮点>}`                                                                                           |
| `GET /api/sessions?limit=20&before=<last_active_at>` | —                  | `{"items": [会话摘要]}`，按 `last_active_at` 倒序；`before` 用来翻页                                                         |
| `GET /api/sessions/{id}`                             | —                  | 会话摘要 + `"screen": {配置的 screen 段}` + `"connections": [{connected_at, disconnected_at, t_from, t_to}]`；不存在返回 404 |
| `GET /api/session`                                   | —                  | 「当前会话」：活动连接所在的会话；没有就取最近一个未结束的；都没有返回 404。格式同上                                         |
| `PATCH /api/sessions/{id}`                           | `{"title": "..."}` | 更新后的会话摘要                                                                                                             |
| `POST /api/sessions/{id}/end`                        | —                  | `{"id", "ended_at"}`。会话正在进行时同时断开它的连接，并在后台生成最后一份滚动纪要（响应不等它）                             |
| `POST /api/session/end`                              | —                  | 同上，作用于当前会话                                                                                                         |
| `DELETE /api/sessions/{id}`                          | —                  | `{"id"}`。连同截图文件和任务目录一起删；会话正在进行返回 409                                                                 |

**会话摘要**：`{"id", "title", "started_at", "ended_at", "last_active_at", "state": "live" | "interrupted" | "ended",
"duration_secs", "utterance_count", "speakers": ["王老师", …], "preview": [{"speaker", "text"}（最后两条发言）]}`。
`duration_secs` 是各次连接实际时长之和，不含中断的空档；正在进行的那一段算到现在。

补充说明（`web/sessions_api.py`）：`GET /api/sessions` 的参数 `limit`（1–500，默认 20）、`before`；列表响应是 `{"items": [...]}`。`GET /api/session` 与 `GET /api/sessions/{id}` 的响应是会话摘要加上 `screen`、`members`（配置里的成员名单，给说话人改名当候选）和 `connections`（`[{connected_at, disconnected_at, t_from, t_to}]`）。`PATCH` 的 `title` 去首尾空白、最长 200 字，返回更新后的会话摘要。`POST …/end` 返回 `{"id", "ended_at"}`；会话正在进行时，先给页面发 `session_closed(reason="ended")`、取消管线、等它收尾，再写 `ended_at`。找不到会议返回 404 `找不到这场会议`；没有任何会议时 `GET /api/session` 返回 404 `现在没有会议`；`DELETE` 一场进行中的会议返回 409。

对时：浏览器记下发请求前后的本地时间 `t0`、`t1`，则「服务端时间 − 本地时间」的偏移量
≈ `server_time − (t0 + t1) / 2`。连接建立时测 3 次取往返最短的一次。

### 5.2 WebRTC 信令

| 方法与路径         | 说明                                                                                              |
| ------------------ | ------------------------------------------------------------------------------------------------- |
| `POST /api/offer`  | 请求体与响应由 Pipecat 的 `SmallWebRTCRequestHandler` 定义，原样转交                              |
| `PATCH /api/offer` | 追加 ICE 候选，同上                                                                               |
| `GET /api/ice`     | `{"ice_servers": [{"urls": [...], "username"?, "credential"?}]}`：浏览器建立连接时用的 ICE 服务器 |

写法与 Pipecat 自带的运行器一致（见 pipecat-notes.md §2）。客户端在连接参数的 `requestData`
里可带 `{"session_id": "<要继续的会话>"}`：

- 不带：新建会话。
- 带，且会话存在：继续它（已中断或已结束的都可以；已结束的先重新打开）。具体做法见 architecture.md §3.1。
- 带，但会话不存在：在协商之前返回 404 `{"error": "找不到这场会议"}`。
- 已有别的活动连接时，新连接顶替它（同一时刻只有一路）。

实现：`session_id` 为 `null` 等同于不带；不是字符串或是空串返回 400；会议不存在返回 404，两种情况都不会进入协商、
不会建管线。新建不再结束别的会议。服务端在管线里做的事：`SessionManager.attach` 顶替旧连接（哪怕就是这场会议的另一路）、
重新打开已结束的会议、算 `base_secs = max(此刻 − started_at, 时间轴已经用到的地方)` 并写一行连接记录；会议记录器和识别服务
都从 `base_secs` 接着计时；实时模型的上下文由数据库重建（`build_context_messages`，最新纪要 + 最近 `keep_recent_minutes` 分钟），
重建失败就从空的开始；开了预热的话连接建立后立刻预热一次。

实现要点（`web/app.py`）：`POST /api/offer` 不用 FastAPI 的请求体类型，而是读原始 JSON 再交给
`SmallWebRTCRequest.from_dict`——浏览器端 SDK 把连接参数放在**驼峰**的 `requestData` 里，只有 `from_dict`
认识它（也接受 `request_data`）；格式不对返回 400。协商成功后，`run_bot(connection, request_data, resources)`
作为后台任务运行到连接断开。`PATCH /api/offer` 追加 ICE 候选，请求体 `{"pc_id", "candidates": [{"candidate",
"sdp_mid", "sdp_mline_index"}]}`，响应 `{"status": "success"}`。`GET /` 在 `client/dist` 不存在时返回一段
「请先构建客户端」的提示页（503），不是 500。错误一律是 `{"error": "<中文说明>"}`（包括 404、422）。

**ICE 服务器。** `server.ice_servers` 在连接的两端都用：服务端交给 `SmallWebRTCRequestHandler`；浏览器每次自己发起连接之前
取一次 `GET /api/ice`，把结果交给 WebRTC SDK（`SmallWebRTCTransport.iceServers`）。每一项的形状与浏览器的 `RTCIceServer`
相同，只有配置了 `username`、`credential` 的项才带这两个字段。凭据在应用启动时从 `credential_env` 指定的环境变量读出。
能访问 `/api` 的人都能读到它，所以用 TURN 时应当设访问口令（§5.7），并让 TURN 服务器只中继到本机。请求失败时浏览器不带
ICE 服务器连接，同一局域网内照样能连上。列表为空（默认）= 只用本机候选地址。

### 5.3 截图

`POST /api/frames`，`multipart/form-data`：

| 字段          | 类型       | 说明                                                          |
| ------------- | ---------- | ------------------------------------------------------------- |
| `captured_at` | 浮点字符串 | 采集时刻，**已换算成服务端时钟**的 Unix 秒                    |
| `image`       | 文件       | `image/webp` 或 `image/jpeg`，长边不超过 `screen.max_side_px` |

响应 `{"id": <截图编号>, "t": <会话时间轴秒>}`。校验：当前有进行中的会议；文件类型合法；
大小 ≤ 4 MB；`captured_at` 与服务端当前时间相差不超过 60 秒（否则 400）。
宽高由服务端解码图片得到。

`GET /api/frames/{id}/image` 返回图片文件；`GET /api/frames?session_id=` 返回
`{"items": [{"id", "t", "width", "height", "caption", "caption_status"}]}`（按时间升序；`session_id` 省略 = 当前会话）。

补充说明（`screen/ingest.py`、`web/frames_api.py`）：

- 状态码：没有进行中的会议 404；不是图片、不是 WebP / JPEG、长边超过 `screen.max_side_px`、`captured_at` 不是数字或与服务端
  时间相差超过 60 秒 → 400；超过 4 MB → 413；`screen.enabled = false` → 403；缺字段 → 422；落盘失败 → 500（同时撤销数据库里那一行）。
- 文件类型以**解码结果**为准，不看浏览器声称的类型；文件名是 `<6 位截图编号>.webp`（或 `.jpg`）。
- `t = max(0, captured_at − sessions.started_at)`。
- **画面有没有变由服务端再判一次**：把新图缩成 64×36 灰度，与这场会议里上一张「变了」的图比（分成 8×6 的小块，取变化最大的那一块的平均差 / 255，
  阈值 `screen.change_threshold`；浏览器端决定要不要上传用的是同一个算法。不用整张的平均差：白底幻灯片只换了文字时整张只差 1% 左右）。没变的图（浏览器的兜底上传）照常进时间线、照常推 `frame`，但不再单独生成摘要、
  不往实时模型的上下文追加 `[画面]` 行，摘要沿用上一张的。只和上一张「变了」的图比，所以缓慢的累计变化迟早会超过阈值。
  这份「上一张」只在内存里，服务重启后第一张按「变了」处理。
- **画面摘要（`screen/caption.py`）**：每次只做最新一张待处理的截图，积压的记为 `skipped`；助理应答时暂停，
  被取消的那张应答完再做（除非又来了更新的画面）；模型出错或给出空摘要记为 `failed`。成功后写回数据库、推 `frame_caption`、
  向实时模型的上下文追加 `[画面 时:分:秒] 摘要`（`run_llm=False`）。画面没变的截图沿用上一张的摘要并推 `frame_caption`，
  但不再追加上下文行；上一张没有成功的摘要时，它当作新画面重做（相当于每个兜底间隔重试一次）。
  摘要是「无关画面」时不进上下文。会议已经换了一场时，摘要只入库，不推给新的会议。
  `screen.caption_provider = "agent_llm"` 时由后台 agent 的那个模型生成（要求 `agent.supports_vision`，且地址和模型名已填）。
- 图片响应带 `Cache-Control: private, no-cache`：删掉会议后新截图可能复用旧编号，不能让浏览器把图片当成永久不变的。

### 5.4 转录与说话人

下面所有接口的 `session_id` 都可省略，省略时指「当前会话」（§5.1）。

| 方法与路径                                          | 说明                                                                                                                                                                                                                                                          |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/utterances?session_id=&after_id=&limit=`  | id 大于 `after_id` 的发言，按 `id` 升序。重连后补齐字幕用                                                                                                                                                                                                     |
| `GET /api/utterances?session_id=&tail=50`           | **最近 50 条**（仍按 `id` 升序返回）。页面一打开就显示「最近对话」用；与 `after_id` 互斥                                                                                                                                                                      |
| `GET /api/utterances?session_id=&before_id=&limit=` | 向更早的方向翻页，按 `id` 升序返回                                                                                                                                                                                                                            |
| `GET /api/speakers?session_id=`                     | `[{"idx", "display_name"}]`                                                                                                                                                                                                                                   |
| `PUT /api/speakers/{idx}`                           | 请求体 `{"session_id"?, "display_name": "王老师"}`；改名后向浏览器广播 `speaker` 消息                                                                                                                                                                         |
| `POST /api/utterances/speaker`                      | 请求体 `{"session_id"?, "ids": [发言编号…], "speaker_idx": 2}` 或 `{…, "new_speaker": "张老师"}`：把选中的发言改成另一个说话人（已有的，或新建一个）；响应 `{"speaker": {idx, display_name}, "ids": [真正改了的]}`；会议正在进行时逐条广播 `utterance_update` |
| `POST /api/speakers/{idx}/merge`                    | 请求体 `{"session_id"?, "into": 2}`：把 `idx` 的全部发言并入 `into`，删除 `idx`；响应 `{"from", "into", "display_name", "moved"}`；会议正在进行时广播 `speakers_merged`                                                                                       |

发言项：`{"id", "speaker_idx", "speaker_name", "t_start", "t_end", "text", "source"}`；`/api/utterances` 与 `/api/speakers` 的响应都是 `{"items": [...]}`。`limit` 与 `tail` 都是 1–500；`after_id`、`before_id`、`tail` 同时给出返回 400。说话人名字去首尾空白、最长 50 字；`PUT /api/speakers/{idx}` 返回 `{"idx", "display_name"}`，说话人不存在返回 404 `这场会议里没有这个说话人`；改的是正在进行的会议时，同时更新记录器里的名字（之后的字幕和上下文行用新名字）并推 `speaker` 消息。

改发言人：`ids` 是非空的整数列表，一次最多 500 条；`speaker_idx` 和 `new_speaker` 给且只给一个，否则 400。
`speaker_idx` 不能是负数（助理、键入的文字不能当发言人），`0` 表示改成「未知」，其余必须是这场会议里已有的说话人（否则 404）。
只改语音识别来的发言（`source = "asr"`）；助理的话、键入的文字、不属于这场会议的编号被忽略，不算错，响应里的 `ids` 只列真正改了的。
新建的说话人编号从 1000 起（`store/db.py` 的 `MANUAL_SPEAKER_BASE`），和说话人区分给出的编号分开；服务重启后新流的编号偏移
不把它们算进去。会议正在进行、而被改的发言刚落库不到 5 秒时，事后更正（§4.3 第 5 条）可能再把它改回去。
页面上：能改的行，左边的时间 / 说话人那一块可以点（选中 / 取消，按住 Shift 点是连选）、可以按住上下拖（连选一段），文字那一块不参与；
选中之后，字幕上方那排说话人从「点了改名」变成「点了就把选中的发言归过去」，末尾多一个「＋ 新说话人」（`client/src/selection.ts`）。

页面上的时间是两条轨：会话时间轴上的 `t`（会议进行到哪），和它对应的墙上钟点（浏览器所在时区）。后者由页面自己算
（`client/src/timeline.ts`）：`t` 落在哪次连接里，就是那次连接的 `connected_at + (t − t_from)`；对不上任何一次连接时用
`started_at + t`。服务端的数据格式没有变。

合并说话人：只能合并说话人区分给出的编号（`idx`、`into` 都大于 0 且不相同，否则 400）；有一个不存在返回 404。
`idx` 交办过的后台任务也记到 `into` 名下。页面根据一条 `speakers_merged` 消息自行更新已加载的发言。

说话人第一次出现时自动建一条记录，默认显示名为「说话人 N」；`-1` 的显示名是助理的名字；`-2` 是键入的文字，默认「文字输入」。

### 5.5 任务

| 方法与路径                             | 说明                              |
| -------------------------------------- | --------------------------------- |
| `GET /api/tasks?session_id=`           | 任务列表（不含详细结果）          |
| `GET /api/tasks/{id}`                  | 单个任务：全部字段 + 进度事件列表 |
| `POST /api/tasks/{id}/cancel`          | 取消                              |
| `GET /api/tasks/{id}/artifacts/{name}` | 下载产物文件                      |

补充说明（`web/tasks_api.py`）：

- 路径里的 `{id}` 是完整编号（`<会话 id>.t3`）。`GET /api/tasks` 的 `session_id` 省略 = 当前会话，响应 `{"items": [...]}`。
- 列表项：`{"id", "label", "goal", "status", "brief", "error", "modality", "created_at", "started_at", "finished_at"}`。
- 详情在列表项之外还有：`detail_md`、`sources`、`artifacts`（文件名）、`requested_by`（交办人的显示名）、`requested_t`、
  `events`（`[{at, kind, summary}]`，不含工具参数等细节）、`outbound`——**这次任务外发的内容**：
  `{"goal", "t_from", "t_to", "frames": [{id, t}], "model_host"}`。
- `POST …/cancel` 返回取消后的列表项；任务已经结束时原样返回，不算错；后台任务功能没开而任务又没结束时 503。
- 产物：只给任务自己报告过、并且确实取回了任务目录的文件，其余 404。PNG / JPEG / WebP / GIF 按图片返回；
  其他一律 `application/octet-stream` 加 `Content-Disposition: attachment`（产物是远端模型写的代码生成的，
  不让浏览器把它当页面或脚本执行），并带 `X-Content-Type-Options: nosniff`。
- 页面上：右侧「后台任务」面板（有任务才出现），每个任务一行——短编号、状态、目标、最近一步或结论；点开看详细结果
  （按纯文字显示，不解释成 HTML）、图片产物、文件产物、来源（只有 http/https 的做成链接）、进度、外发内容与截图缩略图；
  没结束的任务可以取消。
- Markdown 导出多了「后台任务」一节（有任务才有）：每个任务的目标、状态、结论或原因、详细结果、来源、产物文件的相对路径。
  详细结果是纯文本，放进导出时符号转义、换行保留。

### 5.6 导出与报告

| 方法与路径                          | 说明                                                                                                                        |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/export/{session_id}.md`   | Markdown 转录：标题与时间 → 纪要 → 按时间排列的发言（相邻同一说话人的合并成段）→ 截图引用 → 任务结果                        |
| `GET /api/export/{session_id}.json` | 结构化 JSON：会话、说话人、发言、截图、纪要、任务（含事件）、各次连接、最近一份报告                                         |
| `GET /api/export/{session_id}.zip`  | 完整包：`transcript.md`、`session.json`、`report.md`（若有）、`frames/`、`tasks/` 下的产物                                  |
| `POST /api/sessions/{id}/report`    | 触发生成会后报告（异步）；响应 202 `{"report_id", "status": "running"}`；该会话已有一份在生成时返回 409                     |
| `GET /api/sessions/{id}/report`     | 最近一份：`{"id", "status": "running" \| "done" \| "failed", "created_at", "provider", "text_md", "error"}`；还没有返回 404 |
| `GET /api/sessions/{id}/report.md`  | 最近一份报告的文本下载                                                                                                      |

**Markdown 导出的实现（`web/export.py`）**：

- 响应是 `text/markdown; charset=utf-8`，带 `Content-Disposition: attachment`；文件名取会议标题
  （中文走 `filename*=UTF-8''…`，另给一个纯 ASCII 的后备名 `meeting-<编号前 8 位>.md`）。
- 结构：`# 标题`（没起名字的用「未命名会议 + 开始时间」）→ 开始 / 结束时间、状态、时长、发言人 → `## 纪要`
  （最新一份累积纪要，注明覆盖到哪）→ `## 转录`。
- 转录里发言和画面按时间排在一起。相邻、同一说话人、同一来源、间隔不超过 120 秒的发言合并成一段
  （中文之间直接相接，两边都是英文或数字时加一个空格）；中间夹了别人的发言或画面就另起一段。
  每段开头是 `**说话人** `时:分:秒``，键入的文字在名字后标「（文字）」。
- 画面是引用块：`> **画面** `时:分:秒`：摘要` 加图片 `frames/<文件名>`（相对路径，与压缩包里的布局一致；
  单独下载这份 Markdown 时图片不显示）。连续相同的摘要只出现一次（兜底截图），「无关画面」不导出，没有摘要的只放图。
- 转录文字里的 Markdown 符号会被转义。有后台任务时最后是 `## 后台任务` 一节。
- 其他后缀、缺后缀返回 404；会议不存在返回 404。进行中的会议也可以导出（导出到当时为止）。
- 页面上，会议横幅里有三个导出链接：转录、JSON、完整包。

**JSON 与压缩包（`web/export.py`）**：

- `.json` 的顶层字段：`format_version`（现在是 1）、`session`（`id, title, started_at, ended_at, last_active_at, state, duration_secs`）、
  `connections`（`[{connected_at, disconnected_at, t_from, t_to}]`）、`speakers`（`[{idx, display_name}]`）、
  `utterances`（同 §5.4 的发言项）、`frames`（`[{id, t, file, width, height, caption, caption_status}]`，`file` 是压缩包里的相对路径
  `frames/<文件名>`）、`digests`（全部滚动纪要 `[{t_from, t_to, text, created_at}]`）、`tasks`（任务的全部字段 + `events`，
  `artifacts` 是压缩包里的相对路径 `tasks/<短编号>/<文件名>`）、`report`（最近一份生成好的 `{id, created_at, provider, text_md}`，
  没有是 `null`）。中文不转义，缩进 2 格。时间约定同数据库：`t*` 是会话时间轴的秒，`*_at` 是 Unix 秒。
- `.zip` 的成员依次是：`transcript.md`（和 `.md` 导出一字不差）、`session.json`、`report.md`（有生成好的报告才有）、
  `frames/` 下的截图、`tasks/<短编号>/` 下的任务产物。文字成员压缩，文件成员只打包不压缩（图片本来就压过）。
  用标准库 `zipfile` 往一个只能写的流里写、边读边出，不在内存里攒整个包。
- **不能借导出读到别处的文件**：产物文件名是绝对路径、带 `..` 或带盘符的一律不收（Markdown、JSON 里也不列出）；
  每个成员解析成真实路径后必须落在这场会议自己的目录 `<data_dir>/sessions/<会话 id>/` 里面，并且文件确实存在，否则跳过。
  取完清单之后文件被删掉的也只是跳过那一个。

**会后报告（`pipeline/report.py`、`web/reports_api.py`）**：

- `POST` 返回 202 `{"report_id", "status": "running"}`；会议正在进行返回 409（先结束或断开）；这场会议已有一份在生成返回 409；
  没有可用的模型返回 503；会议不存在 404。失败或完成之后可以再触发，多出一份新的。
- `GET` 返回最近一份（不管状态）；`report.md` 只给最近一份 `done` 的，文件名是「<会议标题> 会后报告.md」。
- 生成步骤：先把滚动纪要补到最后（补不上不拦着）→ 取全部发言、截图、任务、纪要 → 转录不超过 `report.max_input_chars`
  就整份交给模型；超过则分段（写满上限就切；一段已经半满、又跨过某份滚动纪要的时间窗口边界时，在边界处切），
  每段先用 `report_section.md` 提要点，再把各段要点交给 `report.md` 合并。转录里键入的文字在名字后标「（打字）」。
- 报告开头的标题、开始时间、实际时长、发言人、生成时间由代码写；正文是模型写的 Markdown（提示词里只允许「## 标题」和
  「- 列表」两种标记，因为页面是按保留换行的纯文字显示的）。
- 用的是后台模型的入口：助理正在应答时请求会被抢占，等它空下来重发那一次。整份报告限时 15 分钟。
- `report.provider = "agent_llm"` 时用后台 agent 的那个远端模型，`check_warnings` 会提示整场会议的转录要发往那个地址。

生成中的报告页面每 2 秒轮询一次 `GET /api/sessions/{id}/report`，不依赖音频连接。页面上，字幕区上方有「字幕 / 报告」两个页签。

### 5.7 访问口令

需要显式开启。`server.password_env` 留空（默认）时以下内容都不生效：`/api` 不设限，
`GET /api/auth` 返回 `enabled: false`。填写后，启动时从这个环境变量读取口令，`web/auth.py` 里的中间件
拦住 `/api/` 下除 `GET /api/auth` 和 `POST /api/auth/login` 以外的全部路径。静态页面（`/`、`/assets/…`）
不拦截，里面没有会议数据。

| 方法与路径              | 请求                  | 响应                                                                                                                                                                   |
| ----------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/auth`         | —                     | `{"enabled", "authenticated", "csrf_token"}`；未登录或未启用口令时 `csrf_token` 为 `null`                                                                              |
| `POST /api/auth/login`  | `{"password": "..."}` | `{"enabled": true, "authenticated": true, "csrf_token"}`，并下发会话 Cookie。口令不对 401；尝试过多 429，`Retry-After` 给出秒数；请求体超过 16 KiB 413；未启用口令 404 |
| `POST /api/auth/logout` | —                     | `{"enabled": true, "authenticated": false, "csrf_token": null}`，并清除 Cookie                                                                                         |

**受保护的请求。** 没有有效的会话 Cookie 时返回 401 `{"error": "请先登录"}`。`POST`、`PUT`、`PATCH`、
`DELETE` 还要求请求头 `X-CSRF-Token` 等于当前会话的令牌，否则 403。`POST`/`PATCH /api/offer` 也不例外：
客户端把这个请求头交给 WebRTC SDK，SDK 在发送 offer 和 ICE 候选时一并带上。`GET` 和 `HEAD`
只需要 Cookie，所以 `<img>` 图片和下载链接照常可用。

**会话 Cookie。** 名为 `am_session`，属性 `HttpOnly; SameSite=Strict; Path=/`，`Max-Age` 为
`server.auth_session_days`，请求经由 HTTPS 到达时加 `Secure`。取值为 `<到期时刻，Unix 秒>.<随机数>.<签名>`，
签名是对前两段做的 HMAC-SHA256。到期即失效，不做滑动续期。

**密钥。** 首次启动时生成 32 字节随机密钥写入 `<data_dir>/auth_secret`（仅属主可读）。启动时用 scrypt
（`n=2^14, r=8, p=1`）对口令做一次拉伸，以该随机密钥为盐；签名密钥是拉伸结果的 HMAC-SHA256。因此修改口令或删除
这个文件都会让所有设备退出登录；即使 Cookie 和随机密钥同时泄露，每猜一次口令也要完整算一次 scrypt。CSRF 令牌是用同一密钥
对会话随机数做的 HMAC-SHA256，不在任何地方存储。

**登录尝试。** 每次尝试都用同样的 scrypt 参数拉伸（在工作线程里做，同时最多 4 个），再按恒定时间比较。同一客户端地址在任意 5 分钟内最多失败 5 次；超过后返回 429，
直到最早的那次失败移出时间窗口。登录成功会清零该地址的计数。在反向代理之后，只有 uvicorn 信任该代理时
（环境变量 `FORWARDED_ALLOW_IPS`，默认 `127.0.0.1,::1`）才从 `X-Forwarded-For` 取客户端地址。

**登录请求的大小。** 登录接口不需要会话就能访问，所以请求体分块读取，一超过 16 KiB 就返回 413 `{"error": "请求体太大"}`；
`Content-Length` 声明超限的，一个字节都不读就拒绝。超限或格式不对（400）的请求体不计入失败次数。限速检查在前。

**启动检查。** 填写了口令变量名但变量未设置、或口令短于 8 个字符时，`check_ready` 会列为缺项。
`server.host` 不是回环地址又没有配置口令时，`server_warnings(cfg)` 返回一条提醒；配置了 TURN 服务器而没有口令时
（凭据可以经 `GET /api/ice` 读到）再返回一条。它们由 `check` 和 `serve` 打印，不发给浏览器。

已经建立的 WebRTC 连接不会因为 Cookie 到期或在别处退出登录而中断；之后的 HTTP 请求会被拒绝。

### 5.8 健康检查与基础指标

这些运维 GET 接口实现 [#10](https://github.com/weizyyy/Agentic-Meeting/issues/10)，
返回 `Content-Type: application/json`，不接收请求体或探测目标参数，保持匿名访问。
启用访问口令（§5.7）后也保持匿名：`LoginRequired` 保护 `/api` 及其下路径，
这三个根路径位于该前缀之外。后续鉴权改动须保持**这三个精确路径的 GET** 匿名，
不因此免除业务 API 的鉴权，也不承诺额外运维方法。响应不含会议内容或凭据，
但匿名数值会体现负载和活动程度；这些端点不代表实例可以安全地暴露到公网。

| 方法与路径     | HTTP 状态及含义                                                                      |
| -------------- | ------------------------------------------------------------------------------------ |
| `GET /healthz` | 200，精确响应 `{"status":"ok"}`：HTTP 进程能响应；不探测数据库、网络、模型或文件系统 |
| `GET /readyz`  | 200 时 `status="ok"` 或 `"degraded"`；503 时 `status="not_ready"`                    |
| `GET /metrics` | 始终为 200，`status="ok"` 或 `"partial"`，包括采集失败                               |

就绪要求 `lifecycle="running"`、已打开的应用数据库通过只读检查，以及 ASR `status="ok"`。
ASR 承担核心转录功能，因此是必选。实时模型、TTS、嵌入与 agent 端点**对 readiness 而言**是可选项：
它们故障时仍能转录（§3.4、architecture §9），并不保证应答或语义搜索可用。
architecture 中实时模型的必选标注描述完整助理，而不是这里的就绪门槛。
已启用的可选服务出现 `unavailable` 或 `unknown` 时，就绪状态为 `degraded`；
`ok`、`reachable` 与 `disabled` 不触发降级。核心条件失败优先，统一为 `not_ready`。
就绪检查的 503 响应是状态快照，不套普通业务错误的 `{"error": "..."}` 格式。

**生命周期与资源缺失。** 每个应用实例初始为 `starting`，lifespan 完成资源初始化后才变为
`running`，开始清理前先变为 `stopping`。直接请求尚未启动或正在/已经关闭的应用时，
就绪接口安全地返回 503，不访问不存在或已关闭的资源。存储为 `unknown`，
原因是 `not_started` 或 `shutting_down`；启用的服务同样为 `unknown` 并使用该原因，
关闭的服务仍为 `disabled`，服务快照 age 为 `null`。
这些阶段的指标状态为 `partial`：连接数、发言重试队列深度、任务队列深度、任务计数与所有样本
age 均为 `null`；关闭的画面摘要队列深度已知为 0，启用但资源不可用时 `depth=null`。
即使尚无资源，enabled/required 元数据也按配置给出。启动后，画面摘要的 `enabled` 取实际 worker
的 `enabled` 属性；启动前取 `caption_provider(cfg)` 是否选择了 provider。
真实 ASGI 服务器可能在 lifespan 启动完成后才接受 HTTP，关闭时也可能先停止监听；
不承诺在服务器实际监听区间之外还能访问任一端点。

**服务覆盖范围。** 每份响应精确包含下列五个逻辑名称，不能省略条目。
`launch.enabled=false` 仅表示进程由外部管理，不代表功能关闭。

| 逻辑名称    | `enabled`                                                                                                                                                 | `required` | 只读探测                                                                                 |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | ---------------------------------------------------------------------------------------- |
| `asr`       | 恒为 true                                                                                                                                                 | true       | `GET <asr.base_url 的 origin>/health`                                                    |
| `realtime`  | 恒为 true，取 `realtime_llm.active`                                                                                                                       | false      | llama-server 接入取 origin 的 `/health`；通用 OpenAI 兼容接入取当前 base URL + `/models` |
| `tts`       | `tts.enabled`                                                                                                                                             | false      | 配置 base URL 的 origin + `/health`                                                      |
| `embedding` | `embedding.enabled`                                                                                                                                       | false      | 配置 base URL 的 origin + `/health`                                                      |
| `agent`     | `agent.enabled`，**或** `caption_provider(cfg) == "agent_llm"`，**或** `realtime.digest_provider == "agent_llm"`，**或** `report.provider == "agent_llm"` | false      | agent base URL + `/models`                                                               |

`caption_provider(cfg)` 要求 `screen.enabled`、`screen.caption` 和所选 provider 支持视觉。
即使后台任务委托关闭，滚动纪要/报告的 provider 选择仍可能使用 agent 端点。
已选择的 agent 端点缺少 base URL 或 model 时为 `unavailable/invalid_config`，不能当作 `disabled`。
无效 HTTP(S) 目标也使用 `invalid_config`；只探测配置中的目标。配置支持凭据的服务沿用环境变量
机制发送凭据。探测不查找可执行文件、不生成模板、不启动进程、不执行推理或 chat-completion 请求，
不读取会议材料。回环地址绕过环境代理，远端沿用现有代理策略。
不跟随重定向，避免配置中的目标把凭据转发到其他主机。
说话人区分是进程内库；Docker、MCP、浏览器 ICE 与网络带宽不属于这份 HTTP 推理服务快照。

| 服务 `status` | 允许的 `reason`                                                      | 含义                                                          |
| ------------- | -------------------------------------------------------------------- | ------------------------------------------------------------- |
| `ok`          | `healthy`                                                            | 专用 `/health` 协议返回 HTTP 200                              |
| `reachable`   | `http_response`                                                      | 通用 `/models` 收到了任意 HTTP 响应                           |
| `unavailable` | `timeout`、`connection_failed`、`http_error`、`invalid_config`       | 探测超时、传输失败、health 返回非 200，或配置中的目标无法使用 |
| `unknown`     | `not_started`、`shutting_down`、`refresh_failed`、`budget_exhausted` | 当前没有观测：生命周期阶段、刷新失败，或共享/请求预算耗尽     |
| `disabled`    | `disabled`                                                           | 该可选服务未被选择；`enabled=false`                           |

`enabled=false` 必须对应 `status="disabled"`，`enabled=true` 则禁止该状态。
通用 `/models` 即使返回 HTTP 200 也只能是 `reachable`，不能是 `ok`；
401/403/404（以及 503）不证明凭据正确、模型访问被允许或推理成功。
相反，专用 `/health` 的 HTTP 503 是 `unavailable/http_error`。
存储只允许 `ok/checked`、`unavailable/timeout`、`unavailable/storage_error`、
`unknown/not_started`、`unknown/shutting_down`。轻量的 `SELECT 1` 只检查现有连接能否读取，
不检查磁盘容量或承诺下一次写入一定持久化。

**封闭的响应 schema。** 以下 JSON Schema 2020-12 定义三个完整响应体
（`health`、`ready`、`metrics`），列出的字段全部必填，禁止额外字段。
上文及下文中的状态/原因组合和跨字段规则同样具有约束力。
所有数值必须有限，秒与计数非负，`null` 永远不等于零。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "oneOf": [{"$ref":"#/$defs/health"},{"$ref":"#/$defs/ready"},{"$ref":"#/$defs/metrics"}],
  "$defs": {
    "seconds": {
      "type": ["number","null"],
      "minimum": 0
    },
    "count": {
      "type": ["integer","null"],
      "minimum": 0
    },
    "zero_or_one": {
      "type": ["integer","null"],
      "minimum": 0,
      "maximum": 1
    },
    "lifecycle": {
      "enum": ["starting","running","stopping"]
    },
    "service": {
      "type": "object",
      "additionalProperties": false,
      "required": ["enabled","required","status","reason"],
      "properties": {"enabled":{"type":"boolean"},"required":{"type":"boolean"},"status":{"enum":["ok","reachable","unavailable","unknown","disabled"]},"reason":{"enum":["healthy","http_response","timeout","connection_failed","http_error","invalid_config","not_started","shutting_down","refresh_failed","budget_exhausted","disabled"]}}
    },
    "required_service": {
      "allOf": [{"$ref":"#/$defs/service"},{"properties":{"enabled":{"const":true},"required":{"const":true}}}]
    },
    "active_optional_service": {
      "allOf": [{"$ref":"#/$defs/service"},{"properties":{"enabled":{"const":true},"required":{"const":false}}}]
    },
    "optional_service": {
      "allOf": [{"$ref":"#/$defs/service"},{"properties":{"required":{"const":false}}}]
    },
    "services": {
      "type": "object",
      "additionalProperties": false,
      "required": ["asr","realtime","tts","embedding","agent"],
      "properties": {"asr":{"$ref":"#/$defs/required_service"},"realtime":{"$ref":"#/$defs/active_optional_service"},"tts":{"$ref":"#/$defs/optional_service"},"embedding":{"$ref":"#/$defs/optional_service"},"agent":{"$ref":"#/$defs/optional_service"}}
    },
    "storage": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status","reason"],
      "properties": {"status":{"enum":["ok","unavailable","unknown"]},"reason":{"enum":["checked","timeout","storage_error","not_started","shutting_down"]}}
    },
    "screen_queue": {
      "type": "object",
      "additionalProperties": false,
      "required": ["enabled","depth"],
      "properties": {"enabled":{"type":"boolean"},"depth":{"$ref":"#/$defs/zero_or_one"}}
    },
    "queues": {
      "type": "object",
      "additionalProperties": false,
      "required": ["transcript_retry","screen_caption","agent_tasks"],
      "properties": {"transcript_retry":{"$ref":"#/$defs/count"},"screen_caption":{"$ref":"#/$defs/screen_queue"},"agent_tasks":{"$ref":"#/$defs/count"}}
    },
    "task_counts": {
      "anyOf": [{"type":"object","additionalProperties":false,"required":["queued","running","succeeded","failed","cancelled"],"properties":{"queued":{"type":"integer","minimum":0},"running":{"type":"integer","minimum":0},"succeeded":{"type":"integer","minimum":0},"failed":{"type":"integer","minimum":0},"cancelled":{"type":"integer","minimum":0}}},{"type":"null"}]
    },
    "health": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status"],
      "properties": {"status":{"const":"ok"}}
    },
    "ready": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status","lifecycle","storage","services","service_snapshot_age_seconds"],
      "properties": {"status":{"enum":["ok","degraded","not_ready"]},"lifecycle":{"$ref":"#/$defs/lifecycle"},"storage":{"$ref":"#/$defs/storage"},"services":{"$ref":"#/$defs/services"},"service_snapshot_age_seconds":{"$ref":"#/$defs/seconds"}}
    },
    "metrics": {
      "type": "object",
      "additionalProperties": false,
      "required": ["status","lifecycle","live_connections","caption_lag_seconds","caption_sample_age_seconds","queues","task_counts","task_counts_age_seconds","services","service_snapshot_age_seconds"],
      "properties": {"status":{"enum":["ok","partial"]},"lifecycle":{"$ref":"#/$defs/lifecycle"},"live_connections":{"$ref":"#/$defs/zero_or_one"},"caption_lag_seconds":{"$ref":"#/$defs/seconds"},"caption_sample_age_seconds":{"$ref":"#/$defs/seconds"},"queues":{"$ref":"#/$defs/queues"},"task_counts":{"$ref":"#/$defs/task_counts"},"task_counts_age_seconds":{"$ref":"#/$defs/seconds"},"services":{"$ref":"#/$defs/services"},"service_snapshot_age_seconds":{"$ref":"#/$defs/seconds"}}
    }
  }
}
```

**指标字典。** 这些是应用实例的 gauge，不是进程启动后的累计 counter。
内存字段每次从当前资源读取，缓存的数据库/服务字段给出各自 age。

| 字段                            | 精确口径及重置/故障语义                                                                                                                                                                                                              |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `live_connections`              | 当前已向 `SessionManager` 登记 worker 和 recorder 的活动连接数，取 0 或 1。管线仍在组装或接管间隙为 0；资源不存在/不可读为 `null`。不计历史连接行数                                                                                  |
| `caption_lag_seconds`           | 新 `ASRDelta` 第一次成功发送非空 `CaptionUpdate` 后，按共享会话音频时间轴计算 `raw_lag = recorder.elapsed_secs - delta.audio_end_secs`。只接受有限且 `raw_lag >= -1 / ASR_SAMPLE_RATE` 的值，再保存 `max(0, raw_lag)`                |
| `caption_sample_age_seconds`    | 距该成功样本的单调时钟秒数。无样本时两个字幕字段都为 `null`                                                                                                                                                                          |
| `queues.transcript_retry`       | 活动 recorder 等待下次存储重试的未写入发言数；空闲为 0；已登记连接的 recorder 不存在/不可读时为 `null`                                                                                                                               |
| `queues.screen_caption.enabled` | 画面摘要 worker 是否启用，启动前按上文的配置后备规则给出                                                                                                                                                                             |
| `queues.screen_caption.depth`   | 等待处理的新画面槽位数，取 0 或 1，不含正在处理的画面及复用摘要的 followers。关闭为 0；启用但 worker 不可用/不可读时为 `null`                                                                                                        |
| `queues.agent_tasks`            | 与 `task_counts` 同一数据库聚合快照的 `queued` 数；聚合不可用时为 `null`                                                                                                                                                             |
| `task_counts`                   | 对**数据库全部保留任务**按 `queued/running/succeeded/failed/cancelled` 分组计数。空库为五个零；关闭 agent 不会隐藏保留任务；删除任务可降低计数；重启恢复结果体现在下次快照。查询失败时整组为 `null`，不能使用 `TaskManager._running` |
| `task_counts_age_seconds`       | 成功计数快照的单调时钟 age，有效期严格小于 5 秒；计数不可用时为 `null`                                                                                                                                                               |
| `services`                      | 与就绪接口共用的固定名称快照及状态语义                                                                                                                                                                                               |
| `service_snapshot_age_seconds`  | 自该轮服务刷新完成后的单调时钟 age，有效期严格小于 5 秒；当前快照中任一启用服务为 `unknown`，或尚无完成的刷新时为 `null`                                                                                                             |

同一 delta 至多尝试采样一次，包括它同时出现在临时/最终帧或产生多个说话人字幕分片的情况。
只在非空字幕消息成功送入输出管线后采样；发送失败、纯空白/空字幕、键入文字和助理发言不产生样本。
非有限数值输入或负差超过一个音频采样周期（`1 / ASR_SAMPLE_RATE`，当前为 1/16000 秒）时，
拒绝本次采样，不影响字幕、不伪造零。拒绝后保留之前的 lag 和采样时间戳，因此 age 继续增长；
没有旧样本则两个字幕字段仍为 `null`。拒绝采样本身不触发指标采集 partial；
该容差仅处理舍入误差，不能掩盖时钟不一致。新 recorder/连接初始没有样本；当前连接消失时，
指标中的字幕字段清空，即使是同一会议的重连。静音期间 lag 保留上次值，样本 age 增长。
它估计字幕输出时服务端的音频积压，不是浏览器显示延迟或逐词最终定稿延迟，
不能据此证明达到 architecture 中的 1.5 秒最终字幕目标，也不包括网络传送和浏览器绘制。
恢复后的音频沿用共享会话时间轴，不会再次叠加恢复 base。禁止用 Unix 时间减会话相对秒数。

`partial` 表示所需指标采集失败、资源不可用，或启用服务的状态为 `unknown`。
已经观测到 `unavailable` 本身是有效服务数据，不会单独触发 partial。
自然的“尚无字幕样本” `null`、空闲队列的 0 和功能关闭都不是失败。
就绪接口的 `degraded` 表示可选服务功能降级，指标的 `partial` 表示观测不完整，两者独立。

**成本、新鲜度与清理。** 常量按应用实例拥有，不增加用户配置键：

- 服务按需刷新，TTL 为 5 秒。已启用服务并发探测，一轮整体预算 2 秒；
  每实例最多一轮刷新，并发就绪/指标请求共享它及其失败，不建立永久轮询任务。
- 专用健康探测超时为 `unavailable/timeout`。一轮/请求预算耗尽导致目标结果无法获得时，
  为 `unknown/budget_exhausted`，保留其他已完成目标的结果。
  意外刷新失败为 `unknown/refresh_failed`，不能继续给出旧 `ok`。
  含 unknown 的完成轮次同样可复用 5 秒以避免请求风暴，但对外 age 为 `null`；
  已过期快照必须刷新后才可使用。
- 就绪接口每次在 0.5 秒内检查存储，预算包含任何锁等待。
  重叠请求可共享进行中的检查，已完成的成功结果不缓存供 readiness 使用。
  已观测的存储失败立即使任务计数缓存失效。
- 任务计数使用一次只读分组查询，预算 0.5 秒（包含等待），成功快照 TTL 为 5 秒。
  并发指标请求共享进行中的查询；采集失败返回 `null`，不返回旧计数。
  只有新的成功查询才能恢复计数。缓存的成功只描述最近样本，不保证数据库仍然可用。
- 就绪/指标采集从进入端点起的整体截止时间为 2.5 秒，包含等待共享刷新、锁和数据库读取。
  事件循环调度与 HTTP 传输可增加时间；采集超时返回安全的状态/partial 响应。
  请求取消仍须传播，且不能取消其他请求共享的刷新。
- 取消/清理时关闭自建 HTTP client，在关闭数据库前取消并等待进行中的采集任务。
  缓存时间戳及单调时钟属于应用实例，不使用模块全局状态。
  现有 CLI 探测仍保留独立的 5 秒超时。
- 响应只能包含封闭 schema 中的字段、固定服务逻辑名、布尔值、数字 gauge 和白名单状态/原因。
  不暴露 URL、端口、模型名、路径、凭据、异常消息、上游响应正文、进程参数、
  session/task/frame id、说话人名字、转录文字或截图。故障转换为安全原因码。

**示例。** 以下均为完整响应体，服务是否关闭随配置变化。

正常就绪，HTTP 200：

```json
{
  "status": "ok", "lifecycle": "running",
  "storage": {"status": "ok", "reason": "checked"},
  "services": {
    "asr": {"enabled": true, "required": true, "status": "ok", "reason": "healthy"},
    "realtime": {"enabled": true, "required": false, "status": "reachable", "reason": "http_response"},
    "tts": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "embedding": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "agent": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"}
  },
  "service_snapshot_age_seconds": 0.0
}
```

可选 TTS 不可用，HTTP 200：

```json
{
  "status": "degraded", "lifecycle": "running",
  "storage": {"status": "ok", "reason": "checked"},
  "services": {
    "asr": {"enabled": true, "required": true, "status": "ok", "reason": "healthy"},
    "realtime": {"enabled": true, "required": false, "status": "ok", "reason": "healthy"},
    "tts": {"enabled": true, "required": false, "status": "unavailable", "reason": "timeout"},
    "embedding": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "agent": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"}
  },
  "service_snapshot_age_seconds": 1.0
}
```

核心 ASR 不可用，HTTP 503：

```json
{
  "status": "not_ready", "lifecycle": "running",
  "storage": {"status": "ok", "reason": "checked"},
  "services": {
    "asr": {"enabled": true, "required": true, "status": "unavailable", "reason": "http_error"},
    "realtime": {"enabled": true, "required": false, "status": "ok", "reason": "healthy"},
    "tts": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "embedding": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "agent": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"}
  },
  "service_snapshot_age_seconds": 0.0
}
```

空闲实例、尚无字幕样本，HTTP 200：

```json
{
  "status": "ok", "lifecycle": "running", "live_connections": 0,
  "caption_lag_seconds": null, "caption_sample_age_seconds": null,
  "queues": {"transcript_retry": 0, "screen_caption": {"enabled": false, "depth": 0}, "agent_tasks": 0},
  "task_counts": {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0},
  "task_counts_age_seconds": 0.0,
  "services": {
    "asr": {"enabled": true, "required": true, "status": "ok", "reason": "healthy"},
    "realtime": {"enabled": true, "required": false, "status": "ok", "reason": "healthy"},
    "tts": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "embedding": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "agent": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"}
  },
  "service_snapshot_age_seconds": 0.0
}
```

有字幕样本但任务计数采集失败，HTTP 200：

```json
{
  "status": "partial", "lifecycle": "running", "live_connections": 1,
  "caption_lag_seconds": 0.3, "caption_sample_age_seconds": 2.0,
  "queues": {"transcript_retry": 2, "screen_caption": {"enabled": true, "depth": 1}, "agent_tasks": null},
  "task_counts": null, "task_counts_age_seconds": null,
  "services": {
    "asr": {"enabled": true, "required": true, "status": "ok", "reason": "healthy"},
    "realtime": {"enabled": true, "required": false, "status": "ok", "reason": "healthy"},
    "tts": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "embedding": {"enabled": false, "required": false, "status": "disabled", "reason": "disabled"},
    "agent": {"enabled": true, "required": false, "status": "reachable", "reason": "http_response"}
  },
  "service_snapshot_age_seconds": 0.5
}
```

## 6. 数据通道消息

### 6.1 服务端 → 浏览器

服务端向管线推 `RTVIServerMessageFrame(data=<下面的对象>)`；浏览器在 `onServerMessage` 回调里收到。
每条消息都有 `type` 字段：

| `type`             | 其余字段                                                                  | 含义                                                                                                                                                                                     |
| ------------------ | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caption`          | `segment_id, speaker_idx, speaker_name, t_start, stable, unstable`        | 当前正在说的这一段的实时字幕；同一 `segment_id` 的后一条覆盖前一条                                                                                                                       |
| `utterance`        | `id, segment_id, speaker_idx, speaker_name, t_start, t_end, text, source` | 一条发言已定稿落库；界面用它替换对应 `segment_id` 的实时字幕。`id` 是界面上已有的发言时，表示新片段并进了那一条（§4.3 第 7 条）：`text` 是并好的全文，`segment_id` 那行实时字幕收掉      |
| `utterance_update` | `id, speaker_idx, speaker_name`                                           | 说话人更正                                                                                                                                                                               |
| `speaker`          | `idx, display_name`                                                       | 说话人改名                                                                                                                                                                               |
| `speakers_merged`  | `from, into, display_name`                                                | 说话人 `from` 并入了 `into`（`display_name` 是 `into` 的显示名）：页面把已加载的发言改到 `into` 名下，并从说话人列表里去掉 `from`                                                        |
| `frame`            | `id, t, width, height`                                                    | 新截图                                                                                                                                                                                   |
| `frame_caption`    | `id, caption`                                                             | 截图摘要已生成                                                                                                                                                                           |
| `assistant_state`  | `state`：`idle` / `listening` / `thinking` / `speaking`                   | 助理状态                                                                                                                                                                                 |
| `task`             | `id, label, goal, status, brief, error, modality, created_at`             | 任务创建或状态变化（`id` 是完整编号，`label` 是会议内的短编号 `t3`）                                                                                                                     |
| `task_event`       | `task_id, at, kind, summary`                                              | 任务进度                                                                                                                                                                                 |
| `notice`           | `level`：`info` / `warn` / `error`，`text`                                | 需要让用户知道的提示（如某服务不可用）。实时模型、语音合成的请求出错时，服务端把管线里的错误换成一句中文发出来（`pipeline/errors.py`）；Pipecat 自带的 RTVI `error` 消息页面只显示致命的 |
| `session`          | `id, title, started_at, resumed, base_secs, state`                        | 连接建立后发一次：这路连接挂在哪个会话上；`resumed` 为真表示是继续，不是新建；`base_secs` 是本次连接的时间轴起点                                                                         |
| `session_closed`   | `reason`：`taken_over` / `ended` / `server_stopping`                      | 连接即将被服务端关闭的原因（尽力而为地发出），页面据此提示而不是静默变成未连接                                                                                                           |

`segment_id` 是服务端生成的递增整数，一次「开始说话 → 停止说话」内可能因换人而产生多个。

实时模型上下文的三种写入都经过会议记录器，共用一把锁：发言定稿的「落库 + 追加一行」、
画面摘要的追加（`append_context_line`）、压缩时的整体重建（`rebuild_context`，在锁里读数据库再推
`LLMMessagesUpdateFrame`）。

会议记录器（`pipeline/recorder.py`）怎么产生这些消息：

- 每个识别增量（`ASRDelta`，临时转录帧和转录帧共用同一个对象，只处理一次）交给 `TranscriptAssembler`
  （`diar/fusion.py`，规则见 §4.3），它返回字幕更新和发言定稿。`caption` 的 `stable` 是**这一行字幕**到目前为止的定稿文字
  （换人切开后新的一行从头累计），`unstable` 是尾巴；`speaker_idx` / `speaker_name` 是当前行的说话人（还没结论是 0 /「未知」）；
  `t_start` 是这一行的起点；文字没变的重复字幕不发。一行没有定稿文字就收尾时，发一条 `stable` 和 `unstable` 都为空的 `caption`，
  界面据此清掉灰色的临时字幕。
- 每条发言定稿：落库 → 推 `utterance` → 向实时模型的上下文追加一行 `[时:分:秒 说话人] 文本`（`LLMMessagesAppendFrame`，
  `run_llm=False`），然后才放行触发用户轮次的那条转录帧。落库失败时 `utterance` 的 `id` 为 `null`（这条发言进重试队列，
  之后补写）。助理自己的话（`source="assistant"`）和浏览器键入的文字（`source="text"`）没有对应的字幕行，`segment_id` 为 `null`。
- 落库后 5 秒内每秒核对一次说话人，变了就更新数据库并推 `utterance_update`。
- 与助理说话时段重叠的增量不进字幕、不落库、不进上下文（§4.3 第 6 条）。

`assistant_state` 服务端只发 `listening`（被叫到名字时）；`speaking` / `idle` 由浏览器端按
SDK 的 `onBotStartedSpeaking` / `onBotStoppedSpeaking` 回调自行显示。

助理应答的文字不通过自定义消息传递，而是使用客户端 SDK（`@pipecat-ai/client-js` 1.13）自带的回调：`onBotLlmStarted` / `onBotLlmText` / `onBotLlmStopped`
拿流式文字，`onBotStartedSpeaking` / `onBotStoppedSpeaking` 拿朗读状态。

### 6.2 浏览器 → 服务端

用 `client.sendClientMessage(type, data)`；服务端在 `RTVIProcessor` 的 `on_client_message` 事件里处理。

| `type`         | `data`                    | 含义                                                                                                                                                         |
| -------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `text_input`   | `{"text": "..."}`         | 打字提问或委托任务。等同于一次已唤醒的用户请求，不需要唤醒词，只以文字回答（见 §6.3）。文本去掉首尾空白后不能为空、不超过 2000 字，否则丢弃并回一条 `notice` |
| `screen_state` | `{"sharing": true/false}` | 屏幕共享的开始/暂停，仅用于界面状态与日志                                                                                                                    |

### 6.3 文字输入与应答模态

`text_input` 的处理（`pipeline/text_input.py`，设计见 architecture.md §5.5）：

1. 校验文本；写一条发言（`source="text"`、`speaker_idx=-2`、`addressed_to_assistant=1`），推 `utterance` 消息，
   向上下文追加一行 `[时:分:秒 文字输入] 文本`。
2. 触发模型时向管线依次推 `TextRequestFrame()`（记号）、`LLMMessagesAppendFrame([...], run_llm=True)`。
   管线里紧挨实时模型之前的 `ModalityGate` 在每个新请求（向下游的 `LLMContextFrame`）进入模型前推
   `LLMConfigureOutputFrame(skip_tts=有没有记号)`：文字请求只出文字，语音请求照常朗读；工具结果回来后的再次生成
   不经过闸门，沿用这次请求的模态（原因见 architecture.md §5.5）。语音合成关闭时不放记号。不推 `InterruptionFrame`。
3. 助理正在生成回答（助理侧聚合器的一轮开始到结束）、执行工具（模型服务的 `on_function_calls_started` 到下一次生成开始）
   或朗读（记录器收到 `Bot*SpeakingFrame`）时，文字请求排队
   （先进先出，最多 5 条，满了丢弃并 `warn`），助理空闲后依次触发；排队时发一条 `info` 级 `notice`。
   排队的请求已经落库、显示在字幕里，只是还没交给模型。发出请求后一直没收到「助理开始回答」的事件（模型出错了）
   超过 60 秒就不再等，继续处理排队的。
4. 校验：去首尾空白后非空、不超过 2000 字（按字符数），不通过发 `warn` 级 `notice`，不落库。data 格式不对（不是 `{"text": "…"}`）
   也提示。
5. 任务记录委托当时的模态 `tasks.modality`；文字委托的任务完成后只出文字（做法见 §7「播报的时机与模态」）。

## 7. 实时模型的工具

用 Pipecat 的「直接函数」方式定义（函数签名和文档字符串即工具描述，见 pipecat-notes.md §6）。
工具的名字、参数名用英文，描述用中文。返回值都是可序列化为 JSON 的字典，字段尽量少。

| 工具             | 参数                                                                                                         | 返回                                                                                          | 类别 |
| ---------------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- | ---- |
| `recall`         | `query?`（关键词或一句话）、`speaker?`（显示名）、`minutes_ago_from?`、`minutes_ago_to?`、`limit?`           | `{"items": [{"time": "00:14:02", "speaker": "王老师", "text": "..."}]}`                       | 同步 |
| `get_digest`     | `scope`：`"all"` / `"recent"`                                                                                | `{"digest": "...", "covers_until": "00:42:10", "since_then": [最近未纳入纪要的发言]}`         | 同步 |
| `look_at_screen` | `frame_ids?`（逗号分开的截图编号，最多 3 个）                                                                | 不带参数：把最新截图加入上下文，并列出更早的截图供挑选；带编号：把那几张之前的截图加入上下文  | 同步 |
| `delegate_task`  | `goal`（完整、独立可读的任务描述）、`minutes_of_context?`（带上最近几分钟的转录，默认 5）、`include_screen?` | 先回报 `{"task_id": "t3", "status": "accepted"}`；结束时回报 `{"task_id", "status", "brief"}` | 异步 |
| `task_status`    | `task_id?`（不填 = 最近一个）                                                                                | `{"task_id", "status", "goal", "recent_steps": [...]}`                                        | 同步 |
| `cancel_task`    | `task_id`                                                                                                    | `{"task_id", "status"}`                                                                       | 同步 |

时间参数用「距现在多少分钟」而不是绝对时间：实时模型不擅长算时间，而「刚才」「十分钟前」
可以直接映射。返回里的时间是会话时间轴的 `时:分:秒`。

查询类工具的实现细节（`pipeline/tools.py`）：

- 工具通过 `params.app_resources`（`AppResources`）拿存储、嵌入客户端、配置；「现在是哪场会议、进行到第几秒」
  来自正在进行的那路连接（`resources.sessions.live`，秒数是记录器的 `elapsed_secs`）。
- 参数全部可选。`minutes_ago_from` 是较早的一端、`minutes_ago_to` 是较晚的一端，`0` = 不限；给反了自动换过来；
  模型给的字符串、空值、负数都按「没给」处理。`limit` 默认 10、最多 30；单条文字超过 300 字截断。
- `recall` 的 `speaker`：先按显示名精确匹配，再看是不是只有一个名字包含它；找不到或有歧义时返回
  `{"items": [], "note": "没有找到叫「…」的说话人", "speakers": [这场会议的说话人名单]}`。
- `get_digest`：`all` = 最新一份累积纪要 + 纪要之后的发言（最多最近 40 条）；`recent` = 不带纪要，只给最近一个纪要间隔
  （`realtime.digest_interval_minutes`）内的发言原文（最多 60 条）。还没有纪要时 `digest` 为空并带 `note`。
- `look_at_screen`：返回 `{"time", "seconds_ago", "caption", "note"}`，并把最新截图作为一条**用户消息**
  （`[画面 时:分:秒] 屏幕截图` + 图片）直接加进上下文，排在这次工具调用的记录之后（等那条记录出现，最多等 0.5 秒）。
  没有截图、模型不识图、截图文件读不到时不加图片，在 `note` 里说明。图片进了上下文就一直在，直到下一次压缩。
  同时返回 `earlier`：更早的截图清单 `[{"id", "time", "caption"}]`（有摘要、不是「无关画面」、和相邻的不重复，最多最近 20 张），
  供模型判断要不要回看。带 `frame_ids`（逗号分开的编号，来自 `earlier`）再调用时：返回 `{"frames": [{"id", "time", "caption"}], "note"}`，
  并把这几张（一次最多 3 张，只认本场会议的）各作为一条用户消息（`[画面 时:分:秒] 之前的屏幕截图（编号 N）` + 图片）加进上下文。
  模型不识图时忽略 `frame_ids`，只给最新一张的摘要。
- **对助理提的要求不算会议内容**：键入的文字（`source="text"`）和最近 60 秒内带助理名字的识别发言不出现在工具结果里
  ——不然问「谁提到过学习率」时这句提问自己会被找回来。助理自己说过的话照常返回。
- 查不到东西不报错，在 `note` 里用一句话说明；没有进行中的会议返回 `{"note": "现在没有进行中的会议"}`；
  工具内部出错返回 `{"error": "这个工具暂时用不了"}`，不让异常进管线。
- 工具列表的顺序固定（`recall`、`get_digest`、`look_at_screen`），每次请求的工具定义逐字一致，前缀缓存才稳定。
- 实时模型的提示词（`config/prompts/realtime_system.md`）只介绍这次会话里确实提供的工具；任务相关的三个单独放在
  `config/prompts/realtime_tasks.md`，`agent.enabled` 为真时才拼进去。

`delegate_task` 的声明方式：用 `@tool_options(cancel_on_interruption=False,
timeout_secs=<agent.task_timeout_secs + 余量>)` 声明；创建任务后立刻调用一次
`result_callback(..., properties=FunctionCallResultProperties(is_final=False))` 回报「已受理」；
任务结束后再调用一次 `result_callback(最终结果)`。

**任务工具的实现（`pipeline/tools.py`）**：

- 三个任务工具只在 `agent.enabled = true` 时提供；提示词里讲它们的那一段（`config/prompts/realtime_tasks.md`）也只在这时并进去。
  工具列表的顺序固定：`recall`、`get_digest`、`look_at_screen`、`delegate_task`、`task_status`、`cancel_task`。
- `delegate_task(goal, minutes_of_context=5, include_screen=false)`：`goal` 必填（空的不建任务，回 `error`），最长 2000 字；
  转录范围是最近 `minutes_of_context` 分钟（上限 60，不合理的值按 5）；`include_screen` 为真时带这段时间里最近 3 张截图，
  这段时间里没有新截图（屏幕没变）就带最新的一张。交办人：语音委托记最近一条定稿发言的说话人，文字委托记「文字输入」。
  调用超时 = `agent.task_timeout_secs` + 140 秒（等「没人说话」的 20 秒加收尾的余量）。
- 最终回报：成功 `{"task_id", "status": "succeeded", "brief"}`；失败或取消 `{"task_id", "status": "failed" | "cancelled", "reason"}`。
- **播报的时机与模态**：任务委托时记下那次请求的模态（看模态闸门）。任务做完后——
  语音委托的：先等「没有人在说话」（会议记录器的 `wait_quiet`，最长 20 秒），再把结果交回去；
  文字委托的：不等，直接交回去，全程不出声。交回去之前往模型服务的队列里放一个 `LLMConfigureOutputFrame`
  （结果触发的那次生成走的是向上游的上下文帧，不经过模态闸门，所以要在这里直接设定）。
  结果写进上下文之后把任务的 `announced` 置位。
- `task_status(task_id="")` / `cancel_task(task_id="")`：`task_id` 是短编号（`t2`，大小写和空格不敏感），留空 = 最近交办的一个。
  没有任务、没有这个编号时在 `note` 里说明；已经结束的任务取消时也只是说明。
- **发给模型的样子**（`pipeline/async_tools.py`）：Pipecat 为异步工具往上下文里写的是一段 JSON（英文说明 + 再编码一次的结果，
  中文全是 `\uXXXX` 转义）。发请求之前把它们改写成中文行：占位 →「后台任务已经开始，结果稍后送达……」；
  已受理 → `[任务 t1 已受理] 后台正在处理，做完后你会收到结果。`；完成 → `[任务 t1 完成] 结论`；
  失败 / 取消 → `[任务 t1 失败] 原因` / `[任务 t1 已取消] …`。上下文里存的仍是 Pipecat 的原样消息，只改发出去的那一份；
  压缩重建上下文时，最近做完的任务也按同样的格式各写一行。
- Pipecat 在有异步工具时会往系统提示词后面自动加一段英文（要求「把晚到的结果附在下一次回复末尾，不要单独成一次回复」），
  与本项目的流程相反，已在 `RealtimeLLMService` 中去掉；怎么对待晚到的结果由 `realtime_tasks.md` 说明。

配置里 `realtime.direct_mcp_tools` 非空时，另把这些 MCP 工具直接挂给实时模型（见 pipecat-notes.md §7）。

## 8. 后台任务

### 8.1 任务管理器（`agent/tasks.py`）

```python
class TaskManager:
    async def submit(self, *, session_id: str, goal: str, requested_by: int, requested_t: float,
                     transcript_window: tuple[float, float], frame_ids: list[int],
                     modality: str) -> TaskRecord: ...
    async def wait(self, task_id: str) -> TaskResult: ...      # 失败或取消时抛出 TaskFailed
    async def cancel(self, task_id: str) -> TaskRecord | None: ...
    async def status(self, session_id: str, label: str | None = None) -> dict | None: ...
    async def add_event(self, task_id: str, kind: str, summary: str, payload: dict | None = None) -> None: ...
```

- 整个应用只有一个任务管理器；连接断开或更换会议之后，任务仍会继续运行。
- 并发数由 `agent.max_concurrent_tasks` 限制，运行时间由 `agent.task_timeout_secs` 限制。
- **编号。** 数据库主键是 `<会话 id>.t<序号>`，全库唯一。点号之后的部分是短编号（`TaskRecord.label`），
  用于口头指代、工具参数和界面；HTTP 接口使用完整编号。
- `t_from`、`t_to` 和 `frame_ids_json` 记录交给 agent 的转录时间范围与截图，即该任务外发的内容。
- `wait` 在失败或取消时抛出 `TaskFailed(status, reason)`；任务不存在时抛出 `LookupError`。
  需要让使用者看到的失败原因由运行器以 `RunnerError("原因")` 抛出；其他异常只报告为内部错误及异常类型，细节写入日志。
- 排队中的任务也可以取消。`status` 不带短编号时返回该会议最近创建的任务，附最近三条进度。
- 应用重启后，状态为 `queued` 或 `running` 的任务一律标记为 `failed`。
- 工作目录：`<data_dir>/sessions/<会话>/tasks/<短编号>/`。

### 8.2 运行器的输入与输出（`agent/runner.py`）

输入给 agent 的首条用户消息由三部分组成：

1. 任务目标（实时模型给的 `goal`）。
2. 相关转录：`transcript_window` 时间范围内的发言，格式同 architecture.md §6 的上下文行。
3. 相关截图：`frame_ids` 对应的图片，以 `image_url`（`data:image/webp;base64,...`）形式附上；
   同时把原图复制进任务工作目录，沙箱里的代码可以读取。

agent 的最终回答必须是如下 JSON 对象（由 agent 的系统提示词要求，运行器负责校验；校验失败时
把原始回答整体作为 `detail_md`，`brief` 使用一句固定的话）：

```json
{
  "brief": "不超过 60 个字的口语化结论，可以直接念出来",
  "detail_md": "在任务面板中展示的详细结果，纯文本",
  "sources": ["https://..."],
  "artifacts": ["plot.png"]
}
```

实现细节（`agent/runner.py`、`agent/sandbox.py`）：

- 首条用户消息的文字部分分节：`# 任务目标`、`# 相关的会议转录`（没有就写明没有）、`# 屏幕截图`（几张、文件在 `input/` 下）、
  `# 这次的限制`（没有代码执行环境、某个检索服务连不上时才有）。转录最多带 400 行。
- 截图文件名是 `screen_<时分秒>_<截图编号>.webp`，同时写进任务目录的 `input/` 和沙箱工作区的 `input/`。
  `agent.supports_vision = false` 时不附图片，只在文字里说明有截图。别的会议的截图、读不到的截图不带。
- 最终回答的解析较为宽容：允许包在代码块中、前后带一两句话（取第一个 `{` 到最后一个 `}`），也接受字符串中直接出现的换行；
  没有 `brief` 或解析不了才走兜底。`artifacts` 里的文件名只接受工作区内的相对路径，再从沙箱取回任务目录，
  **取不回来的不算产物**；没有代码执行环境时 `artifacts` 恒为空。单个产物上限 20 MB。
- 可以直接告诉用户的失败原因（`RunnerError`）：后台任务功能已关闭、agent 还没有配置、没装依赖、连不上远端模型、
  密钥无效、远端模型返回了错误（状态码）、步骤太多（超过 `agent.max_turns` 步）、agent 运行出错（异常类型）。
- 沙箱开不起来、某个 MCP 服务器连不上都不让任务失败：记一条 `note` 进度，并在输入里告诉 agent。
- SDK 的具体用法见 [agents-sdk-notes.md](agents-sdk-notes.md)。

### 8.3 进度事件

运行器把 agent 框架的流式事件转换成一句中文的 `summary`，可以直接朗读：

| 事件          | `kind`        | `summary` 示例                 |
| ------------- | ------------- | ------------------------------ |
| 任务开始      | `status`      | 开始处理                       |
| 调用 MCP 工具 | `tool_call`   | 正在检索「对比学习 温度系数」  |
| 工具返回      | `tool_result` | 工具返回了结果（约 320 字）    |
| 执行代码      | `tool_call`   | 正在运行一段代码               |
| 代码结束      | `tool_result` | 代码运行完成                   |
| 任务结束      | `status`      | 已完成 / 失败：<原因> / 已取消 |

措辞规则（`describe_tool_call` / `describe_tool_output`）：工具参数里有 `query` / `q` / `keywords` 之类的字段时是
「正在检索「…」」，有 `url` 时是「正在打开「…」」，否则「正在调用工具 xxx」；工具返回只说个大概——
「工具返回了结果（约 N 字）」「工具返回了错误」「工具没有返回内容」，不朗读原文。沙箱的工具：「正在运行一段代码 / 代码运行完成 / 代码运行超时」「正在写文件」
「正在查看图片」。另有 `note` 类的提示（沙箱不可用、检索服务连不上）。引用的查询词最多 60 个字。

## 9. 推理服务的命令行

进程管理器（`services/supervisor.py`）按配置生成命令行。所用参数以锁定版本的
`third_party/llama.cpp/tools/server/README.md` 和 `third_party/qwentts.cpp/tools/tts-server.cpp` 为准。

**实时模型**（只在 `realtime_llm.mode = "llama_server"` 时生成；字段取自 `[realtime_llm.llama_server]`）

```
llama-server -m <model_path> [--mmproj <mmproj_path>] -a <model>
             --host 127.0.0.1 --port <取自 base_url>
             -c <ctx_size> -np <parallel> -ngl <gpu_layers>
             --jinja --reasoning <on|off>      # thinking=false 时为 off
             <extra_args...>
```

`realtime_llm.mode = "openai_api"` 时不生成启动命令，只生成一条不受管的记录用于连通性检查：
`GET <base_url>/models`（带上密钥），**收到任何 HTTP 响应都算连通**（不同服务对这个路由的支持不一，
401、404 也说明地址是通的）；只有连接失败或超时才算不通。

**语音识别**

```
llama-server -m <model_path> --mmproj <mmproj_path>
             --host 127.0.0.1 --port <取自 base_url>
             -c <ctx_size> -np 1 -ngl <gpu_layers>
             --jinja --chat-template-file <data_dir>/run/asr_chat_template.jinja
             --cache-ram 0
             <extra_args...>
```

`--cache-ram 0` 关掉 llama-server 放在内存里的提示缓存（默认上限 8 GB）：识别每次请求的音频都不同，这份缓存用不上，
不关闭的话进程内存会在最初十几分钟内增长 8 GB（[benchmarks.md](benchmarks.md#50-分钟内的资源占用)）。嵌入服务同理。

其中模板文件由进程管理器在启动前生成：内容是识别格式档案（`asr.profile`）里的 `chat_template`，
按 UTF-8 原样写入。`asr.launch.enabled = false`（识别服务另行启动）时，该服务必须带上同样的
`--chat-template-file`；`agentic-meeting services status` 会在输出中给出提醒。

**嵌入**

```
llama-server -m <model_path> -a <embedding.model> --embedding
             --host 127.0.0.1 --port <取自 base_url> -ngl <gpu_layers> --cache-ram 0 <extra_args...>
```

仅设置 `gpu_layers = "0"` 时，嵌入服务仍会在每块显卡上各占用几百 MB；
在 `extra_args` 中加入 `--device none` 可以避免，配置模板中已包含此项。

**语音合成**

```
tts-server --model <model_path> --codec <codec_path> --alias <tts.model>
           --host 127.0.0.1 --port <取自 base_url> --lang <default_language> <extra_args...>
```

通用规则：

- 健康检查：`llama-server` 与 `tts-server` 都用 `GET /health`（返回 200 即就绪）。
- 服务名固定为 `realtime`、`asr`、`tts`、`embedding`。
- 子进程的标准输出与错误输出追加写入 `data/logs/<服务名>.log`；启动失败时报错信息带上**本次运行**产生的
  最后 30 行（不含以前运行留下的内容）。
- `launch.env` 合并进子进程环境变量；`launch.enabled = false` 的服务不启动，只做健康检查，
  并且只检查一次、不等待。
- 本项目启动的服务只绑定 `127.0.0.1`，所以受管服务的 `base_url` 必须是回环地址并写明端口；
  否则 `build_specs` 报错，提示改用 `launch.enabled = false`。健康检查地址：受管服务一律
  `http://127.0.0.1:<端口>/health`；不受管的取自 `base_url` 的协议、主机与端口。
- 启动前先探测一次：服务已经就绪（上次崩溃残留的进程、用户另开的同款服务）就复用它，不重复启动，
  退出时也不结束它。
- 本机地址的健康检查不走系统代理（设了 `HTTP_PROXY` 的机器上，经代理访问 `127.0.0.1` 会误判不通）；
  远端地址按系统设置走代理，与应用之后实际访问它的方式一致。
- 退出时先发终止信号，5 秒后仍未退出再强制结束。Windows 上用
  `subprocess.CREATE_NEW_PROCESS_GROUP` 启动：它让终端里的 Ctrl+C 不直接打到子进程，收尾统一由
  进程管理器完成。Windows 上终止信号本身就是强制结束。
- 父进程被强杀或崩溃时来不及收尾，由操作系统代劳：Windows 上子进程放进一个设了
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的作业对象，父进程一消失系统就结束它们；Linux 上子进程用
  `prctl(PR_SET_PDEATHSIG)` 登记「父进程死了给我发终止信号」；macOS 没有对应的机制，强杀之后要自己结束残留的推理进程。

**应用向实时模型发请求时附带的字段**（放在 OpenAI SDK 的 `extra_body` 里）：一律取自
`cfg.realtime_llm.request_extra_body(background=...)`：

| 接入方式       | 内容                                                                                                                                                                                            |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llama_server` | 配置中的 `extra_body` + `top_k`（若配置了）+ `{"id_slot": <槽位>, "cache_prompt": true}`；实时应答与预热用 `realtime_slot`，后台任务（画面摘要、纪要）传 `background=True` 用 `background_slot` |
| `openai_api`   | 配置中的 `extra_body` + `top_k`（若配置了）。不带任何 llama.cpp 专有字段                                                                                                                        |

**语音合成接口**（qwentts.cpp `tts-server`）：

- `POST /v1/audio/speech`，请求体 `{"model", "input", "voice", "language", "response_format": "pcm"}`，
  请求体必须是 UTF-8。响应 `Content-Type: audio/pcm`，分块流式返回 24 kHz、16 位小端、单声道 PCM。
- `language` 可省略（用启动时 `--lang` 的默认值）。
- 音色名不存在时返回 502 `{"error": {"message": "synthesis failed", ...}}`，不是 4xx。
  可用音色用 `GET /v1/audio/voices` 查询，返回 `{"voices": [{"name": ..., "kind": "speaker"}, ...]}`；
  进程管理器在语音合成就绪后应查一次，配置的 `tts.voice` 不在其中时给出明确报错并列出可用音色
  （`check_tts_voice`，`services up` 调用，报错即停掉全部服务）。地址取 `<tts.base_url>/audio/voices`
  （`base_url` 已含 `/v1`）。服务没有这个接口（404）、返回的不是预期格式、或请求失败时只记警告：
  其他 OpenAI 兼容的语音合成服务可能不提供该接口，这并不代表配置有误。
