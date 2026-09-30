# -*- coding: utf-8 -*-
"""用户现在在不在电脑前。

自动发送是这个工具里唯一会**抢前台 + 挪鼠标**的动作（`fill()` 要先
`SetForegroundWindow` 才能把字打进去）。人正坐在电脑前打字时，那一抢就是实打实的打扰：
他可能正在别的窗口里敲东西，焦点被夺走的那几个字就打飞了。

所以独立窗口那条路加了一道「你走开了才发」的闸。判据用 `GetLastInputInfo`——
它数的是**全系统**的键盘/鼠标输入，不需要装钩子、不需要管理员权限、也读不到你按了什么键
（只有「最后一次输入距现在多久」这一个数字）。

⚠️ **这一整套只对独立窗口的会话生效**（见 main.auto_send_reply）。没有独立窗口的老用户
走的是主窗口那条路，行为一个字都不能变——那是这个项目最硬的约束。
"""
import ctypes
import ctypes.wintypes as wt

# 多久没碰键鼠算「走开了」。8 秒是设计文档 D8 定的值：短于它，人还在屏幕前；
# 长于它，多半是真走开了。比 auto_send_delay 的默认值（5 秒）长，所以「倒计时走完那一刻
# 你还坐在电脑前」是会被拦住的——这正是想要的效果。
USER_IDLE_SECONDS = 8.0

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def elapsed(now_ms: int, last_ms: int) -> float:
    """两个 32 位 tick 之间差了几秒。**必须带掩码**：`GetTickCount` 49.7 天回绕一次，
    直接相减会在回绕那一刻得到一个巨大的负数（-49.7 天），把「刚动过鼠标」算成
    「走了 49 天」——那是**放行**方向，正好是最危险的方向。"""
    return ((int(now_ms) - int(last_ms)) & 0xFFFFFFFF) / 1000.0


def idle_seconds() -> float:
    """距上一次键盘/鼠标输入过了几秒。

    拿不到（API 失败、结构体没填上）一律返回 **0.0** = 「用户正在操作」——
    这个函数的输出决定要不要抢前台，认不准就往「别抢」的方向倒。
    """
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    if not info.dwTime:
        return 0.0
    return elapsed(_kernel32.GetTickCount(), info.dwTime)


def user_is_away(threshold: float = USER_IDLE_SECONDS) -> bool:
    """用户是不是走开了（这么久没碰键鼠）。"""
    return idle_seconds() >= threshold
