"""存储层：基于 aiosqlite 的会议数据库（docs/interfaces.md §2）。

* 一个数据库文件存所有会议；一个连接串行执行（单场会议、写入量很小），不引入连接池。
  写方法统一在一把锁里完成「语句 + 提交」，并发调用不会把别人的半截事务一起提交。
* 建库时执行 ``schema.sql``（全部 ``IF NOT EXISTS``，重复打开无害）、加载 sqlite-vec、按配置的维度建向量表。
  已有的向量表维度与配置不一致时报错并提示，**不自动删表**。
* 会话状态不存列（architecture.md §3.1）：这里只提供 ``ended_at`` 和连接记录，「进行中」由应用内存判断。
* 召回（``recall``）：结构化过滤 + 关键词 + 向量。关键词 ≥ 3 个字符走全文索引（trigram），1–2 个字符走 LIKE
  （interfaces.md §2.3）；给了查询向量时再在同样的过滤条件里做一次向量检索，两路结果合并去重（§2.4）。
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import time
import uuid
from collections.abc import Sequence
from itertools import zip_longest
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    SPEAKER_TYPED,
    SPEAKER_UNKNOWN,
    Connection,
    Digest,
    NamedUtterance,
    Report,
    ScreenFrame,
    Session,
    SessionSummary,
    SpeakerInfo,
    TaskEvent,
    TaskRecord,
    Utterance,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_VEC_DIMS = re.compile(r"float\[(\d+)\]")
_TASK_NUMBER = re.compile(r"\.t(\d+)$")

VECTOR_TOP_K = 8  # 召回时向量检索最多贡献几条（没有相关度门槛，取多了全是噪声）

_UTTERANCE_COLUMNS = (
    "u.id, u.session_id, u.speaker_idx, u.t_start, u.t_end, u.text, u.source, "
    "u.addressed_to_assistant, sp.display_name AS speaker_name"
)
_UTTERANCE_FROM = "FROM utterances u LEFT JOIN speakers sp ON sp.session_id = u.session_id AND sp.idx = u.speaker_idx"


# 手动新建的说话人从这个编号起，和说话人区分给出的编号（1 起，个位数）分开：
# 之后流里再认出新的人，不会和手动建的撞号。
MANUAL_SPEAKER_BASE = 1000


class StoreError(RuntimeError):
    """数据库本身的问题（不是调用参数的问题），消息直接面向用户。"""


def default_speaker_name(idx: int, assistant_name: str) -> str:
    """说话人没有被改过名时的显示名。"""
    if idx == SPEAKER_ASSISTANT:
        return assistant_name
    if idx == SPEAKER_TYPED:
        return "文字输入"
    if idx == SPEAKER_UNKNOWN:
        return "未知"
    return f"说话人 {idx}"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def pack_vector(vector: Sequence[float]) -> bytes:
    """向量 → sqlite-vec 要的 float32 小端字节串。"""
    return struct.pack(f"<{len(vector)}f", *vector)


def _fts_phrase(term: str) -> str:
    """把查询词包成 FTS5 的短语，避免里面的符号被当成语法。"""
    return '"' + term.replace('"', '""') + '"'


class Store:
    def __init__(self, db: aiosqlite.Connection, *, assistant_name: str) -> None:
        self._db = db
        self._assistant_name = assistant_name
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 打开与关闭
    # ------------------------------------------------------------------ #

    @classmethod
    async def open(
        cls, path: str | Path, embedding_dims: int, *, assistant_name: str = "助理"
    ) -> Store:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.enable_load_extension(True)
            await db.load_extension(sqlite_vec.loadable_path())
            await db.enable_load_extension(False)
            await db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            await cls._migrate(db)
            await cls._ensure_vector_table(db, embedding_dims)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        return cls(db, assistant_name=assistant_name)

    @staticmethod
    async def _migrate(db: aiosqlite.Connection) -> None:
        """给旧库补上后来才有的列（``schema.sql`` 全是 ``IF NOT EXISTS``，已有的表不会被它改动）。"""
        added = {
            "digests": [("last_utterance_id", "INTEGER NOT NULL DEFAULT 0")],
            "tasks": [
                ("t_from", "REAL NOT NULL DEFAULT 0"),
                ("t_to", "REAL NOT NULL DEFAULT 0"),
                ("frame_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ],
        }
        for table, columns in added.items():
            async with db.execute(f"PRAGMA table_info({table})") as c:
                existing = {row["name"] for row in await c.fetchall()}
            for name, ddl in columns:
                if name not in existing:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @staticmethod
    async def _ensure_vector_table(db: aiosqlite.Connection, dims: int) -> None:
        async with db.execute("SELECT sql FROM sqlite_master WHERE name = 'utterances_vec'") as c:
            row = await c.fetchone()
        if row is None:
            await db.execute(
                f"CREATE VIRTUAL TABLE utterances_vec USING vec0(embedding float[{int(dims)}])"
            )
            return
        found = _VEC_DIMS.search(row["sql"] or "")
        if found is None or int(found.group(1)) != dims:
            existing = found.group(1) if found else "未知"
            raise StoreError(
                f"数据库里的向量表维度是 {existing}，与配置的 embedding.dimensions = {dims} 不一致。"
                "换了嵌入模型的话，请把配置改回原来的维度，或另指一个数据目录；"
                "程序不会自动删除已有的向量表。"
            )

    async def close(self) -> None:
        await self._db.close()

    async def check_available(self) -> None:
        """只检查现有连接可读，不访问业务表；错误和取消由调用方按预算处理。"""
        await self._one("SELECT 1")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    async def _one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._db.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    def _name(self, idx: int, stored: str | None) -> str:
        return stored if stored is not None else default_speaker_name(idx, self._assistant_name)

    @staticmethod
    def _session(row: aiosqlite.Row) -> Session:
        return Session(
            id=row["id"],
            title=row["title"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            last_active_at=row["last_active_at"],
        )

    def _named(self, row: aiosqlite.Row) -> NamedUtterance:
        utterance = Utterance(
            id=row["id"],
            session_id=row["session_id"],
            speaker_idx=row["speaker_idx"],
            t_start=row["t_start"],
            t_end=row["t_end"],
            text=row["text"],
            source=row["source"],
            addressed_to_assistant=bool(row["addressed_to_assistant"]),
        )
        return NamedUtterance(utterance, self._name(row["speaker_idx"], row["speaker_name"]))

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #

    async def create_session(self, title: str = "", *, now: float | None = None) -> Session:
        now = time.time() if now is None else now
        session = Session(id=uuid.uuid4().hex, title=title, started_at=now, last_active_at=now)
        async with self._lock:
            await self._db.execute(
                "INSERT INTO sessions (id, title, started_at, last_active_at) VALUES (?, ?, ?, ?)",
                (session.id, title, now, now),
            )
            await self._db.commit()
        return session

    async def get_session(self, session_id: str) -> Session | None:
        row = await self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._session(row) if row else None

    async def end_session(self, session_id: str, *, now: float | None = None) -> Session | None:
        """写 ``ended_at``。已经结束的不改写第一次的时间。"""
        now = time.time() if now is None else now
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET ended_at = COALESCE(ended_at, ?), "
                "last_active_at = MAX(last_active_at, ?) WHERE id = ?",
                (now, now, session_id),
            )
            await self._db.commit()
        return await self.get_session(session_id)

    async def end_unended_sessions(self) -> int:
        """把所有还没结束的会话标为已结束，结束时间取它最后一次活动的时间（会议实际停在那里）。返回条数。"""
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE sessions SET ended_at = last_active_at WHERE ended_at IS NULL"
            )
            await self._db.commit()
            return cursor.rowcount

    async def reopen_session(self, session_id: str) -> Session | None:
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET ended_at = NULL WHERE id = ?", (session_id,)
            )
            await self._db.commit()
        return await self.get_session(session_id)

    async def touch_session(self, session_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET last_active_at = MAX(last_active_at, ?) WHERE id = ?",
                (now, session_id),
            )
            await self._db.commit()

    async def latest_unended_session(self) -> Session | None:
        row = await self._one(
            "SELECT * FROM sessions WHERE ended_at IS NULL "
            "ORDER BY last_active_at DESC, started_at DESC LIMIT 1"
        )
        return self._session(row) if row else None

    async def rename_session(self, session_id: str, title: str) -> Session | None:
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
            )
            await self._db.commit()
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """删除会话及其名下的全部行（外键级联），向量表要单独清。截图文件由调用方清理。"""
        async with self._lock:
            ids = [
                r[0]
                for r in await self._all(
                    "SELECT id FROM utterances WHERE session_id = ?", (session_id,)
                )
            ]
            for utterance_id in ids:
                await self._db.execute(
                    "DELETE FROM utterances_vec WHERE rowid = ?", (utterance_id,)
                )
            cursor = await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            deleted = cursor.rowcount > 0
            await self._db.commit()
        return deleted

    # ---- 会议列表 ----

    async def list_sessions(
        self, limit: int = 20, before: float | None = None, *, now: float | None = None
    ) -> list[SessionSummary]:
        """按最近活动倒序；``before`` 是上一页最后一条的 ``last_active_at``。

        ``now`` 给出时，没关闭的连接（正在进行的会议）的时长算到 ``now``；不给就算到会话最后一次活动。
        """
        sql = "SELECT * FROM sessions"
        params: list[Any] = []
        if before is not None:
            sql += " WHERE last_active_at < ?"
            params.append(before)
        sql += " ORDER BY last_active_at DESC, started_at DESC LIMIT ?"
        params.append(limit)
        return [await self._summary(self._session(r), now) for r in await self._all(sql, params)]

    async def get_summary(
        self, session_id: str, *, now: float | None = None
    ) -> SessionSummary | None:
        session = await self.get_session(session_id)
        return await self._summary(session, now) if session else None

    async def _summary(self, session: Session, now: float | None = None) -> SessionSummary:
        sid = session.id
        count = (await self._one("SELECT count(*) FROM utterances WHERE session_id = ?", (sid,)))[0]
        speakers = [
            r[0]
            for r in await self._all(
                "SELECT s.display_name FROM speakers s WHERE s.session_id = ? AND s.idx > 0 "
                "AND EXISTS (SELECT 1 FROM utterances u WHERE u.session_id = s.session_id "
                "AND u.speaker_idx = s.idx) ORDER BY s.idx",
                (sid,),
            )
        ]
        recent = await self._all(
            f"SELECT {_UTTERANCE_COLUMNS} {_UTTERANCE_FROM} "
            "WHERE u.session_id = ? ORDER BY u.id DESC LIMIT 2",
            (sid,),
        )
        preview = [(n.speaker_name, n.utterance.text) for n in map(self._named, reversed(recent))]

        duration = 0.0
        has_open = False
        for c in await self._all(
            "SELECT connected_at, disconnected_at, t_from, t_to FROM session_connections "
            "WHERE session_id = ?",
            (sid,),
        ):
            if c["disconnected_at"] is None or c["t_to"] is None:
                has_open = True
                until = session.last_active_at if now is None else now
                duration += max(0.0, until - c["connected_at"])
            else:
                duration += max(0.0, c["t_to"] - c["t_from"])
        return SessionSummary(session, duration, count, speakers, preview, has_open)

    # ------------------------------------------------------------------ #
    # 连接记录
    # ------------------------------------------------------------------ #

    async def open_connection(self, session_id: str, *, connected_at: float, t_from: float) -> int:
        async with self._lock:
            cursor = await self._db.execute(
                "INSERT INTO session_connections (session_id, connected_at, t_from) VALUES (?, ?, ?)",
                (session_id, connected_at, t_from),
            )
            await self._db.execute(
                "UPDATE sessions SET last_active_at = MAX(last_active_at, ?) WHERE id = ?",
                (connected_at, session_id),
            )
            await self._db.commit()
            return int(cursor.lastrowid or 0)

    async def close_connection(
        self, connection_id: int, *, disconnected_at: float, t_to: float
    ) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE session_connections SET disconnected_at = ?, t_to = ? WHERE id = ?",
                (disconnected_at, t_to, connection_id),
            )
            await self._db.execute(
                "UPDATE sessions SET last_active_at = MAX(last_active_at, ?) "
                "WHERE id = (SELECT session_id FROM session_connections WHERE id = ?)",
                (disconnected_at, connection_id),
            )
            await self._db.commit()

    async def timeline_end(self, session_id: str) -> float:
        """这场会议的时间轴已经用到了哪里：各次连接的终点和发言的终点里最晚的那个（没有就是 0）。

        继续会议时新一次连接的起点不能早于它，否则两次连接的发言会在时间上交错。
        """
        row = await self._one(
            "SELECT MAX("
            "COALESCE((SELECT MAX(COALESCE(t_to, t_from)) FROM session_connections "
            "WHERE session_id = ?1), 0), "
            "COALESCE((SELECT MAX(t_end) FROM utterances WHERE session_id = ?1), 0))",
            (session_id,),
        )
        return float(row[0] or 0.0) if row else 0.0

    async def list_connections(self, session_id: str) -> list[Connection]:
        rows = await self._all(
            "SELECT * FROM session_connections WHERE session_id = ? ORDER BY connected_at, id",
            (session_id,),
        )
        return [
            Connection(
                id=r["id"],
                session_id=r["session_id"],
                connected_at=r["connected_at"],
                t_from=r["t_from"],
                disconnected_at=r["disconnected_at"],
                t_to=r["t_to"],
            )
            for r in rows
        ]

    async def close_dangling_connections(self) -> int:
        """服务启动时调用：上次崩溃或被强杀时没来得及关闭的连接记录，按会话最后一次活动的时间补关。"""
        async with self._lock:
            rows = await self._all(
                "SELECT c.id, c.connected_at, c.t_from, s.last_active_at, "
                "(SELECT MAX(u.t_end) FROM utterances u WHERE u.session_id = c.session_id) AS last_t "
                "FROM session_connections c JOIN sessions s ON s.id = c.session_id "
                "WHERE c.disconnected_at IS NULL"
            )
            for r in rows:
                await self._db.execute(
                    "UPDATE session_connections SET disconnected_at = ?, t_to = ? WHERE id = ?",
                    (
                        max(r["connected_at"], r["last_active_at"]),
                        max(r["t_from"], r["last_t"] or r["t_from"]),
                        r["id"],
                    ),
                )
            await self._db.commit()
        return len(rows)

    # ------------------------------------------------------------------ #
    # 说话人
    # ------------------------------------------------------------------ #

    async def ensure_speaker(
        self, session_id: str, idx: int, display_name: str | None = None
    ) -> SpeakerInfo:
        """不存在就以默认显示名（或给定的名字）创建；已存在的不改动（包括用户改过的名字）。"""
        async with self._lock:
            await self._ensure_speaker(session_id, idx, display_name)
            await self._db.commit()
        return SpeakerInfo(idx, await self.speaker_name(session_id, idx))

    async def _ensure_speaker(self, session_id: str, idx: int, display_name: str | None) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO speakers (session_id, idx, display_name) VALUES (?, ?, ?)",
            (session_id, idx, display_name or default_speaker_name(idx, self._assistant_name)),
        )

    async def rename_speaker(self, session_id: str, idx: int, display_name: str) -> bool:
        name = display_name.strip()
        if not name:
            raise ValueError("说话人的名字不能为空")
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE speakers SET display_name = ? WHERE session_id = ? AND idx = ?",
                (name, session_id, idx),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def max_speaker_idx(self, session_id: str) -> int:
        """这场会议里说话人区分给出过的最大编号（没有就是 0；手动新建的不算）。

        服务重启后新开的说话人区分流用它作编号偏移。
        """
        row = await self._one(
            "SELECT MAX(idx) FROM speakers WHERE session_id = ? AND idx > 0 AND idx < ?",
            (session_id, MANUAL_SPEAKER_BASE),
        )
        return int(row[0] or 0) if row else 0

    async def add_speaker(self, session_id: str, display_name: str) -> SpeakerInfo:
        """手动新建一个说话人（改字幕的发言人时，名单里没有这个人）。编号从 ``MANUAL_SPEAKER_BASE`` 起。"""
        name = display_name.strip()
        if not name:
            raise ValueError("说话人的名字不能为空")
        async with self._lock:
            row = await self._one(
                "SELECT MAX(idx) FROM speakers WHERE session_id = ?", (session_id,)
            )
            idx = max(MANUAL_SPEAKER_BASE - 1, int(row[0] or 0) if row else 0) + 1
            await self._db.execute(
                "INSERT INTO speakers (session_id, idx, display_name) VALUES (?, ?, ?)",
                (session_id, idx, name),
            )
            await self._db.commit()
        return SpeakerInfo(idx, name)

    async def has_speaker(self, session_id: str, idx: int) -> bool:
        row = await self._one(
            "SELECT 1 FROM speakers WHERE session_id = ? AND idx = ?", (session_id, idx)
        )
        return row is not None

    async def set_utterances_speaker(
        self, session_id: str, utterance_ids: Sequence[int], speaker_idx: int
    ) -> list[int]:
        """把这场会议里选中的发言改成另一个说话人，返回真正改了的发言编号（升序）。

        只改语音识别来的发言（助理的话、键入的文字不是「谁说的」的问题）；不属于这场会议的编号被忽略。
        """
        wanted = sorted({int(i) for i in utterance_ids})
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        async with self._lock:
            rows = await self._all(
                f"SELECT id FROM utterances WHERE session_id = ? AND source = 'asr' "
                f"AND id IN ({marks}) ORDER BY id",
                (session_id, *wanted),
            )
            found = [r["id"] for r in rows]
            if not found:
                return []
            await self._ensure_speaker(session_id, speaker_idx, None)
            marks = ",".join("?" * len(found))
            await self._db.execute(
                f"UPDATE utterances SET speaker_idx = ? WHERE id IN ({marks})",
                (speaker_idx, *found),
            )
            await self._db.commit()
        return found

    async def merge_speakers(self, session_id: str, src: int, dst: int) -> int | None:
        """把说话人 ``src`` 的全部发言并入 ``dst``，删除 ``src``。返回并过去的发言条数。

        只能合并说话人区分给出的编号（大于 0）；两个都必须存在、且不是同一个，否则返回 ``None``、什么都不改。
        ``src`` 交办过的后台任务也记到 ``dst`` 名下。
        """
        if src <= 0 or dst <= 0 or src == dst:
            return None
        async with self._lock:
            rows = await self._all(
                "SELECT idx FROM speakers WHERE session_id = ? AND idx IN (?, ?)",
                (session_id, src, dst),
            )
            if len(rows) != 2:
                return None
            cursor = await self._db.execute(
                "UPDATE utterances SET speaker_idx = ? WHERE session_id = ? AND speaker_idx = ?",
                (dst, session_id, src),
            )
            await self._db.execute(
                "UPDATE tasks SET requested_by = ? WHERE session_id = ? AND requested_by = ?",
                (dst, session_id, src),
            )
            await self._db.execute(
                "DELETE FROM speakers WHERE session_id = ? AND idx = ?", (session_id, src)
            )
            await self._db.commit()
            return cursor.rowcount

    async def list_speakers(self, session_id: str) -> list[SpeakerInfo]:
        rows = await self._all(
            "SELECT idx, display_name FROM speakers WHERE session_id = ? ORDER BY idx",
            (session_id,),
        )
        return [SpeakerInfo(r["idx"], r["display_name"]) for r in rows]

    async def speaker_name(self, session_id: str, idx: int) -> str:
        """当前显示名；还没出现过的说话人给默认名，不建记录。"""
        row = await self._one(
            "SELECT display_name FROM speakers WHERE session_id = ? AND idx = ?", (session_id, idx)
        )
        return self._name(idx, row["display_name"] if row else None)

    # ------------------------------------------------------------------ #
    # 发言
    # ------------------------------------------------------------------ #

    async def add_utterance(self, utterance: Utterance, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        async with self._lock:
            await self._ensure_speaker(utterance.session_id, utterance.speaker_idx, None)
            cursor = await self._db.execute(
                "INSERT INTO utterances (session_id, speaker_idx, t_start, t_end, text, source, "
                "addressed_to_assistant) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    utterance.session_id,
                    utterance.speaker_idx,
                    utterance.t_start,
                    utterance.t_end,
                    utterance.text,
                    utterance.source,
                    int(utterance.addressed_to_assistant),
                ),
            )
            await self._db.execute(
                "UPDATE sessions SET last_active_at = MAX(last_active_at, ?) WHERE id = ?",
                (now, utterance.session_id),
            )
            await self._db.commit()
            utterance.id = int(cursor.lastrowid or 0)
        return utterance.id

    async def extend_utterance(
        self, utterance_id: int, *, text: str, t_end: float, now: float | None = None
    ) -> bool:
        """把一条发言的文字换成更长的版本（相邻片段并进来之后），结束时间跟着往后。

        返回是否改了。发言不存在、或者已经被某份滚动纪要纳入（再改它，纪要就看不到新加的那部分）时不改，
        调用方照常另存一条。文字变了，向量要重算：清掉旧向量，交给嵌入回填。
        """
        now = time.time() if now is None else now
        async with self._lock:
            row = await self._one("SELECT session_id FROM utterances WHERE id = ?", (utterance_id,))
            if row is None:
                return False
            digested = await self._one(
                "SELECT MAX(last_utterance_id) FROM digests WHERE session_id = ?",
                (row["session_id"],),
            )
            if digested is not None and (digested[0] or 0) >= utterance_id:
                return False
            await self._db.execute(
                "UPDATE utterances SET text = ?, t_end = MAX(t_end, ?), embedded = 0 WHERE id = ?",
                (text, t_end, utterance_id),
            )
            await self._db.execute("DELETE FROM utterances_vec WHERE rowid = ?", (utterance_id,))
            await self._db.execute(
                "UPDATE sessions SET last_active_at = MAX(last_active_at, ?) WHERE id = ?",
                (now, row["session_id"]),
            )
            await self._db.commit()
        return True

    async def update_utterance_speaker(self, utterance_id: int, speaker_idx: int) -> bool:
        async with self._lock:
            row = await self._one("SELECT session_id FROM utterances WHERE id = ?", (utterance_id,))
            if row is None:
                return False
            await self._ensure_speaker(row["session_id"], speaker_idx, None)
            await self._db.execute(
                "UPDATE utterances SET speaker_idx = ? WHERE id = ?", (speaker_idx, utterance_id)
            )
            await self._db.commit()
        return True

    async def list_utterances(
        self,
        session_id: str,
        *,
        after_id: int | None = None,
        before_id: int | None = None,
        tail: int | None = None,
        limit: int = 200,
    ) -> list[NamedUtterance]:
        """按 id 升序返回。``after_id`` / ``before_id`` / ``tail`` 三选一（互斥）：

        * ``after_id``：id 大于它的，最多 ``limit`` 条（重连补齐）；
        * ``before_id``：id 小于它的最近 ``limit`` 条（向上翻页）；
        * ``tail``：整场最近的 ``tail`` 条（页面一打开显示「最近对话」）。
        """
        if sum(x is not None for x in (after_id, before_id, tail)) > 1:
            raise ValueError("after_id、before_id、tail 只能给一个")
        if tail is not None and tail < 1:
            raise ValueError("tail 必须大于 0")
        if limit < 1:
            raise ValueError("limit 必须大于 0")

        where, params, descending, count = "u.session_id = ?", [session_id], False, limit
        if after_id is not None:
            where += " AND u.id > ?"
            params.append(after_id)
        elif before_id is not None:
            where += " AND u.id < ?"
            params.append(before_id)
            descending = True
        elif tail is not None:
            descending, count = True, tail
        order = "DESC" if descending else "ASC"
        rows = await self._all(
            f"SELECT {_UTTERANCE_COLUMNS} {_UTTERANCE_FROM} WHERE {where} ORDER BY u.id {order} LIMIT ?",
            [*params, count],
        )
        named = [self._named(r) for r in rows]
        return named[::-1] if descending else named

    async def all_utterances(self, session_id: str) -> list[NamedUtterance]:
        """整场会议的全部发言，按时间、编号升序（导出用）。"""
        rows = await self._all(
            f"SELECT {_UTTERANCE_COLUMNS} {_UTTERANCE_FROM} WHERE u.session_id = ? "
            "ORDER BY u.t_start, u.id",
            (session_id,),
        )
        return [self._named(r) for r in rows]

    async def utterances_for_context(
        self, session_id: str, *, after_id: int | None, t_from: float
    ) -> list[NamedUtterance]:
        """重建实时模型上下文要用的原文：编号大于 ``after_id`` 的（纪要还没纳入的），或开始时间不早于
        ``t_from`` 的（最近若干分钟的）。``after_id`` 为 ``None`` 时只按时间取。按时间、编号升序。
        """
        if after_id is None:
            where, params = "u.t_start >= ?", [t_from]
        else:
            where, params = "(u.id > ? OR u.t_start >= ?)", [after_id, t_from]
        rows = await self._all(
            f"SELECT {_UTTERANCE_COLUMNS} {_UTTERANCE_FROM} WHERE u.session_id = ? AND {where} "
            "ORDER BY u.t_start, u.id",
            [session_id, *params],
        )
        return [self._named(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 召回
    # ------------------------------------------------------------------ #

    async def recall(
        self,
        session_id: str,
        *,
        query: str | None = None,
        query_vector: Sequence[float] | None = None,
        speaker_idx: int | None = None,
        speaker_name: str | None = None,
        t_from: float | None = None,
        t_to: float | None = None,
        limit: int = 20,
        vector_k: int = VECTOR_TOP_K,
        max_distance: float | None = None,
    ) -> list[NamedUtterance]:
        """interfaces.md §2.4：先结构化过滤，再关键词 + 向量；最多 ``limit`` 条，按时间升序。

        * 没有查询词：满足条件的**最近** ``limit`` 条。
        * 有查询词：按空白拆成多个词，任意一个命中即可（≥ 3 个字符走全文索引，更短的走 LIKE），取最近的。
        * 另给了 ``query_vector``（查询词的嵌入）：在同样的过滤条件里取向量最近的 ``vector_k`` 条，
          与关键词结果合并去重。合起来超过 ``limit`` 时两路轮流取（关键词按从新到旧、向量按从近到远），
          所以语义最接近的那几条不会被一堆关键词命中挤掉，反过来也一样。
          ``max_distance`` 是相关度门槛：向量距离超过它的不算命中（``None`` = 不设）。
        """
        where, params = ["u.session_id = ?"], [session_id]
        if speaker_idx is not None:
            where.append("u.speaker_idx = ?")
            params.append(speaker_idx)
        if speaker_name is not None:
            where.append("sp.display_name = ?")
            params.append(speaker_name)
        if t_from is not None:
            where.append("u.t_start >= ?")
            params.append(t_from)
        if t_to is not None:
            where.append("u.t_start <= ?")
            params.append(t_to)

        base_where, base_params = " AND ".join(where), list(params)

        terms = (query or "").split()
        if terms:
            alternatives: list[str] = []
            for term in terms:
                if len(term) >= 3:
                    alternatives.append(
                        "u.id IN (SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?)"
                    )
                    params.append(_fts_phrase(term))
                else:
                    alternatives.append("u.text LIKE ? ESCAPE '\\'")
                    params.append(f"%{_escape_like(term)}%")
            where.append("(" + " OR ".join(alternatives) + ")")

        rows = await self._all(
            f"SELECT {_UTTERANCE_COLUMNS} {_UTTERANCE_FROM} WHERE {' AND '.join(where)} "
            "ORDER BY u.t_start DESC, u.id DESC LIMIT ?",
            [*params, limit],
        )
        keyword_hits = [self._named(r) for r in rows]  # 从新到旧
        if not terms or query_vector is None or vector_k < 1:
            return keyword_hits[::-1]

        vector_ids = [
            r["rowid"]
            for r in await self._all(
                "SELECT rowid, distance FROM utterances_vec WHERE embedding MATCH ? AND k = ? "
                f"AND rowid IN (SELECT u.id {_UTTERANCE_FROM} WHERE {base_where} AND u.embedded = 1) "
                "ORDER BY distance",
                [pack_vector(query_vector), min(vector_k, limit), *base_params],
            )
            if max_distance is None or r["distance"] <= max_distance
        ]
        vector_hits: list[NamedUtterance] = []
        if vector_ids:
            marks = ",".join("?" * len(vector_ids))
            found = {
                r["id"]: self._named(r)
                for r in await self._all(
                    f"SELECT {_UTTERANCE_COLUMNS} {_UTTERANCE_FROM} WHERE u.id IN ({marks})",
                    vector_ids,
                )
            }
            vector_hits = [found[i] for i in vector_ids if i in found]  # 从近到远

        merged: dict[int, NamedUtterance] = {}
        for pair in zip_longest(keyword_hits, vector_hits):
            for item in pair:
                if item is not None and len(merged) < limit:
                    assert item.utterance.id is not None
                    merged.setdefault(item.utterance.id, item)
        return sorted(merged.values(), key=lambda n: (n.utterance.t_start, n.utterance.id or 0))

    # ------------------------------------------------------------------ #
    # 嵌入回填
    # ------------------------------------------------------------------ #

    async def unembedded_utterances(self, limit: int = 16) -> list[tuple[int, str]]:
        """还没有向量的发言（所有会议），按编号从小到大：``[(发言编号, 文字)]``。"""
        rows = await self._all(
            "SELECT id, text FROM utterances WHERE embedded = 0 ORDER BY id LIMIT ?", (limit,)
        )
        return [(r["id"], r["text"]) for r in rows]

    async def set_embeddings(self, items: Sequence[tuple[int, Sequence[float]]]) -> int:
        """写向量表并置位 ``embedded``。发言在这期间被删掉的跳过。返回写入的条数。"""
        written = 0
        async with self._lock:
            for utterance_id, vector in items:
                if (
                    await self._one("SELECT 1 FROM utterances WHERE id = ?", (utterance_id,))
                    is None
                ):
                    continue
                # vec0 不支持 INSERT OR REPLACE（已核实），先删再插
                await self._db.execute(
                    "DELETE FROM utterances_vec WHERE rowid = ?", (utterance_id,)
                )
                await self._db.execute(
                    "INSERT INTO utterances_vec (rowid, embedding) VALUES (?, ?)",
                    (utterance_id, pack_vector(vector)),
                )
                await self._db.execute(
                    "UPDATE utterances SET embedded = 1 WHERE id = ?", (utterance_id,)
                )
                written += 1
            await self._db.commit()
        return written

    # ------------------------------------------------------------------ #
    # 截图
    # ------------------------------------------------------------------ #

    @staticmethod
    def _frame(row: aiosqlite.Row) -> ScreenFrame:
        return ScreenFrame(
            id=row["id"],
            session_id=row["session_id"],
            t=row["t"],
            path=row["path"],
            width=row["width"],
            height=row["height"],
            caption=row["caption"],
            caption_status=row["caption_status"],
        )

    async def add_frame(
        self,
        session_id: str,
        *,
        t: float,
        width: int,
        height: int,
        suffix: str,
        caption_status: str = "pending",
        now: float | None = None,
    ) -> ScreenFrame:
        """登记一张截图。文件名用截图编号（递增）：``sessions/<会话>/frames/<6 位编号><后缀>``，相对数据目录。

        只写数据库；图片文件由调用方按返回的 ``path`` 落盘（落盘失败要调 ``delete_frame`` 撤销）。
        """
        now = time.time() if now is None else now
        async with self._lock:
            cursor = await self._db.execute(
                "INSERT INTO frames (session_id, t, path, width, height, caption_status) "
                "VALUES (?, ?, '', ?, ?, ?)",
                (session_id, t, width, height, caption_status),
            )
            frame_id = int(cursor.lastrowid or 0)
            path = f"sessions/{session_id}/frames/{frame_id:06d}{suffix}"
            await self._db.execute("UPDATE frames SET path = ? WHERE id = ?", (path, frame_id))
            await self._db.execute(
                "UPDATE sessions SET last_active_at = MAX(last_active_at, ?) WHERE id = ?",
                (now, session_id),
            )
            await self._db.commit()
        return ScreenFrame(
            id=frame_id,
            session_id=session_id,
            t=t,
            path=path,
            width=width,
            height=height,
            caption_status=caption_status,
        )

    async def delete_frame(self, frame_id: int) -> None:
        async with self._lock:
            await self._db.execute("DELETE FROM frames WHERE id = ?", (frame_id,))
            await self._db.commit()

    async def get_frame(self, frame_id: int) -> ScreenFrame | None:
        row = await self._one("SELECT * FROM frames WHERE id = ?", (frame_id,))
        return self._frame(row) if row else None

    async def list_frames(
        self, session_id: str, *, t_from: float | None = None, t_to: float | None = None
    ) -> list[ScreenFrame]:
        """按时间升序；``t_from`` / ``t_to`` 是闭区间。"""
        where, params = ["session_id = ?"], [session_id]
        if t_from is not None:
            where.append("t >= ?")
            params.append(t_from)
        if t_to is not None:
            where.append("t <= ?")
            params.append(t_to)
        rows = await self._all(
            f"SELECT * FROM frames WHERE {' AND '.join(where)} ORDER BY t, id", params
        )
        return [self._frame(r) for r in rows]

    async def latest_frame(self, session_id: str) -> ScreenFrame | None:
        row = await self._one(
            "SELECT * FROM frames WHERE session_id = ? ORDER BY t DESC, id DESC LIMIT 1",
            (session_id,),
        )
        return self._frame(row) if row else None

    async def set_frame_caption(
        self, frame_id: int, *, status: str, caption: str | None = None
    ) -> bool:
        """写画面摘要的状态；``caption`` 不给时保留原来的文字。"""
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE frames SET caption_status = ?, caption = COALESCE(?, caption) WHERE id = ?",
                (status, caption, frame_id),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # 滚动纪要
    # ------------------------------------------------------------------ #

    @staticmethod
    def _digest(row: aiosqlite.Row) -> Digest:
        return Digest(
            id=row["id"],
            session_id=row["session_id"],
            t_from=row["t_from"],
            t_to=row["t_to"],
            text=row["text"],
            created_at=row["created_at"],
            last_utterance_id=row["last_utterance_id"],
        )

    async def add_digest(
        self,
        session_id: str,
        *,
        t_from: float,
        t_to: float,
        text: str,
        last_utterance_id: int,
        now: float | None = None,
    ) -> Digest:
        now = time.time() if now is None else now
        async with self._lock:
            cursor = await self._db.execute(
                "INSERT INTO digests (session_id, t_from, t_to, text, created_at, last_utterance_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, t_from, t_to, text, now, last_utterance_id),
            )
            await self._db.commit()
            return Digest(
                int(cursor.lastrowid or 0), session_id, t_from, t_to, text, now, last_utterance_id
            )

    async def latest_digest(self, session_id: str) -> Digest | None:
        row = await self._one(
            "SELECT * FROM digests WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
        )
        return self._digest(row) if row else None

    async def list_digests(self, session_id: str) -> list[Digest]:
        rows = await self._all(
            "SELECT * FROM digests WHERE session_id = ? ORDER BY id", (session_id,)
        )
        return [self._digest(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 会后报告
    # ------------------------------------------------------------------ #

    @staticmethod
    def _report(row: aiosqlite.Row) -> Report:
        return Report(
            id=row["id"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            status=row["status"],
            provider=row["provider"],
            text_md=row["text_md"],
            error=row["error"],
        )

    async def create_report(
        self, session_id: str, *, provider: str = "", now: float | None = None
    ) -> int:
        """建一份「生成中」的报告，返回编号。"""
        now = time.time() if now is None else now
        async with self._lock:
            cursor = await self._db.execute(
                "INSERT INTO reports (session_id, created_at, status, provider) "
                "VALUES (?, ?, 'running', ?)",
                (session_id, now, provider),
            )
            await self._db.commit()
            return int(cursor.lastrowid or 0)

    async def finish_report(self, report_id: int, text_md: str) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE reports SET status = 'done', text_md = ?, error = NULL WHERE id = ?",
                (text_md, report_id),
            )
            await self._db.commit()

    async def fail_report(self, report_id: int, error: str) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE reports SET status = 'failed', error = ? WHERE id = ?", (error, report_id)
            )
            await self._db.commit()

    async def fail_running_reports(self, reason: str) -> int:
        """服务启动时调用：上次没生成完的报告标为失败。返回条数。"""
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE reports SET status = 'failed', error = ? WHERE status = 'running'",
                (reason,),
            )
            await self._db.commit()
            return cursor.rowcount

    async def latest_report(self, session_id: str, *, done_only: bool = False) -> Report | None:
        """最近一份报告；``done_only`` 时只要生成好的（导出用）。"""
        sql = "SELECT * FROM reports WHERE session_id = ?"
        if done_only:
            sql += " AND status = 'done'"
        row = await self._one(sql + " ORDER BY id DESC LIMIT 1", (session_id,))
        return self._report(row) if row else None

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #

    @staticmethod
    def _task(row: aiosqlite.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            session_id=row["session_id"],
            goal=row["goal"],
            requested_by=row["requested_by"],
            requested_t=row["requested_t"],
            status=row["status"],
            brief=row["brief"],
            detail_md=row["detail_md"],
            sources=json.loads(row["sources_json"] or "[]"),
            artifacts=json.loads(row["artifacts_json"] or "[]"),
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            announced=bool(row["announced"]),
            modality=row["modality"],
            t_from=row["t_from"],
            t_to=row["t_to"],
            frame_ids=json.loads(row["frame_ids_json"] or "[]"),
        )

    async def create_task(
        self,
        session_id: str,
        *,
        goal: str,
        requested_by: int = 0,
        requested_t: float = 0.0,
        t_from: float = 0.0,
        t_to: float = 0.0,
        frame_ids: Sequence[int] = (),
        modality: str = "voice",
        now: float | None = None,
    ) -> TaskRecord:
        """建一个排队中的任务。编号是 ``<会话 id>.t<序号>``，序号在这场会议内递增（从 1 起，不复用）。"""
        now = time.time() if now is None else now
        async with self._lock:
            rows = await self._all("SELECT id FROM tasks WHERE session_id = ?", (session_id,))
            taken = [int(m.group(1)) for r in rows if (m := _TASK_NUMBER.search(r["id"]))]
            task_id = f"{session_id}.t{max(taken, default=0) + 1}"
            await self._db.execute(
                "INSERT INTO tasks (id, session_id, goal, requested_by, requested_t, created_at, "
                "modality, t_from, t_to, frame_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    session_id,
                    goal,
                    requested_by,
                    requested_t,
                    now,
                    modality,
                    t_from,
                    t_to,
                    json.dumps(list(frame_ids)),
                ),
            )
            await self._db.commit()
        task = await self.get_task(task_id)
        assert task is not None
        return task

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = await self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._task(row) if row else None

    async def find_task(self, session_id: str, label: str | None) -> TaskRecord | None:
        """按会议内的短编号（``t3``，大小写和空格不敏感）找任务；``label`` 为空时取这场会议最近建的一个。"""
        if label and label.strip():
            return await self.get_task(f"{session_id}.{label.strip().lower()}")
        row = await self._one(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        return self._task(row) if row else None

    async def list_tasks(self, session_id: str) -> list[TaskRecord]:
        rows = await self._all(
            "SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
        )
        return [self._task(r) for r in rows]

    async def update_task(self, task_id: str, **fields: Any) -> TaskRecord | None:
        """改任务的若干列。``sources`` / ``artifacts`` 给列表，``announced`` 给布尔值。"""
        columns = {
            "status": "status",
            "brief": "brief",
            "detail_md": "detail_md",
            "error": "error",
            "started_at": "started_at",
            "finished_at": "finished_at",
            "sources": "sources_json",
            "artifacts": "artifacts_json",
            "announced": "announced",
        }
        unknown = set(fields) - set(columns)
        if unknown:
            raise ValueError(f"任务表没有这些可改的列：{sorted(unknown)}")
        if fields:
            values = [
                json.dumps(list(v), ensure_ascii=False)
                if k in ("sources", "artifacts")
                else (int(v) if k == "announced" else v)
                for k, v in fields.items()
            ]
            assignments = ", ".join(f"{columns[k]} = ?" for k in fields)
            async with self._lock:
                await self._db.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ?", [*values, task_id]
                )
                await self._db.commit()
        return await self.get_task(task_id)

    async def fail_unfinished_tasks(self, reason: str, *, now: float | None = None) -> int:
        """把还在排队或运行中的任务一律标为失败（服务重启后调用：它们的执行已经不在了）。返回条数。"""
        now = time.time() if now is None else now
        async with self._lock:
            cursor = await self._db.execute(
                "UPDATE tasks SET status = 'failed', error = ?, finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (reason, now),
            )
            await self._db.commit()
            return cursor.rowcount

    async def add_task_event(
        self,
        task_id: str,
        kind: str,
        summary: str,
        payload: dict | None = None,
        *,
        now: float | None = None,
    ) -> TaskEvent:
        now = time.time() if now is None else now
        async with self._lock:
            cursor = await self._db.execute(
                "INSERT INTO task_events (task_id, at, kind, summary, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    now,
                    kind,
                    summary,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )
            await self._db.commit()
            return TaskEvent(int(cursor.lastrowid or 0), task_id, now, kind, summary, payload)

    async def list_task_events(self, task_id: str, *, last: int | None = None) -> list[TaskEvent]:
        """按先后顺序；``last`` 给出时只要最近的若干条。"""
        if last is not None:
            rows = (
                await self._all(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                    (task_id, last),
                )
            )[::-1]
        else:
            rows = await self._all(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        return [
            TaskEvent(
                r["id"],
                r["task_id"],
                r["at"],
                r["kind"],
                r["summary"],
                json.loads(r["payload_json"]) if r["payload_json"] else None,
            )
            for r in rows
        ]
