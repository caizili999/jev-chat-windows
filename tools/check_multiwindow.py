# -*- coding: utf-8 -*-
"""多独立窗口 · 父进程侧（main.py 的 S3 切片）离线检查。跑：python tools/check_multiwindow.py

**不起 Qt 窗口、不截图、不联网、不碰微信**：这里只喂子进程队列消息给 `main.drain()`，
看父进程的状态机对不对。真窗口那半边由 tools/check_windows.py 管。

钉住的是「7 个单值全局改成按会话」这件事上最容易悄悄错的地方：

1. `("session", hwnd, 会话名, is_main)` 分路——主窗口会换会话（老行为），独立窗口钉住一个会话；
2. `("area", 会话名, rect)` / `("lines", …)` 里的坐标**必须落到那个会话自己的窗口上**，
   落错就是把回复打进别人那儿；
3. `("gone", hwnd)` 只清那个窗口，不能顺手把别的会话也清了；
4. rerun 从「一个槽」改成「按会话排队」——原来三个群同时来消息，第二个会把第一个顶掉；
5. **回退路径逐字节不变**：没有独立窗口时，`showing()` 必须等价于老的 `state["chat"] == title`。

每一组断言都配了「把旧实现换回来会不会失败」的反证（见 main() 末尾那段说明）。
"""
import io
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import main  # noqa: E402

_MAIN = 67239        # 主窗口（实测那个 1440x753 的）
_POP_A = 920244      # 独立窗口：程序员烧烤🦞技术交流群v3.0
_POP_B = 527032      # 独立窗口：1群
_AREA = (100, 200, 900, 800, (255, 255, 255), 60)


class FakeQ:
    """把一串队列消息喂给 drain()。drain() 会一直 get_nowait 到空，空就 return。"""

    def __init__(self, msgs=()):
        self.msgs = list(msgs)

    def get_nowait(self):
        if not self.msgs:
            raise queue.Empty
        return self.msgs.pop(0)


class FakeOv:
    """只记「被调用了什么」，不碰 Qt。drain() 用到的方法一个都不能少——少一个就是
    AttributeError，正好替我们把「drain 又长出新依赖了」这件事顶出来。"""

    def __init__(self, shown=""):
        self.calls = []
        self.shown = shown
        self.pending = False

    # ── drain() 会调的 ──
    def set_status(self, text, kind="idle"):
        self.calls.append(("status", text, kind))

    def log(self, line):
        self.calls.append(("log", line))

    def set_chat(self, title):
        self.calls.append(("set_chat", title))
        self.shown = title

    def set_capture(self, on, *a):
        self.calls.append(("capture", on))

    def set_busy(self, on):
        self.calls.append(("busy", on))

    def invalidate_replies(self):
        self.calls.append(("invalidate",))

    def cancel_auto(self, reason):
        self.calls.append(("cancel_auto", reason))
        self.pending = False

    def auto_pending(self):
        return self.pending

    def log_message(self, who, text, name, chat=None):
        self.calls.append(("msg", chat, who, text, name))

    def set_targets(self, title, senders, current):
        self.calls.append(("targets", title, list(senders), current))

    def refresh_windows(self):
        # 窗口集合变了要重画首页那一行「允许自动回复」。这里只记一笔。
        self.calls.append(("refresh_windows",))

    def notify_unpaired(self, title):
        # 「这个窗口还没配到联系人」那条非模态提示。真正的界面由 check_kb_ui 验。
        self.calls.append(("unpaired", title))

    def begin_auto(self, text, seconds, note=""):
        self.calls.append(("begin", text, seconds, note))
        self.pending = True

    def can_auto_send(self):
        # 回退路径（主窗口）那道老闸。这里默认放行，要拦的时候由用例改。
        return True

    # ── 别的检查里 main 会调的（这里不打桩的话 AttributeError）──
    def after(self, *a):
        pass

    def current_chat(self):
        return self.shown

    def set_failed(self, *a):
        pass

    def show(self, *a):
        pass

    def set_context_info(self, *a, **k):
        pass

    def kinds(self):
        return [c[0] for c in self.calls]

    def chat_calls(self):
        return [c[1] for c in self.calls if c[0] == "set_chat"]


def _setup(msgs, shown=""):
    """把 main 切到「离线可跑」的样子，返回 (ov, 还原函数)。"""
    real = {k: vars(main).get(k) for k in ("ov", "q", "history_rec", "start_analyze",
                                           "chats", "state", "kb_store", "_unpaired_warned")}
    ov = FakeOv(shown)
    main.ov = ov
    main.q = FakeQ(msgs)
    main.history_rec = None
    main.kb_store = None
    main.chats = {}
    # 「已经提示过没配对」的那个集合也要清空：它按标题去重，不清的话这一组用例说了
    # 「1群」之后，下一组用例就再也等不到那条提示了（断言会静默变成空跑）。
    main._unpaired_warned = set()
    main.state = {"busy": False, "auto_title": "", "wins": {}, "input_has": {}, "rerun": {},
                  "batches": {}, "quiet": {}, "chat": "", "analyzing": "", "auto_queue": {}}
    main.start_analyze = lambda title, msgs, auto_ok=False: main.chats.setdefault(
        title, {}).setdefault("_started", []).append((list(msgs), auto_ok))
    # 「群里安静窗口」那半边由 check_auto_send 专门管；这里关掉它，否则群消息会先走 arm_quiet、
    # 压根到不了 rerun 那条路——那样这些断言就全成了摆设（跑了但什么都没验）。
    real_any = main.settings.auto_send_group_any
    main.settings.auto_send_group_any = lambda: False

    def restore():
        main.settings.auto_send_group_any = real_any
        for k, v in real.items():
            setattr(main, k, v)
    return ov, restore


def _started(title):
    return main.chats.get(title, {}).get("_started", [])


# ── 1. session 消息分两条路 ────────────────────────────────────────────────────

def check_session_routing():
    """主窗口换会话 = 跟过去；独立窗口只登记、**不抢视图**（S6 有了标签页之后）。

    为什么独立窗口不再自动切过去：每发现一个窗口就切一次，用户开三个窗口看到的是
    「界面停在最后一个」，反而找不到东西。「现在盯的是哪几个」已经由会话标签页
    摆在那儿了（D11），标签页才是那个出口。
    """
    ov, restore = _setup([
        ("session", _MAIN, "小分队", True),
        ("session", _POP_A, "程序员烧烤🦞技术交流群v3.0", False),
        ("session", _POP_B, "1群", False),
    ])
    try:
        main.drain()
        assert main.state["chat"] == "小分队", main.state["chat"]
        assert set(main.state["wins"]) == {"小分队", "程序员烧烤🦞技术交流群v3.0", "1群"}
        assert main.state["wins"]["小分队"] == {"hwnd": _MAIN, "area": None, "is_main": True}
        assert main.state["wins"]["1群"]["hwnd"] == _POP_B
        assert main.state["wins"]["1群"]["is_main"] is False
        # 界面已经在小分队上了，两个独立窗口只登记、不上屏。
        # 要是这里变成三个 set_chat，就是「开三个窗口、界面停在最后一个」那个毛病。
        assert ov.chat_calls() == ["小分队"], ov.chat_calls()
    finally:
        restore()

    # 界面上还什么都没在看（启动那一下）：第一个独立窗口该顶上去，之后的就不抢了
    ov, restore = _setup([
        ("session", _POP_A, "程序员烧烤🦞技术交流群v3.0", False),
        ("session", _POP_B, "1群", False),
    ])
    try:
        main.drain()
        assert ov.chat_calls() == ["程序员烧烤🦞技术交流群v3.0"], ov.chat_calls()
        assert set(main.state["wins"]) == {"程序员烧烤🦞技术交流群v3.0", "1群"}
    finally:
        restore()

    # 主窗口换会话：界面跟过去（微信是准的），而且**只有真的换了才调 set_chat**
    ov, restore = _setup([("session", _MAIN, "小分队", True), ("session", _MAIN, "小分队", True),
                          ("session", _MAIN, "老婆", True)])
    try:
        main.drain()
        assert main.state["chat"] == "老婆"
        assert ov.chat_calls() == ["小分队", "老婆"], ov.chat_calls()
    finally:
        restore()

    # 空标题（主窗口刚建、还没 OCR 出来）不许拿去切界面——那是占位，不是会话名
    ov, restore = _setup([("session", _MAIN, "", True)])
    try:
        main.drain()
        assert ov.chat_calls() == [], ov.chat_calls()
        assert main.state["wins"] == {}, "空标题不该建出一个会话"
        assert main.state["chat"] == ""
    finally:
        restore()
    print("session 消息分路 ok（主窗口跟过去 / 独立窗口只登记不抢视图 / 空标题不建会话）")


# ── 1b. 「这个窗口还没配到联系人」的提示 ────────────────────────────────────────

class FakeKb:
    """只认 `find_contact` 的知识库替身——这条提示只问这一件事。"""

    def __init__(self, known):
        self.known = set(known)

    def find_contact(self, title, app=""):
        return object() if title in self.known else None


def check_unpaired_warning():
    """提示的三个前提缺一不说：独立窗口 / 有知识库 / 这个标题还没说过。

    这条提示是「会话名从 OCR 头部改成 Windows 窗口标题」的必然后果（DESIGN_MULTIWINDOW
    §6.2）：窗口标题跟现有联系人一个都对不上（`1群` vs `1群(4)Q`，那个 Q 是 OCR 把 🔍
    认成的字），不配一次那些历史会**静默**失效，用户只会以为功能坏了。
    """
    # ① 没有知识库：一个字都不说。那种情况下根本没有「配到联系人」这回事，
    #    凭空冒一条提示就破坏了「不加知识库就逐字节不变」这条最硬的约束。
    ov, restore = _setup([("session", _POP_A, "1群", False)])
    try:
        main.drain()
        assert [c for c in ov.calls if c[0] == "unpaired"] == [], \
            "没有知识库时不该有这条提示"
    finally:
        restore()

    # ② 有知识库：独立窗口对不上才说；主窗口的会话永远不说；配得上的也不说
    ov, restore = _setup([("session", _MAIN, "小分队", True),
                          ("session", _POP_A, "1群", False),
                          ("session", _POP_B, "老婆", False)])
    main.kb_store = FakeKb({"老婆"})
    try:
        main.drain()
        said = [c[1] for c in ov.calls if c[0] == "unpaired"]
        assert said == ["1群"], \
            f"只有对不上的独立窗口该被提示（主窗口和配得上的都不说），实际 {said}"
    finally:
        restore()

    # ③ 同一个标题只说一次：再收到一次 session 不该又弹一遍（唠叨会让人直接关掉它）
    ov, restore = _setup([("session", _POP_A, "1群", False)])
    main.kb_store = FakeKb(set())
    try:
        main.drain()
        main.q = FakeQ([("session", _POP_A, "1群", False)])
        main.drain()
        said = [c[1] for c in ov.calls if c[0] == "unpaired"]
        assert said == ["1群"], f"同一个窗口只说一次，实际 {said}"
    finally:
        restore()

    # ④ 知识库自己炸了也不该把采集拖垮（提示不出来不是故障）
    class BoomKb:
        def find_contact(self, title, app=""):
            raise OSError("知识库读不了")

    ov, restore = _setup([("session", _POP_A, "1群", False)])
    main.kb_store = BoomKb()
    try:
        main.drain()   # 不该抛
        assert main.state["wins"]["1群"]["hwnd"] == _POP_A, "提示出不来也不能影响窗口登记"
    finally:
        restore()
    print("未配对窗口提示 ok（无库不说 / 只对独立窗口 / 只说一次 / 炸了不拖垮采集）")


# ── 2. 坐标按会话落 ────────────────────────────────────────────────────────────

def check_area_is_per_session():
    """`("area", 会话名, rect)` 只能落到那个会话自己的窗口上。

    落错就是把回复打进别人那儿——这是多窗口下最贵的一种错，而且看不出错在哪。
    """
    ov, restore = _setup([
        ("session", _MAIN, "小分队", True),
        ("session", _POP_B, "1群", False),
        ("area", "1群", (10, 20, 30, 40)),
    ])
    try:
        main.drain()
        assert main.state["wins"]["1群"]["area"] == (10, 20, 30, 40)
        assert main.state["wins"]["小分队"]["area"] is None, "别的会话的坐标被写串了"
    finally:
        restore()

    # lines 里带的坐标同理
    ov, restore = _setup([
        ("session", _POP_A, "烧烤群", False),
        ("lines", "烧烤群", [("her", "阿杰", "你好", None)], (7, 8, 9, 10)),
    ])
    try:
        main.drain()
        assert main.state["wins"]["烧烤群"]["area"] == (7, 8, 9, 10)
    finally:
        restore()

    # 主窗口**换会话**时坐标要继承：子进程的 last_area 是按窗口记的、没变，不会再发一次 area。
    # 不继承的话 fill_reply 会对着一个刚切过去的会话说「输入区域尚不可用」。
    ov, restore = _setup([
        ("session", _MAIN, "小分队", True),
        ("area", "小分队", (11, 22, 33, 44)),
        ("session", _MAIN, "老婆", True),
    ])
    try:
        main.drain()
        assert main.state["wins"]["老婆"]["area"] == (11, 22, 33, 44), \
            f"换会话后坐标没继承：{main.state['wins']['老婆']}"
    finally:
        restore()
    print("坐标按会话落 ok（各记各的 / lines 里的也算 / 换会话时继承同一窗口的）")


# ── 3. 窗口消失只清它自己 ──────────────────────────────────────────────────────

def check_gone_is_scoped():
    """一个窗口关掉，别的会话一个字都不能动。"""
    ov, restore = _setup([
        ("session", _MAIN, "小分队", True),
        ("session", _POP_B, "1群", False),
        ("area", "1群", (10, 20, 30, 40)),
        ("input", "1群", False),
        ("gone", _POP_B),
    ])
    try:
        main.drain()
        assert "1群" not in main.state["wins"], "关掉的窗口没清"
        assert "小分队" in main.state["wins"], "把别的会话一起清了"
        assert main.state["chat"] == "小分队", "主窗口的游标被别的窗口关掉带走了"
    finally:
        restore()

    # 主窗口关掉 → 游标清空（老代码只在 "dead" 时清 area，这里多一步是安全的：
    # 游标留着会让 showing() 对主窗口的会话继续返回 True）
    ov, restore = _setup([("session", _MAIN, "小分队", True), ("gone", _MAIN)])
    try:
        main.drain()
        assert main.state["wins"] == {} and main.state["chat"] == ""
    finally:
        restore()

    # 关掉的正好是正在倒计时的那个会话 → 倒计时必须收掉，不能到点往一个没了的窗口按发送键
    ov, restore = _setup([("session", _POP_B, "1群", False)])
    try:
        main.drain()
        main.state["auto_title"] = "1群"
        ov.pending = True
        main.q = FakeQ([("gone", _POP_B)])
        main.drain()
        assert ("cancel_auto", "那个窗口已经关掉了，已取消这次自动发送。") in ov.calls, ov.calls
        assert main.state["auto_title"] == ""
    finally:
        restore()
    print("窗口消失只清它自己 ok（别的会话不动 / 主窗口清了游标 / 倒计时被收掉）")


# ── 4. 输入框状态按会话 ────────────────────────────────────────────────────────

def check_input_is_per_session():
    """输入框是「按窗口」的东西，所以必须按会话记。而且**别的会话里打字不许取消这条倒计时**。"""
    ov, restore = _setup([
        ("session", _POP_A, "烧烤群", False),
        ("session", _POP_B, "1群", False),
        ("input", "1群", True),
        ("input", "烧烤群", True),
    ])
    try:
        main.state["auto_title"] = "烧烤群"
        ov.pending = True
        main.drain()
        assert main.state["input_has"] == {"1群": True, "烧烤群": True}, main.state["input_has"]
        # 1群 先报的 True，那会儿 auto_title 还是空 → 不该取消；
        # 烧烤群 报 True 时才该取消（它才是正在倒计时的那个）
        cancels = [c for c in ov.calls if c[0] == "cancel_auto"]
        assert len(cancels) == 1, f"只该取消正在倒计时的那个：{cancels}"
    finally:
        restore()
    print("输入框状态按会话 ok（各记各的 / 别的会话打字不取消这条倒计时）")


# ── 5. rerun 从「一个槽」改成「按会话排队」 ────────────────────────────────────

def check_rerun_queue():
    """分析期间来了两个会话的新消息：两个都要跑，一个都不能丢。

    老实现只有一个槽（`state["rerun"] = …`），第二个会把第一个顶掉——三个群同时说话时
    会有两个群永远等不到回复。这是「三个会话都自动回」最直接的一个坑。
    """
    ov, restore = _setup([
        ("session", _POP_A, "烧烤群", False),
        ("session", _POP_B, "1群", False),
        ("lines", "烧烤群", [("her", "阿杰", "烧烤走起", None)], _AREA),
        ("lines", "1群", [("her", "KK", "在吗", None)], _AREA),
    ])
    try:
        # 让第一轮看起来正在跑：start_analyze 会写 _started，所以先手动置忙
        main.state["busy"] = True
        main.drain()
        assert set(main.state["rerun"]) == {"烧烤群", "1群"}, \
            f"两个会话都该排上队，实际 {set(main.state['rerun'])}"

        # 结果回来了 → 该跑排着的那条，而不是把这个结果贴上去
        main.state["busy"] = False
        main.results.put(("ok", {"candidates": ["甲"]}, "烧烤群", main.chat_of("烧烤群")["rev"], None))
        main.tick()
        assert len(_started("烧烤群")) == 1, _started("烧烤群")
        assert set(main.state["rerun"]) == {"1群"}, "排过的那条要出队，没排过的要留着"

        # 再回一个结果 → 1群 那条也该跑上
        main.state["busy"] = False
        main.results.put(("ok", {"candidates": ["乙"]}, "烧烤群", main.chat_of("烧烤群")["rev"], None))
        main.tick()
        assert len(_started("1群")) == 1, f"1群 那条被顶掉了：{_started('1群')}"
        assert main.state["rerun"] == {}
    finally:
        restore()

    # 同一个会话连着来三条：只跑最后一次（前面的上下文已经被最后那条包进去了）
    ov, restore = _setup([("session", _POP_A, "烧烤群", False)])
    try:
        main.drain()
        main.state["busy"] = True
        for text in ("一", "二", "三"):
            main.q = FakeQ([("lines", "烧烤群", [("her", "阿杰", text, None)], _AREA)])
            main.drain()
        assert len(main.state["rerun"]) == 1, "同一个会话只该排一条"
        msgs, _auto = main.state["rerun"]["烧烤群"][1], main.state["rerun"]["烧烤群"][2]
        assert [m[1] for m in msgs] == ["一", "二", "三"], f"排着的必须是最后一次的完整上下文：{msgs}"
    finally:
        restore()
    print("rerun 按会话排队 ok（两个会话都不丢 / 同会话只留最新 / 排过就出队）")


# ── 6. 回退路径逐字节不变 ──────────────────────────────────────────────────────

def check_fallback_is_unchanged():
    """**没有独立窗口时，新代码必须跟老代码完全等价。**

    老代码里「这个会话还显示着吗」= `state["chat"] == title`。新的 `showing()` 在只有
    主窗口时必须给出同一个答案——这是「不开独立窗口的老用户行为逐字节不变」的落点。
    """
    ov, restore = _setup([("session", _MAIN, "小分队", True)])
    try:
        main.drain()
        # 主窗口显示着小分队：老的 state["chat"] == "小分队" → True
        assert main.state["chat"] == "小分队"
        assert main.showing("小分队") is True
        assert main.showing("老婆") is False, "主窗口显示的是小分队，老婆不该算「显示着」"

        # 换会话之后：老代码 state["chat"] == "老婆" → 小分队 变 False
        main.q = FakeQ([("session", _MAIN, "老婆", True)])
        main.drain()
        assert main.showing("老婆") is True and main.showing("小分队") is False, \
            "主窗口换了会话之后，老会话必须不再算「显示着」（老代码就是这个行为）"

        # 独立窗口是另一套：钉住了就一直显示着，跟 state["chat"] 无关
        main.q = FakeQ([("session", _POP_B, "1群", False)])
        main.drain()
        assert main.showing("1群") is True
        main.state["chat"] = "别的会话"
        assert main.showing("1群") is True, "独立窗口钉住的会话不该被主窗口的游标影响"
        assert main.showing("老婆") is False, "主窗口已经不在显示老婆了"

        # 没有窗口的会话一律 False（老代码里 state["chat"] 对不上就是 False）
        assert main.showing("没见过的会话") is False
    finally:
        restore()

    # `_win_area` 只在同一个 hwnd 上继承——不同窗口之间绝不能互相借坐标
    ov, restore = _setup([
        ("session", _MAIN, "小分队", True),
        ("area", "小分队", (1, 2, 3, 4)),
        ("session", _POP_B, "1群", False),
    ])
    try:
        main.drain()
        assert main.state["wins"]["1群"]["area"] is None, \
            "另一个窗口的坐标被借过来了——那会把回复打进错的窗口"
    finally:
        restore()
    print("回退路径逐字节不变 ok（showing 等价于老 state[\"chat\"] / 坐标不跨窗口借）")


# ── 7. fill_reply 按会话查窗口 ────────────────────────────────────────────────

def check_fill_targets_the_right_window():
    """填入必须落到「界面上正看着的那个会话」自己的窗口上。"""
    ov, restore = _setup([
        ("session", _MAIN, "小分队", True),
        ("session", _POP_B, "1群", False),
        ("area", "1群", (10, 20, 30, 40)),
    ])
    try:
        main.drain()
        real_fill = main.fill
        real_target = main.settings.reply_target
        sent = []
        main.fill = lambda hwnd, area, text: sent.append((hwnd, area, text))
        main.settings.reply_target = lambda: False
        try:
            ov.shown = "1群"
            main.fill_reply("候选甲")
            assert sent == [(_POP_B, (10, 20, 30, 40), "候选甲")], sent

            # 界面上正看着一个没有窗口的会话 → 报错，绝不猜一个窗口填进去
            ov.shown = "没有窗口的会话"
            try:
                main.fill_reply("候选乙")
            except RuntimeError as e:
                assert "未找到微信窗口" in str(e), str(e)
            else:
                raise AssertionError("没有窗口的会话居然填成功了——这就是发错人的入口")
            assert len(sent) == 1, "报错那条不许真填进去"
        finally:
            main.fill = real_fill
            main.settings.reply_target = real_target
    finally:
        restore()
    print("fill_reply 按会话查窗口 ok（填自己的窗口 / 没窗口就报错不猜）")


def _send_probe(title, is_main):
    """把 main 摆成「这条回复已经可以发了」，返回 (sent 列表, 还原函数)。

    独立窗口要额外桩三件东西：句柄有效 / 窗口标题还是那个会话 / 用户走开了。
    这三条就是 S5 新加的三道闸——主窗口那条路**刻意不走它们**。
    """
    ov, restore = _setup([("session", _POP_B if not is_main else _MAIN, title, is_main)])
    main.drain()
    main.state["wins"][title]["area"] = _AREA
    main.state["input_has"][title] = False
    main.state["auto_title"] = title
    if is_main:
        main.state["chat"] = title

    sent = []
    real = (main.send_text, main.W.alive, main.W.title_of, main.focus.user_is_away)
    main.send_text = lambda hwnd, area, text, key: (sent.append(text), "Enter")[1]
    main.W.alive = lambda hwnd: True
    main.W.title_of = lambda hwnd: title
    main.focus.user_is_away = lambda: True

    def undo():
        (main.send_text, main.W.alive, main.W.title_of, main.focus.user_is_away) = real
        restore()
    return sent, undo


def _statuses(ov):
    return [c[1] for c in ov.calls if c[0] == "status"]


def check_send_guards():
    """发送前的三道新闸（设计文档 §7 的 1/2/5 条）。**只对独立窗口生效。**

    为什么必须有第 2 条：句柄是子进程 ~1 秒前报上来的，这中间用户完全可能把那个独立窗口
    关掉、微信又把同一个 hwnd 复用给了**别的**会话。光看「句柄还有效」会照样发出去——
    那就把回复打进了另一个人的聊天里。把窗口标题读回来对一遍是唯一的防线。
    """
    # ① 正常：该发就发
    sent, undo = _send_probe("1群", is_main=False)
    try:
        main.auto_send_reply("甲")
        assert sent == ["甲"], sent

        # ② 句柄失效（窗口关了）
        main.W.alive = lambda hwnd: False
        sent.clear()
        main.auto_send_reply("甲")
        assert sent == [], sent
        assert any("窗口已经关掉了" in s for s in _statuses(main.ov)), _statuses(main.ov)

        # ③ 句柄还在、但标题已经不是那个会话了（hwnd 被复用给了别的会话）→ 绝不能发
        main.W.alive = lambda hwnd: True
        main.W.title_of = lambda hwnd: "别的群"
        sent.clear()
        main.auto_send_reply("甲")
        assert sent == [], "标题对不上还发出去了——这就是往错群发消息"
        assert any("换成别的会话" in s for s in _statuses(main.ov)), _statuses(main.ov)

        # ④ 用户正坐在电脑前 → 不抢他的焦点
        main.W.title_of = lambda hwnd: "1群"
        main.focus.user_is_away = lambda: False
        sent.clear()
        main.auto_send_reply("甲")
        assert sent == [], sent
        assert any("不抢你的焦点" in s for s in _statuses(main.ov)), _statuses(main.ov)

        # ⑤ 标题比对必须走同一套归一化：窗口标题带零宽字符 / 只差大小写不算「换了会话」
        #    （不然用户改个大小写就会莫名其妙发不出去）
        main.focus.user_is_away = lambda: True
        main.W.title_of = lambda hwnd: "1群\u200b"
        sent.clear()
        main.auto_send_reply("甲")
        assert sent == ["甲"], "只差零宽字符就被当成「换了会话」，太脆了"
    finally:
        undo()

    # ⑥ **回退路径不走这三道闸**：没有独立窗口的老用户行为必须逐字节不变。
    #    把三道闸全设成「最该拦」的状态，主窗口那条路照样要发出去。
    sent, undo = _send_probe("小分队", is_main=True)
    try:
        main.W.alive = lambda hwnd: False
        main.W.title_of = lambda hwnd: "完全不是这个会话"
        main.focus.user_is_away = lambda: False
        main.auto_send_reply("甲")
        assert sent == ["甲"], f"回退路径被新闸拦住了——老用户行为变了：{sent}"
    finally:
        undo()
    print("发送前三道新闸 ok（窗口没了 / 换了会话 / 你在电脑前都不发；回退路径不受影响）")


def check_auto_queue():
    """一次只发一个（D9）+ 同会话只留最新一条（D10）。

    为什么必须排队：`fill()` 要抢前台、点输入框、按发送键。两条倒计时同时到点必然打架，
    打输的那条可能把字打进赢的那个窗口里——那是往错的会话发消息。
    """
    ov, restore = _setup([("session", _POP_A, "烧烤群", False),
                          ("session", _POP_B, "1群", False)])
    try:
        main.drain()
        for t in ("烧烤群", "1群"):
            main.state["wins"][t]["area"] = _AREA
            main.state["input_has"][t] = False
        real = (main.settings.auto_send_on, main.settings.auto_send_dm,
                main.settings.auto_send_group, main.settings.auto_send_group_any,
                main.settings.my_name, main.settings.auto_send_delay,
                main.settings.auto_chat, main.ov.can_auto_send)
        main.settings.auto_send_on = lambda: True
        main.settings.auto_send_dm = lambda: True
        main.settings.auto_send_group = lambda: False
        main.settings.auto_send_group_any = lambda: False
        main.settings.my_name = lambda: ""
        main.settings.auto_send_delay = lambda: 5
        main.settings.auto_chat = lambda t: True
        main.ov.can_auto_send = lambda: True
        try:
            def arm(title, text):
                chat = main.chat_of(title)
                chat["auto"] = True
                chat["history"].append(("her", "在吗", None))
                main.state["batches"][title] = 5
                main.start_auto(title, {"candidates": [text], "best_index": 0, "judged": True})

            # 第一条：直接摆出来
            arm("烧烤群", "甲")
            assert [c for c in ov.calls if c[0] == "begin"] == [("begin", "甲", 5, "")], ov.calls
            assert main.state["auto_title"] == "烧烤群"

            # 第二条是**别的**会话 → 排队，不许顶掉正在倒计时的那条
            ov.calls.clear()
            arm("1群", "乙")
            assert not [c for c in ov.calls if c[0] == "begin"], "两个会话同时抢前台了"
            assert main.state["auto_queue"] == {"1群": ("乙", "")}, main.state["auto_queue"]
            assert main.state["auto_title"] == "烧烤群", "正在倒计时的会话不该被换掉"

            # 同一个会话再来一条 → **顶掉**（不是排队）：这是新结果，排上去会先发一条过期的
            ov.calls.clear()
            arm("烧烤群", "甲二")
            assert [c for c in ov.calls if c[0] == "begin"] == [("begin", "甲二", 5, "")], ov.calls
            assert main.state["auto_queue"] == {"1群": ("乙", "")}, "同会话不该进队列"

            # 倒计时空下来了 → 轮到排着队的那条
            ov.pending = False
            main.pump_auto_queue()
            assert [c for c in ov.calls if c[0] == "begin"][-1] == ("begin", "乙", 5, ""), ov.calls
            assert main.state["auto_title"] == "1群"
            assert main.state["auto_queue"] == {}

            # 排队期间那个窗口关了 → 直接丢掉，别到点往一个没了的窗口发
            ov.pending = True
            main.state["auto_title"] = "烧烤群"
            arm("1群", "丙")
            assert main.state["auto_queue"] == {"1群": ("丙", "")}
            main.q = FakeQ([("gone", _POP_B)])
            main.drain()
            assert main.state["auto_queue"] == {}, "窗口关掉了，排着的队该一起丢掉"
            ov.pending = False
            ov.calls.clear()
            main.pump_auto_queue()
            assert not [c for c in ov.calls if c[0] == "begin"], "窗口都没了还发"
        finally:
            (main.settings.auto_send_on, main.settings.auto_send_dm,
             main.settings.auto_send_group, main.settings.auto_send_group_any,
             main.settings.my_name, main.settings.auto_send_delay,
             main.settings.auto_chat, main.ov.can_auto_send) = real
    finally:
        restore()
    print("一次只发一个 ok（不同会话排队 / 同会话顶掉 / 空下来接着发 / 窗口没了就丢）")


def check_focus_guard():
    """焦点护栏（app/focus.py）：tick 回绕、以及「拿不准就往不发倒」。

    回绕那条最容易写错：`GetTickCount` 49.7 天回绕一次，直接相减会在回绕那一刻得到
    -49.7 天——被算成「用户走了 49 天」，于是**放行**。而这条闸存在的唯一理由就是
    「别在人在的时候抢前台」，往放行方向错正好错在刀刃上。
    """
    from app import focus

    assert focus.elapsed(10_000, 2_000) == 8.0
    # 回绕那一刻：now 比 last 小很多。带掩码之后差值仍然是对的
    assert abs(focus.elapsed(1_000, 0xFFFFFF00) - 1.256) < 1e-9, \
        focus.elapsed(1_000, 0xFFFFFF00)
    assert abs(focus.elapsed(0xFFFFFFF0, 0xFFFFFF00) - 0.24) < 1e-9, \
        focus.elapsed(0xFFFFFFF0, 0xFFFFFF00)
    # 同一个 tick = 0 秒
    assert focus.elapsed(12345, 12345) == 0.0

    real = focus.idle_seconds
    try:
        focus.idle_seconds = lambda: 0.0
        assert focus.user_is_away() is False, "刚动过（0 秒）必须算「没走开」"
        focus.idle_seconds = lambda: focus.USER_IDLE_SECONDS - 0.001
        assert focus.user_is_away() is False, "差一点点到阈值不该算走开"
        focus.idle_seconds = lambda: focus.USER_IDLE_SECONDS
        assert focus.user_is_away() is True, "正好到阈值就该算走开"
    finally:
        focus.idle_seconds = real

    # 真调一次系统 API：不该抛，而且必须是个 >= 0 的数（读不到时返回 0.0 = 「正在操作」，
    # 方向永远是「别抢前台」）
    got = focus.idle_seconds()
    assert isinstance(got, float) and got >= 0.0, got

    # 窗口句柄的辅助函数对脏值不能炸，而且方向也是「当它没了」
    assert main.W.alive(0) is False and main.W.alive(None) is False
    assert main.W.title_of(0) == "" and main.W.title_of(None) == ""
    print("焦点护栏 ok（tick 回绕算得对 / 阈值边界 / 拿不准就当「正在操作」）")


def main_check() -> int:
    check_session_routing()
    check_unpaired_warning()
    check_area_is_per_session()
    check_gone_is_scoped()
    check_input_is_per_session()
    check_rerun_queue()
    check_fallback_is_unchanged()
    check_fill_targets_the_right_window()
    check_send_guards()
    check_auto_queue()
    check_focus_guard()
    print("多窗口父进程侧检查全部通过"
          "（session 分路 / 未配对提示 / 坐标按会话 / gone 只清自己 / 输入框按会话 / "
          "rerun 排队 / 回退不变 / 填入按会话 / 发前三道闸 / 排队 / 焦点护栏）")
    return 0


if __name__ == "__main__":
    sys.exit(main_check())
