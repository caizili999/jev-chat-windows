# -*- coding: utf-8 -*-
"""该盯哪几个微信窗口、每个窗口叫什么会话。

程序原来只盯**一个**窗口——标题「微信」的主窗口。想同时盯多个聊天，
唯一的物理路径是微信 4.0.5+ 的**独立聊天窗口**（右键会话 → 在独立窗口中打开），
每个都是独立顶层窗口，可以各自截图。另外两条路都排除掉了：主动点会话列表会清掉用户的
红点并抢鼠标；读本地数据库已被律师函 / DMCA 1201 封杀。详见 docs/DESIGN_MULTIWINDOW.md。

**这个模块只用 stdlib。** 判断逻辑全是纯函数（`pick_chat_windows()` / `choose_targets()`），
所以能离线测（见 tools/check_windows.py）；只有 `enum_wechat_windows()` 碰 ctypes。

两处实测得来、反直觉的事实（都写进注释了，别再踩）：
1. **独立窗口的会话名要取 Windows 窗口标题，不能 OCR 头部。**
   `ocr.read_title()` 在独立窗口上返回垃圾（实测 `'口'`/`'一'`）——独立窗口的自定义标题栏
   （⊙ 订阅号图标 + 最小化/关闭按钮）**落在 WGC 捕获的客户区内**，头部裁剪把标题栏也框了进去。
   窗口标题反而是准的、免费的：`老婆` / `1群` / `程序员烧烤🦞技术交流群v3.0`。
2. **主窗口不一定在屏幕上。** 实测收托盘时它是个 `160x28 @(-32000,-32000)` 的窗口，
   `IsWindowVisible` 照样返回 True。所以尺寸阈值是必需的，不能只靠可见性。
"""
import ctypes
import ctypes.wintypes as wt
import os
import re

# 主窗口的标题。它不是「会话」，是装会话列表的壳。
_MAIN_TITLE = "微信"

# 微信里**不是聊天**的窗口标题（小写比对）。看图窗是实测确认存在的一个；
# 其余是兜底：IME 那类窗口属于输入法，标题可能被带进来。
_NON_CHAT_TITLES = {
    "微信", "weixin",
    "图片和视频",
    "default ime", "msctfime ui", "hintwnd",
}

# 尺寸下限。独立窗口实测 614x648；收托盘的主窗口是 160x28。
# 取 260x240 是因为 capture.chat_area() 自己也有下限（宽 <100 或消息区高 <40 就返回 None），
# 比它宽松一点，真正的判据留给 chat_area()。
MIN_W, MIN_H = 260, 240

_ZERO_WIDTH = re.compile(r"[\u200B-\u200D\uFEFF]")
_WECHAT_EXES = ("weixin.exe", "wechat.exe")


def _as_text(title) -> str:
    """窗口标题只可能是 str，非 str 一律当空串——**刻意不做 `str()` 强转**。

    强转出来的 `"123"` 是一个**看起来合法**的会话名，会一路建出一个幽灵会话
    （独立的 readers / chats / 授权 / 队列）；空串则会被 `looks_like_chat_window()`
    直接丢掉。这个模块里会话名是**所有状态的 key**，方向永远是「宁可不要这个窗口」。
    """
    return title if isinstance(title, str) else ""


def normalize_title(title) -> str:
    """窗口标题 → 比对用的键：去掉零宽字符、trim、小写。

    跟 app.kb.store.normalize_name 同一个口径（那边还要剥群人数后缀，这里不剥——
    窗口标题本来就不带成员数，实测 `1群` 就是 `1群`）。
    """
    return _ZERO_WIDTH.sub("", _as_text(title)).strip().lower()


def session_name(title) -> str:
    """窗口标题 → 会话显示名。只做「去零宽 + trim」，**不**小写（要拿去显示）。"""
    return _ZERO_WIDTH.sub("", _as_text(title)).strip()


def is_main_window(title) -> bool:
    return normalize_title(title) == _MAIN_TITLE


def _as_size(value) -> int:
    """尺寸只可能是 int，脏值当 0（= 太小，直接排除）。bool 是 int 的子类，先排掉。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def looks_like_chat_window(title, w, h, visible=True) -> bool:
    """便宜的粗筛：不截图就能判的几件事。**不是最终判据**——
    真正的判据是「这一帧能不能过 capture.chat_area()」，那个要截图才知道。

    为什么值得先粗筛：枚举每 ~1 秒跑一次，截图 + 跑 chat_area 要几十毫秒，
    而屏幕上大多数窗口（主窗口、看图窗、工具窗）一眼就能排除。

    脏值一律往「排除」的方向倒（非 str 的标题当空、非 int 的尺寸当 0）：
    这个函数的输出决定后面要不要为它开一套采集，认不准就不开。
    """
    if not visible:
        return False
    name = normalize_title(title)
    if not name or name in _NON_CHAT_TITLES:
        return False
    return _as_size(w) >= MIN_W and _as_size(h) >= MIN_H


def pick_chat_windows(raw) -> list:
    """粗筛出候选聊天窗口，**按会话名去重**。

    raw: [{"hwnd":…, "title":…, "visible":bool, "w":…, "h":…}, …]

    同一个会话开了两个窗口（理论上微信不让，但不值得赌）时**只留第一个**，
    其余丢掉——会话名是后面所有状态的 key（readers / chats / 授权 / 队列），
    同一个 key 挂两套采集会互相覆盖，比少盯一个窗口糟得多。
    丢掉的由调用方从 `choose_targets()` 的 dropped 里拿到，可以记一行日志。
    """
    out, seen = [], {}
    for item in raw or []:
        if not looks_like_chat_window(item.get("title"), item.get("w", 0), item.get("h", 0),
                                      item.get("visible", True)):
            continue
        key = normalize_title(item.get("title"))
        if key in seen:
            seen[key]["dups"] += 1
            continue
        entry = dict(item)
        entry["session"] = session_name(item.get("title"))
        # 独立窗口的会话名就是窗口标题。主窗口不走这条路（它的标题永远是「微信」），
        # 但主窗口不会出现在这里——它被 _NON_CHAT_TITLES 挡住了。
        entry["is_main"] = False
        entry["dups"] = 0
        seen[key] = entry
        out.append(entry)
    return out


def find_main_window(raw) -> dict | None:
    """主窗口。判据跟老的 `capture.find_wechat_hwnd()` **一字不差**：可见的窗口里挑标题「微信」的，
    没有就取第一个可见的微信窗口。

    ⚠️ **刻意不看尺寸**，这是实测踩出来的：微信收托盘时主窗口是 `160x28 @(-32000,-32000)`，
    `IsWindowVisible` 照样返回 True，老代码照样会挑中它（然后 `chat_area()` 认不出来，
    每帧报一句「消息区认不出来」）。加上尺寸阈值就会把这种用户从「采集着但认不出」
    变成「压根没启动、报未找到微信窗口」——那是实打实的行为变化。

    尺寸阈值只该拦**独立窗口的候选**（`pick_chat_windows()` 里那一条），因为那些窗口
    不是老代码挑的、是老代码压根看不见的新东西。
    """
    visible = [item for item in (raw or []) if item.get("visible", True)]
    for item in visible:
        if is_main_window(item.get("title")):
            return _as_main(item)
    # 一个标题「微信」的都没有：老代码取第一个可见的微信窗口（看图窗、工具窗也会被取到）
    return _as_main(visible[0]) if visible else None


def _as_main(item) -> dict:
    """主窗口的会话名**只能 OCR 头部得到**（窗口标题永远是「微信」）。
    留空 + is_main 标记，让采集层知道该走哪条路。"""
    entry = dict(item)
    entry["session"] = ""
    entry["is_main"] = True
    return entry


def choose_targets(raw) -> dict:
    """这一轮该盯谁。返回 {"windows": [...], "fallback": dict|None, "dropped": int}。

    **有独立窗口就只盯独立窗口，主窗口不参与**——否则同一个会话在主窗口和独立窗口
    各开一份会被算两遍（同一份消息进两次分析、两次自动发送）。
    **一个独立窗口都没有时回退到主窗口**，这时行为跟加这个功能之前完全一样——
    「不开独立窗口的老用户一个字节都不变」这条就是在这里守住的。
    """
    chats = pick_chat_windows(raw)
    if chats:
        return {"windows": chats, "fallback": None,
                "dropped": sum(c["dups"] for c in chats)}
    return {"windows": [], "fallback": find_main_window(raw), "dropped": 0}


# ── 下面开始碰 Windows API（上面的纯函数不依赖它）────────────────────────────

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_of(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower() if ok else ""
    finally:
        _kernel32.CloseHandle(handle)


def enum_wechat_windows() -> list:
    """所有微信顶层窗口（**不管可见性**，判断交给纯函数）。

    为什么要连不可见的也枚举：收托盘的主窗口是「可见」但尺寸是 160x28，
    而独立窗口在被别的窗口盖住时仍然可见——真正要区分的是尺寸和标题，
    那些判断都在纯函数里，这里只管把原始事实捞出来。
    """
    rows = []

    def cb(hwnd, _):
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _exe_of(pid.value) in _WECHAT_EXES:
            length = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            rect = wt.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append({
                "hwnd": int(hwnd),
                "title": buf.value,
                "visible": bool(_user32.IsWindowVisible(hwnd)),
                "w": int(rect.right - rect.left),
                "h": int(rect.bottom - rect.top),
            })
        return True

    _user32.EnumWindows(_ENUMPROC(cb), 0)
    return rows


def discover() -> dict:
    """枚举 + 决策一步到位。找不到任何窗口时返回空 targets（调用方据此报「微信开着吗」）。"""
    return choose_targets(enum_wechat_windows())


def alive(hwnd) -> bool:
    """这个窗口句柄还有效吗（窗口没被关掉）。

    自动发送前要用它再确认一次。为什么不能只信 `state["wins"]`：那是子进程 ~1 秒前报上来的，
    这中间用户完全可能把窗口关掉了——对着一个没了的窗口按发送键，轻则报错、重则打到别的窗口上。
    """
    return bool(hwnd) and bool(_user32.IsWindow(hwnd))


def title_of(hwnd) -> str:
    """窗口标题原文。

    **这是「往错群发消息」的唯一防线**：句柄还有效、但那个窗口已经不是原来那个会话了
    （独立窗口被关掉之后微信把同一个 hwnd 复用给了别的会话，或者用户手动改了群名）。
    光靠「句柄还在」不够，必须把标题读回来跟生成候选时的会话名对一遍。
    """
    if not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value
