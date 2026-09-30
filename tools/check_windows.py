# -*- coding: utf-8 -*-
"""窗口发现的离线检查。跑：python tools/check_windows.py

**不起 Qt、不截图、不联网、不碰微信**——`app/windows.py` 的判断逻辑全是纯函数，
这里就喂假窗口列表进去。只有最后一组会真枚举一次（微信没开时应当优雅返回空，不抛）。

钉住的是「多窗口」这件事上最容易悄悄错的三处：
1. 把不是聊天的窗口（主窗口 / 看图窗 / 工具窗 / 收托盘那个 160x28）当成会话；
2. 同一个会话开两个窗口时**不**去重 —— 会话名是后面所有状态的 key，
   同一个 key 挂两套采集会互相覆盖；
3. 主窗口的回退条件写错 —— 「不开独立窗口的老用户行为逐字节不变」全靠这一条。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 输出强制 UTF-8：Windows 控制台代码页可能是 cp936，CI 上可能是 cp1252，
# cp1252 编不出中文会直接在 print 那行 UnicodeEncodeError。跟其它 check_*.py 同一写法。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.windows import (MIN_H, MIN_W, choose_targets, discover,  # noqa: E402
                         looks_like_chat_window, normalize_title,
                         pick_chat_windows, session_name)

# 实测抓到的真实窗口（尺寸也是实测的），拿它们当夹具比编数据可信
MAIN_TRAY = {"hwnd": 67238, "title": "微信", "visible": True, "w": 160, "h": 28}
MAIN_OPEN = {"hwnd": 67239, "title": "微信", "visible": True, "w": 1440, "h": 753}
POP_GROUP = {"hwnd": 920244, "title": "程序员烧烤🦞技术交流群v3.0", "visible": True,
             "w": 614, "h": 648}
POP_1Q = {"hwnd": 527032, "title": "1群", "visible": True, "w": 614, "h": 648}
POP_WIFE = {"hwnd": 1903470, "title": "老婆", "visible": True, "w": 614, "h": 648}
TOOL_WIN = {"hwnd": 67322, "title": "Weixin", "visible": True, "w": 176, "h": 199}
IMG_WIN = {"hwnd": 111111, "title": "图片和视频", "visible": True, "w": 900, "h": 700}
IME_WIN = {"hwnd": 132834, "title": "Default IME", "visible": True, "w": 0, "h": 0}


def check_non_chat_is_excluded():
    """不是聊天的窗口一个都不能被选中。"""
    # 收托盘的主窗口：可见 = True，但尺寸 160x28 —— 只靠 IsWindowVisible 会把它当会话
    assert not looks_like_chat_window("微信", 160, 28, True), "收托盘的主窗口被当成会话了"
    assert not looks_like_chat_window("微信", 1440, 753, True), "主窗口被当成会话了"
    assert not looks_like_chat_window("图片和视频", 900, 700, True), "看图窗被当成会话了"
    assert not looks_like_chat_window("Weixin", 176, 199, True), "工具窗被当成会话了"
    assert not looks_like_chat_window("Default IME", 800, 600, True), "IME 窗被当成会话了"
    # 大小写不敏感：微信进程真会把工具窗叫 Weixin
    assert not looks_like_chat_window("WEIXIN", 800, 600, True)
    # 不可见的一律不算
    assert not looks_like_chat_window("老婆", 614, 648, False), "不可见的窗口被选中了"
    # 空标题
    for bad in ("", "   ", "\u200b", "\u200b \ufeff"):
        assert not looks_like_chat_window(bad, 614, 648, True), f"空标题被选中：{bad!r}"

    picked = pick_chat_windows([MAIN_TRAY, MAIN_OPEN, TOOL_WIN, IMG_WIN, IME_WIN])
    assert picked == [], f"一堆非聊天窗口里选出了会话：{[p['title'] for p in picked]}"
    print("非聊天窗口全部排除 ok（收托盘的主窗口 / 主窗口 / 看图窗 / 工具窗 / IME / 空标题）")


def check_real_windows_are_picked():
    """实测的三个独立窗口必须被选中，会话名必须是窗口标题原文（含 emoji）。"""
    picked = pick_chat_windows([MAIN_TRAY, POP_GROUP, POP_1Q, POP_WIFE])
    names = [p["session"] for p in picked]
    assert names == ["程序员烧烤🦞技术交流群v3.0", "1群", "老婆"], names
    # emoji 不能被吃掉、也不能被小写化（要拿去显示）
    assert "🦞" in names[0], "emoji 被吞了——窗口标题原样才对"
    assert all(p["hwnd"] for p in picked), "hwnd 丢了"
    print("实测的三个独立窗口都选中了 ok（会话名 = 窗口标题原文，emoji 保留）")


def check_duplicate_session_is_deduped():
    """同一个会话开两个窗口：只留一个，且能报出丢掉的个数。

    会话名是 readers / chats / 授权 / 队列的 key，同一个 key 挂两套采集会互相覆盖。
    """
    same = dict(POP_1Q, hwnd=999999)
    picked = pick_chat_windows([POP_1Q, same])
    assert len(picked) == 1, f"同一个会话选出了 {len(picked)} 个窗口"
    assert picked[0]["hwnd"] == POP_1Q["hwnd"], "留下的应当是先出现的那个"
    assert choose_targets([POP_1Q, same])["dropped"] == 1, "丢掉的个数没报出来"

    # 零宽字符和大小写都算同一个会话（微信会往标题里塞零宽字符）
    assert len(pick_chat_windows([POP_1Q, dict(POP_1Q, hwnd=2, title="1群\u200b")])) == 1, \
        "带零宽字符的同一标题没去重"
    assert len(pick_chat_windows([{"hwnd": 1, "title": "KK", "visible": True, "w": 614, "h": 648},
                                  {"hwnd": 2, "title": "kk", "visible": True, "w": 614, "h": 648}])) == 1, \
        "只差大小写的同一标题没去重"
    print("同会话去重 ok（零宽字符 / 大小写都归到同一个 key，丢掉的个数有报）")


def check_main_window_fallback():
    """回退规则：有独立窗口时主窗口**不参与**；一个都没有时才回退到主窗口。

    这一条就是「不开独立窗口的老用户行为逐字节不变」的全部实现。
    """
    # 有独立窗口 → 主窗口不参与
    t = choose_targets([MAIN_OPEN, POP_1Q, POP_WIFE])
    assert t["fallback"] is None, "有独立窗口时主窗口不该参与（同一会话会被算两遍）"
    assert len(t["windows"]) == 2, t["windows"]
    assert all(w["hwnd"] != MAIN_OPEN["hwnd"] for w in t["windows"]), "主窗口混进来了"

    # 没有独立窗口 → 回退到可见的主窗口
    t = choose_targets([MAIN_OPEN, TOOL_WIN, IMG_WIN])
    assert t["windows"] == [], t["windows"]
    assert t["fallback"] is not None and t["fallback"]["hwnd"] == MAIN_OPEN["hwnd"], \
        "没有独立窗口时必须回退到主窗口，否则老用户什么都监控不到"
    assert t["fallback"]["is_main"] is True and t["fallback"]["session"] == "", \
        "回退目标的会话名必须留空 + 打 is_main 标记（它只能靠 OCR 头部得到会话名）"

    # ⚠️ 收托盘那个 160x28 的主窗口**照样是**回退目标——这是老代码的行为，必须原样保留。
    # 老 find_wechat_hwnd() 挑的是「可见 + 标题微信」，压根不看尺寸；它挑中之后
    # chat_area() 认不出来、每帧报一句「消息区认不出来」。加上尺寸阈值就会把这种用户
    # 从「采集着但认不出」变成「压根没启动、报未找到微信窗口」——那是实打实的行为变化。
    t = choose_targets([MAIN_TRAY, TOOL_WIN])
    assert t["fallback"] is not None and t["fallback"]["hwnd"] == MAIN_TRAY["hwnd"], \
        "收托盘的主窗口仍然是回退目标（跟老 find_wechat_hwnd 一致，尺寸由 chat_area 去否决）"

    # 一个标题「微信」的都没有 → 取第一个可见的微信窗口（老代码的兜底，一字不差）
    t = choose_targets([TOOL_WIN, IMG_WIN])
    assert t["fallback"] is not None and t["fallback"]["hwnd"] == TOOL_WIN["hwnd"], \
        "没有标题「微信」的窗口时该退回第一个可见的微信窗口（老代码的兜底）"

    # 不可见的一律不当回退目标（老代码在枚举回调里就按 IsWindowVisible 过滤了）
    t = choose_targets([dict(MAIN_OPEN, visible=False)])
    assert t["fallback"] is None, "不可见的窗口不该当回退目标"

    # 什么都没有
    t = choose_targets([])
    assert t == {"windows": [], "fallback": None, "dropped": 0}, t
    # 只有不可见的窗口 = 跟「什么都没有」一样（老代码在枚举回调里就按 IsWindowVisible 过滤了）
    t = choose_targets([dict(MAIN_OPEN, visible=False), dict(TOOL_WIN, visible=False)])
    assert t["windows"] == [] and t["fallback"] is None, t
    print("主窗口回退 ok（有独立窗口不参与 / 没有才回退 / 收托盘与兜底都跟老代码一致 / 空列表不炸）")


def check_size_boundary():
    """尺寸边界要正好卡在 MIN_W / MIN_H 上，不能差一像素就放行。"""
    assert looks_like_chat_window("X", MIN_W, MIN_H, True), "正好到下限的应当通过"
    assert not looks_like_chat_window("X", MIN_W - 1, MIN_H, True), "差一像素宽就放行了"
    assert not looks_like_chat_window("X", MIN_W, MIN_H - 1, True), "差一像素高就放行了"
    assert not looks_like_chat_window("X", 0, 0, True)
    assert not looks_like_chat_window("X", -100, 600, True)
    print(f"尺寸边界 ok（{MIN_W}x{MIN_H} 通过，各差一像素都拦住）")


def check_name_helpers():
    """会话名 / 比对键的归一化口径，以及脏值一律倒向「排除」。"""
    assert session_name("  老婆  ") == "老婆", "会话名要 trim"
    assert session_name("1群\u200b") == "1群", "会话名要去零宽字符"
    # **非 str 不许 str() 强转**：强转出来的 "123" 是个看起来合法的会话名，
    # 会一路建出一个幽灵会话（独立的 readers / chats / 授权 / 队列）。空串才会被丢掉。
    assert session_name(None) == "" and session_name(123) == "", "脏值必须当空，不能强转"
    assert session_name(["1群"]) == "", "列表这类脏值也必须当空"
    assert normalize_title(123) == "" and normalize_title(None) == ""
    # 比对键小写、会话名保留原样——这两件事不能混（显示要原样，去重要小写）
    assert normalize_title("KK") == "kk" and session_name("KK") == "KK"
    assert normalize_title("程序员烧烤🦞技术交流群v3.0") == "程序员烧烤🦞技术交流群v3.0"
    # 脏尺寸不能炸（`w >= MIN_W` 拿字符串比会 TypeError）
    for dirty in (None, "614", [614], True):
        assert not looks_like_chat_window("老婆", dirty, dirty, True), f"脏尺寸放行了：{dirty!r}"
    assert not pick_chat_windows([{"hwnd": 1, "title": "老婆", "w": "614", "h": "648"}]), \
        "脏尺寸的窗口被选中了"
    # 主窗口不看尺寸（见 check_main_window_fallback），所以脏尺寸不该把它排除掉；
    # 但它必须还是个能用的 dict，不能因为脏尺寸就炸
    main = choose_targets([{"hwnd": 1, "title": "微信", "w": None, "h": None}])["fallback"]
    assert main is not None and main["hwnd"] == 1 and main["is_main"] is True, main
    print("会话名 / 比对键 ok（trim + 去零宽；显示保原样、比对键小写；脏值一律排除）")


def check_discover_never_raises():
    """真枚举一次：微信没开时也不能抛，只能返回空。"""
    t = discover()
    assert isinstance(t, dict) and "windows" in t and "fallback" in t, t
    n = len(t["windows"])
    print(f"真枚举 ok（当前发现 {n} 个独立窗口"
          f"{'，回退到主窗口' if t['fallback'] else ''}）")


def main() -> None:
    check_non_chat_is_excluded()
    check_real_windows_are_picked()
    check_duplicate_session_is_deduped()
    check_main_window_fallback()
    check_size_boundary()
    check_name_helpers()
    check_discover_never_raises()
    print("窗口发现检查全部通过（非聊天排除 / 实测窗口选中 / 同会话去重 / 主窗口回退 / 尺寸边界）")


if __name__ == "__main__":
    main()
