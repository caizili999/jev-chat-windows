# -*- coding: utf-8 -*-
"""把「此刻屏幕上有什么」变成一次分析能额外带上的上下文（对齐上游 core/kb/ContextBuilder.kt）：
这次聊的是哪个联系人、他更早的历史、以及哪些知识笔记该带上。

刻意做得很笨很便宜（上游 v1.3 的取舍）：名字/别名**精确**匹配、笔记**纯子串**匹配、一个硬字符预算。
没有打分、没有 embedding、不自动建联系人、不出网。

**不依赖 Qt**，能单独离线测（见 tools/check_kb.py）。
"""
from __future__ import annotations

from app.kb.models import ChatContext, LogEntry, side_of
from app.kb.store import MAX_LOG, normalize_text

BUDGET_CHARS = 1500      # 笔记 + 历史加起来不能超过这么多字符
MAX_HIT_NOTES = 5        # 最多带几条「命中」的（非常驻）笔记
_MATCH_WINDOW = 6        # 只在最近这几条消息里找笔记关键词
_DEDUPE_MIN_LEN = 4      # 短于这个长度的屏上重复，不值得去重


def build(store, title, messages, app: str = "",
          history_enabled: bool = False, history_count: int = 30) -> ChatContext:
    """一次分析的知识库上下文。每个字段都可能是空的——用户还没建知识库时这就是常态。

    store: KbStore。
    title: 当前会话标题（联系人靠它匹配）。
    messages: 屏上的消息，`(who, text)` / `(who, text, name)` / 带 from+text 的 dict 都认；
              who ∈ {me, her, gray}，gray 那些行（时间戳、群里的发言人名）调用方别喂进来。
    app: 会话来源标识（本项目固定 "wechat"），只用于「两个联系人都命中时优先选见过它的那个」。
    history_enabled: 用户有没有打开「记录聊天历史（只存本机）」。**默认关**。
    history_count: 注入最近多少条历史，0~100（0 = 不注入，但仍会记录）。

    ⚠️ 这个函数有**副作用**：history_enabled 且匹配到联系人时，会把这一屏追加进该联系人的历史。
    上游就是这么做的（记录与注入同一个入口），调用方按「每次分析前调一次」来用。
    """
    msgs = _norm_messages(messages)

    # 1. 联系人——只匹配，绝不在这里创建。
    contact = store.find_contact(title, app)

    # 2. 历史——只有用户明确打开才会记录和注入。
    history = (history_for(store, contact, msgs, app, history_count)
               if history_enabled and contact is not None else [])

    # 3. 笔记——常驻的，加上关键词命中的。
    enabled = [n for n in store.notes() if n.enabled]
    always_on = [n for n in enabled if n.always_on]
    hits = match_notes([n for n in enabled if not n.always_on], title, msgs)

    # 4. 预算：常驻笔记豁免；其余的共享 BUDGET_CHARS——先丢最旧的历史，再整条丢笔记
    #    （绝不截半条笔记）。
    trimmed_history = list(history)
    trimmed_hits = list(hits)
    while _cost(trimmed_hits, trimmed_history) > BUDGET_CHARS and trimmed_history:
        trimmed_history.pop(0)
    while _cost(trimmed_hits, trimmed_history) > BUDGET_CHARS and trimmed_hits:
        trimmed_hits.pop()

    return ChatContext(contact, trimmed_history, always_on + trimmed_hits)


def history_for(store, contact, msgs, app: str, history_count: int = 30) -> list:
    """先把屏上消息记下来，再读回最近的一段、去掉**已经显示在屏幕上**的那些。

    只有长到「精确重复肯定就是同一条消息」的行才拿去去重（见 _DEDUPE_MIN_LEN）。
    """
    now = store.now()
    store.append_log(contact.id, [LogEntry(side_of(who), text, now, app)
                                 for who, text in msgs])
    n = max(0, min(100, int(history_count or 0)))
    if n == 0:
        return []
    on_screen = {f"{side} {text}" for side, text in msgs if len(text) >= _DEDUPE_MIN_LEN}
    # **先按最宽的窗口过滤，再 takeLast(n)。** 反过来会让当前屏上的消息吃掉配额——
    # 要 30 条历史，实际只拿到「30 减去屏上已有的那几条」。
    return [e for e in store.recent_log(contact.id, MAX_LOG)
            if f"{e.side} {e.text}" not in on_screen][-n:]


def match_notes(candidates: list, title, msgs: list) -> list:
    """一条笔记命中，是指它的**任一 tag 或它的标题**出现在会话标题里、或出现在最近几条消息里。

    越新的笔记越优先占满 MAX_HIT_NOTES 的名额。
    """
    if not candidates:
        return []
    haystack = normalize_text(title)
    for _, text in msgs[-_MATCH_WINDOW:]:
        haystack += "\n" + normalize_text(text)
    if not haystack.strip():
        return []
    hits = []
    for n in candidates:
        for raw in list(n.tags) + [n.title]:
            needle = normalize_text(raw)
            if needle and needle in haystack:
                hits.append(n)
                break
    hits.sort(key=lambda n: n.updated_at, reverse=True)   # 稳定排序，跟上游一致
    return hits[:MAX_HIT_NOTES]


def as_knowledge(ctx: ChatContext) -> dict:
    """ChatContext → core 认的那个**普通 dict**（见 core.questions.knowledge_parts）。

    这是 app → core 的唯一交接面：core 不能反向 import app，所以在这里把对象摊平成
    {"background": str, "history": [{"from":…, "text":…}]}，边界因此是显式的、可离线测的。
    """
    return {"background": ctx.background(),
            "history": [{"from": e.side, "text": e.text} for e in ctx.history]}


def _cost(notes: list, history: list) -> int:
    """预算按字符算，跟上游同口径：笔记算「标题 + 正文 + 2」，历史算「正文 + 3」。

    诚实说一处差异：上游是 Kotlin 的 `String.length`（UTF-16 码元），这里 `len()` 是码点。
    中文（BMP）两边一样；只有 emoji 这类增补平面字符上，上游算 2、这里算 1。
    不影响「有没有超预算」的判定方向，不值得为此在 Python 里模拟代理对。
    """
    return (sum(len(n.title) + len(n.content) + 2 for n in notes)
            + sum(len(e.text) + 3 for e in history))


def _norm_messages(messages) -> list:
    """统一成 [(side, text)]，side 只认 "me" / "her"。跟 core/questions.build_state 认同样的形状。"""
    out = []
    for item in messages or []:
        if isinstance(item, dict):
            who, text = item.get("from"), item.get("text")
        else:
            who, text = item[0], item[1]
        out.append((side_of(who), str(text if text is not None else "")))
    return out
