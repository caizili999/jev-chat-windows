# -*- coding: utf-8 -*-
"""知识库这条路的自检（对齐上游 core/kb/KbSelfCheck.kt）。

跑的是最容易**悄悄错**的那几处：半角/全角人数下的名字归一化、笔记关键词命中、
历史对「屏上已有的消息」的去重、以及预算注入。跑完把自己造的东西删干净。

它对着**真实的 KbStore** 跑（临时笔记 + 临时联系人，最后删掉），所以用户在设置页点一下
就能确认这条链路在自己机器上是通的。要离线测就给它一个建在临时目录上的 store
（见 tools/check_kb.py）。

**不依赖 Qt**，返回一句人话总结，不抛异常。
"""
from __future__ import annotations

from app.kb import context as ctx_builder
from app.kb.models import Contact, LogEntry, Note
from app.kb.store import KbStore, new_id, normalize_name

_BARE = "测试群"
_TITLE = "测试群(12)"          # 半角括号人数
_ALIAS = "测试群（12）"         # 全角括号人数，故意的
_PADDED = "  测试群  "
_OLD_LINE = "上周说好周五交自检稿"
_FACT = "自检用的虚构事实：项目代号叫小蓝。"
_REL = "自检用的关系描述"
_APP = "wechat"


def run(store: KbStore) -> str:
    """返回一行「自检通过 / 自检失败」的总结。"""
    failures = []

    # 0. 先单独查名字归一化：它的正则在 store 模块导入时就编译好了，一个引擎不认的
    #    写法以前能把整个类（连带每一次分析）拖死。这里先把它钉住。
    try:
        for raw in (_TITLE, _ALIAS, _PADDED, _BARE):
            got = normalize_name(raw)
            if got != _BARE:
                failures.append(f"名称归一化失败：输入 {raw!r} 得到 {got!r}，应为 {_BARE!r}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"名称归一化异常：{type(e).__name__} {e}")

    note_id = new_id()
    contact_id = new_id()
    try:
        store.save_note(Note(id=note_id, title="自检临时笔记", content=_FACT,
                             tags=["测试"], always_on=False, enabled=True))
        store.save_contact(Contact(id=contact_id, name="自检临时联系人",
                                   aliases=[_ALIAS], apps=[_APP], relationship=_REL))

        msgs = [("her", "自检消息一：这条够长可以去重"),
                ("me", "自检消息二：这条也够长")]

        def build(history_enabled=True, count=30):
            return ctx_builder.build(store, _TITLE, msgs, app=_APP,
                                     history_enabled=history_enabled, history_count=count)

        # 1. 联系人命中：靠全角别名 + 去人数
        c1 = build()
        if c1.contact is None or c1.contact.id != contact_id:
            failures.append(f"联系人未命中（标题 {_TITLE} 应匹配别名 {_ALIAS}）")

        # 2. 笔记命中：tag「测试」出现在会话标题里
        if not any(n.id == note_id for n in c1.notes):
            failures.append(f"笔记未命中（tag=测试 应命中标题 {_TITLE}）")

        # 3. 屏上消息记下来了，但不会作为历史回注给自己
        if c1.history:
            failures.append(f"历史去重失败：当屏消息不该出现在注入历史里（{len(c1.history)} 条）")
        if store.log_size(contact_id) != len(msgs):
            failures.append(f"历史落盘条数不对：期望 {len(msgs)}，实际 {store.log_size(contact_id)}")

        # 4. 旧的一行要活下来；同一屏再采一遍不会重复写。
        #    screen_batch=False：这是手工注入的一行，不是一次屏幕采集，
        #    所以不拿它跟「上次记录的那一屏」比。
        store.append_log(contact_id, [LogEntry("her", _OLD_LINE, store.now() - 86_400_000, _APP)],
                         screen_batch=False)
        c2 = build()
        if len(c2.history) != 1 or c2.history[0].text != _OLD_LINE:
            failures.append(f"历史注入不对：期望仅 1 条旧消息，实际 {len(c2.history)} 条")
        if store.log_size(contact_id) != len(msgs) + 1:
            failures.append(f"重复采集被写了第二遍：{store.log_size(contact_id)} 条")

        # 5. background 把那条编造的事实和关系带进了 prompt
        background = c2.background("默认关系")
        if "小蓝" not in background:
            failures.append("background 里没有笔记正文")
        if _REL not in background:
            failures.append("background 里没有联系人关系")

        # 6. 历史默认是关的（只有明确打开才有）
        if build(history_enabled=False).history:
            failures.append("history_enabled=False 时仍注入了历史")
    except Exception as e:  # noqa: BLE001
        failures.append(f"异常：{type(e).__name__} {e}")
    finally:
        try:
            store.delete_note(note_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            store.delete_contact(contact_id)
        except Exception:  # noqa: BLE001
            pass

    counts = store.counts()
    if not failures:
        return (f"自检通过：联系人匹配 / 笔记命中 / 历史去重 / 预算注入都正常。"
                f"当前知识库 {counts.notes} 条笔记、{counts.contacts} 个联系人、"
                f"{counts.log_lines} 条历史。")
    return f"自检失败（{len(failures)}）：" + "；".join(failures)
