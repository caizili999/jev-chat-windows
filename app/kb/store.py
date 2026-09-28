# -*- coding: utf-8 -*-
"""知识库存储：程序目录下 `知识库/` 里的几份纯 JSON（对齐上游 core/kb/KbStore.kt）。

    知识库/notes.json                    全部笔记
    知识库/contacts.json                 全部联系人
    知识库/logs/<contactId>.json         每个联系人的聊天历史（≤ MAX_LOG 条）
    知识库/logs/<contactId>.screen.json  上一次写进去的那一屏的比对键

单写者：每个读写都过同一把锁，写盘走「临时文件 + rename」，写到一半被强杀也不会留下半份
JSON。序列化是手写的（不引第三方库）。**聊天正文永不进日志**——problems 里只出现文件名、
条数、长度这类结构信息。

**不依赖 Qt**，能单独离线测（见 tools/check_kb.py）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import replace

from app.kb.models import Contact, KbCounts, LogEntry, Note

MAX_LOG = 300   # 每个联系人最多留多少条历史


# ── 名字归一化：上游最脏的那块，踩过真机的坑 ────────────────────────────────────
#
# 上游的教训：`Regex(...)` 写在 Kotlin 的 `val` 里会在 `<clinit>` 阶段执行，一个坏正则
# 变成 ExceptionInInitializerError，**整个类连带每一次分析全挂**（Android 的 ICU 拒绝了
# 老的人数正则 `[(...)]` 写法）。Python 的 re 没有这个毛病，但结构照留：编译失败只意味着
# 跳过那一步清洗，绝不能把整条链拖死。

def _safe_regex(pattern: str):
    """编译一个正则；失败返回 None（调用方降级到 trim + 小写）。"""
    try:
        return re.compile(pattern)
    except re.error:
        return None


_ZERO_WIDTH = _safe_regex("[\u200b-\u200d\ufeff]")

# 尾部的人数。写成转义码点的择一分支，而不是把括号放进字符类：
# 设备上的 ICU 把 `[(...)]` 读成没闭合的字符类（"missing closing bracket"）。
# 源码里故意不出现字面的全角括号。
_TRAILING_COUNT = _safe_regex(r"\s*(?:\(|\uFF08)\s*\d+\s*(?:\)|\uFF09)\s*$")


def _strip_zero_width(s: str) -> str:
    return _ZERO_WIDTH.sub("", s) if _ZERO_WIDTH else s


def _strip_trailing_count(s: str) -> str:
    return _TRAILING_COUNT.sub("", s).strip() if _TRAILING_COUNT else s


def normalize_name(s) -> str:
    """匹配用的名字键：去首尾空白、去零宽字符、去掉群人数 `(12)`（半角或全角）、转小写。

    两个正则都没编译出来时降级成「trim + 小写」——少一步清洗，但绝不抛。
    """
    if not s:
        return ""
    return _strip_trailing_count(_strip_zero_width(str(s)).strip()).strip().lower()


def display_name(s) -> str:
    """跟 normalize_name 同样的清洗，但**保留原始大小写**，用来显示。"""
    if not s:
        return ""
    return _strip_trailing_count(_strip_zero_width(str(s)).strip()).strip()


def normalize_text(s) -> str:
    """子串匹配用的宽松键（**不去人数**——正文里 `(12)` 是有意义的）。"""
    if not s:
        return ""
    return _strip_zero_width(str(s)).strip().lower()


def new_id() -> str:
    """12 位十六进制随机 id（上游是 UUID 前 12 位）。"""
    return uuid.uuid4().hex[:12]


def _now_ms() -> int:
    return int(time.time() * 1000)


class KbStore:
    """一个进程一个实例，由 main.py 持有。写盘出错只记进 problems，不往外抛。"""

    def __init__(self, root: str, now=_now_ms):
        self.root = root          # 就是那个「知识库」目录本身
        self._now = now
        self._lock = threading.RLock()   # 可重入：counts() 里会再调 load_log
        self._notes_cache = None
        self._contacts_cache = None
        self._log_cache = {}             # {contact_id: [LogEntry]}
        self._last_screen_cache = {}     # {contact_id: [str]}
        self._unreadable = set()         # 解析不了又移不走的文件：永不覆盖
        self.problems = []
        self._noted = set()

    # ── 路径 ────────────────────────────────────────────────────────────────

    @property
    def notes_file(self) -> str:
        return os.path.join(self.root, "notes.json")

    @property
    def contacts_file(self) -> str:
        return os.path.join(self.root, "contacts.json")

    def log_file(self, contact_id: str) -> str:
        return os.path.join(self.root, "logs", f"{contact_id}.json")

    def screen_file(self, contact_id: str) -> str:
        return os.path.join(self.root, "logs", f"{contact_id}.screen.json")

    # ── 笔记 ────────────────────────────────────────────────────────────────

    def notes(self) -> list:
        with self._lock:
            return list(self._load_notes())

    def note(self, note_id: str):
        with self._lock:
            for n in self._load_notes():
                if n.id == note_id:
                    return n
            return None

    def save_note(self, note: Note) -> bool:
        """按 id 插入或替换。返回是否真的落到盘上。"""
        with self._lock:
            lst = self._load_notes()
            stamped = note.stamped(self._now())
            for i, n in enumerate(lst):
                if n.id == note.id:
                    lst[i] = stamped
                    break
            else:
                lst.append(stamped)
            ok = self._write_atomic(self.notes_file, _notes_json(lst))
            if not ok:
                self._notes_cache = None   # 内存不能声称一次没成功的写入
            return ok

    def delete_note(self, note_id: str) -> bool:
        with self._lock:
            lst = self._load_notes()
            kept = [n for n in lst if n.id != note_id]
            if len(kept) == len(lst):
                return True
            lst[:] = kept
            ok = self._write_atomic(self.notes_file, _notes_json(lst))
            if not ok:
                self._notes_cache = None
            return ok

    # ── 联系人 ──────────────────────────────────────────────────────────────

    def contacts(self) -> list:
        with self._lock:
            return list(self._load_contacts())

    def contact(self, contact_id: str):
        with self._lock:
            for c in self._load_contacts():
                if c.id == contact_id:
                    return c
            return None

    def save_contact(self, c: Contact) -> bool:
        with self._lock:
            lst = self._load_contacts()
            stamped = c.stamped(self._now())
            for i, old in enumerate(lst):
                if old.id == c.id:
                    lst[i] = stamped
                    break
            else:
                lst.append(stamped)
            ok = self._write_atomic(self.contacts_file, _contacts_json(lst))
            if not ok:
                self._contacts_cache = None
            return ok

    def delete_contact(self, contact_id: str) -> bool:
        """删联系人**连同它的历史文件**。"""
        with self._lock:
            lst = self._load_contacts()
            ok = True
            kept = [c for c in lst if c.id != contact_id]
            if len(kept) != len(lst):
                lst[:] = kept
                ok = self._write_atomic(self.contacts_file, _contacts_json(lst))
                if not ok:
                    self._contacts_cache = None
            self._log_cache.pop(contact_id, None)
            self._last_screen_cache.pop(contact_id, None)
            _silent_unlink(self.log_file(contact_id))
            _silent_unlink(self.screen_file(contact_id))
            return ok

    def find_contact(self, title, app: str = ""):
        """会话标题 → 联系人（按归一化后的名字或别名精确匹配）。

        **从不创建**：认不出的标题就是没有联系人（上游 v1.3 的改动——联系人只由用户建立）。

        app 只用于「两个都命中时优先选已经见过这个来源的那个」。
        """
        with self._lock:
            want = normalize_name(title)
            if not want:
                return None
            hits = [c for c in self._load_contacts()
                    if normalize_name(c.name) == want
                    or any(normalize_name(a) == want for a in c.aliases)]
            if not hits:
                return None
            for c in hits:
                if app and app in c.apps:
                    return c
            return hits[0]

    def save_or_merge_contact(self, title, app: str = "") -> str:
        """把当前会话标题存成联系人；已经能匹配上的就并进去。返回给用户看的一句话。"""
        display = display_name(title)
        if not display:
            return "当前会话没有标题，存不了"
        existing = self.find_contact(title, app)
        if existing is None:
            raw = str(title or "").strip()
            aliases = [raw] if display_name(title) != raw and raw else []
            self.save_contact(Contact(id=new_id(), name=display, aliases=aliases,
                                      apps=[app] if app else []))
            return f"已存为联系人「{display}」"
        apps = list(existing.apps)
        if app and app not in apps:
            apps.append(app)
        raw = str(title or "").strip()
        known = [normalize_name(existing.name)] + [normalize_name(a) for a in existing.aliases]
        aliases = list(existing.aliases)
        # ⚠️ 上游遗留：这个分支**永远不成立**。能走到「并入」就说明 normalize_name(title)
        # 已经匹配上了某个联系人，那它必然在 known 里。结果就是「一键存」记录不下新的拼写，
        # 别名只能靠设置页手动编辑。照上游原样保留，没有擅自「修好」。
        if raw and normalize_name(raw) not in known:
            aliases.append(raw)
        if apps == existing.apps and aliases == existing.aliases:
            return f"联系人「{existing.name}」已存在"
        self.save_contact(replace(existing, apps=apps, aliases=aliases))
        return f"已并入联系人「{existing.name}」"

    # ── 历史 ────────────────────────────────────────────────────────────────

    def append_log(self, contact_id: str, entries, screen_batch: bool = True) -> bool:
        """追加「一屏消息」，只留最新 MAX_LOG 条。

        **单位是一整个序列，不是一堆行。** 一次采集给出的是当前可见的整屏 S（从上到下）；
        P 是这个联系人上一次写进去的那一屏。同一屏里两行一样的短消息是两个位置、记两条——
        不合并，也不会因为「太短没法去重」或者「以前说过」被丢掉。

        规则，按顺序：
          - S 等于 P                → 又是同一屏，什么都不写。
          - log 是空的              → 整屏都写。
          - log 尾部匹配 S 的前 k 行（k > 0）→ 屏幕向上滚了 (S.size - k) 行，只追加新出来的尾巴。
          - k 是 0 且 S 跟 P 毫无交集 → 用户往上翻到了我们早就存过的旧消息；
            这一轮什么都不写，而不是把旧历史在文件末尾再抄一遍。
          - 其它                    → 整屏都追加。

        screen_batch=False 是「手写注入一条、这不是一次屏幕采集」，原样追加。
        """
        if not entries:
            return True
        with self._lock:
            screen = [e for e in entries if e.text and e.text.strip()]
            if not screen:
                return True
            lst = self._load_log(contact_id)
            keys = [_key(e.side, e.text) for e in screen]
            prev = self._load_last_screen(contact_id) if screen_batch else []
            prev_set = set(prev)   # 下面那步「跟上一屏毫无交集」要按集合查，别每条都重建

            # 跟上次同一屏：没发生值得记的事。
            if screen_batch and prev and prev == keys:
                return True

            # log 尾部已经覆盖了 S 的前多少行。
            k = 0
            max_k = min(len(lst), len(keys))
            for cand in range(max_k, 0, -1):
                match = True
                for i in range(cand):
                    e = lst[len(lst) - cand + i]
                    if _key(e.side, e.text) != keys[i]:
                        match = False
                        break
                if match:
                    k = cand
                    break

            if not screen_batch:
                tail = screen
            elif not lst:
                tail = screen
            elif k > 0:
                tail = screen[k:]
            elif prev and not any(kk in prev_set for kk in keys):
                # 跟上次那一屏毫无交集。**这里其实分不清两种情况**：
                #   ① 用户往上翻，在看我们早就存过的旧消息 → 不该在文件尾再抄一遍；
                #   ② 两次分析之间一口气涌进来一整屏以上的新消息 → 本该记下来。
                # 两种长得一模一样，上游按 ① 处理，照旧不写。
                #
                # 但**「上一屏」标记必须跟着更新**。不更新的话它会永远停在旧屏上，
                # 于是之后每一轮都零交集、每一轮都走这个分支，日志从此再不增长
                # ——真踩过：一口气聊了一屏多，日志 80 分钟一条没记，界面永远显示 18 条。
                # 更新之后，下一轮只要屏幕是平滑前进的（跟这一屏有交集），就会正常补上。
                self._note(f"append_log: contact={contact_id} 跳过这一屏"
                           f"（跟上一屏零交集，可能是在往上翻）；上一屏标记已更新")
                self._save_last_screen(contact_id, keys)
                return True
            else:
                tail = screen

            if not tail:
                if screen_batch:
                    self._save_last_screen(contact_id, keys)
                return True

            lst.extend(tail)
            while len(lst) > MAX_LOG:
                lst.pop(0)
            ok = self._write_atomic(self.log_file(contact_id), _log_json(lst))
            if not ok:
                self._log_cache.pop(contact_id, None)
            if ok and screen_batch:
                self._save_last_screen(contact_id, keys)
            return ok

    def recent_log(self, contact_id: str, n: int) -> list:
        """最新的 n 条，旧→新。"""
        if n <= 0:
            return []
        with self._lock:
            lst = self._load_log(contact_id)
            return list(lst) if len(lst) <= n else list(lst[len(lst) - n:])

    def log_size(self, contact_id: str) -> int:
        with self._lock:
            return len(self._load_log(contact_id))

    def clear_log(self, contact_id: str) -> None:
        with self._lock:
            self._log_cache.pop(contact_id, None)
            self._last_screen_cache.pop(contact_id, None)
            _silent_unlink(self.log_file(contact_id))
            _silent_unlink(self.screen_file(contact_id))

    # ── 管理 ────────────────────────────────────────────────────────────────

    def counts(self) -> KbCounts:
        with self._lock:
            contacts = self._load_contacts()
            lines = sum(len(self._load_log(c.id)) for c in contacts)
            return KbCounts(len(self._load_notes()), len(contacts), lines)

    def clear_all(self) -> None:
        """清掉知识库的每一个文件。只删 `知识库/` 目录——密钥、聊天记录、config.json 都不碰。"""
        with self._lock:
            self._notes_cache = None
            self._contacts_cache = None
            self._log_cache.clear()
            self._last_screen_cache.clear()
            shutil.rmtree(self.root, ignore_errors=True)
            self._unreadable.clear()

    def take_problems(self) -> list:
        """取走攒下的提示（取完清空）。上层拿去写日志/弹一次状态栏。"""
        out, self.problems = self.problems, []
        return out

    def now(self) -> int:
        """当前时间（毫秒）。暴露出来是因为 context 层要给新历史打时间戳，而时钟是可注入的。"""
        return self._now()

    # ── 上一屏 ──────────────────────────────────────────────────────────────

    def _load_last_screen(self, contact_id: str) -> list:
        """这个联系人上次写进去的那一屏的 (side, text) 序列，作为比对键。

        内存和磁盘都留一份：重新打开一个一直没动过的会话时，才不会把同一屏又追加一遍。
        """
        if contact_id in self._last_screen_cache:
            return self._last_screen_cache[contact_id]
        out = []
        arr, trustworthy = self._read_json_array(self.screen_file(contact_id))
        if isinstance(arr, list):
            out = [s for s in (str(x) for x in arr) if s]
        if trustworthy:
            self._last_screen_cache[contact_id] = out
        return out

    def _save_last_screen(self, contact_id: str, keys) -> None:
        self._last_screen_cache[contact_id] = list(keys)
        if not self._write_atomic(self.screen_file(contact_id),
                                  json.dumps(list(keys), ensure_ascii=False)):
            self._last_screen_cache.pop(contact_id, None)

    # ── 读 ──────────────────────────────────────────────────────────────────

    def _load_notes(self) -> list:
        if self._notes_cache is not None:
            return self._notes_cache
        out = []
        arr, trustworthy = self._read_json_array(self.notes_file)
        if isinstance(arr, list):
            for o in arr:
                if isinstance(o, dict):
                    out.append(Note.from_json(o, new_id))
        if trustworthy:
            self._notes_cache = out
        return out

    def _load_contacts(self) -> list:
        if self._contacts_cache is not None:
            return self._contacts_cache
        out = []
        arr, trustworthy = self._read_json_array(self.contacts_file)
        if isinstance(arr, list):
            for o in arr:
                if isinstance(o, dict):
                    out.append(Contact.from_json(o, new_id))
        if trustworthy:
            self._contacts_cache = out
        return out

    def _load_log(self, contact_id: str) -> list:
        if contact_id in self._log_cache:
            return self._log_cache[contact_id]
        out = []
        arr, trustworthy = self._read_json_array(self.log_file(contact_id))
        if isinstance(arr, list):
            for o in arr:
                if isinstance(o, dict):
                    out.append(LogEntry.from_json(o))
        if trustworthy:
            self._log_cache[contact_id] = out
        return out

    def _read_json_array(self, path: str):
        """读一份 JSON 数组，返回 (arr_or_None, trustworthy)。

        trustworthy 只在一种恶劣情形下为 False：文件在、解析不了、**而且移不走**。
        那时「空列表」只是猜的，所以既不能进缓存、更不能覆盖用户的数据。
        """
        if not os.path.exists(path):
            self._unreadable.discard(os.path.abspath(path))
            return None, True
        try:
            with open(path, encoding="utf-8") as f:
                arr = json.load(f)
        except Exception:  # noqa: BLE001 —— 任何读/解析失败都算「坏文件」，往下走保全流程
            # 损坏的文件改成一个带时间戳的名字留着，而不是让下一次保存无声地盖掉它。
            # 只有原件真的保全了，「从空开始」才是安全的。
            backup = os.path.join(os.path.dirname(path),
                                  f"{os.path.basename(path)}.corrupt.{self._now()}")
            try:
                os.replace(path, backup)
                kept = True
            except OSError:
                kept = False
            if kept:
                self._unreadable.discard(os.path.abspath(path))
                self._note(f"知识库文件解析不了，已备份为 {os.path.basename(backup)}")
            else:
                self._unreadable.add(os.path.abspath(path))
                self._note(f"知识库文件 {os.path.basename(path)} 解析不了又移不走，"
                           "已停止写入以免覆盖，请手动检查")
            return None, kept
        return arr, True

    # ── 写 ──────────────────────────────────────────────────────────────────

    def _write_atomic(self, path: str, text: str) -> bool:
        """临时文件 + rename，崩溃也留不下半份文档。

        rename 是一步**替换**掉目标（同一目录内），先删旧文件再写会在中间被杀时全丢。
        返回 False = 数据没落到盘上；调用方会把自己的缓存丢掉，下次读回到文件。
        """
        if os.path.abspath(path) in self._unreadable:
            self._note(f"拒绝覆盖解析不了的文件 {os.path.basename(path)}")
            return False
        folder = os.path.dirname(path) or "."
        tmp = os.path.join(folder, os.path.basename(path) + ".tmp")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # 个别文件系统（网络盘）上 fsync 会报错；原子性来自 rename，不来自它
        except OSError as e:
            _silent_unlink(tmp)
            self._note(f"知识库写不进去（{getattr(e, 'strerror', None) or e}）：{folder}")
            return False
        try:
            os.replace(tmp, path)
            return True
        except OSError:
            pass
        # 同目录 rename 不该失败。真失败了，原地覆盖是仅剩的一招——不是原子的，所以要说出来。
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._note(f"rename 失败，已原地覆盖 {os.path.basename(path)}（非原子写）")
            return True
        except OSError as e:
            self._note(f"知识库写不进去（{getattr(e, 'strerror', None) or e}）：{path}")
            return False
        finally:
            _silent_unlink(tmp)

    def _note(self, msg: str) -> None:
        if msg in self._noted:
            return
        self._noted.add(msg)
        self.problems.append(msg)


# ── 序列化：字段名跟上游逐字对齐，方便两边对读 ──────────────────────────────

def _notes_json(lst) -> str:
    return json.dumps([n.to_json() for n in lst], ensure_ascii=False)


def _contacts_json(lst) -> str:
    return json.dumps([c.to_json() for c in lst], ensure_ascii=False)


def _log_json(lst) -> str:
    return json.dumps([e.to_json() for e in lst], ensure_ascii=False)


def _key(side: str, text: str) -> str:
    """比对键。上游用 NUL 分隔，因为 side 和 text 都可能含任意字符。"""
    return side + "\u0000" + text


def _silent_unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
