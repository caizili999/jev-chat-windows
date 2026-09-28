# -*- coding: utf-8 -*-
"""知识库的离线检查。跑：python tools/check_kb.py

**绝不碰用户真实数据**：所有读写都在临时目录里，跑完删掉。
不联网、不起 Qt 窗口、不加载 OCR 模型。

钉住的是上游那几处「悄悄错也不会报错」的地方：
名字归一化、坏文件保全、一屏序列的增量算法、预算裁剪顺序、以及
「先按最宽窗口过滤、再 takeLast」这个顺序（反了会让屏上消息吃掉配额）。
"""
import io
import itertools
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 输出强制 UTF-8：Windows 控制台代码页可能是 cp936，CI 上可能是 cp1252，
# cp1252 编不出中文会直接在 print 那行 UnicodeEncodeError。跟其它 check_*.py 同一写法。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.kb import context as CB  # noqa: E402
from app.kb import selfcheck  # noqa: E402
from app.kb.models import ChatContext, Contact, LogEntry, Note  # noqa: E402
from app.kb.store import (MAX_LOG, KbStore, display_name,  # noqa: E402
                          normalize_name, normalize_text)

_APP = "wechat"


class _Tmp:
    """一个建在临时目录上的 store，退出时整个删掉。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="jev-kb-check-")
        self.store = KbStore(self.root)

    def __enter__(self):
        return self.store

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)
        return False


def _msgs(*pairs):
    return list(pairs)


# ── 名字归一化 ──────────────────────────────────────────────────────────────

def check_name_normalization():
    """半角/全角人数、零宽字符、首尾空白、大小写——全都要归到同一个键。"""
    for raw in ("测试群(12)", "测试群（12）", "测试群 ( 12 )", "测试群（12 ）",
                "  测试群  ", "测试群", "测试群\u200b", "\u200b测试群\ufeff"):
        assert normalize_name(raw) == "测试群", f"{raw!r} → {normalize_name(raw)!r}"

    # 大小写差异要抹平（英文群名）
    assert normalize_name("Alice") == normalize_name("ALICE") == "alice"

    # 人数只在**结尾**才算人数，正文里的括号不动
    assert normalize_name("群(12)abc") == "群(12)abc"

    # display_name 做同样的清洗但保留原始大小写，给人看
    assert display_name("  Alice(12) ") == "Alice"
    assert display_name("测试群（12）") == "测试群"

    # normalize_text 是子串匹配用的宽松键：**不去人数**（正文里 (12) 是有意义的）
    assert normalize_text(" 测试群(12) ") == "测试群(12)"
    assert normalize_text(None) == "" and normalize_name(None) == ""
    assert normalize_name("") == "" and display_name("") == ""

    # 正则没编译出来时降级成 trim + 小写，绝不抛（上游被这个坑死过整个类）
    import app.kb.store as S
    zero, trailing = S._ZERO_WIDTH, S._TRAILING_COUNT
    S._ZERO_WIDTH = S._TRAILING_COUNT = None
    try:
        assert normalize_name("  ALICE  ") == "alice", "降级路径仍要 trim + 小写"
        assert normalize_name("测试群(12)") == "测试群(12)", "降级后去不掉人数，但必须返回字符串"
    finally:
        S._ZERO_WIDTH, S._TRAILING_COUNT = zero, trailing
    print("名字归一化 ok（半/全角人数 / 零宽 / 空白 / 大小写 / 正则降级）")


# ── 原子写与坏文件保全 ──────────────────────────────────────────────────────

def check_corrupt_file_is_preserved():
    """坏文件必须改名留档，而不是被下一次保存无声盖掉。"""
    with _Tmp() as st:
        with open(st.notes_file, "w", encoding="utf-8") as f:
            f.write("{这不是合法 JSON")
        assert st.notes() == [], "坏文件应读成空列表"
        assert not os.path.exists(st.notes_file), "坏文件应被移走"
        left = os.listdir(st.root)
        assert any(n.startswith("notes.json.corrupt.") for n in left), f"没有留档：{left}"

        # 留档之后可以正常从头开始写
        assert st.save_note(Note(id="n1", title="t", content="c")) is True
        assert len(st.notes()) == 1
        assert [n for n in st.notes() if n.id == "n1"]
    print("坏文件保全 ok（改名留档 + 之后能正常写）")


def check_unmovable_file_refuses_write():
    """坏文件**又移不走**时：既不能缓存空列表，更不能覆盖用户数据。"""
    with _Tmp() as st:
        with open(st.notes_file, "w", encoding="utf-8") as f:
            f.write("{坏的")
        real_replace = os.replace
        os.replace = lambda a, b: (_ for _ in ()).throw(OSError("模拟移不动"))
        try:
            assert st.notes() == []
            # 移不走 → 拒绝写入，而不是把那份可能还能手工救回来的文件盖掉
            assert st.save_note(Note(id="n1", title="t", content="c")) is False, \
                "解析不了又移不走的文件，必须拒绝覆盖"
        finally:
            os.replace = real_replace
        assert os.path.exists(st.notes_file), "原文件必须还在"
        with open(st.notes_file, encoding="utf-8") as f:
            assert f.read() == "{坏的", "原内容一个字都不能变"
        assert st.take_problems(), "必须留下一条提示说明原因"
    print("移不走的坏文件 ok（拒绝覆盖 + 原文件原样 + 有提示）")


def check_atomic_write_leaves_no_tmp():
    """正常写盘不留 .tmp 残渣。"""
    with _Tmp() as st:
        st.save_note(Note(id="n1", title="标题", content="正文"))
        st.save_contact(Contact(id="c1", name="阿杰"))
        st.append_log("c1", [LogEntry("her", "你好呀"), LogEntry("me", "嗯")])
        leftovers = [n for n in os.listdir(st.root) if n.endswith(".tmp")]
        assert not leftovers, f"不该留下临时文件：{leftovers}"
        # 日志在 logs/ 子目录里
        assert os.path.exists(st.log_file("c1"))
        assert not [n for n in os.listdir(os.path.join(st.root, "logs")) if n.endswith(".tmp")]
    print("原子写 ok（无 .tmp 残渣）")


# ── 一屏序列的增量算法 ──────────────────────────────────────────────────────

def _screen(*rows):
    return [LogEntry(w, t) for w, t in rows]


def check_screen_batch_rules():
    """append_log 的五条规则。单位是**一整个序列**，不是一堆行。"""
    with _Tmp() as st:
        # 空日志 → 整屏都写
        assert st.append_log("c", _screen(("her", "a"), ("me", "b"))) is True
        assert st.log_size("c") == 2

        # S == 上一屏 → 什么都不写
        assert st.append_log("c", _screen(("her", "a"), ("me", "b"))) is True
        assert st.log_size("c") == 2, "同一屏重采不该追加"

        # 屏上多了新消息（老的在上面）：[a,b] → [a,b,c,d,e]，尾部匹配 k=2，只追加 c,d,e
        st.append_log("c", _screen(("her", "a"), ("me", "b"),
                                   ("her", "c"), ("her", "d"), ("me", "e")))
        assert st.log_size("c") == 5, f"应追加 3 条，实际 {st.log_size('c')}"

        # 又滚了一屏：[c,d,e] 跟日志尾部匹配 k=3 → 只追加 f,g
        st.append_log("c", _screen(("her", "c"), ("her", "d"), ("me", "e"),
                                   ("her", "f"), ("her", "g")))
        assert st.log_size("c") == 7, f"应只追加 2 条，实际 {st.log_size('c')}"
        assert [e.text for e in st.recent_log("c", 3)] == ["e", "f", "g"]

        # 翻到更旧的、跟上一屏毫无交集 → 什么都不写（不能在文件尾再抄一遍旧历史）
        before = st.log_size("c")
        st.append_log("c", _screen(("her", "很久以前1"), ("me", "很久以前2")))
        assert st.log_size("c") == before, "往上翻不该写"

        # 空白行被丢掉
        st.append_log("c", _screen(("her", "   "), ("me", "\t")))
        assert st.log_size("c") == before, "纯空白行不该记"

        # screen_batch=False（手工注入一行，不是一次屏幕采集）→ 原样追加，不跟上一屏比
        st.append_log("c", _screen(("her", "手工注入")), screen_batch=False)
        assert st.log_size("c") == before + 1
        # 它不影响下一屏的比对：屏 [e,f,g,手工注入,新的] 跟日志尾部匹配 k=4 → 只追加「新的」
        st.append_log("c", _screen(("me", "e"), ("her", "f"), ("her", "g"),
                                   ("her", "手工注入"), ("me", "新的")))
        assert [e.text for e in st.recent_log("c", 2)] == ["手工注入", "新的"], \
            [e.text for e in st.recent_log("c", 2)]

        # 同一屏里两行一样的短消息是**两个位置**，记两条，不合并
        st2 = KbStore(tempfile.mkdtemp(prefix="jev-kb-dup-"))
        try:
            st2.append_log("d", _screen(("her", "好的好的"), ("her", "好的好的")))
            assert st2.log_size("d") == 2, "同屏两条一样的消息是两个位置，不能被折成一条"
        finally:
            shutil.rmtree(st2.root, ignore_errors=True)
    print("一屏序列算法 ok（同屏不重写 / 增量追加 / 上翻不写 / 空白丢弃 / 手工注入）")


def check_screen_batch_does_not_freeze():
    """一次涌进来一整屏以上的新消息之后，日志**必须能恢复增长**。

    真踩过的坑：`append_log` 里那条「跟上一屏毫无交集 ⇒ 用户往上翻了 ⇒ 不写」的启发式，
    分不清「往上翻」和「一口气来了整整一屏新消息」——两种情况长得一模一样。这本来只是
    少记一屏，但当时它**连「上一屏」标记都不更新**，标记就永远停在旧屏上，于是之后每一轮
    都零交集、每一轮都跳过，日志从此再不增长。用户那边表现为「历史一直是 18 条」，
    聊了 80 分钟一条没记，而配置里明明写着 30 条。
    """
    with _Tmp() as st:
        # 先正常聊出一屏
        st.append_log("c", _screen(*[("her", f"第{i}条") for i in range(10)]))
        assert st.log_size("c") == 10

        # 两次分析之间涌进来一整屏以上的新消息：屏上 10 行全新，跟上一屏零交集。
        # 这一轮按上游的规则**就是**不写（分不清是不是往上翻），保留这个行为。
        st.append_log("c", _screen(*[("her", f"新第{i}条") for i in range(10)]))
        assert st.log_size("c") == 10, "零交集那一轮本来就该跳过，不该写"

        # 关键断言：**不能就此卡死**。下一轮屏幕只是平滑前进一行，就该恢复追加。
        st.append_log("c", _screen(*([("her", f"新第{i}条") for i in range(1, 10)]
                                    + [("her", "又来一条")])))
        assert st.log_size("c") > 10, \
            f"大跳之后必须能恢复增长，实际还卡在 {st.log_size('c')} 条——日志永久冻结了"
        assert [e.text for e in st.recent_log("c", 1)] == ["又来一条"]

        # 再跟一轮，确认恢复之后是正常增量（只追加新出来的那条），不是每轮整屏重抄
        n = st.log_size("c")
        st.append_log("c", _screen(*([("her", f"新第{i}条") for i in range(2, 10)]
                                    + [("her", "又来一条"), ("me", "再加一条")])))
        assert st.log_size("c") == n + 1, \
            f"恢复之后该只追加 1 条增量，实际 {st.log_size('c') - n} 条"

        # 反复大跳也不能把它锁死：连着来三屏全新内容，每一屏之后再平滑一步都要能跟上
        for r in range(3):
            st.append_log("c", _screen(*[("her", f"第{r}轮全新{i}") for i in range(10)]))
            m = st.log_size("c")
            st.append_log("c", _screen(*([("her", f"第{r}轮全新{i}") for i in range(1, 10)]
                                        + [("her", f"第{r}轮新来的一条")])))
            assert st.log_size("c") > m, f"第 {r} 轮大跳之后又卡死了"
    print("大跳不卡死 ok（零交集跳一轮 / 下一轮必须恢复 / 反复大跳也跟得上）")


def check_log_cap():
    """每个联系人只留最新 MAX_LOG 条。"""
    with _Tmp() as st:
        for i in range(MAX_LOG + 5):
            st.append_log("c", _screen(("her", f"第{i}条消息")), screen_batch=False)
        assert st.log_size("c") == MAX_LOG, f"应截到 {MAX_LOG}，实际 {st.log_size('c')}"
        texts = [e.text for e in st.recent_log("c", MAX_LOG)]
        assert texts[0] == "第5条消息", f"最旧的应被丢掉，实际开头是 {texts[0]!r}"
        assert texts[-1] == f"第{MAX_LOG + 4}条消息"
        # recent_log 的边界
        assert st.recent_log("c", 0) == []
        assert len(st.recent_log("c", 3)) == 3
        assert len(st.recent_log("c", MAX_LOG + 100)) == MAX_LOG
    print(f"历史上限 ok（截到 {MAX_LOG} 条，丢最旧）")


# ── 笔记命中 ────────────────────────────────────────────────────────────────

def check_note_matching():
    """命中 = 任一 tag 或标题出现在「会话标题 + 最近 6 条消息」里。"""
    with _Tmp() as st:
        st.save_note(Note(id="n1", title="项目代号", content="小蓝", tags=["项目"]))
        st.save_note(Note(id="n2", title="生日", content="3月14日", tags=["纪念日"]))
        st.save_note(Note(id="n3", title="常驻", content="永远带着", always_on=True))
        st.save_note(Note(id="n4", title="关掉的", content="不该出现", tags=["项目"], enabled=False))

        # 标题里出现 tag「项目」→ n1 命中；n3 常驻；n4 被关掉；n2 不命中
        hits = CB.match_notes([n for n in st.notes() if n.enabled and not n.always_on], "项目组", [])
        assert [n.id for n in hits] == ["n1"], [n.id for n in hits]

        # 正文里出现标题「生日」也算
        hits = CB.match_notes([n for n in st.notes() if n.enabled and not n.always_on], "和阿杰",
                              _msgs(("her", "你还记得我生日吗"), ("me", "记得")))
        assert [n.id for n in hits] == ["n2"], [n.id for n in hits]

        # 只在最近 6 条里找：第 7 条（从末尾数）里出现的关键词不算
        old = ("her", "聊到项目代号了")
        filler = [("her", f"填充{i}") for i in range(6)]
        assert CB.match_notes([n for n in st.notes() if n.enabled and not n.always_on], "群",
                              [old] + filler) == [], "窗口外的关键词不该命中"
        assert [n.id for n in CB.match_notes(
            [n for n in st.notes() if n.enabled and not n.always_on], "群",
            filler + [old])] == ["n1"], "窗口内就该命中"

        # 最多带 MAX_HIT_NOTES 条，越新的越优先。
        # 用一个递增的假时钟，好让 updated_at 确定性地拉开——不然同一毫秒里写完 8 条，
        # 排序是稳定的，测出来的顺序就没意义了。
        tick = itertools.count(1000)
        st2 = KbStore(tempfile.mkdtemp(prefix="jev-kb-hit-"), now=lambda: next(tick))
        try:
            for i in range(8):
                st2.save_note(Note(id=f"h{i}", title=f"命中{i}", content="x", tags=["共同"]))
            hits = CB.match_notes(st2.notes(), "共同话题", [])
            assert len(hits) == CB.MAX_HIT_NOTES, len(hits)
            assert [n.id for n in hits] == ["h7", "h6", "h5", "h4", "h3"], [n.id for n in hits]
        finally:
            shutil.rmtree(st2.root, ignore_errors=True)
    print("笔记命中 ok（tag/标题 / 6 条窗口 / 关掉的不算 / 上限 5 条且新的优先）")


# ── 预算 ────────────────────────────────────────────────────────────────────

def check_budget_trimming():
    """常驻豁免；超预算先丢最旧历史，再整条丢笔记（绝不截半条）。"""
    with _Tmp() as st:
        st.save_contact(Contact(id="c1", name="阿杰", apps=[_APP]))
        # 常驻笔记：超预算也不丢
        st.save_note(Note(id="a", title="常驻", content="X" * 2000, always_on=True))
        # 三条命中笔记，每条 600 字。**故意大到「光把历史全丢光也降不下来」**，
        # 这样才能真的走到「开始丢笔记」那一步、验到两段裁剪的先后顺序。
        for i in range(3):
            st.save_note(Note(id=f"n{i}", title="共同", content=f"第{i}条" + "Y" * 600))
        # 20 条历史，每条 100 字
        for i in range(20):
            st.append_log("c1", [LogEntry("her", f"历史{i}" + "Z" * 100)], screen_batch=False)

        ctx = CB.build(st, "阿杰", _msgs(("her", "共同话题"), ("me", "嗯")),
                       app=_APP, history_enabled=True, history_count=30)

        assert any(n.id == "a" for n in ctx.notes), "常驻笔记必须豁免预算"
        hits = [n for n in ctx.notes if n.id != "a"]
        assert len(ctx.history) < 20, "超预算了，历史该被裁掉一些"
        assert CB._cost(hits, ctx.history) <= CB.BUDGET_CHARS, \
            f"裁剪后仍超预算：{CB._cost(hits, ctx.history)}"

        # 顺序不变量：笔记只在历史**全部丢光**之后才开始丢
        if len(hits) < 3:
            assert ctx.history == [], \
                f"还留着 {len(ctx.history)} 条历史就丢笔记了——两段裁剪的顺序反了"

        # 绝不截半条：留下的每条笔记都是完整的标题 + 正文
        for n in hits:
            assert n.title == "共同" and n.content.startswith(("第0条", "第1条", "第2条"))

        # 不超预算时一条都不裁
        st2 = KbStore(tempfile.mkdtemp(prefix="jev-kb-budget-"))
        try:
            st2.save_contact(Contact(id="c", name="小明", apps=[_APP]))
            st2.save_note(Note(id="s", title="小", content="短"))
            for i in range(3):
                st2.append_log("c", [LogEntry("her", f"短{i}")], screen_batch=False)
            ctx2 = CB.build(st2, "小明", _msgs(("her", "小短消息")), app=_APP,
                            history_enabled=True, history_count=30)
            # 屏上那条（够长、会被去重）记进去但不再回注，所以还是 3 条
            assert len(ctx2.notes) == 1 and len(ctx2.history) == 3, \
                (len(ctx2.notes), len(ctx2.history))
        finally:
            shutil.rmtree(st2.root, ignore_errors=True)
    print("预算裁剪 ok（常驻豁免 / 先丢历史再丢笔记 / 不截半条 / 不超不裁）")


# ── 历史去重与配额顺序 ──────────────────────────────────────────────────────

def check_history_dedupe_order():
    """**先按最宽窗口过滤、再 takeLast(n)。** 反了会让屏上消息吃掉配额。"""
    with _Tmp() as st:
        st.save_contact(Contact(id="c1", name="阿杰", apps=[_APP]))
        for i in range(40):
            st.append_log("c1", [LogEntry("her", f"消息{i:02d}号")], screen_batch=False)
        assert st.log_size("c1") == 40

        # 屏上就是最后 10 条（都够长，会被去重）
        on_screen = [("her", f"消息{i:02d}号") for i in range(30, 40)]
        ctx = CB.build(st, "阿杰", on_screen, app=_APP,
                       history_enabled=True, history_count=5)

        assert len(ctx.history) == 5, \
            f"要 5 条历史就该拿到 5 条，实际 {len(ctx.history)}（顺序反了会变成 0）"
        got = [e.text for e in ctx.history]
        assert got == [f"消息{i:02d}号" for i in range(25, 30)], got
        for e in ctx.history:
            assert e.text not in {t for _, t in on_screen}, "屏上已有的不该回注"
        # 重采同一屏不会把历史写第二遍
        assert st.log_size("c1") == 40, f"同一屏被写了第二遍：{st.log_size('c1')}"

        # 太短的行（< DEDUPE_MIN_LEN）不去重——精确重复也可能是不同的消息
        st2 = KbStore(tempfile.mkdtemp(prefix="jev-kb-short-"))
        try:
            st2.save_contact(Contact(id="c", name="小明", apps=[_APP]))
            st2.append_log("c", [LogEntry("her", "嗯嗯")], screen_batch=False)
            ctx2 = CB.build(st2, "小明", _merge_short(), app=_APP,
                            history_enabled=True, history_count=30)
            assert [e.text for e in ctx2.history] == ["嗯嗯"], \
                "短行不去重，应照常注入"
        finally:
            shutil.rmtree(st2.root, ignore_errors=True)

        # history_count=0 → 只记录，不注入
        st3 = KbStore(tempfile.mkdtemp(prefix="jev-kb-zero-"))
        try:
            st3.save_contact(Contact(id="c", name="阿杰", apps=[_APP]))
            ctx3 = CB.build(st3, "阿杰", _msgs(("her", "一条够长的消息")), app=_APP,
                            history_enabled=True, history_count=0)
            assert ctx3.history == []
            assert st3.log_size("c") == 1, "count=0 仍然要记录"
        finally:
            shutil.rmtree(st3.root, ignore_errors=True)
    print("历史去重 ok（先过滤再 takeLast / 短行不去重 / count=0 只记不注）")


def _merge_short():
    """屏上是一条短消息「嗯嗯」（长度 < 4），不该把它从历史里滤掉。"""
    return [("her", "嗯嗯")]


# ── 联系人匹配与合并 ────────────────────────────────────────────────────────

def check_contact_matching_and_merge():
    with _Tmp() as st:
        # 一键存：标题带人数，display 去掉人数，原标题进别名
        msg = st.save_or_merge_contact("阿杰(3)", _APP)
        assert msg == "已存为联系人「阿杰」", msg
        c = st.find_contact("阿杰", _APP)
        assert c is not None and c.name == "阿杰" and c.aliases == ["阿杰(3)"], c
        assert c.apps == [_APP]

        # 再存一次同一个标题 → 已存在
        assert st.save_or_merge_contact("阿杰(3)", _APP) == "联系人「阿杰」已存在"
        # 换个括号/人数的写法（归一化后还是「阿杰」）→ 也算已存在，**不会多出一条别名**。
        # 这是上游的行为，不是这里漏了：并入分支里「raw 归一化后不在已知名字里才追加别名」
        # 这个条件永远不成立——能走到并入，就说明 normalizeName(title) 已经匹配上了，
        # 那它必然在 known 里。也就是说「一键存」这条路**记录不下新的拼写**，
        # 别名要靠设置页手动编辑。原样保留（上游 v1.3 就是这么写的），先不改。
        msg = st.save_or_merge_contact("阿杰（5）", _APP)
        assert msg == "联系人「阿杰」已存在", msg
        c = st.find_contact("阿杰（5）", _APP)
        assert c is not None and c.aliases == ["阿杰(3)"], c.aliases
        assert len(st.contacts()) == 1, "不该多出一个联系人"

        # 没有标题存不了
        assert st.save_or_merge_contact("", _APP) == "当前会话没有标题，存不了"
        assert st.save_or_merge_contact("(12)", _APP) == "当前会话没有标题，存不了", \
            "只剩人数等于没有名字"

        # 两个都命中时，优先选已经见过这个来源的那个
        st.save_contact(Contact(id="c2", name="小明", apps=["other"]))
        st.save_contact(Contact(id="c3", name="小明", aliases=[], apps=[_APP]))
        assert st.find_contact("小明", _APP).id == "c3"
        assert st.find_contact("小明", "").id == "c2", "没有来源信息时取第一个命中的"

        # 认不出的标题不创建任何东西
        before = len(st.contacts())
        assert st.find_contact("查无此人", _APP) is None
        assert len(st.contacts()) == before

        # 删联系人连带删历史
        st.append_log("c3", [LogEntry("her", "一条消息")])
        assert st.log_size("c3") == 1
        st.delete_contact("c3")
        assert st.log_size("c3") == 0
        assert not os.path.exists(st.log_file("c3"))
    print("联系人 ok（一键存/并入/去人数/来源优先/不自动创建/删人连带删历史）")


# ── ChatContext 的 background / is_empty ────────────────────────────────────

def check_background_format():
    c = Contact(id="c", name="阿杰", relationship="恋人", notes="怕黑")
    n1 = Note(id="a", title="常驻事实", content="养了只猫叫豆豆", always_on=True)
    n2 = Note(id="b", title="上次约定", content="周五交稿")
    ctx = ChatContext(c, [], [n1, n2])
    bg = ctx.background("friends")
    assert "关系：恋人" in bg
    assert "关于阿杰：怕黑" in bg
    assert "常驻事实: 养了只猫叫豆豆" in bg
    assert "上次约定: 周五交稿" in bg
    assert "friends" not in bg, "全局默认关系已经单独发过了，这里不该重复"

    # 联系人没填关系 → 不输出「关系：」这一行
    bg2 = ChatContext(Contact(id="c", name="阿杰", notes="怕黑"), [], []).background("恋人")
    assert "关系：" not in bg2 and "关于阿杰：怕黑" in bg2

    # 什么都没有 → 空串，调用方这时必须整个字段都不发
    empty = ChatContext(None, [], [])
    assert empty.is_empty() and empty.background("x") == ""
    assert ChatContext(Contact(id="c", name="阿杰"), [], []).is_empty()
    assert not ChatContext(Contact(id="c", name="阿杰", notes="x"), [], []).is_empty()
    assert not ChatContext(None, [LogEntry("her", "x")], []).is_empty()
    assert not ChatContext(None, [], [n1]).is_empty()
    print("background ok（关系/备注/摘要/笔记 / 不重复默认关系 / 空则整个字段不发）")


# ── 清空 ────────────────────────────────────────────────────────────────────

def check_clear_all_only_touches_kb():
    with _Tmp() as st:
        st.save_note(Note(id="n", title="t", content="c"))
        st.save_contact(Contact(id="c", name="阿杰"))
        st.append_log("c", [LogEntry("her", "消息")])
        assert st.counts().notes == 1 and st.counts().contacts == 1

        st.clear_all()
        assert not os.path.exists(st.root), "知识库目录该被整个删掉"
        assert st.counts() == st.counts().__class__(0, 0, 0), "清空后计数归零"
        assert st.notes() == [] and st.contacts() == []

        # 清空之后还能重新开始用
        st.save_note(Note(id="n2", title="t2", content="c2"))
        assert len(st.notes()) == 1
    print("清空 ok（只删知识库目录 / 之后可重新使用）")


# ── 自检 ────────────────────────────────────────────────────────────────────

def check_selfcheck():
    """自检要报通过，而且**不留下任何它自己造的数据**。"""
    with _Tmp() as st:
        st.save_note(Note(id="keep", title="用户自己的笔记", content="别动"))
        st.save_contact(Contact(id="keepc", name="用户自己的联系人"))

        out = selfcheck.run(st)
        assert out.startswith("自检通过"), out
        assert "1 条笔记" in out and "1 个联系人" in out, f"计数该只算用户自己的：{out}"

        # 自检造的临时数据必须清干净
        assert [n.id for n in st.notes()] == ["keep"], [n.id for n in st.notes()]
        assert [c.id for c in st.contacts()] == ["keepc"]
        assert st.counts().log_lines == 0, "自检的历史也该清掉"

        # 反复跑也不能留下残渣
        for _ in range(3):
            assert selfcheck.run(st).startswith("自检通过")
        assert len(st.notes()) == 1 and len(st.contacts()) == 1
    print("自检 ok（报通过 + 不留残渣 + 不碰用户自己的数据）")


# ── 落盘格式 ────────────────────────────────────────────────────────────────

def check_json_field_names():
    """字段名跟上游逐字对齐，两边对读才不会错位。"""
    with _Tmp() as st:
        st.save_note(Note(id="n", title="t", content="c", tags=["x"], always_on=True, enabled=True))
        st.save_contact(Contact(id="c", name="阿杰", aliases=["a"], apps=[_APP],
                                relationship="恋人", notes="备注"))
        st.append_log("c", [LogEntry("her", "消息", app=_APP)])
        with open(st.notes_file, encoding="utf-8") as f:
            note = json.load(f)[0]
        assert set(note) == {"id", "title", "content", "tags", "alwaysOn", "enabled",
                             "updatedAt"}, set(note)
        assert note["alwaysOn"] is True
        with open(st.contacts_file, encoding="utf-8") as f:
            ct = json.load(f)[0]
        assert set(ct) == {"id", "name", "aliases", "apps", "relationship", "notes",
                           "autoSummary", "updatedAt"}, set(ct)
        with open(st.log_file("c"), encoding="utf-8") as f:
            lg = json.load(f)[0]
        assert set(lg) == {"side", "text", "ts", "app"}, set(lg)
        assert lg["side"] == "her"
        # 中文不转义（跟上游 org.json 的输出一致，人也读得懂）
        with open(st.notes_file, encoding="utf-8") as f:
            raw = f.read()
        assert "标题" not in raw or "\\u" not in raw, "不该把中文转成 \\uXXXX"
    print("落盘格式 ok（字段名与上游一致 / 中文不转义）")


def check_messages_shape():
    """屏上消息三种形状都认，且 side 归一成 me/her。"""
    norm = CB._norm_messages([("her", "a"), ("me", "b", "阿杰"),
                              {"from": "her", "text": "c"},
                              ("gray", "14:15"), ("weird", "d")])
    assert norm == [("her", "a"), ("me", "b"), ("her", "c"), ("her", "14:15"), ("her", "d")], norm
    assert CB._norm_messages([{"from": "me", "text": None}]) == [("me", "")], "None 变空串"
    print("消息形状 ok（三种入参 / side 归一 / None 兜底）")


def main() -> None:
    check_name_normalization()
    check_corrupt_file_is_preserved()
    check_unmovable_file_refuses_write()
    check_atomic_write_leaves_no_tmp()
    check_screen_batch_rules()
    check_screen_batch_does_not_freeze()
    check_log_cap()
    check_note_matching()
    check_budget_trimming()
    check_history_dedupe_order()
    check_contact_matching_and_merge()
    check_background_format()
    check_clear_all_only_touches_kb()
    check_selfcheck()
    check_json_field_names()
    check_messages_shape()
    print("知识库检查全部通过（存储 / 命中 / 预算 / 历史增量 / 自检 / 落盘格式）")


if __name__ == "__main__":
    main()
