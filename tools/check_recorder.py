# -*- coding: utf-8 -*-
"""聊天记录导出的离线检查。跑：python tools/check_recorder.py

**绝不碰用户真实数据**：所有写入都在临时目录里，跑完删掉；settings._CONFIG 也指到临时文件。
不联网、不起 Qt 窗口（Reader 那部分用假的 OCR 结果喂进去，不加载模型）。
"""
import csv
import io
import json
import os
import queue
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 输出强制 UTF-8。Windows 上 stdout 的编码跟着控制台代码页走（中文系统 cp936、CI 的
# GitHub runner 是 cp1252），cp1252 编不出中文，脚本会在 print 那一行直接 UnicodeEncodeError。
# 跟其它 check_*.py 保持同一写法，别只在本地能跑。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

import app.ocr as ocr  # noqa: E402
from app import recorder, settings  # noqa: E402

_MISSING = object()  # 用来区分「模块里原本没有这个属性」和「原本是 None」


def _box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def _rows(root, chat, day):
    path = os.path.join(root, chat, day + ".csv")
    if not os.path.exists(path):
        return []
    return _read(path)[1:]  # 去掉表头


# ── 时间戳解析 ──────────────────────────────────────────────────────────────

def check_parse_time():
    """微信那行灰字时间戳 → datetime。认不出的必须返回 None——猜错会污染按日期分层。"""
    now = datetime(2026, 9, 24, 15, 30)  # 星期四
    assert ocr.parse_time("14:15", now) == datetime(2026, 9, 24, 14, 15)
    # 程序跨零点还在跑：屏幕上是昨晚 23:50 那批，解出来晚于 now 就该退一天，否则会归到明天
    late = datetime(2026, 9, 24, 0, 5)
    assert ocr.parse_time("23:50", late) == datetime(2026, 9, 23, 23, 50), "晚于 now 的该退一天"
    assert ocr.parse_time("昨天 14:15", now) == datetime(2026, 9, 23, 14, 15)
    assert ocr.parse_time("昨天 23:50", late) == datetime(2026, 9, 23, 23, 50), "「昨天」不该再退"
    assert ocr.parse_time("星期二 14:15", now) == datetime(2026, 9, 22, 14, 15)
    assert ocr.parse_time("星期四 14:15", now) == datetime(2026, 9, 24, 14, 15), "就是今天"
    assert ocr.parse_time("9月20日 14:15", now) == datetime(2026, 9, 20, 14, 15)
    assert ocr.parse_time("2026年9月20日 14:15", now) == datetime(2026, 9, 20, 14, 15)
    assert ocr.parse_time("12月31日 08:00", now) == datetime(2025, 12, 31, 8, 0), "晚于 now 的该退一年"
    assert ocr.parse_time("2026年12月31日 08:00", now) == datetime(2026, 12, 31, 8, 0), "写明年份就不动"

    # 认不出的一律 None：宁可让调用方继承上一条，也不能猜出一个错日期
    for bad in ("", None, "古法编程非遗传承人", "对方撤回了一条消息", "@小金 在吗",
                "25:00", "14:60", "9月32日 10:00", "前天 14:15", "昨天", "14:15:30",
                "https://example.com/a:b", "群公告：今晚八点开会"):
        assert ocr.parse_time(bad, now) is None, f"{bad!r} 不该被认成时间戳"

    # 长度卡在 12 个字符前缀上：「2026年9月20日」正好 10 个，够用
    assert ocr.parse_time("2026年9月20日 23:59", now) is not None
    print("ocr.parse_time() 时间戳解析 ok")


def check_reader_lines():
    """Reader 输出的形状：灰字该分流的要分流，而且**发言人名那条老逻辑一点没变**。

    这是这次改动风险最高的地方——时间戳和发言人名都走 gray 分支，改错了会让群聊里的名字
    认不出来（所有消息挂到上一个人头上），或者时间戳被当成一条消息记进 CSV。
    """
    pane = np.array([240, 240, 240], dtype=np.uint8)
    bubble = np.array([255, 255, 255], dtype=np.uint8)
    kinds = {}

    def fake_who_said(_chat, box):
        return kinds[box[0][1]]

    real = ocr.who_said
    ocr.who_said = fake_who_said
    try:
        chat = np.zeros((400, 400, 3), dtype=np.uint8)
        r = ocr.Reader.__new__(ocr.Reader)  # 绕开 __init__：它会加载 RapidOCR（40MB + 几秒）
        r.lh, r.seen = None, []

        # y 从小到大：时间戳（居中）→ 发言人名（靠左）→ her 气泡 → 时间戳 → me 气泡
        #            x0   y0   x1   y1   文本
        spec = [("ts1", 170, 10, 230, 26, "14:15", "gray"),
                ("nm1", 20, 40, 120, 56, "阿杰", "gray"),
                ("b1", 40, 60, 200, 80, "今天天气不错", "her"),
                ("b2", 40, 90, 200, 110, "那我们去哪", "her"),
                ("ts2", 170, 130, 230, 146, "昨天 09:05", "gray"),
                ("b3", 220, 150, 380, 170, "我来说两句", "me")]
        res = []
        for key, x0, y0, x1, y1, text, kind in spec:
            res.append((_box(x0, y0, x1, y1), text, 0.9))
            kinds[y0] = (kind, pane if kind == "gray" else bubble, 12)
        r.ocr = lambda img, use_cls=False: (res, None)

        lines = r.read(chat, pane)
        got = [(w, n, t, st) for w, n, t, _y, st in lines]
        assert len(got) == 3, f"只有 3 条消息，灰字两条都不该变成消息：{got}"
        assert got[0][:3] == ("her", "阿杰", "今天天气不错"), got[0]
        assert got[0][3] == ocr.parse_time("14:15"), f"该带上第一条时间戳：{got[0][3]}"
        assert got[1][:3] == ("her", "阿杰", "那我们去哪"), "名字要一直带到下一个名字行"
        assert got[1][3] == got[0][3], "中间那条没有时间戳，该继承同一个基准"
        assert got[2][0] == "me" and got[2][1] is None, got[2]
        assert got[2][3] == ocr.parse_time("昨天 09:05"), f"该换成新的基准：{got[2][3]}"
        assert got[2][3].date() < datetime.now().date(), got[2][3]

        # 发言人名那条老逻辑：靠左、短、不带冒号才算名字；时间戳（带冒号）不能顶掉它
        assert got[0][1] == "阿杰" and got[1][1] == "阿杰"

        # 增量判定沿用 seen（不含时间戳），所以同一帧再读一次不该报出新消息。
        # 注意必须先走一次 new_lines——seen 是它填的，read() 自己不写。
        first = r.new_lines(lines)
        assert len(first) == 3, first
        again = r.new_lines(r.read(chat, pane))
        assert again == [], f"同一屏重复读不该当成新消息：{again}"

        # 时间戳不参与判重：seen 里存的是三元组，跟改动前完全一样
        assert all(len(s) == 3 for s in r.seen), r.seen[:2]
    finally:
        ocr.who_said = real
    print("ocr.Reader 灰字分流与发言人名 ok")


# ── 会话名 → 目录名 ────────────────────────────────────────────────────────

def check_safe_name():
    assert recorder.safe_name("小分队") == ("小分队", False)
    assert recorder.safe_name("A/B:C*D?E\"F<G>H|I") == ("A_B_C_D_E_F_G_H_I", True)
    # 前后空格被 strip 掉，但目录名看起来跟群名一模一样，不值得为它弹一条「名字被改过」
    assert recorder.safe_name("  前后空格  ") == ("前后空格", False)
    assert recorder.safe_name("结尾有点...") == ("结尾有点", True)
    assert recorder.safe_name("") == ("未命名会话", True)
    assert recorder.safe_name("   ") == ("未命名会话", True)
    assert recorder.safe_name(None) == ("未命名会话", True)
    assert recorder.safe_name("CON") == ("_CON", True), "保留名要避开"
    assert recorder.safe_name("com1") == ("_com1", True), "大小写不敏感"
    assert recorder.safe_name("🔒秘密基地") == ("🔒秘密基地", False), "emoji 是合法文件名，别动它"
    assert recorder.safe_name("控制\x01字符") == ("控制_字符", True)

    long_name = "很长的群名" * 30
    out, renamed = recorder.safe_name(long_name)
    assert renamed and len(out) == recorder._MAX_NAME + 7, len(out)
    # 截断必须带哈希，否则两个前缀相同的长群名会撞进同一个目录
    other = "很长的群名" * 29 + "尾"
    assert recorder.safe_name(other)[0] != out, "长群名截断后不能撞名"
    print("recorder.safe_name() 目录名转义 ok")


# ── 记录器 ──────────────────────────────────────────────────────────────────

def check_recorder_basic(root):
    """最基本的：建目录、写表头、四列、方向。"""
    rec = recorder.Recorder(root)
    day = "2026-09-24"
    rec.add("小分队", [("her", "阿杰", "今天天气不错", datetime(2026, 9, 24, 14, 15)),
                       ("me", None, "驴也行", datetime(2026, 9, 24, 14, 16))])
    rec.close()
    assert os.path.isdir(os.path.join(root, "小分队"))
    raw = _read(os.path.join(root, "小分队", day + ".csv"))
    assert raw[0] == recorder.HEADER, raw[0]
    assert raw[1] == ["2026-09-24 14:15", "对方", "阿杰", "今天天气不错"], raw[1]
    assert raw[2] == ["2026-09-24 14:16", "我", "", "驴也行"], raw[2]

    # 程序替我发的那句要标成「程序」，而不是混进「我」里——事后审计全靠这一列
    rec2 = recorder.Recorder(root)
    rec2.note_sent("小分队", "驴也行，驴力气大")
    rec2.add("小分队", [("me", None, "驴也行，驴力气大", datetime(2026, 9, 24, 14, 20))])
    rec2.close()
    assert _rows(root, "小分队", day)[-1] == \
        ["2026-09-24 14:20", "程序", "", "驴也行，驴力气大"], _rows(root, "小分队", day)[-1]
    # 认领过一次就不再认领：用户自己打的同样一句话仍然是「我」
    rec2.note_sent("小分队", "在吗")
    rec2.add("小分队", [("me", None, "在吗", datetime(2026, 9, 24, 14, 21)),
                        ("me", None, "在吗", datetime(2026, 9, 24, 14, 22))])
    rec2.close()
    tail = _rows(root, "小分队", day)[-2:]
    assert [r[1] for r in tail] == ["程序", "我"], tail
    print("recorder 落盘与方向列 ok")


def check_recorder_dedup(root):
    """去重：同一条消息重复上报不能重复写；但真实重复的内容不能被吞。"""
    day = "2026-09-24"
    title = "去重群"
    rec = recorder.Recorder(root)
    rows = [("her", "阿杰", "在吗", datetime(2026, 9, 24, 10, 0)),
            ("her", "阿杰", "在吗", datetime(2026, 9, 24, 10, 0))]
    rec.add(title, rows)
    rec.add(title, rows)  # 整屏重报：同一批再来一次
    rec.close()
    assert len(_rows(root, title, day)) == 1, _rows(root, title, day)

    # OCR 抖动：重报时同一个字识别成了别的字，也要判成同一条
    rec = recorder.Recorder(root)
    rec.add(title, [("her", "阿杰", "不好意思", datetime(2026, 9, 24, 10, 1))])
    rec.close()
    rec = recorder.Recorder(root)
    rec.add(title, [("her", "阿杰", "不好竟思", datetime(2026, 9, 24, 10, 1))])
    rec.close()
    assert len(_rows(root, title, day)) == 2, _rows(root, title, day)

    # 时间戳不参与判重键：同一条消息两次报上来、一次解析到时间一次没解析到，仍然算同一条
    rec = recorder.Recorder(root)
    rec.add(title, [("her", "阿杰", "吃饭了吗", datetime(2026, 9, 24, 12, 0))])
    rec.close()
    rec = recorder.Recorder(root)
    rec.add(title, [("her", "阿杰", "吃饭了吗", None)])  # None 会继承，不会变成墙钟
    rec.close()
    assert len(_rows(root, title, day)) == 3, _rows(root, title, day)

    # 不同人说同样的话不算重复（发送者进键）
    rec = recorder.Recorder(root)
    rec.add(title, [("her", "小明", "在吗", datetime(2026, 9, 24, 10, 2))])
    rec.close()
    assert len(_rows(root, title, day)) == 4

    # 窗口外的同内容消息照记：同一个人在群里两次说「好的」是两条真实消息。
    # 先把「好的」写下去，再用 60 条填充把它挤出 50 行的去重窗口，然后新进程再报一次同样的话。
    # 填充故意用 60 个不同的发送者：similar() 很宽松（「填充 10」和「填充 11」只差一个字也算同一条），
    # 靠换文本挤窗口会挤不动，靠换人最稳。
    rec = recorder.Recorder(root)
    base = datetime(2026, 9, 24, 11, 0)
    rec.add(title, [("her", "阿杰", "好的", base)])
    rec.add(title, [("her", f"群友{i}", "填充", base + timedelta(minutes=1 + i))
                    for i in range(60)])
    rec.close()
    before = len(_rows(root, title, day))
    rec = recorder.Recorder(root)
    rec.add(title, [("her", "阿杰", "好的", base + timedelta(minutes=120))])
    rec.close()
    assert len(_rows(root, title, day)) == before + 1, "窗口外的不该被吞"
    print("recorder 去重（窗口内 / 模糊 / 跨运行） ok")


def check_recorder_time(root):
    """时间：没有时间戳就继承上一条；一条都没有才退回墙钟，并且要说破。"""
    day = "2026-09-24"
    title = "时间群"
    rec = recorder.Recorder(root, now=lambda: datetime(2026, 9, 24, 23, 59))
    rec.add(title, [("her", "阿杰", "早上好", datetime(2026, 9, 24, 8, 0)),
                    ("her", "阿杰", "吃过了", None),
                    ("her", "阿杰", "那我走了", None)])
    rec.close()
    got = [r[0] for r in _rows(root, title, day)]
    assert got == ["2026-09-24 08:00", "2026-09-24 08:00", "2026-09-24 08:00"], got

    # 一条时间戳都没有：退回墙钟，但必须留下一条提示，不能让用户以为那就是聊天时间
    rec = recorder.Recorder(root, now=lambda: datetime(2026, 9, 24, 23, 59))
    rec.add("没时间戳的群", [("her", "阿杰", "只有我", None)])
    rec.close()
    assert _rows(root, "没时间戳的群", day)[0][0] == "2026-09-24 23:59"
    problems = rec.take_problems()
    assert any("时间戳" in p for p in problems), problems
    assert rec.take_problems() == [], "同一条提示只该给一次"
    print("recorder 时间继承与墙钟兜底 ok")


def check_recorder_days(root):
    """按**消息真实时间**分文件：23:59 的补录消息归前一天，不归「程序看到它的那天」。"""
    rec = recorder.Recorder(root)
    rec.add("跨天群", [("her", "阿杰", "睡前一句", datetime(2026, 9, 23, 23, 59)),
                       ("her", "阿杰", "起床一句", datetime(2026, 9, 24, 7, 30)),
                       ("her", "阿杰", "跨天一条", datetime(2026, 9, 23, 23, 58))])
    rec.close()
    assert len(_rows(root, "跨天群", "2026-09-23")) == 2, _rows(root, "跨天群", "2026-09-23")
    assert len(_rows(root, "跨天群", "2026-09-24")) == 1
    # 同一批里日期是乱的，文件内必须各自按日期归好
    days = sorted(_rows(root, "跨天群", "2026-09-23"))
    assert days[0][0] == "2026-09-23 23:58" and days[1][0] == "2026-09-23 23:59", days
    print("recorder 跨天分文件 ok")


def check_recorder_bom(root):
    """BOM 只能出现在文件开头一次。

    utf-8-sig 的编解码器会在**流的第一次写入**时吐 BOM；追加模式下第一次写入的位置就是文件
    末尾，无脑用 utf-8-sig 追加会把 BOM 插进文件中间，后面每行都跟着一个看不见的 \\ufeff。
    """
    title = "BOM群"
    path = os.path.join(root, title, "2026-09-24.csv")
    texts = ["早上好", "吃过了", "那我走了"]  # 必须真的不一样：similar() 会把「第0条/第1条」判成同一条
    for i, text in enumerate(texts):
        rec = recorder.Recorder(root)  # 每次都是新进程，模拟程序反复重启
        rec.add(title, [("her", "阿杰", text, datetime(2026, 9, 24, 9, i))])
        rec.close()
    raw = open(path, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf"), "开头该有 BOM，否则 Excel 打开是乱码"
    bom = b"\xef\xbb\xbf"
    assert raw.count(bom) == 1, f"BOM 只该有一次，实际 {raw.count(bom)}"
    rows = _read(path)
    assert len(rows) == 4, rows  # 表头 + 3 行
    assert all(not c.startswith("\ufeff") for r in rows for c in r), rows
    print("recorder BOM 只写一次 ok")


def check_recorder_failure(tmp):
    """写盘失败不能往外抛——记录是旁路，拖垮采集和自动发送才是真事故。"""
    # 拿一个**文件**当父目录，makedirs 必然失败
    blocker = os.path.join(tmp, "blocker")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    rec = recorder.Recorder(os.path.join(blocker, "聊天记录"))
    rec.add("小分队", [("her", "阿杰", "写不进去", datetime(2026, 9, 24, 10, 0))])
    rec.close()  # 不该抛
    problems = rec.take_problems()
    assert any("写不进去" in p for p in problems), problems
    # 失败的那批要留着，下次 flush 再试（不能悄悄丢掉）
    assert rec._pending["小分队"], "写失败的那批必须留着重试"
    print("recorder 写盘失败不抛且留待重试 ok")


def check_recorder_poll(root):
    """定时 flush：不是每条都写（攒批），也不能攒到退出才写（被强杀就全丢）。"""
    clock = [1000.0]
    rec = recorder.Recorder(root, clock=lambda: clock[0])
    rec.add("定时群", [("her", "阿杰", "第一条", datetime(2026, 9, 24, 10, 0))])
    path = os.path.join(root, "定时群", "2026-09-24.csv")
    assert not os.path.exists(path), "刚加一条不该立刻落盘（还没到 5 秒 / 20 条）"
    clock[0] += 4
    rec.poll()
    assert not os.path.exists(path), "没到 5 秒不该落盘"
    clock[0] += 2
    rec.poll()
    assert os.path.exists(path), "到点该落盘"

    # 攒够 20 条也要立刻落盘，不用等时间。20 个不同的人说同一句话——靠换人保证不被判重
    # （similar() 太宽松，靠改文案凑不出 20 条互不相似的短消息）
    clock[0] += 100
    rec.poll()
    rec.add("批量群", [("her", f"群友{i}", "在吗", datetime(2026, 9, 24, 10, i)) for i in range(20)])
    assert os.path.exists(os.path.join(root, "批量群", "2026-09-24.csv")), "攒够 20 条该立刻落盘"
    rec.close()
    print("recorder 定时与批量 flush ok")


def check_recorder_rename_problem(root):
    """名字被改写要说破一次，而且只说一次。"""
    rec = recorder.Recorder(root)
    rec.add("非法/名字", [("her", "阿杰", "早上好", datetime(2026, 9, 24, 10, 0))])
    rec.add("非法/名字", [("her", "阿杰", "那我走了", datetime(2026, 9, 24, 10, 1))])
    rec.close()
    problems = rec.take_problems()
    assert sum("不能直接当文件夹名" in p for p in problems) == 1, problems
    assert os.path.isdir(os.path.join(root, "非法_名字"))
    print("recorder 改名提示只报一次 ok")


# ── main 的分叉 ─────────────────────────────────────────────────────────────

def check_main_fork(tmp):
    """main.drain() 收到 4 元组的新行：history 照旧（三元组），记录器拿到带时间的那份。"""
    import main

    path = os.path.join(tmp, "fork.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友"}, f, ensure_ascii=False)
    settings._CONFIG = path

    class FakeQ:
        def __init__(self, msgs):
            self.msgs = list(msgs)

        def get_nowait(self):
            if not self.msgs:
                raise queue.Empty
            return self.msgs.pop(0)

    ov = SimpleNamespace(current_chat=lambda: "小分队", invalidate_replies=lambda: None,
                         log_message=lambda *a, **k: None, set_targets=lambda *a: None,
                         set_busy=lambda *a: None, set_status=lambda *a, **k: None,
                         log=lambda *a: None, after=lambda *a: None,
                         set_chat=lambda *a: None, auto_pending=lambda: False,
                         cancel_auto=lambda *a, **k: None, refresh_windows=lambda: None)
    ts = datetime(2026, 9, 24, 14, 15)
    started = []
    # ov / q 只在 __main__ 那段里才存在，import 进来的 main 没有这两个属性，直接造给它。
    # start_analyze 必须打桩：不然这里会真起一个线程去调模型（离线检查不该联网）。
    # 恢复时按「原本有没有」来：原本没有的要删掉，别把假对象留在 main 里给后面的检查看见。
    names = ("chats", "ov", "q", "history_rec", "start_analyze")
    real = {k: vars(main).get(k, _MISSING) for k in names}
    real_wins, real_chat = main.state["wins"], main.state["chat"]
    main.ov = ov
    # 先来一条 session：会话名 → 窗口的映射就是这么建立的（多独立窗口协议）。
    # 少了它 win_of("小分队") 是 None，后面那条 lines 里的坐标就没地方落。
    main.q = FakeQ([("session", 7, "小分队", True),
                    ("lines", "小分队", [("her", "阿杰", "你好", ts)], (0, 0, 10, 10))])
    main.history_rec = recorder.Recorder(os.path.join(tmp, "记录"))
    main.chats = {}
    main.start_analyze = lambda title, msgs, auto_ok=False: started.append((title, list(msgs)))
    try:
        main.drain()
        hist = main.chats["小分队"]["history"]
        assert list(hist) == [("her", "你好", "阿杰")], f"history 必须还是三元组：{list(hist)}"
        assert started == [("小分队", [("her", "你好", "阿杰")])], started
        assert main.state["wins"]["小分队"]["area"] == (0, 0, 10, 10), \
            "lines 里带的坐标要落到这个会话的窗口上（fill_reply 靠它）"
        # 记录器是攒批写的，close() 才会 flush；不 close 就读文件会读空
        main.history_rec.close()
        rows = _rows(os.path.join(tmp, "记录"), "小分队", "2026-09-24")
        assert rows == [["2026-09-24 14:15", "对方", "阿杰", "你好"]], rows
    finally:
        for k, v in real.items():
            if v is _MISSING:
                delattr(main, k)
            else:
                setattr(main, k, v)
        main.state["wins"], main.state["chat"] = real_wins, real_chat
    print("main.drain() 分叉（history 不变 + 记录器拿到时间） ok")


def check_sync_history(tmp):
    """开关一拨，记录器跟着建/收；关掉时**最后那次 flush 的问题也要说出来**，不能跟着记录器一起丢。"""
    import main

    path = os.path.join(tmp, "sync.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友"}, f, ensure_ascii=False)
    settings._CONFIG = path
    logged = []
    names = ("ov", "history_rec")
    real = {k: vars(main).get(k, _MISSING) for k in names}
    main.ov = SimpleNamespace(log=lambda line: logged.append(line))
    main.history_rec = None
    try:
        settings.save(None, "朋友", save_history_on=True)
        main.sync_history()
        assert main.history_rec is not None, "开关开着就该建一个记录器"
        rec = main.history_rec
        main.sync_history()
        assert main.history_rec is rec, "重复调用不该重建——重建会把攒着的那批记录丢掉"

        # 把根目录指到一个**文件**上，最后那次 flush 必然失败
        blocker = os.path.join(tmp, "sync_blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        rec.root = os.path.join(blocker, "聊天记录")
        rec.add("小分队", [("her", "阿杰", "早上好", datetime(2026, 9, 24, 10, 0))])
        settings.save(None, "朋友", save_history_on=False)
        main.sync_history()
        assert main.history_rec is None, "关掉开关就该放掉记录器"
        assert any("写不进去" in line for line in logged), f"最后那次 flush 的问题被吞了：{logged}"
    finally:
        for k, v in real.items():
            if v is _MISSING:
                delattr(main, k)
            else:
                setattr(main, k, v)
    print("main.sync_history() 建/收与收尾提示 ok")


def check_settings_switch(tmp):
    """开关的默认值、落盘、读回，以及它跟「自动发送」那组开关完全独立。"""
    path = os.path.join(tmp, "settings.json")
    settings._CONFIG = path
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友"}, f, ensure_ascii=False)
    assert settings.save_history() is False, "默认必须关：它要长期往磁盘写文件"
    assert os.path.basename(settings.history_dir()) == "聊天记录"
    assert os.path.isabs(settings.history_dir()), "不能是相对路径（双击 exe 时 cwd 可能是桌面）"

    settings.save(None, "朋友", save_history_on=True)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["save_history"] is True, "该落盘"
    assert settings.auto_send_on() is False, "它不该把自动发送那组开关顶起来"

    settings.save(None, "朋友", save_history_on=False)
    assert settings.save_history() is False
    # 不传 = 保留，别的调用方（比如只改关系的）不该顺手把它关掉
    settings.save(None, "朋友", save_history_on=True)
    settings.save(None, "朋友")
    assert settings.save_history() is True, "没传的开关必须保留"
    print("settings 保存聊天记录开关 ok")


def main_check():
    tmp = tempfile.mkdtemp(prefix="jev_rec_")
    real_config = settings._CONFIG
    try:
        check_parse_time()
        check_reader_lines()
        check_safe_name()
        root = os.path.join(tmp, "聊天记录")
        check_recorder_basic(root)
        check_recorder_dedup(root)
        check_recorder_time(root)
        check_recorder_days(root)
        check_recorder_bom(root)
        check_recorder_failure(tmp)
        check_recorder_poll(root)
        check_recorder_rename_problem(root)
        check_main_fork(tmp)
        check_sync_history(tmp)
        check_settings_switch(tmp)
    finally:
        settings._CONFIG = real_config
        shutil.rmtree(tmp, ignore_errors=True)
    print("聊天记录导出检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main_check())
