# -*- coding: utf-8 -*-
"""本地知识库（对齐上游 jev-chat-jarvis 的 core/kb）。

四块：
- models     数据模型（Note / Contact / LogEntry / ChatContext / KbCounts）
- store      存储（程序目录下 `知识库/` 的几份 JSON，原子写，坏文件保全）
- context    「此刻屏幕上有什么」→ 该带上哪些联系人 / 历史 / 笔记（含 1500 字预算）
- selfcheck  设置页那个「自检」

**不出网、不依赖 Qt**：整包能离线跑（见 tools/check_kb.py）。聊天正文不进日志。

导入顺序有讲究：context 依赖 store，selfcheck 依赖 context——按这个次序导出，
`from app.kb import selfcheck` 才不会撞上循环导入。
"""
from app.kb.models import ChatContext, Contact, KbCounts, LogEntry, Note  # noqa: F401
from app.kb.store import MAX_LOG, KbStore, display_name, new_id, normalize_name, normalize_text  # noqa: F401
from app.kb import context  # noqa: F401
from app.kb import selfcheck  # noqa: F401
