"""持久化：会议、发言、截图、纪要、任务。

schema.sql     表结构
db.py          ``Store``：aiosqlite 封装——建库、会话 / 连接 / 说话人 / 发言 / 截图 / 纪要 / 任务 / 报告的读写，
               按说话人 / 时间 / 关键词 / 向量召回
embeddings.py  调嵌入服务并在后台回填向量表
"""

from agentic_meeting.store.db import Store, StoreError, default_speaker_name

__all__ = ["Store", "StoreError", "default_speaker_name"]
