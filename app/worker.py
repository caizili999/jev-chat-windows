# -*- coding: utf-8 -*-
"""子进程：截图 → 定位消息区 → OCR → 按会话去重，全在这边跑。
一次 OCR 250~800ms，放父进程的 Qt 主线程界面就僵了。
只往队列里丢纯 tuple/str/datetime（底色 bg 是 numpy，留在这边不过队列）。帧全程内存，绝不落盘。

**多窗口**：盯哪几个窗口不再由父进程指定，这里自己发现（见 app/windows.py）。
有独立聊天窗口就盯它们（一个窗口一个会话），一个都没有就**回退到主窗口**——
后者跟加这个功能之前完全一样，这是「不开独立窗口的老用户行为逐字节不变」的落点。

每个窗口一套 Capture + Reader，会话名有两个来源，**不能混**：

- **独立窗口**：会话名 = **Windows 窗口标题**。实测这是准的、免费的，而且**必须**这样：
  `read_title()` 在独立窗口上会返回垃圾（实测 `'口'`/`'一'`）——独立窗口的自定义标题栏
  （⊙ 订阅号图标 + 最小化/关闭按钮）**落在 WGC 捕获的客户区内**，头部裁剪把它也框了进去，
  而 `read_title` 取的是「最靠上的一行」。
- **主窗口（回退）**：窗口标题永远是「微信」，只能 OCR 头部那一条，跟以前一模一样。

队列协议（跟父进程的约定，比单窗口时代多了一个「哪个会话/哪个窗口」的维度）：

    ("session", hwnd, 会话名, is_main)  某个窗口现在显示这个会话（第一次见到 / 会话变了）
    ("gone", hwnd)                那个窗口没了（关掉了、或者不再是聊天窗）
    ("lines", 会话名, new, rect)  新消息，new = [(who, name, text, ts)]
    ("area", 会话名, rect)        消息区坐标变了（窗口挪了 / 拖了大小）
    ("input", 会话名, has)        输入框里有没有字（只在状态变化时报）
    ("status" | "paused" | "resumed" | "dead", …)   跟以前一样，是全局的

一个窗口一个会话、会话名不重复（去重见 windows.pick_chat_windows），所以队列里带**会话名**
就够了，父进程自己拿会话名反查 hwnd。带 hwnd 的只有 session/gone 两条——那是「窗口」的事。

`is_main` 是给父进程分路用的：主窗口会**换会话**（会话名跟着 OCR 变），独立窗口钉住一个会话。
父进程对这两条路的处理不一样——主窗口那条要跟老代码逐字节一致，独立窗口那条是新行为。
"""
import ctypes
import time
import traceback

import numpy as np

from app import windows as W
from app.capture import Capture, chat_area, input_has_text, unminimize
from app.ocr import Reader, read_title, similar

_INPUT_POLL = 0.25      # 每个窗口的输入框最快 0.25 秒看一次。纯像素判断只要几毫秒，但没必要每帧都算
_DISCOVER_EVERY = 1.0   # 多久重新发现一次窗口。开/关一个独立窗口最多 1 秒后才被发现，够快了
_IDLE_SLEEP = 0.5       # 一个窗口都没有时的轮询间隔
_MISS_LIMIT = 3         # 连着这么多轮一个窗口都没有 → 当成「微信关了」


def _err(q):
    """异常压成一行发给父进程，子进程的 stderr 一般没人看得见。"""
    q.put(("status", " ".join(traceback.format_exc().split())[-200:]))


class _Win:
    """一个被盯的窗口：一套 Capture + Reader + 各自的去重与输入框状态。

    reader 留在窗口上（而不是按会话名），因为主窗口换会话时**会话名会变而窗口不变**——
    按窗口挂住 reader，切回上一个会话时还能沿用同一套去重状态。会话名变了就把 reader 换掉
    （新会话的历史跟旧会话无关，沿用旧 seen 会把新会话的消息当旧的吞掉）。
    """

    def __init__(self, hwnd, session, is_main):
        self.hwnd = hwnd
        self.session = session
        self.is_main = is_main
        self.cap = Capture(hwnd)          # 可能抛，由调用方兜
        self.reader = Reader()
        self.area = None                  # 最近一次认出来的消息区 6 元组
        self.last_area = None             # 上次发给父进程的 4 元组，变了才再发
        self.head = None                  # 上一帧的头部像素（主窗口靠它省 OCR）
        self.warned = False               # 「消息区认不出来」是否已经报过，拖窗口时别每帧刷
        self.last_input = None
        self.last_input_at = 0.0

    def stop(self):
        try:
            self.cap.stop()
        except Exception:  # noqa: BLE001 —— 停不下来也得让它被丢掉
            pass

    def rename(self, session):
        """会话变了（只有主窗口会）：换一套去重状态。"""
        self.session = session
        self.reader = Reader()
        self.head = None


def _targets():
    """这一轮该盯哪些窗口。返回 [(hwnd, session, is_main)]。

    主窗口的 session 是空串——它的会话名要 OCR 才知道，见 _pump。
    """
    t = W.discover()
    out = [(w["hwnd"], w["session"], False) for w in t["windows"]]
    if t["fallback"] is not None:
        out.append((t["fallback"]["hwnd"], "", True))
    return out


def _sync(q, wins):
    """重新发现一遍：开新的窗口、收掉没了的窗口。返回这一轮有没有窗口。

    收掉时把 reader 一起丢掉——窗口关掉再打开，屏幕上的存量消息会被当成「新消息」报上来，
    这正是父进程那个「每个会话第一批不自动发送」要挡的东西（batches 计数）。
    """
    try:
        want = _targets()
    except Exception:  # noqa: BLE001 —— 枚举失败不该让子进程退出，下一轮再试
        _err(q)
        return bool(wins)

    wanted = {hwnd: (session, is_main) for hwnd, session, is_main in want}
    for hwnd in list(wins):
        if hwnd not in wanted:
            wins[hwnd].stop()
            del wins[hwnd]
            q.put(("gone", hwnd))

    for hwnd, (session, is_main) in wanted.items():
        if hwnd in wins:
            continue
        try:
            win = _Win(hwnd, session, is_main)
        except Exception as e:  # noqa: BLE001 —— 一个窗口开不起来不该拖垮其余的
            q.put(("status", "无法开始采集这个窗口：" +
                   (" ".join(str(e).split())[:120] or type(e).__name__)))
            continue
        wins[hwnd] = win
        q.put(("session", hwnd, session, is_main))
    return bool(wins)


def _session_of(q, win, full, area):
    """这个窗口现在显示哪个会话。主窗口走 OCR，独立窗口用窗口标题。

    主窗口那条路要跟已知会话名做一次相似度归并：OCR 抖一下（「小分队」↔「小分认」）
    不能分裂出一个新会话。独立窗口不用——窗口标题是精确字符串，没有 OCR 噪声。
    """
    if not win.is_main:
        return win.session
    x0, y0, x1, y1, _bg, y_pane = area
    crop = full[y_pane:y0, x0:x1]
    if win.head is not None and np.array_equal(crop, win.head):
        return win.session            # 头部一个像素都没动，别白跑一次 OCR
    win.head = crop
    name = read_title(crop)
    name = next((k for k in _known_sessions if similar(k, name)), name) if name else ""
    # ponytail: 认不出就沿用上次；开头就认不出给个占位名，总比把消息全丢了强
    return name or win.session or "当前会话"


_known_sessions = []   # 已经报给父进程的会话名，主窗口的 OCR 结果往这里归并


def _pump(q, win, watch_input):
    """处理一个窗口的一帧。任何异常都只报一行，绝不退出（一帧出错不该停掉整个采集）。"""
    try:
        unminimize(win.hwnd)
        full = win.cap.settled()
        if full is not None:
            area = chat_area(full)  # 每次停稳都重算：拖完窗口微信布局会晚一拍才铺好
            if area is None:
                if not win.warned:
                    q.put(("status", "消息区认不出来（窗口太小？）"))
                    win.warned = True
            else:
                win.warned = False
                win.cap.area = area  # 采集线程拿它做 diff
                win.area = area
                x0, y0, x1, y1, bg, y_pane = area
                rect = (x0, y0, x1, y1)

                session = _session_of(q, win, full, area)
                if session != win.session:
                    win.rename(session)
                    q.put(("session", win.hwnd, session, win.is_main))
                if session not in _known_sessions:
                    _known_sessions.append(session)

                if rect != win.last_area:
                    q.put(("area", session, rect))
                    win.last_area = rect

                # new 是 [(who, name, text, ts)]，ts 是这条消息的聊天时间（datetime，可能为 None，
                # 由父进程按「继承上一条」补）。它只给聊天记录导出用，分析那半不看它。
                new = win.reader.new_lines(win.reader.read(full[y0:y1, x0:x1], bg))
                if new:
                    q.put(("lines", session, new, rect))
        # 输入框有没有内容。**必须用 latest 帧**：消息区停稳才走上面那段，而用户打字时
        # 消息区一个像素都没变、settled() 根本不返回——拿停稳帧去判断只会永远看到「空」，
        # 那正好是最危险的误判方向（以为框是空的，一粘贴就把人家的草稿拼上去发了）。
        #
        # ⚠️ 这一段**不在**上面那个 else 里：消息区认不出来的那些帧（窗口太小、布局没铺好）
        # 也必须继续盯着输入框。少盯一会儿就等于在自动发送面前撤掉一道闸。
        if watch_input is not None and watch_input.is_set():
            now = time.monotonic()
            frame, area = win.cap.latest, win.cap.area
            if frame is not None and area is not None and now - win.last_input_at >= _INPUT_POLL:
                win.last_input_at = now
                has = input_has_text(frame, area)
                if has != win.last_input:
                    win.last_input = has
                    q.put(("input", win.session, has))
        else:
            win.last_input = None  # 关掉再开时重新报一次，父进程不该拿着旧状态做决定
    except Exception:  # noqa: BLE001
        _err(q)


def run(q, enabled, watch_input=None):
    """enabled 置位=采集，清掉=暂停。暂停时把所有 WGC 会话都停掉（Windows 那圈黄色采集边框
    也跟着没了），恢复时重新发现 + 重开；wins 里已经建好的会被丢掉，所以恢复后每个会话的
    第一批消息都会被当成「新消息」报一遍——这正是「第一批不自动发送」要挡的。

    watch_input 置位时额外盯着输入框有没有内容（自动发送要发之前确认框是空的）。它跟 enabled
    分开，是因为自动发送默认关着——没开的人不该白付这份计算。
    """
    global _known_sessions
    ctypes.windll.user32.SetProcessDPIAware()
    wins = {}
    next_discover = 0.0
    misses = 0
    while True:
        if not enabled.is_set():
            if wins:
                for win in wins.values():
                    win.stop()
                wins.clear()
                q.put(("paused",))
            enabled.wait()
            continue

        now = time.monotonic()
        if now >= next_discover:
            next_discover = now + _DISCOVER_EVERY
            had = bool(wins)
            if not _sync(q, wins):
                misses += 1
                # 连着几轮一个窗口都没有 = 微信关了（或者收托盘且没开独立窗口）。
                # 只在**之前有窗口**的情况下才报 dead——不然一启动没开微信就报「采集停了」，
                # 用户会以为程序坏了，实际是他还没开微信。
                if had and misses >= _MISS_LIMIT:
                    q.put(("dead", "采集停了（微信关了？）"))
                    return
                time.sleep(_IDLE_SLEEP)
                continue
            misses = 0
            _known_sessions = [w.session for w in wins.values() if w.session]

        for win in list(wins.values()):
            if not win.cap.alive():
                # 采集线程死了（窗口被关了 / WGC 出错）。收掉它，让下一轮发现去补。
                win.stop()
                wins.pop(win.hwnd, None)
                q.put(("gone", win.hwnd))
                continue
            _pump(q, win, watch_input)
        time.sleep(0.05)
