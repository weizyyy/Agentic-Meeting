-- Agentic-Meeting 的 SQLite 结构（一个数据库文件存所有会议：<data_dir>/meetings.db）。
-- 约定：
--   * t / t_start / t_end 是会话时间轴上的秒；*_at 是 Unix 时间戳（秒，REAL）。
--   * 向量表 utterances_vec 的维度来自配置（embedding.dimensions），因此不在本文件里，
--     由 store/db.py 在建库时用 sqlite-vec 动态创建：
--       CREATE VIRTUAL TABLE IF NOT EXISTS utterances_vec USING vec0(embedding float[<dim>]);
--     其 rowid 等于 utterances.id。
--   * 连接建立后需执行：PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,           -- uuid4 hex
    title       TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL,              -- 会话时间轴 0 点对应的 Unix 时间
    ended_at    REAL,                       -- 空 = 没点过「结束」（进行中或已中断）
    last_active_at REAL NOT NULL DEFAULT 0, -- 最近一次有连接挂上、断开或写入发言的时间；会议列表按它排序
    keep INTEGER NOT NULL DEFAULT 0,
    deletion_pending INTEGER NOT NULL DEFAULT 0
);

-- 每次连接一行。t_from / t_to 是这次连接覆盖的会话时间轴（秒）；中断的空档就是相邻两行之间的缝。
-- 会话状态不存列：ended_at 非空 = 已结束；为空时应用内存里有活动连接 = 进行中，否则 = 已中断。
CREATE TABLE IF NOT EXISTS session_connections (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    connected_at    REAL NOT NULL,
    disconnected_at REAL,
    t_from          REAL NOT NULL,
    t_to            REAL
);
CREATE INDEX IF NOT EXISTS idx_connections_session ON session_connections (session_id, connected_at);

-- 说话人编号 → 显示名。idx 取值见 agentic_meeting.types：-2 键入的文字，-1 助理，0 未知，1.. 说话人区分输出。
CREATE TABLE IF NOT EXISTS speakers (
    session_id    TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    display_name  TEXT    NOT NULL,
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS utterances (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    speaker_idx INTEGER NOT NULL DEFAULT 0,
    t_start     REAL    NOT NULL,
    t_end       REAL    NOT NULL,
    text        TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'asr' CHECK (source IN ('asr', 'assistant', 'text')),
    addressed_to_assistant INTEGER NOT NULL DEFAULT 0,
    embedded    INTEGER NOT NULL DEFAULT 0, -- 是否已写入 utterances_vec
    write_token TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_utterances_session_time ON utterances (session_id, t_start);
CREATE INDEX IF NOT EXISTS idx_utterances_speaker ON utterances (session_id, speaker_idx, t_start);

-- 全文检索。trigram 分词对中文可用，但只能匹配 ≥3 个字符的查询；
-- 2 个字符的查询须退回 LIKE（已实测，见 docs/interfaces.md §2.3）。
CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5 (
    text,
    content = 'utterances',
    content_rowid = 'id',
    tokenize = 'trigram'
);
CREATE TRIGGER IF NOT EXISTS utterances_ai AFTER INSERT ON utterances BEGIN
    INSERT INTO utterances_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS utterances_ad AFTER DELETE ON utterances BEGIN
    INSERT INTO utterances_fts (utterances_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS utterances_au AFTER UPDATE OF text ON utterances BEGIN
    INSERT INTO utterances_fts (utterances_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO utterances_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS frames (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    t           REAL    NOT NULL,
    path        TEXT    NOT NULL,           -- 相对 data_dir，例如 sessions/<id>/frames/000123.webp
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    caption     TEXT,
    write_token TEXT NOT NULL DEFAULT '',
    caption_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (caption_status IN ('pending', 'done', 'failed', 'skipped'))
);
CREATE INDEX IF NOT EXISTS idx_frames_session_time ON frames (session_id, t);

-- 滚动纪要。text 是累积的（覆盖从会议开头到 t_to）；t_from / t_to 只记这一次新纳入的那一段，相邻两份首尾相接。
-- last_utterance_id：这份纪要已经纳入到哪条发言为止（按编号而不是按时间：助理的话是一轮结束时才落库的，
-- 它的开始时间可能早于上一份纪要的终点）。旧库没有这一列，由 store/db.py 在打开时补上。
CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    t_from      REAL NOT NULL,
    t_to        REAL NOT NULL,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_utterance_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_digests_session_time ON digests (session_id, t_to);

-- id 是「<会话 id>.t<序号>」：全库唯一；点号后面那段（t1、t2…）是一场会议内递增的短编号，口头和界面上用它。
-- t_from / t_to / frame_ids_json：委托时带给 agent 的转录时间范围和截图编号（也就是这次任务外发了什么）。
-- 后三列旧库没有，由 store/db.py 在打开时补上。
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    goal          TEXT NOT NULL,
    requested_by  INTEGER NOT NULL DEFAULT 0,   -- speaker_idx
    requested_t   REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    brief         TEXT,
    detail_md     TEXT,
    sources_json  TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    error         TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    announced     INTEGER NOT NULL DEFAULT 0,   -- 结果是否已口头播报
    modality      TEXT NOT NULL DEFAULT 'voice' CHECK (modality IN ('voice', 'text')),  -- 委托当时的应答模态
    t_from        REAL NOT NULL DEFAULT 0,
    t_to          REAL NOT NULL DEFAULT 0,
    frame_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks (session_id, created_at);

CREATE TABLE IF NOT EXISTS task_events (
    id        INTEGER PRIMARY KEY,
    task_id   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at        REAL NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('status', 'step', 'tool_call', 'tool_result', 'note')),
    summary   TEXT NOT NULL,                -- 一句中文，可直接念给用户听
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events (task_id, id);

-- 会后报告。同一会话可以有多份，页面显示最近一份。
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    provider    TEXT NOT NULL DEFAULT '',
    text_md     TEXT NOT NULL DEFAULT '',
    error       TEXT,
    write_token TEXT NOT NULL DEFAULT '',
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_reports_session ON reports (session_id, id);
