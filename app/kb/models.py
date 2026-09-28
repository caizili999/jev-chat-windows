# -*- coding: utf-8 -*-
"""本地知识库的数据模型（对齐上游 jev-chat-jarvis 的 core/kb/KbModels.kt）。

全部只存在程序目录下的 `知识库/` 里，纯 JSON——没有数据库、不出网、不导出。
用户能在设置页「清空知识库与历史」一键删掉，删的就是那个目录，别的不动。

**不依赖 Qt**，所以能单独离线测（见 tools/check_kb.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# 上游用 "me" / "other" 两个值。本项目 core/ 那层的口径是 "her" / "me"
# （见 core/questions.build_state 的入参校验），注入历史时不做转换就能直接用，
# 所以这里存 "her" 而不是 "other"。除此之外语义完全一致。
SIDE_ME = "me"
SIDE_HER = "her"


def side_of(who) -> str:
    """把采集层的 who 归一到 KB 的两个取值。

    采集层还有 "gray"（时间戳灰字 / 群里的发言人名），它不该进历史——归到 "her"
    只是为了让 side 永远是这两个值之一，调用方负责别把 gray 那些行喂进来。
    上游是 `if (side == "me") 我 else 对方`，同样只有两档。
    """
    return SIDE_ME if who == SIDE_ME else SIDE_HER


@dataclass
class Note:
    """一条用户手写的自由文本笔记。"""

    id: str
    title: str
    content: str
    tags: list = field(default_factory=list)
    always_on: bool = False   # 常驻：不管在聊什么都注入
    enabled: bool = True
    updated_at: int = 0

    def to_json(self) -> dict:
        return {"id": self.id, "title": self.title, "content": self.content,
                "tags": list(self.tags), "alwaysOn": self.always_on,
                "enabled": self.enabled, "updatedAt": self.updated_at}

    @staticmethod
    def from_json(o: dict, new_id) -> "Note":
        return Note(id=str(o.get("id") or "") or new_id(),
                    title=str(o.get("title") or ""),
                    content=str(o.get("content") or ""),
                    tags=_str_list(o.get("tags")),
                    always_on=bool(o.get("alwaysOn", False)),
                    enabled=bool(o.get("enabled", True)),
                    updated_at=_int(o.get("updatedAt"), 0))

    def stamped(self, now: int) -> "Note":
        return replace(self, updated_at=now)


@dataclass
class Contact:
    """一个聊天对象（人或群）。

    aliases 是让它跨会话生效的关键：同一个人在微信 / QQ / 飞书里显示成不同的会话标题，
    每个标题都能列在这里。
    """

    id: str
    name: str
    aliases: list = field(default_factory=list)
    apps: list = field(default_factory=list)   # 见过它的会话来源标识（本项目固定 "wechat"）
    relationship: str = ""
    notes: str = ""
    auto_summary: str = ""   # 预留给（未实现的）自动摘要；本版从不写入，见上游说明
    updated_at: int = 0

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "aliases": list(self.aliases),
                "apps": list(self.apps), "relationship": self.relationship,
                "notes": self.notes, "autoSummary": self.auto_summary,
                "updatedAt": self.updated_at}

    @staticmethod
    def from_json(o: dict, new_id) -> "Contact":
        return Contact(id=str(o.get("id") or "") or new_id(),
                       name=str(o.get("name") or ""),
                       aliases=_str_list(o.get("aliases")),
                       apps=_str_list(o.get("apps")),
                       relationship=str(o.get("relationship") or ""),
                       notes=str(o.get("notes") or ""),
                       auto_summary=str(o.get("autoSummary") or ""),
                       updated_at=_int(o.get("updatedAt"), 0))

    def stamped(self, now: int) -> "Contact":
        return replace(self, updated_at=now)


@dataclass
class LogEntry:
    """一条记住的聊天行。side 只认 "me" / "her"（见 SIDE_ME / SIDE_HER）。"""

    side: str
    text: str
    ts: int = 0
    app: str = ""

    def to_json(self) -> dict:
        return {"side": self.side, "text": self.text, "ts": self.ts, "app": self.app}

    @staticmethod
    def from_json(o: dict) -> "LogEntry":
        return LogEntry(side=str(o.get("side") or "her") or "her",
                        text=str(o.get("text") or ""),
                        ts=_int(o.get("ts"), 0),
                        app=str(o.get("app") or ""))


@dataclass
class KbCounts:
    """设置页要显示的三行数字。"""

    notes: int = 0
    contacts: int = 0
    log_lines: int = 0


@dataclass
class ChatContext:
    """一次分析能看到的、屏幕之外的东西：对方是谁、更早的历史、命中的知识笔记。"""

    contact: "Contact | None" = None
    history: list = field(default_factory=list)   # [LogEntry]，旧→新
    notes: list = field(default_factory=list)     # [Note]，常驻在前、命中的在后

    def is_empty(self) -> bool:
        """没有任何额外信息可注入时为真——这时**一个字段都不发**（见 core/questions）。"""
        if self.history or self.notes:
            return False
        c = self.contact
        if c is None:
            return True
        return not (c.relationship.strip() or c.notes.strip() or c.auto_summary.strip())

    def background(self, default_relationship: str = "") -> str:
        """注入到判断 state 与起草 prompt 的那段背景文本。

        拼法：关系 + 关于<对方>的备注 + 过往摘要 + 每条命中笔记的「标题: 正文」。全空则返回空串——
        调用方这时必须**整个字段都不发**，而不是发一个空字段。

        default_relationship **故意不用**：联系人不带关系时，那句全局默认关系已经作为
        `chat.relationship` 单独发出去了，在这里再写一遍只是重复。联系人没填关系就干脆
        不输出「关系：」这一行。（跟上游 ChatContext.background 完全一致，参数留着是为了
        调用点对齐。）
        """
        parts = []
        c = self.contact
        if c is not None:
            rel = c.relationship.strip()
            if rel:
                parts.append(f"关系：{rel}\n")
            if c.notes.strip():
                parts.append(f"关于{c.name}：{c.notes.strip()}\n")
            if c.auto_summary.strip():
                parts.append(f"过往摘要：{c.auto_summary.strip()}\n")
        for n in self.notes:
            parts.append(f"{n.title.strip()}: {n.content.strip()}\n")
        return "".join(parts).strip()


# ── 反序列化用的小工具：坏形状一律当默认值，绝不因为一个脏字段让整份文件读不出来 ──

def _str_list(v) -> list:
    """org.json 的 strList：非数组 → 空；数组里非字符串项跳过；每项 trim 后非空才留。"""
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        s = str(item).strip() if isinstance(item, (str, int, float)) else ""
        if s:
            out.append(s)
    return out


def _int(v, default: int) -> int:
    """org.json 的 optLong：拿不出整数就用默认值。bool 是 int 的子类，得先排掉。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default
