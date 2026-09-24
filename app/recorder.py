# -*- coding: utf-8 -*-
"""把对话记录按「会话名 / 日期」分层写成 CSV。

**不依赖 Qt、不依赖 app.ocr**，所以能单独离线测（见 tools/check_recorder.py）。

下面每条都是访谈里权衡过的，不要随手改：

- 目录：<根>/<会话名>/<YYYY-MM-DD>.csv，四列：时间, 方向, 发送者, 内容。
- 去重键 = (方向, 发送者, 内容)，**不含时间**。时间戳灰字在不在屏幕上取决于滚动位置，同一批
  消息重报时可能有的解析到、有的解析不到，把时间塞进键里反而会失效。比较用 similar() 而不是
  ==：重报的那批是重新 OCR 出来的，同一个字可能识别成别的字。
- 只在最近 _WINDOW 条已写记录里查重。窗口外的同内容消息照记——同一个人在群里两次说「好的」
  是两条真实消息，不能被吞掉。
- 跨运行：第一次写某个会话时，读回它目录下最新的 _RELOAD 个 CSV 的尾部装进窗口。子进程重启后
  worker 的 seen 是空的、整屏会重报一遍，只有这一层挡得住。
- 写盘：攒一批，_FLUSH_ROWS 条或 _FLUSH_SECONDS 秒先到先算，退出时再 flush 一次。不是每条都写
  （群里刷屏时每秒好几条），也不是只等退出（被强杀就全丢）。
- 时间：ts 为 None 时继承该会话上一条已知时间；一条都没有才退回墙钟，并记进 problems。
  归档日期一律按**消息真实时间**——23:59 的消息归前一天，不归「程序看到它的那天」。
"""
import csv
import hashlib
import os
import time
from collections import deque
from datetime import datetime

from app.textsim import similar

_WINDOW = 50        # 去重滑动窗口：整屏可见十几到几十条，50 足够覆盖任何一次整屏重报
_RELOAD = 2         # 跨运行去重时读回最新几个 CSV
_FLUSH_SECONDS = 5
_FLUSH_ROWS = 20
_SENT_TTL = 120     # 「程序刚替我发的话」保留多久，超过就不认了
_SENT_MAX = 20      # 最多留几条
_MAX_NAME = 60
_BAD_CHARS = '\\/:*?"<>|'
_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
             | {f"COM{i}" for i in range(1, 10)}
             | {f"LPT{i}" for i in range(1, 10)})
HEADER = ["时间", "方向", "发送者", "内容"]


def safe_name(title):
    """会话名 → 能当 Windows 目录名的字符串。返回 (名字, 是否被改写过)。

    Windows 不允许 \\ / : * ? " < > |，CON/PRN/... 是保留名，名字结尾不能是空格或点。
    emoji 反而是合法的，不动它。
    """
    raw = (title or "").strip()
    out = "".join("_" if c in _BAD_CHARS or ord(c) < 32 else c for c in raw).strip(" .")
    if not out:
        return "未命名会话", True
    if out.split(".")[0].upper() in _RESERVED:
        out = "_" + out
    if len(out) > _MAX_NAME:
        # 直接截断会让两个长群名撞进同一个目录，所以拼一小段原始名字的哈希
        out = out[:_MAX_NAME] + "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return out, out != raw


class Recorder:
    """一个进程一个。写盘出错只记进 problems，绝不往外抛——记录是旁路，不该拖垮采集和自动发送。"""

    def __init__(self, root, clock=time.monotonic, now=datetime.now):
        self.root = root                      # 就是那个「聊天记录」目录本身
        self._clock = clock
        self._now = now
        self._pending = {}                    # {会话目录名: [(时间文本, 方向, 发送者, 内容)]}
        self._recent = {}                     # {会话目录名: deque[(方向, 发送者, 内容)]}
        self._loaded = set()                  # 已经读回过去重窗口的会话目录名
        self._last_ts = {}                    # {会话目录名: datetime}，给没时间戳的消息继承
        self._last_flush = clock()
        self._sent = []                       # [(会话名, 文本, monotonic)]，程序刚替我发出去的话
        self._noted = set()                   # 已经提示过的原因，同一条只说一次
        self.problems = []                    # 给上层取走的一次性提示

    # ── 对外 ────────────────────────────────────────────────────────────────

    def note_sent(self, title, text):
        """记下「程序刚替我发了这句话」。OCR 随后会把这条读成 who='me'，靠这份名单把它标成「程序」。

        只留最近 _SENT_TTL 秒、最多 _SENT_MAX 条：名单太长会把用户自己打的同样一句话误标成程序发的。
        """
        self._sent.append((title, text, self._clock()))
        del self._sent[:-_SENT_MAX]

    def add(self, title, rows):
        """rows = [(who, name, text, ts)]，ts 是 datetime 或 None。"""
        dirname, renamed = safe_name(title)
        if renamed:
            self._note(f"会话名「{title}」不能直接当文件夹名，记录存到了「{dirname}」")
        self._load_window(dirname)
        window = self._recent[dirname]
        out = self._pending.setdefault(dirname, [])
        for who, name, text, ts in rows:
            if ts is None:
                ts = self._last_ts.get(dirname)
                if ts is None:
                    # 一条时间戳都没认出来（刚启动、屏幕上一条灰字都没有）。退回墙钟，但说破，
                    # 免得用户以为这个时间就是聊天时间。
                    ts = self._now()
                    self._note(f"「{title}」还没识别到时间戳，这几条先按当前时间记")
            self._last_ts[dirname] = ts
            direction = "对方" if who == "her" else ("程序" if self._claim_sent(title, text) else "我")
            key = (direction, name or "", text)
            if self._is_dup(window, key):
                continue
            window.append(key)
            out.append((ts.strftime("%Y-%m-%d %H:%M"), direction, name or "", text))
        if len(out) >= _FLUSH_ROWS or self._clock() - self._last_flush >= _FLUSH_SECONDS:
            self.flush()

    def poll(self):
        """主循环定期调（不必很频繁）。到点就把攒着的写下去。"""
        if self._clock() - self._last_flush >= _FLUSH_SECONDS:
            self.flush()

    def flush(self):
        """把攒着的写下去。失败只记进 problems 并**留着下次再试**，不抛。"""
        self._last_flush = self._clock()
        for dirname, rows in list(self._pending.items()):
            if not rows:
                continue
            folder = os.path.join(self.root, dirname)
            try:
                os.makedirs(folder, exist_ok=True)
                # 同一批里可能跨天（补录的存量），所以按**每一行自己的日期**分组，不是按批。
                by_day = {}
                for ts_text, direction, sender, text in rows:
                    by_day.setdefault(ts_text[:10], []).append((ts_text, direction, sender, text))
                for day, day_rows in sorted(by_day.items()):
                    self._append(os.path.join(folder, day + ".csv"), day_rows)
            except Exception as e:  # noqa: BLE001 —— 记录是旁路，任何异常都不许往上抛
                # 别写 e.strerror：非 OSError（编码之类）没有这个属性，会在 except 里再炸一次
                self._note(f"聊天记录写不进去（{getattr(e, 'strerror', None) or e}）：{folder}")
                continue  # 不清 pending，下次 flush 再试
            self._pending[dirname] = []

    def take_problems(self):
        """取走攒下的提示（取完清空）。上层拿去写日志/弹一次状态栏。"""
        out, self.problems = self.problems, []
        return out

    def close(self):
        self.flush()

    # ── 内部 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _append(path, rows):
        # BOM 只在新文件上写一次。**不能无脑用 utf-8-sig 追加**：那个编解码器会在流的第一次
        # 写入时吐 BOM，追加模式下第一次写入的位置就是文件末尾——BOM 会被插进文件中间，
        # 后面每一行都跟着一个看不见的 \\ufeff，Excel 和 csv 都会认歪。
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding=("utf-8-sig" if fresh else "utf-8")) as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(HEADER)
            w.writerows(rows)

    @staticmethod
    def _is_dup(window, key):
        direction, sender, text = key
        for d, s, t in window:
            if d == direction and s == sender and similar(t, text):
                return True
        return False

    def _load_window(self, dirname):
        """第一次写这个会话时，把盘上最近的记录读回去重窗口。

        读最新的 _RELOAD 个文件，不是「今天 + 昨天」：程序可能停好几天再开，按日期取会取空。
        按文件名升序遍历，deque 的 maxlen 就自然把**最新**的那些留在窗口里。
        """
        if dirname in self._loaded:
            return
        self._loaded.add(dirname)
        window = self._recent.setdefault(dirname, deque(maxlen=_WINDOW))
        folder = os.path.join(self.root, dirname)
        try:
            names = sorted(f for f in os.listdir(folder) if f.endswith(".csv"))
        except OSError:
            return  # 目录还不存在 = 第一次记这个会话，正常
        for fn in names[-_RELOAD:]:
            for row in self._tail(os.path.join(folder, fn)):
                window.append(row)

    @staticmethod
    def _tail(path):
        """一个 CSV 里最后 _WINDOW 条的去重键。CSV 的引号能包住换行，没法从文件尾倒着读，
        所以整份读——一天一个文件、几 MB 量级，而且每个会话只在第一次写的时候读一次。"""
        out = deque(maxlen=_WINDOW)
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                next(reader, None)  # 表头
                for row in reader:
                    if len(row) >= 4:
                        out.append((row[1], row[2], row[3]))
        except (OSError, csv.Error, UnicodeDecodeError):
            return []
        return list(out)

    def _claim_sent(self, title, text):
        """这条「我」的消息是不是程序刚发的。命中就划掉——一次只认领一条，免得同一句话认领两次。"""
        now = self._clock()
        for i, (t, sent, at) in enumerate(self._sent):
            if t != title or now - at > _SENT_TTL:
                continue
            if similar(sent, text):
                self._sent.pop(i)
                return True
        return False

    def _note(self, msg):
        if msg in self._noted:
            return
        self._noted.add(msg)
        self.problems.append(msg)
