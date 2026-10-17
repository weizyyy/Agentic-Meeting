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

| 配置段           | 内容                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session`        | 助理名字（即唤醒词，必须是英文单词）、别名（`wake_aliases`：识别把名字写成的其他拼写，或听成的至少两个汉字；只用来唤醒，不作为识别热词）、识别热词、成员名单、数据目录、同时进行的会议数                                                                                                                                                                                                                                  |
| `server`         | 监听地址与端口、对外地址、HTTPS 证书、ICE 服务器、访问口令、单点登录、登录有效期、`meeting_access` 与 `local_users`（§5.11）                                                                                                                                                                                                                                                                                              |
| `realtime_llm`   | 接入方式 `mode`，以及两种方式各自的一整套设置：`[realtime_llm.llama_server]`（地址、模型名字段、是否识图、思考开关、采样参数、槽位分工、启动参数）和 `[realtime_llm.openai_api]`（地址、密钥变量名、模型名、是否识图、是否认识 developer 角色、是否预热、附加请求字段、采样参数）                                                                                                                                         |
| `asr`            | 识别后端、提示词格式档案、步长与窗口、每步不定稿的 token 数、句首回补时长（`preroll_ms`，默认 1500 毫秒）、启动参数                                                                                                                                                                                                                                                                                                       |
| `diarization`    | 后端、动态库路径、权重路径、显卡序号、分段阈值                                                                                                                                                                                                                                                                                                                                                                            |
| `tts`            | 地址、模型名字段、音色、语言、并发请求数、启动参数                                                                                                                                                                                                                                                                                                                                                                        |
| `embedding`      | 地址、模型名字段、向量维度、查询前缀（`query_prefix`，召回时加在查询前面的任务指令，默认空）、相关度门槛（`min_similarity`，余弦相似度，默认 0.4，0 = 不设）、启动参数                                                                                                                                                                                                                                                    |
| `speaker_memory` | 跨会议记住说话人：后端、本机声纹服务的地址、录入需要的语音时长、启动参数（§4.4、§5.14）                                                                                                                                                                                                                                                                                                                                   |
| `audio`          | 入口收音增强：自动增益开关、起始增益、增益上限、目标电平、静音线、电平日志间隔（`audio/gain.py`，architecture.md §4）                                                                                                                                                                                                                                                                                                     |
| `turn`           | 静音阈值、语音检测的音量门限（`vad_min_volume`，默认 0.6 ≈ −50 LUFS，0 = 关闭）、轮次结束模型开关、唤醒窗口（`wake_timeout_secs`，默认 30 秒，必须大于 0；`single_activation` 下助理一答完就回到待唤醒状态，它是「从叫名字到答完」的上限，也是助理说话时能用声音打断的时间）、是否每轮都要叫名字                                                                                                                          |
| `realtime`       | 上下文预算、压缩保留时长、纪要间隔、纪要由谁写（`digest_provider`：`realtime_llm` 或 `agent_llm`，默认前者）、预热间隔、允许直连的 MCP 工具                                                                                                                                                                                                                                                                               |
| `screen`         | 截图间隔与阈值、是否生成摘要、摘要由谁生成                                                                                                                                                                                                                                                                                                                                                                                |
| `transcript`     | 发言怎么分条：同一个人两段之间停顿不超过 `merge_gap_secs`（默认 2 秒，0 = 不并）就并成一条；一条已有 `merge_soft_chars`（40）个字并且停在句末时另起一条；一条最多 `merge_max_chars`（200）个字。整段可以不写                                                                                                                                                                                                              |
| `report`         | 会后报告由谁写（`provider`：`realtime_llm` 或 `agent_llm`，默认前者）、一次请求最多放多少字的转录（`max_input_chars`，默认 8000，超过就分段提要点再合并）。整段可以不写                                                                                                                                                                                                                                                   |
| `agent`          | 远端模型地址与密钥变量名、每个请求都并入的字段（`extra_body`，一般用来指定思考的强度，如 `{ reasoning_effort = "medium" }`；后台任务和直接生成都带）、直接生成（会后报告、滚动纪要、画面摘要）时的输出上限（`generation_max_tokens`，默认 16384；思考也算在里面）、要不要把截图原图也交给它（`attach_frames`，默认否；`max_attached_frames`，默认 40，规则见 architecture.md §5.3）、MCP 服务器列表、沙箱设置、并发与超时 |
| `retention`      | 独立保留期限及清理周期（§1.1）                                                                                                                                                                                                                                                                                                                                                                                            |

规则：

- 未知字段一律报错（`extra="forbid"`）。新增配置项时同步修改 `config.py`、配置模板、
  [configuration.md](configuration.md)，并补充测试。
- 配置里只写密钥的**环境变量名**（字段名以 `_env` 结尾），值用 `config.secret()` 读取。
- 相对路径一律用 `AppConfig.resolve()` 解析（相对仓库根目录）。
- 业务代码不按实时模型的接入方式分支。`RealtimeLLMConfig` 提供以下成员：

  | 成员                                           | 含义                                                                                                                    |
  | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
  | `active`                                       | 当前选中的那一套设置（`base_url`、`api_key_env`、`model`、`supports_vision`、`sampling`…）                              |
  | `managed`                                      | 是否由进程管理器启动实时模型服务（只有 `llama_server` 方式且 `launch.enabled` 时为真）                                  |
  | `has_health_endpoint`                          | 只读探测是否使用地址源的专用 `/health` 端点，与进程由谁启动无关                                                         |
  | `request_extra_body(background=False, lane=0)` | 每次请求要放进 `extra_body` 的字段，已按接入方式合并好。`lane` 是这场会议在进行中的会议里的位置（architecture.md §3.2） |
  | `cache_warm`                                   | 是否做缓存预热                                                                                                          |
  | `supports_developer_role`                      | 服务端是否认识 `developer` 角色                                                                                         |

  只有进程管理器（要不要生成启动命令）需要直接看 `mode` 和 `llama_server.launch`。

  `llama_server` 的 `has_health_endpoint` 为真，包括外部启动的服务。其他方式使用选中的
  `active.base_url` 加 `/models`，即使地址为回环地址也一样；任意 HTTP 响应仅表示可达。
  此属性不新增配置字段或 HTTP 字段。

- `config.check_warnings(cfg)` 返回不妨碍启动、但运维者应当知情的事项（目前是数据外发）。
  `check` 与 `serve` 都会打印这些事项，浏览器连接后各收到一条 `notice` 消息。
- **模型相关的字符串不进代码**。识别模型的对话模板、前缀写法、输出标记放在
  `config/asr_profiles/*.toml`（由 `load_asr_profile()` 读取）；各类提示词放在 `config/prompts/*.md`。
  `tests/test_repository_rules.py` 里有一条测试会扫描 `src/`，发现模型名即失败。

### 1.1 保留期限与录制提示

`[retention]` 可省略。四类自动清理各自默认关闭；未配置时已有数据无限期保留。
取值必须是严格整数（布尔值不算整数）。

| 配置项                  | 默认值 | 范围与含义                        |
| ----------------------- | ------ | --------------------------------- |
| `transcript_days`       | `0`    | 0–36500；0 关闭转录清理           |
| `screenshots_days`      | `0`    | 0–36500；0 关闭截图清理           |
| `reports_days`          | `0`    | 0–36500；0 关闭报告及滚动纪要清理 |
| `task_artifacts_days`   | `0`    | 0–36500；0 关闭任务文件清理       |
| `cleanup_interval_secs` | `3600` | 60–86400；两轮清理之间的秒数      |

一天严格等于 86400 秒，使用服务端 UTC Unix 时间戳比较（§8.4）。0 不表示立即删除。
负数、小数、非有限值和超范围值一律拒绝。这里不配置额外路径、端点或密钥。

`session.recording_notice` 是布尔值，默认 `true`，控制转录连接就绪后发出的信息提示（§6.1）。
关闭它不会隐藏始终可见的录制状态，也不会关闭转录；它不是同意门槛或连接前弹窗。

## 2. 数据库

结构定义在 [`src/agentic_meeting/store/schema.sql`](../../src/agentic_meeting/store/schema.sql)。
数据库文件：`<data_dir>/meetings.db`；截图：`<data_dir>/sessions/<会话id>/frames/<序号>.webp`；
任务工作目录：`<data_dir>/sessions/<会话id>/tasks/<短编号>/`；日志：`<data_dir>/logs/`。

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
- `reports`（会后报告）：`id, session_id, created_at, status('running'|'done'|'failed'), provider, text_md, error, error_code, error_params_json`；
  同一会话可以有多份，页面显示最近一份，导出取最近一份 `done` 的。服务启动时把遗留的 `running` 标为 `failed`。
- `speakers.idx` 的保留值：`0` 未知、`-1` 助理、`-2` 键入的文字（默认显示名「文字输入」，可改名）；说话人区分的输出从 `1` 起。
- `utterances.source`：`'asr'`（语音识别）、`'assistant'`（助理自己的话）、`'text'`（浏览器输入框键入）。
- `tasks.modality`：`'voice'` / `'text'`，委托任务的那一轮是语音还是文字，任务完成后的播报据此决定出不出声。
- `tasks.error` 和 `reports.error` 是中文的失败原因，`error_code` 和 `error_params_json`（默认 `'{}'`）标明是哪一种（§5.10）；这两列出现之前的行代码为 `NULL`。

### 2.6 保留元数据与写回身份

`Store._migrate` 以可重复执行的增量迁移保留旧数据与编号：

| 表                     | 新增列                                                                           |
| ---------------------- | -------------------------------------------------------------------------------- |
| `sessions`             | `keep INTEGER NOT NULL DEFAULT 0`、`deletion_pending INTEGER NOT NULL DEFAULT 0` |
| `utterances`、`frames` | `write_token TEXT NOT NULL DEFAULT ''`                                           |
| `reports`              | `write_token TEXT NOT NULL DEFAULT ''`、`finished_at REAL`                       |

`keep`、`deletion_pending` 只存 0/1，新会议都为 0。迁移为 token 为空的旧行分配新的不透明随机
token，把旧终态报告的 `finished_at` 初始化为 `created_at`（运行中报告保持 null，等启动恢复处理）。
迁移不清旧数据、不改向量维度；再次开库不改变这些值。

每条新发言、截图、报告都分配新 token；改变发言文字时更换 token 并使旧向量失效。
异步回调保留领取工作时读到的原 token；写回、失败更新、撤销删除都在 Store 写锁内比较原
`session_id`、行编号与 token。目标缺失、会议待删除或身份不符就跳过。只检查数字编号不够，
因为 SQLite 会复用它。跳过后不能发旧摘要、追加旧上下文或重建文件/数据行。
token 仅供内部使用，不出现在 HTTP、数据通道消息或导出中。

任务编号继续在同一会议内递增。任务文件过期后任务行仍保留，因此原有最大编号分配不会复用
过期任务的短编号或目录。完整删除会议才删除全部任务；新会议使用新的会议编号。

发言与截图编号在全库单调递增，类别/完整会议删除也不复用。既有 `meta` 表用
`utterances_id_high_water`、`frames_id_high_water` 存十进制高水位。迁移至少初始化为现存最大编号
及保留引用的最大值（`digests.last_utterance_id` 或任务截图编号）。分配时在同一 Store 锁和事务内
推进高水位并显式 INSERT 编号；回滚/重启保持有效高水位，删除不得降低它。
这样才能保留 HTTP `after_id`、纪要游标及历史任务外发截图引用；仅 token 校验不能保护这些持久引用。

### 2.7 声纹片段

`voice_clips` 保存单人语音的声纹，只在 `speaker_memory.backend` 不是 `"none"` 时写入（§4.4）。
表由 `schema.sql` 在启动时建立，已有的数据库不需要迁移。

| 列             | 含义                                                         |
| -------------- | ------------------------------------------------------------ |
| `session_id`   | 所属会议；随 `sessions` 级联删除                             |
| `utterance_id` | 这段语音所在的发言；随 `utterances` 级联删除                 |
| `t_start`      | 第一段语音在会话时间轴上的起点（秒）                         |
| `t_end`        | 最后一段语音的终点                                           |
| `secs`         | 片段里语音的总时长，段与段之间的空档不算                     |
| `diar_idx`     | 语音取自说话人区分的哪个槽位；没有说话人区分时为 `0`         |
| `vector`       | float32 小端，单位长度                                       |
| `model_sha256` | 算出这个向量的模型文件的 SHA-256；不同模型的向量从不混在一起 |
| `created_at`   | 写入时的墙上时间                                             |

片段挂在发言上，不挂在说话人上：它属于谁，永远看发言当前的 `speaker_idx`。所以改发言人、合并说话人（§5.4）
都会带着片段走，删除发言（保留期到了、删除会议）时片段一起删掉。会议待删除、发言已经不在或不是语音识别来的，
就不写。

### 2.8 声纹档案

声纹档案是主持人征得本人同意后录入的人（§5.14）。档案属于整个部署，不属于某场会议。三张表由 `schema.sql`
在启动时建立，已有的数据库不需要迁移。

`voice_profiles`：`id`、`name`、`model_sha256` 和 `dimensions`（算出样本的模型；另一个模型的样本不会加进来）、
`created_at`、`updated_at`（最近一次录入或改名）。

`voice_profile_samples`：这个人在某一场会议里的声纹。`profile_id` 随 `voice_profiles` 级联删除；那场会议被删除时
`session_id` 置为 `NULL`，样本是副本，比会议活得久。`secs` 是样本背后的语音时长，`vector` 是单位长度的 float32 小端：
这位说话人的声纹片段（§2.7）里语音最多的那个模型的片段，按时长加权平均后归一化。同一场会议再录一次会替换它的样本，
每份档案保留最近 8 个样本。

`voice_profile_links`：某场会议里哪位说话人是哪份档案，主键 `(session_id, speaker_idx)`，随会议和档案级联删除。
一份档案在一场会议里最多关联一位说话人。把说话人合并到另一位时，被合并的那位的关联删掉。

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
  `{"type": "notice", "level": "warn", "code": "asr.unavailable", …}`（§6.1.1），丢掉当前识别段的
  半截状态，然后按 1、2、4…秒（上限 30 秒）退避，对**同一个后端实例**反复 `start()`；成功后推一条
  `level: "info"`、`code: "asr.recovered"`。音频计时和音频下传全程不受影响。失败和恢复各提示一次，
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

### 4.4 声纹片段与声纹服务

跨会议记住说话人（#25，设计见 [design/speaker-memory.md](design/speaker-memory.md)）的第一步是在会议中采集
声纹片段。`speaker_memory.backend = "none"` 时下面的事都不做。

**采集**（`voice/clips.py`，由记录器驱动）。记录器把收到的 16 kHz 音频（环形缓冲保留 60 秒）、每段识别语音的
时间范围及它落进或并入的那条发言、助理说话的时段交给 `VoiceClipper`。一条发言不再增长（来了别的发言，或过了
`merge_gap_secs + 1` 秒）之后再等 6 秒音频，让说话人区分和事后更正（§4.3）稳定下来，然后处理。管线正常结束时剩下的发言立即处理（最多 5 秒）；管线被取消（例如连接断开）时直接丢弃。

1. 取这条发言各段语音的时间范围，去掉助理说话的时段（回声；每段往后多算 0.3 秒）。
2. 有说话人区分时，只留恰好一个槽位在说话的部分，并且只留发言的说话人那个槽位；说话人未知或是手动指定的，
   取说得最多的槽位。没有说话人区分时整段都算，槽位记 `0`。
3. 每段两端各裁掉 0.1 秒，丢掉短于 0.3 秒的段，其余按顺序拼成语音不超过 6 秒的窗口；不足 1 秒的窗口丢掉。
4. 每个窗口送声纹服务算向量，连同发言编号落库（§2.7）。

最多 32 条发言排队，再多就丢最旧的。任何失败（声纹服务连不上、一次请求超过 5 秒、返回无效、落库出错）都只丢掉
这一个窗口，每次中断只记一次日志；字幕和识别照常（硬规则 7）。

**声纹服务**（`voice/service.py`）。本项目自己的独立进程，只监听本机回环地址。`speaker_memory.base_url`
也必须是写明端口的回环地址；应用访问它时不走系统代理。

| 方法与路径       | 请求与响应                                                                                                      |
| ---------------- | --------------------------------------------------------------------------------------------------------------- |
| `GET /health`    | 模型加载好之后返回 200                                                                                          |
| `GET /v1/info`   | `{"dimensions", "model_sha256", "sample_rate": 16000}`；`model_sha256` 是模型文件的 SHA-256                     |
| `POST /v1/embed` | 请求体：16 kHz 单声道 16 位小端 PCM（`application/octet-stream`），0.3–30 秒 → `{"embedding": [...]}`，单位长度 |

`/v1/embed` 在请求体长度不对时返回 400，模型从这段音频算不出向量时返回 422；错误都是 `{"error": "<说明>"}`。
客户端第一次成功读到 `/v1/info` 后缓存它，维度不对或含非有限值的向量一律不收。

## 5. HTTP 接口

除信令外，全部是 JSON；业务错误返回 `{"error": "<中文说明>", "code", "params"}` 和合适的状态码，`code` 标明是哪一条错误（§5.9）。
开发时前端跑在 5173 端口并把 `/api` 代理到应用（见 `client/vite.config.ts`）。
就绪检查的 503 响应保留 §5.8 定义的状态快照。需要登录时，中间件保护 `/api`
及其下路径，§5.7 的公开认证接口除外。§5.8 的三个运维 GET 位于 `/api` 之外，保持匿名。

### 5.1 会话与对时

| 方法与路径                                           | 请求                  | 响应                                                                                                                             |
| ---------------------------------------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/time`                                      | —                     | `{"server_time": <Unix 秒，浮点>}`                                                                                               |
| `GET /api/sessions?limit=20&before=<last_active_at>` | —                     | `{"items": [会话摘要]}`，按 `last_active_at` 倒序；`before` 用来翻页                                                             |
| `GET /api/sessions/{id}`                             | —                     | 会话摘要 + `"screen": {配置的 screen 段}` + `"connections": [{connected_at, disconnected_at, t_from, t_to}]`；不存在返回 404     |
| `GET /api/session`                                   | —                     | 「当前会话」：最近连上的那场进行中的会议；没有就取最近一个未结束的；都没有返回 404。格式同上。同时进行好几场时请改用带编号的接口 |
| `PATCH /api/sessions/{id}`                           | `{"title"?, "keep"?}` | 更新后的会话摘要；标题去首尾空白、最长 200 字；keep 是布尔值                                                                     |
| `POST /api/sessions/{id}/end`                        | —                     | `{"id", "ended_at"}`。会话正在进行时同时断开它的连接，并在后台生成最后一份滚动纪要（响应不等它）                                 |
| `POST /api/session/end`                              | —                     | 同上，作用于当前会话                                                                                                             |
| `POST /api/sessions/{id}/text`                       | `{"text": "…"}`       | `{"accepted": <boolean>, "notice": <notice 或 null>}`。旁观这场会议的设备打字提问（见下）                                        |
| `GET /api/sessions/{id}/events`                      | —                     | 旁观这场会议的设备用的服务端推送事件流（见下）                                                                                   |
| `DELETE /api/sessions/{id}`                          | —                     | 数据库、向量、文件完整移除后才返回 `{"id"}`；忙时 409，删除失败 500（§8.4）                                                      |

**会话摘要**：`{"id", "title", "keep": <boolean>, "deletion_pending": <boolean>, "started_at", "ended_at", "last_active_at", "state": "live" | "interrupted" | "ended",
"duration_secs", "utterance_count", "speakers": ["王老师", …], "preview": [{"speaker", "text"}（最后两条发言）]}`。
`duration_secs` 是各次连接实际时长之和，不含中断的空档；正在进行的那一段算到现在。

补充说明（`web/sessions_api.py`）：`GET /api/sessions` 的参数 `limit`（1–500，默认 20）、`before`；列表响应是 `{"items": [...]}`。`GET /api/session` 与 `GET /api/sessions/{id}` 的响应是会话摘要加上 `screen`、`members`（配置里的成员名单，给说话人改名当候选）和 `connections`（`[{connected_at, disconnected_at, t_from, t_to}]`）。`PATCH` 的 `title` 去首尾空白、最长 200 字，返回更新后的会话摘要。`POST …/end` 返回 `{"id", "ended_at"}`；会话正在进行时，先给页面发 `session_closed(reason="ended")`、取消管线、等它收尾，再写 `ended_at`。找不到会议返回 404 `找不到这场会议`；没有任何会议时 `GET /api/session` 返回 404 `现在没有会议`；`DELETE` 一场进行中的会议返回 409。

**其他设备打字提问。** 只读旁观一场进行中会议的页面（不持有连接）用 `POST /api/sessions/{id}/text` 打字提问。
文字进入和会议页面 `text_input` 同一个队列，处理方式相同（§6.3）：写成一条发言、只用文字回答、助理忙时排队。
处理结果放在响应里返回，而不是作为提示发给会议页面：`notice` 是会议页面本来会收到的 §6.1 `notice` 消息
（`text.empty`、`text.too_long`、`text.queue_full` 时 `accepted: false`；`text.queued` 时 `accepted: true`），
直接交给模型时为 `null`。回答写成助理的发言，作为 `utterance` 从旁观页面的事件流推过去（见下）。
会议不存在返回 404 `session.not_found`；会议没有活动连接、或管线还没就绪时返回 409 `session.not_live`。

旁观者提的问题只用文字回答，不管语音合成怎么配置，都不会在会议室里念出来：会议室的扬声器属于持有连接的那台设备，
旁观者却可能在任何地方。旁观者和会议页面共用文字输入队列，所以合起来最多有五条在排队（§6.3）。

文字输入的校验和排队提示（`text.empty`、`text.too_long`、`text.malformed`、`text.queued`、
`text.queue_full`）仅返回请求者：录音页面的 RTC 数据通道或提交请求的 HTTP 响应。
它们不进入会议事件历史或其他页面的事件流；会议级服务故障和恢复提示仍然共享。

**实时旁观一场会议。** 只读页面打开 `GET /api/sessions/{id}/events`，不发 offer、不请求麦克风、不成为录音所有者。
这是单进程内存事件历史，不是持久消息队列。§5.7 的共享口令门禁照常生效，会议访问模式下事件流还要遵守 §5.11。

事件流使用命名 SSE。`snapshot` 和 `meeting` 带 `id: <epoch>:<seq>`，JSON 为
`{session_id, epoch, seq, data}`。每会议 epoch 包含随机进程标识与会议编号；每次发布递增序号。
`Last-Event-ID` 优先于可选的 `after` 查询游标。历史与单订阅队列各限256条；有效游标仅重放之后的事件。
无游标返回完整快照；过期、格式错误、超前、跨会议或旧进程游标先返回无id的
`reset: {reason: "cursor_unavailable"}`，再返回快照。队列溢出时，在仍能发送的情况下返回
`reset: {reason: "slow_consumer"}` 并关闭请求。

登记订阅、选择重放、捕获序号与临时状态之间没有await。随后数据库快照持有现有session work租约，
并发事件保留在队列。snapshot的data包含详情 `session`、全部 `utterances`、`speakers`、`frames`
（含描述状态）、`tasks`（含带稳定id的进度事件）与 `transient: {captions, answer, assistant_state, speaking}`。
快照整体替换持久面板，后续队列事件按稳定id更新。重放和快照补齐旧发言更正、截图与任务，不只是最后id之后的字幕。
页面拒绝其他会议和重复序号；reset清掉游标，等待新快照。

`meeting.data` 包含现有字幕、发言/更正、说话人/合并、截图/描述、任务/进度，以及
`session_updated`、`session_state`、`session_deleted`、`frame_caption_status`、会议级 `notice`、
`assistant_state`、`assistant_speaking`、`assistant_answer: {response_id, phase, text}`。
phase为 `started`、`text`、`ended`、`interrupted`；text为累计内容，不是追加增量。回答进行中
`text` 最多每 0.25 秒发一次，`ended` 和 `interrupted` 总是带完整内容。
stable与unstable都为空的caption撤回临时段，后续快照也不保留；不会持久化空发言。
记录器、ASR状态、回答帧和后台任务绑定来源会议，切换当前录音后迟到结果仍归原会。
仅面向提问者的拒绝提示留在HTTP响应或录音通道；旁听文字提问仍只出文字，不在会议室朗读。

`live` 和 `interrupted` 时保留订阅，只有 `live` 启用文字提问。会议结束、删除/待删除、切会、开始录音连接
或视图卸载时关闭旧订阅；详情404时移除已删会议。浏览器带最后游标重连，重连提示标明事件流暂时中断。

健康的流一直保持打开，5 秒没有事件就发一行 `: heartbeat` 注释。每次网络发送最多等 15 秒：
对端停止读取时关闭请求，finally 关闭生成器并释放订阅。uvicorn的 `timeout_graceful_shutdown=5` 限制应用清理前的请求/任务停机等待。
这是应用/请求期限，不保证立即物理TCP关闭：缓冲字节可保留到对端读取或关闭；被socket阻塞的reset不能算已送达，
下次连接按游标/快照恢复。

每条实际订阅在录音页watchers名单中占一项。User-Agent标签只描述连接，不代表认证用户或独立设备；
两个标签页计两条连接，网络出错后重连会换连接id和起始时间。关闭、终态、溢出或发送超时移除连接；进程重启不保留名单/重放。
未知会议返回404 `session.not_found`，待删除返回409 `session.busy`；启用共享口令时EventSource携带会话cookie（§5.7）。

PATCH 必须是 JSON 对象，至少含 `title`、`keep` 中一个，不能含未知字段。`keep` 只接受 JSON
`true`/`false`，不接受 null、数字或字符串。只改 keep 保留原标题，只改 title 保留 keep；同时改两者
是原子的。无效请求返回 400 且不改数据，未知会议返回 404。进行中的会议也可改 keep。
keep 豁免全部四类自动清理，但不拦截人工删除。

待删除会议仍出现在列表/详情中，让页面标明**待删除**；摘要与详情可读，`deletion_pending=true`，
内容可能已经部分移除。读取内容/下载/导出、PATCH、结束、新报告/任务与继续返回 409。
页面禁用这些操作，保留重试 DELETE，删除失败后刷新列表/详情。keep 不能撤销待删除。
完整删除成功返回 200 `{"id"}`；再次删除已完整移除的编号返回 404；同编号已被另一轮删除持有时返回 409。
鉴权与 CSRF 仍遵循 §5.7。没有活动会议时，「当前会话」查询跳过待删除会议。

对时：浏览器记下发请求前后的本地时间 `t0`、`t1`，则「服务端时间 − 本地时间」的偏移量
≈ `server_time − (t0 + t1) / 2`。连接建立时测 3 次取往返最短的一次。

### 5.2 WebRTC 信令

麦克风初始化失败后，下一次主动开始或继续会重新初始化设备，恢复授权后不需要刷新页面。
SDK 重连替换 peer 时，关闭旧 peer 只保留当前 peer 仍使用的同一麦克风轨道；结束会议仍停止所有发送轨道。

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
- 这场会议正在别的连接上进行时，新连接顶替那一路（一场会议同一时刻只有一路连接）；
- 否则，进行中的会议已经有 `session.max_live_meetings` 场时：上限是 1 就顶替正在进行的那一场；上限大于 1 就在协商之前
  以 409 `session.capacity` 拒绝（`params.max` 是上限）。带着已有连接 `pc_id` 的请求是重新协商，不占新位置（architecture.md §3.2）。开了 `server.meeting_access` 时先做 §5.11 的权限检查，无权录音的调用者得到 401、403 或 404，而不是 409。

实现：`session_id` 为 `null` 等同于不带；不是字符串或是空串返回 400；会议不存在返回 404，两种情况都不会进入协商、
不会建管线。新建不再结束别的会议。服务端在管线里做的事：`SessionManager.attach` 顶替这场会议的旧连接、
重新打开已结束的会议、算 `base_secs = max(此刻 − started_at, 时间轴已经用到的地方)` 并写一行连接记录；会议记录器和识别服务
都从 `base_secs` 接着计时；实时模型的上下文由数据库重建（`build_context_messages`，最新纪要 + 最近 `keep_recent_minutes` 分钟），
重建失败就从空的开始；开了预热的话连接建立后立刻预热一次。

实现要点（`web/app.py`）：`POST /api/offer` 不用 FastAPI 的请求体类型，而是读原始 JSON 再交给
`SmallWebRTCRequest.from_dict`——浏览器端 SDK 把连接参数放在**驼峰**的 `requestData` 里，只有 `from_dict`
认识它（也接受 `request_data`）；格式不对返回 400。协商成功后，`run_bot(connection, request_data, resources)`
作为后台任务运行到连接断开。`PATCH /api/offer` 追加 ICE 候选，请求体 `{"pc_id", "candidates": [{"candidate",
"sdp_mid", "sdp_mline_index"}]}`，响应 `{"status": "success"}`。`GET /` 在 `client/dist` 不存在时返回一段
「请先构建客户端」的提示页（503），不是 500。错误一律是 `{"error": "<中文说明>", "code", "params"}`（包括 404、422，见 §5.9）。

**ICE 服务器。** `server.ice_servers` 在连接的两端都用：服务端交给 `SmallWebRTCRequestHandler`；浏览器每次自己发起连接之前
取一次 `GET /api/ice`，把结果交给 WebRTC SDK（`SmallWebRTCTransport.iceServers`）。每一项的形状与浏览器的 `RTCIceServer`
相同，只有配置了 `username`、`credential` 的项才带这两个字段。凭据在应用启动时从 `credential_env` 指定的环境变量读出。
能访问 `/api` 的人都能读到它，所以用 TURN 时应当设访问口令（§5.7），并让 TURN 服务器只中继到本机。请求失败时浏览器不带
ICE 服务器连接，同一局域网内照样能连上。列表为空（默认）= 只用本机候选地址。

### 5.3 截图

浏览器只在已连接时打开屏幕选择器，同时最多等待一个选择器。结束会议、断线或卸载页面会让
等待中的选择作废；之后才返回的流会停止全部轨道，不开始采集或上传截图。

`POST /api/frames`，`multipart/form-data`：

| 字段          | 类型         | 说明                                                                                                            |
| ------------- | ------------ | --------------------------------------------------------------------------------------------------------------- |
| `captured_at` | 浮点字符串   | 采集时刻，**已换算成服务端时钟**的 Unix 秒                                                                      |
| `image`       | 文件         | `image/webp` 或 `image/jpeg`，长边不超过 `screen.max_side_px`                                                   |
| `session_id`  | 字符串，可选 | 截图属于哪场进行中的会议。同时进行好几场时、或开了 `server.meeting_access` 时必填；不给时用唯一一场进行中的会议 |

响应 `{"id": <截图编号>, "t": <会话时间轴秒>}`。校验：当前有进行中的会议；文件类型合法；
大小 ≤ 4 MB；`captured_at` 与服务端当前时间相差不超过 60 秒（否则 400）。
宽高由服务端解码图片得到。

`GET /api/frames/{id}/image` 返回图片文件；`GET /api/frames?session_id=` 返回
`{"items": [{"id", "t", "width", "height", "caption", "caption_status"}]}`（按时间升序；`session_id` 省略 = 当前会话）。

补充说明（`screen/ingest.py`、`web/frames_api.py`）：

- 状态码：没给 `session_id` 而没有进行中的会议 → 404 `frames.no_live_session`；`session_id` 指的会议没有在进行 → 409 `session.not_live`；不是图片、不是 WebP / JPEG、长边超过 `screen.max_side_px`、
  `captured_at` 不是数字或与服务端时间相差超过 60 秒、同时进行好几场会议或开了 `server.meeting_access` 却没给 `session_id` → 400；超过 4 MB → 413；`screen.enabled = false` → 403；缺字段 → 422；落盘失败 → 500（同时撤销数据库里那一行）。
- 文件类型以**解码结果**为准，不看浏览器声称的类型；文件名是 `<6 位截图编号>.webp`（或 `.jpg`）。
- `t = max(0, captured_at − sessions.started_at)`。
- **画面有没有变由服务端再判一次**：把新图缩成 64×36 灰度，与这场会议里上一张「变了」的图比（分成 8×6 的小块，取变化最大的那一块的平均差 / 255，
  阈值固定为 0.04，`screen.change_threshold` 更小时取它；浏览器端用同一个算法、以 `screen.change_threshold` 决定要不要马上上传，
  只靠兜底间隔传上来的图只要画面变了，照样单独生成摘要。不用整张的平均差：白底幻灯片只换了文字时整张只差 1% 左右）。没变的图（浏览器的兜底上传）照常进时间线、照常推 `frame`，但不再单独生成摘要、
  不往实时模型的上下文追加 `[画面]` 行，摘要沿用上一张的。只和上一张「变了」的图比，所以缓慢的累计变化迟早会超过阈值。
  这份「上一张」只在内存里，服务重启后第一张按「变了」处理。
- **画面摘要（`screen/caption.py`）**：每场会议只做最新一张待处理的截图，积压的记为 `skipped`；同时进行好几场时按排队先后轮流；助理应答时暂停，
  被取消的那张应答完再做（除非又来了更新的画面）；模型出错或给出空摘要记为 `failed`。成功后写回数据库、推 `frame_caption`、
  向实时模型的上下文追加 `[画面 时:分:秒] 摘要`（`run_llm=False`）。画面没变的截图沿用上一张的摘要并推 `frame_caption`，
  但不再追加上下文行；上一张没有成功的摘要时，它当作新画面重做（相当于每个兜底间隔重试一次）。
  摘要是「无关画面」时不进上下文。会议已经换了一场时，摘要只入库，不推给新的会议。
  `screen.caption_provider = "agent_llm"` 时由后台 agent 的那个模型生成（要求 `agent.supports_vision`，且地址和模型名已填）。
- 图片响应带 `Cache-Control: private, no-cache`：不能把过期或已删除图片当成永久可用的内容继续展示。

### 5.4 转录与说话人

下面所有接口的 `session_id` 都可省略，省略时指「当前会话」（§5.1）。

| 方法与路径                                          | 说明                                                                                                                                                                                                                                                          |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/utterances?session_id=&after_id=&limit=`  | id 大于 `after_id` 的发言，按 `id` 升序。重连后补齐字幕用                                                                                                                                                                                                     |
| `GET /api/utterances?session_id=&tail=50`           | **最近 50 条**（仍按 `id` 升序返回）。页面一打开就显示「最近对话」用；与 `after_id` 互斥                                                                                                                                                                      |
| `GET /api/utterances?session_id=&before_id=&limit=` | 向更早的方向翻页，按 `id` 升序返回                                                                                                                                                                                                                            |
| `GET /api/speakers?session_id=`                     | `[{"idx", "display_name"}]`；开启 `speaker_memory` 时每项还有 `voice_secs` 和 `voice_profile`（见下）                                                                                                                                                         |
| `PUT /api/speakers/{idx}`                           | 请求体 `{"session_id"?, "display_name": "王老师"}`；改名后向浏览器广播 `speaker` 消息                                                                                                                                                                         |
| `POST /api/utterances/speaker`                      | 请求体 `{"session_id"?, "ids": [发言编号…], "speaker_idx": 2}` 或 `{…, "new_speaker": "张老师"}`：把选中的发言改成另一个说话人（已有的，或新建一个）；响应 `{"speaker": {idx, display_name}, "ids": [真正改了的]}`；会议正在进行时逐条广播 `utterance_update` |
| `POST /api/speakers/{idx}/merge`                    | 请求体 `{"session_id"?, "into": 2}`：把 `idx` 的全部发言并入 `into`，删除 `idx`；响应 `{"from", "into", "display_name", "moved"}`；会议正在进行时广播 `speakers_merged`                                                                                       |
| `DELETE /api/sessions/{session_id}/voice`           | 清除这场会议的声纹片段（§2.7），发言保留；响应 `{"id", "clips": <删掉的片段数>}`                                                                                                                                                                              |

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

声纹片段：`voice_secs` 是这位说话人名下各条发言的声纹片段语音总时长，保留一位小数；改发言人、合并说话人后跟着变。
`speaker_memory.backend = "none"` 时没有这个字段。`voice_profile` 是关联的声纹档案的 `{"id", "name"}`（§5.14），
没有关联时为 `null`，功能关闭时同样没有。清除声纹片段对进行中和已结束的会议都可用，不看配置；
会议进行中清除后，之后的发言照常采集。会话不存在返回 404。会议权限模式下只有主持人能清除（§5.11），并记一条 `voice.erase`（§5.13）。

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
- 列表项：`{"id", "label", "goal", "status", "brief", "error", "error_code", "error_params", "modality", "created_at", "started_at", "finished_at"}`；失败原因的字段见 §5.10。
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

| 方法与路径                          | 说明                                                                                                                                                               |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GET /api/export/{session_id}.md`   | Markdown 转录：标题与时间 → 纪要 → 按时间排列的发言（相邻同一说话人的合并成段）→ 截图引用 → 任务结果                                                               |
| `GET /api/export/{session_id}.json` | 结构化 JSON：会话、说话人、发言、截图、纪要、任务（含事件）、各次连接、最近一份报告                                                                                |
| `GET /api/export/{session_id}.zip`  | 完整包：`transcript.md`、`session.json`、`report.md`（若有）、`frames/`、`tasks/` 下的产物                                                                         |
| `POST /api/sessions/{id}/report`    | 触发生成会后报告（异步）；响应 202 `{"report_id", "status": "running"}`；该会话已有一份在生成时返回 409                                                            |
| `GET /api/sessions/{id}/report`     | 最近一份：`{"id", "status": "running" \| "done" \| "failed", "created_at", "provider", "text_md", "error", "error_code", "error_params"}`（§5.10）；还没有返回 404 |
| `GET /api/sessions/{id}/report.md`  | 最近一份报告的文本下载                                                                                                                                             |

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

### 5.7 访问口令与单点登录

需要显式开启。三种登录方式配了任一种就需要登录：共享的访问口令（`server.password_env`）、一个或多个
OpenID Connect 身份提供方（`server.oidc`）、受信任反向代理传来的身份头（`server.proxy_auth.trusted_proxies`）。
都没配（默认）时以下内容都不生效：`/api` 不设限，`GET /api/auth` 返回 `enabled: false`。配了任一种时，
`web/auth.py` 里的中间件拦住 `/api/` 下除 `GET /api/auth`、`POST /api/auth/login` 和两个 `GET /api/auth/oidc/…`
以外的全部路径。静态页面（`/`、`/assets/…`）不拦截，里面没有会议数据。几种方式可以同时开；无论用哪种登录，
得到的都是下面同一种会话 Cookie 和 CSRF 令牌，其余接口不知道设备是怎么登录的。

| 方法与路径                               | 请求                                | 响应                                                                                                                                                                   |
| ---------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/auth`                          | —                                   | `{"enabled", "authenticated", "csrf_token", "methods"}`；未登录或不需要登录时 `csrf_token` 为 `null`                                                                   |
| `POST /api/auth/login`                   | `{"password": "..."}`               | 同一个对象，`"authenticated": true`，并下发会话 Cookie。口令不对 401；尝试过多 429，`Retry-After` 给出秒数；请求体超过 16 KiB 413；没配访问口令 404 `auth.not_enabled` |
| `POST /api/auth/logout`                  | —                                   | 同一个对象，`"authenticated": false`、`"csrf_token": null`，并清除 Cookie                                                                                              |
| `GET /api/auth/oidc/{provider}/start`    | 查询参数 `next`（可选）             | 302 跳到身份提供方的授权地址；`provider` 不存在 404 `auth.provider_not_found`。连不上身份提供方时 303 回到页面，带 `#login_error=sso.unavailable`                      |
| `GET /api/auth/oidc/{provider}/callback` | 查询参数 `code`、`state` 或 `error` | 303 跳到 `next` 并下发会话 Cookie；失败时 303 跳到 `next` 并带 `#login_error=<代码>`，不下发会话                                                                       |

`methods` 为 `{"password": bool, "oidc": [{"id", "display_name"}, …], "proxy": bool}`，列出登录页能提供的方式；
无论是否已登录都会给出，不需要登录时为 `false`、`[]`、`false`。

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

#### OpenID Connect 单点登录

`server.oidc` 的每一项是一个身份提供方，用上面地址里的 `id` 区分。流程是带 PKCE（`S256`）和 `nonce` 的授权码流程；
`client_secret_env` 指向密钥时作为机密客户端，否则作为公开客户端：

1. `start` 读取 `<issuer>/.well-known/openid-configuration`（缓存一小时；超时 10 秒），要求其中的 `issuer`
   与配置逐字相同。生成随机的 `state`、`nonce` 和 code verifier，连同身份提供方、回调地址和 `next` 在内存里保留
   10 分钟（最多 1000 个进行中的登录，满了丢最早的）。下发 Cookie `am_sso`（`HttpOnly; SameSite=Lax;
Path=/api/auth/oidc/; Max-Age=600`，HTTPS 时加 `Secure`），值是一个随机数，它的 SHA-256 和进行中的登录存在一起；
   然后带 `response_type=code`、`client_id`、`redirect_uri`、`scope`、`state`、`nonce`、`code_challenge`、
   `code_challenge_method=S256` 跳到授权地址。
2. `callback` 取出并删除 `state` 对应的进行中登录（每个 `state` 只能用一次）；它不存在、已过期、不属于这个身份提供方，
   或者与同一浏览器的 `am_sso` Cookie 对不上，都算失败。然后用 code verifier 和同一个回调地址在令牌接口换取令牌，
   客户端认证用 HTTP Basic（`client_secret_basic`），身份提供方只声明支持 `client_secret_post` 时改用表单。
3. ID 令牌必须用非对称算法（`RS*`、`PS*`、`ES*`、`EdDSA`）签名，算法须在身份提供方的
   `id_token_signing_alg_values_supported` 里（没有这一项时按 `RS256`），公钥取自它的 `jwks_uri`（遇到未知的 key id
   时重新获取，最多每 30 秒一次）。`iss` 须等于 issuer，`aud` 须包含 client id（有 `azp` 或有多个受众时 `azp` 须等于
   client id），`exp`、`iat`、`sub` 必须有，`nonce` 须对上，时钟允许相差 60 秒。不签名的令牌和 HMAC 算法一律拒收。
4. 用户名取 `username_claim` 指定的声明（默认 `preferred_username`，缺失时用 `sub`），显示名取 `name`（缺失时用
   用户名），用户组取 `groups_claim` 指定的声明（字符串列表或单个字符串）。ID 令牌里缺用户名或用户组声明、而身份提供方
   有 `userinfo_endpoint` 时，用 access token 从那里补全；其中的 `sub` 必须与 ID 令牌相同。
5. `allowed_users` 或 `allowed_groups` 不为空时，用户名须在前者里，或者属于后者中的某个组；都为空时身份提供方认证过的人
   都可以登录。

回调地址是 `<public_url>/api/auth/oidc/<id>/callback`，`public_url` 取 `server.public_url`，留空时取请求的协议和主机
（与上面的客户端地址一样，只有 uvicorn 信任代理时才采用 `X-Forwarded-Proto` 和 `Host`）。在身份提供方那边登记的
必须正是这个地址。身份提供方的所有接口都必须是 HTTPS，issuer 是本机回环地址时除外。ID 令牌、access token 和声明
都不保存，只签发会话 Cookie。

`next` 必须是本站的路径（以一个 `/` 开头，最多 2000 个字符），否则换成 `/`。失败原因放在地址的片段里交给页面，
服务端不会再看到它，页面读完就去掉：

| `login_error`     | 原因                                                                  |
| ----------------- | --------------------------------------------------------------------- |
| `sso.unavailable` | 连不上发现文档、公钥、令牌或 userinfo 接口                            |
| `sso.cancelled`   | 身份提供方返回 `error=access_denied`（用户拒绝或取消）                |
| `sso.expired`     | `state` 未知或已过期，或 `am_sso` Cookie 缺失、对不上                 |
| `sso.denied`      | 在身份提供方那边登录成功，但不在 `allowed_users`、`allowed_groups` 里 |
| `sso.failed`      | 其他错误：令牌被拒收、身份提供方返回错误                              |

具体原因记在服务端日志里。只有登录那一下需要身份提供方：它离线时已经登录的设备照常可用，访问口令也始终可用。

#### 反向代理传来的身份头

`server.proxy_auth.trusted_proxies` 列了地址或网段时，直连对端是其中之一、并带有非空 `user_header`
（默认 `X-Forwarded-User`）的请求，不需要 Cookie 就以该用户身份登录。直连对端是 uvicorn 按 `X-Forwarded-For`
改写之前的 TCP 对端，所以客户端没法借这个头冒充代理地址。其他地址发来的同名头一律忽略。可选的 `name_header` 和
`groups_header` 给出显示名和用户组（逗号分隔）；头的值按 UTF-8 读取。`allowed_users`、`allowed_groups` 与
OpenID Connect 一样限定谁能进；被拒绝的用户按未登录处理。

`GET /api/auth` 对这样的请求报告已登录，CSRF 令牌由用户名在会话令牌的同一密钥下派生；改动性的请求照常需要它。
只要代理还在发这个头，退出登录就不起作用。代理必须把它自己没有认证的请求里的这个头去掉。

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
    "at_most_meetings": {
      "type": ["integer","null"],
      "minimum": 0,
      "maximum": 16
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
      "properties": {"enabled":{"type":"boolean"},"depth":{"$ref":"#/$defs/at_most_meetings"}}
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
      "properties": {"status":{"enum":["ok","partial"]},"lifecycle":{"$ref":"#/$defs/lifecycle"},"live_connections":{"$ref":"#/$defs/at_most_meetings"},"caption_lag_seconds":{"$ref":"#/$defs/seconds"},"caption_sample_age_seconds":{"$ref":"#/$defs/seconds"},"queues":{"$ref":"#/$defs/queues"},"task_counts":{"$ref":"#/$defs/task_counts"},"task_counts_age_seconds":{"$ref":"#/$defs/seconds"},"services":{"$ref":"#/$defs/services"},"service_snapshot_age_seconds":{"$ref":"#/$defs/seconds"}}
    }
  }
}
```

**指标字典。** 这些是应用实例的 gauge，不是进程启动后的累计 counter。
内存字段每次从当前资源读取，缓存的数据库/服务字段给出各自 age。

| 字段                            | 精确口径及重置/故障语义                                                                                                                                                                                                                                             |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live_connections`              | 当前已向 `SessionManager` 登记 worker 和 recorder 的活动连接数，取 0 到 `session.max_live_meetings`。管线仍在组装或接管间隙的不计；资源不存在/不可读为 `null`。不计历史连接行数                                                                                     |
| `caption_lag_seconds`           | 新 `ASRDelta` 第一次成功发送非空 `CaptionUpdate` 后，按共享会话音频时间轴计算 `raw_lag = recorder.elapsed_secs - delta.audio_end_secs`。只接受有限且 `raw_lag >= -1 / ASR_SAMPLE_RATE` 的值，再保存 `max(0, raw_lag)`。同时进行好几场时报最大的那一场及它的样本年龄 |
| `caption_sample_age_seconds`    | 距该成功样本的单调时钟秒数。无样本时两个字幕字段都为 `null`                                                                                                                                                                                                         |
| `queues.transcript_retry`       | 各场活动 recorder 等待下次存储重试的未写入发言数之和；空闲为 0；已登记连接的 recorder 不存在/不可读时为 `null`                                                                                                                                                      |
| `queues.screen_caption.enabled` | 画面摘要 worker 是否启用，启动前按上文的配置后备规则给出                                                                                                                                                                                                            |
| `queues.screen_caption.depth`   | 等待处理的新画面槽位数，每场进行中的会议最多 1 个，不含正在处理的画面及复用摘要的 followers。关闭为 0；启用但 worker 不可用/不可读时为 `null`                                                                                                                       |
| `queues.agent_tasks`            | 与 `task_counts` 同一数据库聚合快照的 `queued` 数；聚合不可用时为 `null`                                                                                                                                                                                            |
| `task_counts`                   | 对**数据库全部保留任务**按 `queued/running/succeeded/failed/cancelled` 分组计数。空库为五个零；关闭 agent 不会隐藏保留任务；删除任务可降低计数；重启恢复结果体现在下次快照。查询失败时整组为 `null`，不能使用 `TaskManager._running`                                |
| `task_counts_age_seconds`       | 成功计数快照的单调时钟 age，有效期严格小于 5 秒；计数不可用时为 `null`                                                                                                                                                                                              |
| `services`                      | 与就绪接口共用的固定名称快照及状态语义                                                                                                                                                                                                                              |
| `service_snapshot_age_seconds`  | 自该轮服务刷新完成后的单调时钟 age，有效期严格小于 5 秒；当前快照中任一启用服务为 `unknown`，或尚无完成的刷新时为 `null`                                                                                                                                            |

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

### 5.9 错误代码

`/api` 下的错误响应一律是 `{"error": "<中文说明>", "code": "<代码>", "params": {...}}`。`code` 和 `params`
标明是哪一条错误，`params` 的值是字符串或数字，代码不带参数时为空对象。`error` 是同一条错误的中文，
由服务端按同一个代码生成。页面认得代码、参数也对得上时按界面语言翻译，否则显示 `error`。
和提示代码（§6.1.1）一样，新代码先写进这里，再写返回它的代码；服务端的表在 `src/agentic_meeting/messages.py`，
页面的在 `client/src/i18n/serverErrors.ts`，仓库测试检查三处的代码和参数一致。

`request.invalid` 用于页面不会发出的请求：缺字段、字段格式不对、导出地址不认识、请求校验失败。
它的 `detail` 是给接口调用者看的中文说明，页面不翻译。`request.failed` 用于不经本应用处理器抛出的错误（如 405），
`status` 是 HTTP 状态码。就绪检查的 503 响应（§5.8）是状态快照，不是错误。

| `code`                      | 状态码        | `params`                       | 中文 `error`                                                       |
| --------------------------- | ------------- | ------------------------------ | ------------------------------------------------------------------ |
| `request.invalid`           | 400, 404, 422 | `detail`：string               | 请求格式有误：{detail}                                             |
| `request.too_large`         | 413           | —                              | 请求体太大                                                         |
| `request.failed`            | 任意          | `status`：number               | 请求失败（{status}）                                               |
| `route.not_found`           | 404           | —                              | 没有这个接口或页面                                                 |
| `share.invalid`             | 404           | —                              | 分享无效或已到期／撤销                                             |
| `share.limit`               | 409           | —                              | 本会议最多8个有效链接                                              |
| `auth.required`             | 401           | —                              | 请先登录                                                           |
| `auth.csrf`                 | 403           | —                              | 请求缺少有效的安全令牌，请刷新页面后重试                           |
| `auth.wrong_password`       | 401           | —                              | 口令不对                                                           |
| `auth.too_many`             | 429           | —                              | 尝试次数过多，请稍后再试                                           |
| `auth.not_enabled`          | 404           | —                              | 没有启用访问口令                                                   |
| `auth.provider_not_found`   | 404           | —                              | 没有这个登录方式                                                   |
| `sso.unavailable`           | —（§5.7）     | —                              | 暂时连不上身份提供方，请稍后再试或改用其他登录方式                 |
| `sso.cancelled`             | —（§5.7）     | —                              | 登录已取消                                                         |
| `sso.expired`               | —（§5.7）     | —                              | 登录请求已过期，请重新登录                                         |
| `sso.denied`                | —（§5.7）     | —                              | 这个账号没有被允许登录                                             |
| `sso.failed`                | —（§5.7）     | —                              | 单点登录没有完成，请重试                                           |
| `access.denied`             | 403           | —                              | 无权执行此操作                                                     |
| `access.invalid_member`     | 400           | —                              | 成员不存在或不能主持会议                                           |
| `access.last_host`          | 409           | —                              | 会议必须保留一位主持人                                             |
| `access.busy`               | 409           | —                              | 录音服务正忙                                                       |
| `text.context_too_large`    | 413           | —                              | 问题和提示超出上下文预算                                           |
| `text.interrupted`          | 409           | —                              | 只读问答被实时应答打断，请稍后重新提问                             |
| `text.busy`                 | 429           | —                              | 只读问答队列已满或已有问题处理中                                   |
| `text.timeout`              | 504           | —                              | 只读问答等待超时                                                   |
| `text.failed`               | 502           | —                              | 只读问答没有完成                                                   |
| `storage.not_ready`         | 503           | —                              | 存储尚未就绪                                                       |
| `cleanup.not_ready`         | 503           | —                              | 清理尚未就绪                                                       |
| `session.not_found`         | 404           | —                              | 找不到这场会议                                                     |
| `session.none`              | 404           | —                              | 现在没有会议                                                       |
| `session.busy`              | 409           | —                              | 会议正在处理或等待删除，请稍后重试                                 |
| `session.not_live`          | 409           | —                              | 这场会议现在没有在进行，消息没有发送                               |
| `session.capacity`          | 409           | `max`: number                  | 已经有 {max} 场会议在同时进行，请等其中一场结束或断开后再开始      |
| `session.delete_incomplete` | 500           | —                              | 删除未完成，会议待删除，请稍后重试                                 |
| `session.title_too_long`    | 400           | `max`：number                  | 标题太长（最多 {max} 字）                                          |
| `speaker.not_found`         | 404           | —                              | 这场会议里没有这个说话人                                           |
| `speaker.name_too_long`     | 400           | `max`：number                  | 名字太长（最多 {max} 字）                                          |
| `speaker.not_mergeable`     | 400           | —                              | 只能合并说话人区分给出的说话人（助理、文字输入、「未知」不能合并） |
| `speaker.merge_self`        | 400           | —                              | 不能把说话人合并到自己                                             |
| `utterances.too_many`       | 400           | `max`：number                  | 一次最多改 {max} 条发言                                            |
| `voice.consent_required`    | 400           | —                              | 需要确认本人已同意保存声纹                                         |
| `voice.not_enrollable`      | 400           | —                              | 只能为说话人区分给出的说话人或手动新建的说话人录入声纹             |
| `voice.too_short`           | 400           | `have`：number, `need`：number | 这位说话人的单人语音还不够：已有 {have} 秒，需要 {need} 秒         |
| `voice.profile_not_found`   | 404           | —                              | 找不到这份声纹档案                                                 |
| `voice.profile_in_use`      | 409           | —                              | 这份声纹档案已经关联到本场的另一位说话人                           |
| `voice.model_changed`       | 409           | —                              | 这份声纹档案是用另一个声纹模型录入的，请新建一份                   |
| `frames.disabled`           | 403           | —                              | 截图功能已在配置里关闭                                             |
| `frames.no_live_session`    | 404           | —                              | 现在没有进行中的会议，截图没有保存                                 |
| `frame.not_found`           | 404           | —                              | 找不到这张截图                                                     |
| `frame.bad_format`          | 400           | —                              | 只接受 WebP 或 JPEG 格式的截图                                     |
| `frame.too_wide`            | 400           | `max`：number                  | 截图的长边不能超过 {max} 像素                                      |
| `frame.invalid`             | 400           | —                              | 上传的文件不是有效的图片                                           |
| `frame.too_large`           | 413           | `max_mb`：number               | 截图太大（最多 {max_mb} MB）                                       |
| `frame.empty`               | 400           | —                              | 上传的文件是空的                                                   |
| `frame.clock_skew`          | 400           | `max`：number                  | 截图的采集时间与服务端时间相差超过 {max} 秒，请刷新页面重新对时    |
| `frame.save_failed`         | 500           | —                              | 截图保存失败（磁盘不可写？）                                       |
| `task.not_found`            | 404           | —                              | 找不到这个任务                                                     |
| `tasks.disabled`            | 503           | —                              | 后台任务功能没有开启                                               |
| `artifact.not_found`        | 404           | —                              | 找不到这个产物文件                                                 |
| `report.session_live`       | 409           | —                              | 会议正在进行，结束或断开之后才能生成报告                           |
| `report.no_model`           | 503           | —                              | 没有可用的模型，生成不了报告                                       |
| `report.busy`               | 409           | —                              | 这场会议已经有一份报告正在生成                                     |
| `report.not_found`          | 404           | —                              | 这场会议还没有报告                                                 |
| `report.not_ready`          | 404           | —                              | 这场会议还没有生成好的报告                                         |

### 5.10 任务和报告的失败原因

失败或取消的任务、失败的报告除了中文原因 `error`，还带 `error_code` 和 `error_params`，像 §5.9 的错误一样
标明是哪一种原因。页面认得代码时按界面语言翻译，否则显示 `error`；这两个字段出现之前写下的行是
`error_code: null`、`error_params: {}`。`error` 保持中文，因为实时模型会读它（`task_status`、任务结果），导出也包含它。
服务端的表是 `src/agentic_meeting/messages.py` 的 `FAILURES`，页面的在 `client/src/i18n/serverFailures.ts`，
仓库测试检查三处一致。

| `error_code`            | `error_params`    | 中文 `error`                                                          |
| ----------------------- | ----------------- | --------------------------------------------------------------------- |
| `task.cancelled`        | —                 | 已取消                                                                |
| `task.restarted`        | —                 | 服务重启，任务没有做完                                                |
| `task.shutdown`         | —                 | 服务停止，任务没有做完                                                |
| `task.timeout`          | `minutes`：number | 超时（超过 {minutes} 分钟没有做完）                                   |
| `task.internal`         | `type`：string    | 内部错误（{type}）                                                    |
| `agent.disabled`        | —                 | 后台任务功能已在配置里关闭                                            |
| `agent.not_configured`  | —                 | 后台 agent 还没有配置（config.toml 的 [agent] 里要填模型地址和名字）  |
| `agent.not_installed`   | —                 | 没有安装后台 agent 的依赖（uv sync --extra agent）                    |
| `agent.stopping`        | —                 | 服务正在停止                                                          |
| `agent.too_many_steps`  | `max`：number     | 步骤太多（超过 {max} 步）还没有做完                                   |
| `agent.key_env_missing` | —                 | 远端模型要求密钥，但配置里没有写密钥的环境变量名（agent.api_key_env） |
| `agent.key_unset`       | `env`：string     | 远端模型的密钥没有设置（环境变量 {env} 是空的；设好之后要重新启动）   |
| `agent.key_rejected`    | `env`：string     | 远端模型不接受这个密钥（环境变量 {env}）                              |
| `agent.unreachable`     | —                 | 连不上远端模型                                                        |
| `agent.http_error`      | `status`：number  | 远端模型返回了错误（{status}）                                        |
| `agent.error`           | `type`：string    | agent 运行出错（{type}）                                              |
| `report.interrupted`    | —                 | 服务停止了，这份报告没有生成完                                        |
| `report.timeout`        | —                 | 生成超时                                                              |
| `report.failed`         | `detail`：string  | {detail}                                                              |

`type` 是异常的类名，`env` 是配置里密钥变量的名字，`detail` 是报告失败时的异常原文（最多 200 字，超出加省略号），
这些都不翻译。

### 5.11 会议访问

会议访问给每个人一个本地账号，并把每场会议限定给获得授权的人。它不需要身份提供方，离线也能用。默认关闭
（`server.meeting_access = false`），此时 §5.7 不变：一个可选的共享口令，通过的人能看到所有会议。

**账号。** 每个 `[[server.local_users]]` 条目有稳定的 `id`、`display_name`、`password_env` 和
`can_host`（默认 `false`），至少要有一个账号能主持。口令环境变量缺失或条目无效时启动失败，不会悄悄关掉检查。
没有注册和邮件找回：由运维添加账号并把口令交给本人。服务器用主体 `local:<id>` 识别一个人，从不用显示名。
`id` 不能转给别人；换口令时保留 `id`，会议授权不变，旧登录全部失效。

**登录。** 会议访问模式下 `POST /api/auth/login` 接受 `{user_id, password, site_password?}`。
配置了 `server.password_env` 时必须带 `site_password`；只有共享口令什么也拿不到。`GET /api/auth`
增加 `meeting_access`，登录后还有本人的 `principal: {id, display_name, can_host}`。§5.7 的限速、
请求体上限、scrypt 和 CSRF 仍然有效。登录记在 `login_sessions` 里，存的是 cookie 秘密的摘要、主体和到期时间，
从不存原始 cookie 或口令。`POST /api/auth/logout` 结束当前登录，`POST /api/auth/logout-all`
结束本人的所有登录。

**单点登录。** §5.7 的 OpenID Connect 身份提供方在会议访问模式下同样可用。通过身份提供方 `<id>` 登录的人是主体
`oidc:<id>:<sub>`，`sub` 取自 ID 令牌，所以身份提供方那边改了用户名或显示名，会议授权也不变。每次登录都会把主体和显示名
记进 `principals`，并在 `external_principals(principal_id, can_host)` 里记下它能否主持：用户名在该身份提供方的
`host_users` 里，或者属于 `host_groups` 中的某个组时可以，每次登录重新判断。不要求站点口令，因为身份提供方已经认证过这个人；
谁能进由 `allowed_users` 和 `allowed_groups` 决定。登录和本地账号一样记在 `login_sessions` 里，退出、全部退出、到期和撤权
的效果都相同。把某个身份提供方从配置里删掉后，下次启动起经它登录的设备都失效，它的主体也不能再被授权。`GET /api/auth` 增加与 §5.7 相同的 `methods`，其中 `password` 总是真、`proxy` 总是假。反向代理传来的身份头
不能和会议访问模式同时使用，两者都配置时配置检查报错。

**角色。** 授权存在 `meeting_members(session_id, principal_id, role, allow_questions)` 中。创建会议时，
创建者的 `host` 授权在同一个事务里写入；只有 `can_host` 的账号能创建会议。`watcher` 能读会议，
`allow_questions` 为真时才能提问（默认假）。主持人还能录音和继续、改名、保留、结束和删除会议、改说话人、清除声纹片段、录入和取消关联声纹、
生成报告、取消任务，以及管理成员和分享。每场会议至少保留一位主持人，只有 `can_host` 的账号和能主持的单点登录主体能被设为主持人。
角色从不取自请求数据。会议响应带上本人的 `access: {role, allow_questions, tool_enabled, share_expires_at}`，
页面据此显示对应的控件；服务器仍会自己检查。

| 方法与路径                                         | 权限   | 结果                                                             |
| -------------------------------------------------- | ------ | ---------------------------------------------------------------- |
| `GET /api/sessions/{id}/members`                   | 主持人 | `{items: [{principal_id, display_name, role, allow_questions}]}` |
| `PUT /api/sessions/{id}/members/{principal_id}`    | 主持人 | 给已知的主体设置 `{role, allow_questions}`                       |
| `DELETE /api/sessions/{id}/members/{principal_id}` | 主持人 | 撤销授权；不能移除最后一位主持人                                 |
| `GET /api/sessions/{id}/access-log`                | 主持人 | 访问日志（§5.13）                                                |

页面显示本人的主体 ID；主持人输入准确的 ID 来授权。已知的主体是配置里的账号，或者至少登录过一次的单点登录主体。
没有用户目录。

**开启前的会议。** 没有主人的会议在会议访问模式下看不到，也不会分给第一个访问的人。运维先停掉服务，
运行 `agentic-meeting access list-unclaimed`，再对一个能主持的账号运行
`agentic-meeting access claim <session-id> --owner <user-id>`。重复同样的认领无害；认领别人已有的会议会失败，
会议或账号 ID 打错时给出明确提示而不是报错堆栈。服务占用数据目录时两条命令都拒绝运行：`serve` 会锁住数据目录，
所以同一目录上的第二个服务也无法启动。再次关闭会议访问模式会把所有会议重新开放给共享口令，授权保留，
以后重新开启时继续生效。

**检查范围。** 没有登录时 API 返回 401。没有授权的会议返回与不存在的会议相同的 404；能读但权限不够时返回
403 `access.denied`。列表先过滤再分页，当前会议相关的接口只会落到本人能读的会议上。检查覆盖路径、查询参数和
请求体里的会议 ID，以及截图、任务、导出文件和报告所属的会议。上传截图要写明 `session_id`，并且需要对正在录音的
那场会议有主持权限。`/api/time` 和 `/api/ice` 需要登录；§5.8 的健康检查路由仍然匿名，不含会议内容。

**连接与撤权。** 每个事件流和 WebRTC 连接都绑定一个登录、主体和会议。发起、继续连接和 ICE PATCH
都会核对这个绑定；客户端送来的 `pc_id` 或角色不能代替授权。开始新的录音不能打断本人无权主持的会议，
"忙"的回应（409 `access.busy`）也不透露正在录哪场会议。只有 `session.max_live_meetings` 为 1 时才会顶替；
上限更大时，新的录音从不打断别的会议，位置满了就在上面这些检查之后以 409 `session.capacity` 拒绝（§5.2）。移除成员、注销（单个登录或全部）和登录到期会在一秒内生效，空闲的事件流也一样：
受影响的事件流结束并丢掉排队的事件，录音连接关闭，流式下载在下一块之前停止。已经发出去的字节无法撤回。

§5.1 的事件流在会议访问模式下保留游标、快照和重放规则。游标只是位置，不代表权限：订阅前、发送快照或重放前、
以及每一块发送前都会检查权限。来自别的会议的游标只会换来所请求会议的重置和快照。失去权限后，页面清掉会议内容、
关闭事件流并丢掉游标；之后重新登录会建立新的订阅。

**谁能用工具。** 只有持有当前录音连接的那个登录，能通过数据通道或 `POST /api/sessions/{id}/text`
使用带工具的流水线。其他所有登录，包括其他主持人和同一账号的另一个登录，都按下文走只读问答；其他主持人仍保留
HTTP 管理权限。工具调用记住发起它的登录、主体、会议和连接。注销、到期、撤权或连接结束会取消该登录排队的输入和
未完成的工具调用，迟到的结果既不保存也不发布。已经完成的副作用无法撤回。旧模式不变。

**只读问答。** 这类问题发给已配置实时模型（`cfg.realtime_llm.active`）的后台入口，不带工具，也不接 MCP；
回答里的工具调用一律不执行。这个服务可能在本机、局域网或远端，所以页面保留数据外发提示。发送前和保存回答前都会
重新检查权限。会议不在进行时返回 409 `session.not_live`。

输入只包括这场会议最新的纪要（前 4096 字）、最近 100 条发言（每条 2048 字）和最近 10 条截图说明
（每条 1024 字）；任务行、产出、图片和其他会议从不放进去。记 `B = realtime.context_budget_tokens`，
`R = min(512, sampling.max_tokens 或 512)`，`R` 同时作为 `max_tokens` 发送。完整消息必须同时满足
`estimate_messages(messages) + R <= B`，以及紧凑 JSON 的 UTF-8 字节数不超过 `min(32768, 4 * (B - R))`。
纪要最多占材料预算的四分之一；发言和截图说明从新到旧挑选，过长的加省略标记截短，按时间先后排列，
再从最旧的开始丢弃，直到两个上限都满足。问题本身（最多 2000 字）从不截短：只放系统提示和问题就超限时，
返回 413 `text.context_too_large`，不记录问题，也不调用模型。

最多五个问题排队，每个主体同时最多一个在处理，每个问题连同等待共 60 秒（429 `text.busy`、504
`text.timeout`）。主持人发言优先：被抢占的问题以 409 `text.interrupted` 结束，不自动重试。等待或取消问题
都不会解除主持人流水线设下的暂停。问题在通过预算检查后才记录；成功的回答作为一条助理发言保存一次，
并以普通发言事件发布。失败返回 502 `text.failed`，从不影响转录。

### 5.12 会议分享

会议访问模式下，主持人可以不建账号就给出临时的旁听权限：每场会议一个会议口令（12–128 字符），或最多八个链接。
分享只授予这场会议的 `watcher` 角色；主持人不允许时不能提问。分享默认 24 小时，最长七天（`expires_in`
取 1–604800 秒）。口令用加盐 scrypt 存储，链接存 32 个随机字节的 SHA-256；口令或链接本身只显示一次，
从不存储或写进日志。旧模式没有这些接口。

| 方法与路径                                         | 权限   | 请求体                                      | 结果                         |
| -------------------------------------------------- | ------ | ------------------------------------------- | ---------------------------- |
| `GET /api/sessions/{id}/shares`                    | 主持人 | —                                           | 有效分享，不含秘密           |
| `PUT /api/sessions/{id}/shares/password`           | 主持人 | `{password, expires_in?, allow_questions?}` | 创建或替换会议口令           |
| `POST /api/sessions/{id}/shares/links`             | 主持人 | `{expires_in?, allow_questions?}`           | 新链接，只显示一次           |
| `POST /api/sessions/{id}/shares/{share_id}/rotate` | 主持人 | `{expires_in?, allow_questions?}`           | 替换链接，只显示一次         |
| `DELETE /api/sessions/{id}/shares/{share_id}`      | 主持人 | —                                           | 撤销；重复撤销无害           |
| `POST /api/shares/exchange`                        | 任何人 | 见下文                                      | 给当前登录这场会议的旁听权限 |

`POST /api/shares/exchange` 对链接接受 `{session_id, kind, share_id, token}`，对会议口令接受
`{session_id, kind, password}`；配置了 `server.password_env` 时另带 `site_password`。请求体必须是 JSON，
`Origin` 头的主机要与请求的 `Host` 头一致，所以放在终结 TLS 的反向代理后面也能用；已登录的调用方还要带 CSRF
令牌。分享错误、过期、已撤销或属于别的会议时返回 404 `share.invalid`；第九个链接返回 409 `share.limit`。
格式不对返回 400，请求体过大 413，尝试过多 429 并带 `Retry-After`。没有登录的调用方会得到一个自己的访客登录，
最长 24 小时，也不超过 `server.auth_session_days`；每个地址每小时最多创建 20 个访客登录。访客不能录音、
不能用工具，也不能被授予角色。

分享得来的权限只属于兑换它的那个登录，不会迁到个人登录上；撤销分享只去掉由它得来的权限，不影响成员自己的授权，
也不影响其他登录。`meeting_shares` 存分享 ID、会议、类型、哈希与盐、创建和到期时间以及提问开关；
`share_access` 把登录和分享关联起来并带有封顶的到期时间；`guest_logins` 标记访客主体。删除登录、分享或会议时，
相应权限一起删除；待删除的会议立即拒绝分享访问。过期的行在启动时、每次修改时和每 60 秒清理一次。
降级到旧版本前要先停掉服务并备份整个数据目录。

请求开始时、每次发送响应块前、以及保存迟到的回答前都会检查权限；未结束的响应每 0.5 秒复查一次。
被撤权的事件流或下载最多用 0.25 秒发送最后的空块。链接把令牌放在 URL 的 fragment 里，页面在调用 API
之前把它从地址栏清掉；页面使用 `no-referrer` 和 `private, no-store`。浏览器历史、剪贴板或转发消息中的副本无法远程清除。

### 5.13 访问日志

服务器为每场会议记录谁打开、导出过会议，谁改过它的访问权限。主持人用
`GET /api/sessions/{id}/access-log?limit=&before_id=` 读取（`limit` 为 1–500，默认 100）。响应是
`{items: [{id, at, principal_id, display_name, action, detail}]}`，新的在前；`before_id` 接着上一页往下读。
`at` 是 UNIX 时间戳，`display_name` 是记录时的名字（访客为 `Guest`）。读日志本身不记录。

| `action`           | 何时记录                         | `detail`                                            |
| ------------------ | -------------------------------- | --------------------------------------------------- |
| `meeting.view`     | 某个登录读取会议内容             | `role`、`sources`（权限来源）                       |
| `meeting.export`   | 主持人或旁听者下载导出文件或报告 | `format`                                            |
| `meeting.end`      | 主持人结束会议                   | —                                                   |
| `voice.erase`      | 主持人清除这场会议的声纹片段     | `clips`（删掉的片段数）                             |
| `voice.enroll`     | 主持人录入一位说话人的声纹       | `speaker_idx`、`profile_id`                         |
| `voice.unlink`     | 主持人取消说话人和档案的关联     | `speaker_idx`                                       |
| `recording.start`  | 录音连接开始这场会议             | —                                                   |
| `recording.resume` | 录音连接继续这场会议             | —                                                   |
| `member.set`       | 主持人授予或修改角色             | `principal_id`、`role`、`allow_questions`           |
| `member.remove`    | 主持人撤销授权                   | `principal_id`                                      |
| `share.create`     | 主持人创建分享                   | `kind`、`share_id`、`expires_at`、`allow_questions` |
| `share.rotate`     | 主持人替换分享                   | 同 `share.create`，另有 `replaced_id`               |
| `share.revoke`     | 主持人撤销分享                   | `share_id`                                          |
| `share.redeem`     | 有人通过分享进入                 | `kind`、`share_id`、`guest`                         |
| `task.artifact`    | 有人下载任务产出                 | `task_id`、`name`                                   |

同一个登录对同一场会议 30 分钟内最多记一条 `meeting.view`，持续旁听不会刷满日志。表结构为
`access_log(id, session_id, at, principal_id, display_name, action, detail)`，`detail` 为 JSON。
每场会议保留最新的 2000 条，删除会议时一并删除。写日志失败不会让被记录的请求失败，只记一条错误日志。
旧模式不写日志。

### 5.14 声纹档案与录入

这些接口只在 `speaker_memory.backend` 不是 `"none"` 时存在，否则是 404。响应里只有名字、时长和日期，从不包含向量（§2.8）。

| 方法与路径                                             | 说明                                                                                                                                |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/voice-profiles`                              | `{"items": [档案], "min_enroll_secs"}`，按名字排序                                                                                  |
| `PATCH /api/voice-profiles/{id}`                       | 请求体 `{"name"}`，响应档案                                                                                                         |
| `DELETE /api/voice-profiles/{id}`                      | 删除档案、它的样本和关联，以及各场会议里关联到它的说话人的声纹片段；响应 `{"id", "clips": <删掉的片段数>}`，发言保留                |
| `POST /api/speakers/{idx}/voice-profile`               | 请求体 `{"session_id"?, "consent": true, "name"? \| "profile_id"?}`；响应 `{"speaker": {idx, display_name}, "voice_profile": 档案}` |
| `DELETE /api/speakers/{idx}/voice-profile?session_id=` | 取消这位说话人的关联，档案和样本保留；响应 `{"idx", "voice_profile": null}`                                                         |

档案：`{"id", "name", "samples", "secs", "created_at", "updated_at"}`；`secs` 是全部样本的语音时长，保留一位小数。
名字去首尾空白，最长 50 字。

**录入**记住这场会议（默认当前会议）里说话人 `idx` 的声音。必须带 `"consent": true`，表示本人已经同意
（否则 `voice.consent_required`）。不带 `profile_id` 时新建一份档案，名字是 `name`，默认用说话人的显示名；带
`profile_id` 时给那份档案加一个样本。只能录入说话人区分给出的或手动新建的说话人（`idx` ≥ 1，否则
`voice.not_enrollable`）；说话人不存在返回 404。样本在请求的那一刻由这位说话人的声纹片段（§2.7）算出，所以进行中的会议
说够了就能录：不足 `speaker_memory.min_enroll_secs` 返回 `voice.too_short`，参数 `have`、`need` 是秒数。档案已经关联到本场
另一位说话人时返回 `voice.profile_in_use`，档案是另一个模型录的返回 `voice.model_changed`。录入后说话人关联到这份档案；
名字和档案不同时改成档案的名字，进行中的会议和 `PUT /api/speakers/{idx}` 一样广播 `speaker`。

**权限。** 没有开会议权限时，登录的人都能做。会议权限模式（§5.11）下，录入和取消关联要这场会议的主持人，并记
`voice.enroll`、`voice.unlink`（§5.13）；列出、改名、删除档案对登录的账号开放，通过分享进来的访客不行
（403 `access.denied`）。待删除的会议不能用来录入。

## 6. 数据通道消息

### 6.1 服务端 → 浏览器

服务端向管线推 `RTVIServerMessageFrame(data=<下面的对象>)`；浏览器在 `onServerMessage` 回调里收到。
每条消息都有 `type` 字段：

| `type`             | 其余字段                                                                                | 含义                                                                                                                                                                                                                                                                                                           |
| ------------------ | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caption`          | `segment_id, speaker_idx, speaker_name, t_start, stable, unstable`                      | 当前正在说的这一段的实时字幕；同一 `segment_id` 的后一条覆盖前一条                                                                                                                                                                                                                                             |
| `utterance`        | `id, segment_id, speaker_idx, speaker_name, t_start, t_end, text, source`               | 一条发言已定稿落库；界面用它替换对应 `segment_id` 的实时字幕。`id` 是界面上已有的发言时，表示新片段并进了那一条（§4.3 第 7 条）：`text` 是并好的全文，`segment_id` 那行实时字幕收掉                                                                                                                            |
| `utterance_update` | `id, speaker_idx, speaker_name`                                                         | 说话人更正                                                                                                                                                                                                                                                                                                     |
| `speaker`          | `idx, display_name`                                                                     | 说话人改名                                                                                                                                                                                                                                                                                                     |
| `speakers_merged`  | `from, into, display_name`                                                              | 说话人 `from` 并入了 `into`（`display_name` 是 `into` 的显示名）：页面把已加载的发言改到 `into` 名下，并从说话人列表里去掉 `from`                                                                                                                                                                              |
| `frame`            | `id, t, width, height`                                                                  | 新截图                                                                                                                                                                                                                                                                                                         |
| `frame_caption`    | `id, caption`                                                                           | 截图摘要已生成                                                                                                                                                                                                                                                                                                 |
| `assistant_state`  | `state`：`idle` / `listening` / `thinking` / `speaking`                                 | 助理状态                                                                                                                                                                                                                                                                                                       |
| `task`             | `id, label, goal, status, brief, error, error_code, error_params, modality, created_at` | 任务创建或状态变化（`id` 是完整编号，`label` 是会议内的短编号 `t3`）                                                                                                                                                                                                                                           |
| `task_event`       | `task_id, at, kind, summary`                                                            | 任务进度                                                                                                                                                                                                                                                                                                       |
| `notice`           | `level`：`info` / `warn` / `error`，`code`，`params`，`text`                            | 需要让用户知道的提示（如某服务不可用）。`code` 和 `params` 标明是哪一条提示，页面据此用自己的界面语言显示（§6.1.1）；`text` 是同一条提示的中文，给日志和旧页面用。实时模型、语音合成的请求出错时，服务端把管线里的错误换成提示发出来（`pipeline/errors.py`）；Pipecat 自带的 RTVI `error` 消息页面只显示致命的 |
| `session`          | `id, title, keep, started_at, resumed, base_secs, state`                                | 连接建立后发一次：所属会话与当前保留标记；`resumed` 表示继续；`base_secs` 是本次连接的时间轴起点                                                                                                                                                                                                               |
| `session_closed`   | `reason`：`taken_over` / `ended` / `server_stopping`                                    | 连接即将被服务端关闭的原因（尽力而为地发出），页面据此提示而不是静默变成未连接                                                                                                                                                                                                                                 |
| `watchers`         | `items`：`[{id, device, since}]`                                                        | 通过 `GET /api/sessions/{id}/events` 旁听这场会议的连接（§5.1）：`id` 是应用运行期间唯一的编号，`device` 的含义见那一节，`since` 是开流的时刻（Unix 秒）。在 `session` 之后发一次，名单每变一次再发                                                                                                            |

发送连接就绪消息时，从当前持久状态读取 `session.keep`。`session.recording_notice=true` 时，
每次连接就绪后在 session 消息之后发送一次现有 `notice`：`level="info"`，
`code="recording.active"`（§6.1.1）。新会议与手动继续发送；
同一路连接重复触发 ready 不重复发送。关闭配置只抑制此提示。锁定 SDK 自动重连不再次触发 RTVI
client-ready（[pipecat-notes.md](pipecat-notes.md) §12），因此通过既有传输/HTTP 同步恢复状态，
不重复此提示。不需要新增配置 HTTP 端点；
会议详情加载失败也不能隐藏录制状态。

客户端录制状态始终可见，独立于助理是否被唤醒或正在说话。只有本机已就绪并登记的转录连接
显示**正在转录**；连接中/重连中、未连接与只读查看另一条活动连接使用不同状态。
收到 `session_closed` 即刻清除转录指示，不等待传输断开回调。此状态表示转录，不表示保存原始音频；
原始麦克风音频不归档。
旁边的连接状态表示页面和服务器之间的连接：旁观别的设备上进行中的会议时，它反映只读事件流（§5.1）
是否已连接、正在连接或出错，转录状态仍显示本机未在转录。

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

### 6.1.1 提示代码

每条 `notice` 都带一个下表里的 `code`，以及 `params`：值是字符串或数字的对象（不需要参数时为空对象）。
`text` 是同一条提示的中文，由服务端按同一个代码生成。页面认得代码、参数也对时按界面语言翻译，否则显示
`text`，所以新增的代码不会让页面空白。新代码先写进这里，再写发送它的代码；
`src/agentic_meeting/messages.py` 是服务端的表，`client/src/i18n/serverNotices.ts` 是页面的表，
仓库测试检查三处列出的代码一致。

| `code`                | `level` | `params`                                             | 中文 `text`                                                                   |
| --------------------- | ------- | ---------------------------------------------------- | ----------------------------------------------------------------------------- |
| `recording.active`    | info    | —                                                    | 会议正在转录，发言和共享画面会保存在服务器上                                  |
| `mic.low_level`       | warn    | —                                                    | 麦克风音量过低，请调高系统的输入音量或靠近麦克风                              |
| `asr.unavailable`     | warn    | —                                                    | 识别服务暂时不可用，正在重试                                                  |
| `asr.recovered`       | info    | —                                                    | 识别服务已恢复                                                                |
| `diar.unavailable`    | warn    | `error`：string                                      | 说话人区分不可用，所有发言将记为「未知」，不分说话人：{error}                 |
| `diar.failed`         | warn    | `error`：string                                      | 说话人区分出错，之后的发言将记为「未知」：{error}                             |
| `speakers.renumbered` | info    | —                                                    | 服务重启过，说话人编号可能与之前不同；认错的可以在说话人那里用「合并到…」收拾 |
| `llm.unavailable`     | warn    | `reason`：string，`status`：number，`detail`：string | 助理暂不可用：{reason}                                                        |
| `tts.unavailable`     | warn    | `reason`：string，`status`：number，`detail`：string | 语音不可用，助理这次只出文字：{reason}                                        |
| `text.malformed`      | warn    | —                                                    | 文字消息的格式不对，没有发送                                                  |
| `text.empty`          | warn    | —                                                    | 消息是空的，没有发送                                                          |
| `text.too_long`       | warn    | `max`：number                                        | 消息太长（最多 {max} 字），没有发送                                           |
| `text.queue_full`     | warn    | `max`：number                                        | 助理正忙，排队已满（{max} 条），这条消息没有发送                              |
| `text.queued`         | info    | `position`：number                                   | 助理正忙，这条消息排在第 {position} 位，稍后回答                              |

`error` 和 `detail` 是库或服务给出的原始错误文字，原样显示，不翻译。
`llm.unavailable` 和 `tts.unavailable` 的 `reason` 取下列之一：

| `reason`       | `text` 里的中文                    | 说明                                           |
| -------------- | ---------------------------------- | ---------------------------------------------- |
| `auth`         | 密钥被拒绝，请检查配置里的密钥变量 | HTTP 401 或 403                                |
| `not_found`    | 接口地址或模型名不对（404）        | HTTP 404                                       |
| `rate_limited` | 被服务端限流了，稍后再试           | HTTP 429                                       |
| `server_error` | 服务端出错（{status}）             | HTTP 5xx；`status` 是状态码                    |
| `timeout`      | 请求超时                           |                                                |
| `connection`   | 连不上服务                         |                                                |
| `other`        | {detail}                           | `detail` 是错误原文，最多 120 字，超出加省略号 |

`reason` 不是 `server_error` 时 `status` 为 0；不是 `other` 时 `detail` 为空串。

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
6. 只读旁观会议的设备用 `POST /api/sessions/{id}/text`（§5.1）向同一个队列提问；处理结果的提示放在响应里返回，
   不发给会议页面。

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
  中文全是 `\uXXXX` 转义）。发请求之前把它们改写成中文行：占位只说明工具正在调用、尚未确认任务受理；
  已受理 → `[任务 t1 已受理] 后台正在处理，做完后你会收到结果。`；完成 → `[任务 t1 完成] 结论`；
  失败 / 取消 → `[任务 t1 失败] 原因` / `[任务 t1 已取消] …`。上下文里存的仍是 Pipecat 的原样消息，只改发出去的那一份；
  压缩重建上下文时，最近做完的任务也按同样的格式各写一行。
- Pipecat 在有异步工具时会往系统提示词后面自动加一段英文（要求「把晚到的结果附在下一次回复末尾，不要单独成一次回复」），
  与本项目的流程相反，已在 `RealtimeLLMService` 中去掉；怎么对待晚到的结果由 `realtime_tasks.md` 说明。

明确要求把工作交给后台 agent 时，使用 `delegate_task`，包括只根据会议内容整理文档的任务。
这条规则优先于用于当场概括的 `get_digest`。只有工具返回任务编号和 `status: "accepted"` 后，
助理才能确认已经交办；进度和失败原因必须来自 `task_status` 或任务结果。
异步工具的初始占位消息只表示调用正在进行，不表示任务已经受理；被拒绝的调用不能说成已经交办成功。

`get_digest` 返回的是只读的会中滚动摘要，与会议结束或中断后生成的会后报告（§5.6）不同。
要求「记到纪要里」「更正纪要」时，在对话中确认补充或更正，不交办修改尚不存在的会后报告。
实时工具和后台 agent 都不能编辑本系统的转录、滚动摘要或报告；明确要求的独立文稿可以交办，
但其结果不会回写这些记录。

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
  排队的任务在各场会议之间轮流：空出位置时，正在跑的任务最少的那场会议先上，同一场会议里按排队先后。
- **编号。** 数据库主键是 `<会话 id>.t<序号>`，全库唯一。点号之后的部分是短编号（`TaskRecord.label`），
  用于口头指代、工具参数和界面；HTTP 接口使用完整编号。
- `t_from`、`t_to` 和 `frame_ids_json` 记录交给 agent 的转录时间范围与截图，即该任务外发的内容。
- `wait` 在失败或取消时抛出 `TaskFailed(status, reason)`；任务不存在时抛出 `LookupError`。
  需要让使用者看到的失败原因由运行器以 `RunnerError(代码, **参数)` 抛出，代码见 §5.10；其他异常只报告为内部错误及异常类型，细节写入日志。
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

### 8.4 保留清理与完整删除

**独立类别与时钟。** 每轮只采样一次 UTC `now`，启用类别的 `cutoff = now - days * 86400`；
仅时间锚点严格小于 cutoff 才过期，等于时保留。
`inactive_at = max(started_at, last_active_at, ended_at（若有）)` 是会议不活动时间锚点。
即使墙钟跳向未来，也不清理活动会议或受保护的后台工作。已中断但未结束的会议可以过期，未来时间保留。

| 类别     | 删除集合                                                             | 时间锚点                                                |
| -------- | -------------------------------------------------------------------- | ------------------------------------------------------- |
| 转录     | 会议的发言、对应 `utterances_vec` 行、FTS 项及声纹片段（§2.7，级联） | 会议 `inactive_at`                                      |
| 截图     | 截图行及摘要、应用管理的 `frames/` 子树                              | 会议 `inactive_at`                                      |
| 报告     | 终态报告行与滚动纪要（`digests`）行                                  | 各报告 `finished_at`；各纪要 `created_at`               |
| 任务产物 | 终态任务的应用管理目录及该任务 `artifacts_json` 列表（清为 `[]`）    | 各任务 `finished_at`；旧终态缺此值时回退到 `created_at` |

任务目标、结果、来源、事件及外发元数据（可能含原始会议文字或图像引用）保留到完整删除会议。会议标题、说话人、连接记录、keep
及其他会议元数据也保留。报告、截图摘要、任务输入副本/结果以及已下载的导出都是独立内容副本：
转录过期不会清空其他类别。新报告从完成时、新纪要从生成时、任务文件从任务完成时计时，不从会议开始计时。
保留任务不扫描或删除操作者导出、日志、配置、证书、鉴权密钥、数据库根或模型/运行时目录。

**调度与保护。** 单个 lifespan 持有的清理任务在 Store 迁移及任务/报告启动恢复后跑第一轮，之后每
`cleanup_interval_secs` 跑一轮。四类期限全为 0 时仍运行以重试待删除的人工请求，但不做类别清理。
轮次不重叠。每轮最多接纳 50 个候选会议，10 秒后不再启动新的破坏单元；这只是调度限制，
不承诺能中断已开始的文件系统调用。慢文件工作不占事件循环，实际完成后才能释放所有权。
一个候选失败不影响后续候选或识别/字幕；跳过/失败的候选在之后重试，不得永远饿死其他过期会议。

SELECT 选中并不等于允许删除。最后 claim 与 begin/attach、keep PATCH、任务提交、报告启动及后台写入
登记，使用既有 SessionManager 生命周期协调与 Store 写锁串行判定（先 manager 后 Store，禁止反向）。
此处重读 keep/pending、时间锚点和任务状态。keep/attach 在自动 claim 前成功就阻止清理；
claim 后冲突操作返回 409，直至释放。不能跨文件工作或模型调用持有全局生命周期/Store 锁。

offer 校验在协商前检查 pending，但不代替最后挂接检查。`SessionManager.attach` 在同一协调下、
重新打开会议或创建连接记录之前重查。若 offer 已通过初查、挂接前删除先获得所有权，则拒绝挂接、
安全提示/关闭已协商连接，绝不能启动它的管线。

活动会议（包括 worker 登记前的组装）、接管/停止后实际尚未收尾的旧 worker、数据库 queued/running
任务、已登记的任务终态/事件写入、报告、纪要、截图摘要/跟随项、截图接收/文件写入、记录器重试/
校正，都保护整场会议。SQL 已终态或接管超时后 `_live` 已清空，不代表所有生产者都完成。
自动候选忙则跳过；人工 DELETE 忙则 409，不设 pending、不取消正在产出内容的工作。
嵌入请求可在删除后完成，但只能通过 token 校验跳过已删除/改变的行（§2.6）。
claimed/pending 会议不能启动新工作或接受迟到 INSERT；缓存来源编号或旧快照不能重新生成已移除内容。

**文件与重试。** 自动类别清理先移除所属文件，再提交对应行或产物列表的删除/清空。
真实 I/O 失败保留数据库所有权，之后轮次重试；文件已缺失是成功的幂等操作。数据库失败回滚。
各类别不互相删除。这不是数据库/文件系统原子事务，可能已有部分文件移除。
下一次 claim 前设置 keep 会阻止继续自动删除，但不会恢复已删除文件。

人工 DELETE 忽略 keep。忙状态检查通过后，先持久设置 `deletion_pending=1`，再删除文件。
仅删除该会议所属文件，之后提交向量移除及会议删除；外键级联清发言、声纹片段、声纹档案关联、截图、纪要、报告、
说话人、连接、任务及任务事件（声纹档案的样本（§2.8）保留，只是不再指向这场会议），发言删除触发器清 FTS 索引。真实文件/数据库失败或 claim 后取消都保留 pending；
HTTP 删除失败返回 500，记录并通过重复 DELETE、后续轮次、进程重启重试，即使自动保留全部关闭。
文件仍在或数据库提交失败时不得确认完整成功。安全释放说话人区分/截图/摘要/任务缓存；
迟到生产者不能重建删除文件，也不能写进复用编号。请求与后台重试对同一会议独占所有权，不并行删文件。

**路径所有权。** 配置的数据根只解析一次；仅清固定后代 `sessions/<uuid-hex>/frames`、
`sessions/<uuid-hex>/tasks/<tN>`，完整删除时才清该会议子树。校验会议/任务短编号，不把任意
数据库路径当删除根。中间组件（包括 `sessions` 和会议目录）若是 symlink/reparse point 或路径逃逸
就拒绝，不递归跟随链接目录。所属子树内部的链接只 unlink，不跟随目标。
安全拒绝按真实失败处理，保留所有权，等操作者修复后重试。文件写入登记与实际落盘也遵循同一规则；
取消 `to_thread` 不足以停止写入，必须等底层写入结束后才能释放该会议的在途保护。

**日志与关闭。** 日志只写类别、操作、不透明编号/计数、结果和安全错误类型，不写转录、摘要、
标题、目标、凭据，或带内容/私有路径的异常文字。所需文件工作与数据库提交都成功后才记成功；
重试必须可见。关闭先停止清理接纳，取消/等待清理任务并等已开始文件工作完成，再关闭/等待
原有生产者，最后 Store.close。待删尝试可跨关闭保留。无需调用推理、增加依赖或建立通用监控/任务框架。

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
             -c <ctx_size × max_live_meetings> -np <session.max_live_meetings> -ngl <gpu_layers>
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

**声纹服务**（只在 `speaker_memory.backend` 不是 `"none"` 时生成；§4.4）

```
<python> -m agentic_meeting.voice.service --model <model_path> --host 127.0.0.1
         --port <取自 base_url> --threads <num_threads> <extra_args...>
```

`<python>` 是运行本应用的解释器，需要先 `uv sync --extra speaker`。服务只用 CPU，只读配置的那个模型文件。

**语音合成**

```
tts-server --model <model_path> --codec <codec_path> --alias <tts.model>
           --host 127.0.0.1 --port <取自 base_url> --lang <default_language> <extra_args...>
```

通用规则：

- 健康检查：`llama-server` 与 `tts-server` 都用 `GET /health`（返回 200 即就绪）。
- 服务名固定为 `realtime`、`asr`、`tts`、`embedding`、`speaker`。
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
`cfg.realtime_llm.request_extra_body(background=..., lane=...)`：

| 接入方式       | 内容                                                                                                                                                                                                                                                              |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llama_server` | 配置中的 `extra_body` + `top_k`（若配置了）+ `{"id_slot": <槽位>, "cache_prompt": true}`；实时应答与预热用 `realtime_slot + lane`（`lane` 是这场会议在进行中的会议里的位置，只有一场时为 0），后台任务（画面摘要、纪要）传 `background=True` 用 `background_slot` |
| `openai_api`   | 配置中的 `extra_body` + `top_k`（若配置了）。不带任何 llama.cpp 专有字段                                                                                                                                                                                          |

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
