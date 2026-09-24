# -*- coding: utf-8 -*-
"""用合成数据预览 Qt 界面；不采集、不联网、不操作真实微信。

    python tools/preview_ui.py --state ready
    python tools/preview_ui.py --state ready --screenshot docs/ui_home.png
    python tools/preview_ui.py --judge-engine self --screenshot docs/ui_self.png
    python tools/preview_ui.py --state settings --auto-send both --scroll-bottom \
        --screenshot docs/ui_auto_send.png
    python tools/preview_ui.py --state auto --screenshot docs/ui_countdown.png

演示设置只保存在内存，不读取真实密钥，也不修改环境变量或 config.json。
--judge-engine / --judge-error 用来把三档（完整 / 自判 / 起草）和判断失败那一种界面都截出来；
--auto-send 决定自动发送那两行开关开哪个（开着的档位才会露出倒计时秒数、发送键和群昵称）；
--state auto 额外把「N 秒后自动发送」那条倒计时摆出来。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

# 直接 `python tools/preview_ui.py` 跑时 sys.path[0] 是 tools/，import app 会失败。
# 跟 check_*.py 一个做法，把项目根插进来。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings  # noqa: E402


_STATES = ("ready", "waiting", "loading", "error", "setup", "settings", "paused", "auto")

_CHAT = "示例群"  # 演示里「微信当前开着的」会话：用群聊，回复对象那一行才看得见
# (会话, 谁, 内容, 群里的发言人, 时间)：两个会话，下拉框里都能看到
# 演示数据一律用明显虚构的名字（示例群 / 张三 / 李四 / 王五）——这些截图会进 README 公开，
# 用真实群名或真人昵称等于把人家的聊天记录贴到网上。
_MESSAGES = (
    ("示例群", "her", "周末有人去爬山吗", "张三", "09:12"),
    ("示例群", "me", "我有空，几点集合？", "", "09:15"),
    ("示例群", "her", "我也去，带上我一个", "李四", "09:15"),
    ("示例群", "her", "八点地铁口见，记得带水", "张三", "09:16"),
    (_CHAT, "me", "有空呀，还是上次那家？", "", "18:43"),
    (_CHAT, "her", "好呀！六点见怎么样？我好久没吃了 😋", "", "18:43"),
)
_GROUP = "示例群"
_SENDERS = ("张三", "李四")  # 最近说话的排最前，跟 main.py 那边一个口径

_CANDIDATES = [
    "周六六点没问题，上次那家见～",
    "可以呀，周六六点在上次那家见！我也有点馋了 😋",
    "好呀，就周六六点！需要我先订个位吗？",
]

# 完整模式（openrouter）的结果：有校准过的胜出概率，卡片带百分比。
# 推荐故意放在第二项，方便检查视觉排序和按钮对应关系。
_ANSWERS = {
    "literal_question": {"type": "noul", "noul": 0.98},
    "true_intent": {"type": "choice", "choice": "casual_chat"},
    "danger_level": {"type": "score", "score": 0},
    "should_reply_now": {"type": "noul", "noul": 0.96},
    "best_action": {"type": "choice", "choice": "make_plan"},
    "she_needs": {"type": "choice", "choice": "action"},
    "tension_resolved": {"type": "noul", "noul": 0.99},
    "best_reply": {
        "type": "choice", "choice": "reply_b",
        "probabilities": {"reply_a": 0.21, "reply_b": 0.66, "reply_c": 0.13},
    },
}
_REPLY_TO = "张三"  # 跟 _SENDERS[0] 一致，让「回复给 …」那行在演示里看得见


def _ensure_cjk_fonts(app) -> None:
    """离屏平台（`QT_QPA_PLATFORM=offscreen`）的字体库是**空的**，中文会全渲染成豆腐块 □□□。

    截图是要进 README 的，豆腐块看着像界面坏了（第一次生成出来就是这样）。所以在字体库为空时，
    从系统字体目录手工加载几个中文字体。有显示器的环境下字体库本来就有 100+ 个字体，这里直接返回。
    """
    import os
    from PySide6.QtGui import QFontDatabase
    if QFontDatabase.families():
        return
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    # seguiemj 是表情符号字体：演示对话里带 😋，不加载它那一格会渲染成豆腐块
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc", "seguiemj.ttf"):
        path = os.path.join(folder, name)
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)


def _result(engine: str, judge_error: bool = False) -> dict:
    """按判断引擎造出对应的结果形状——三种档位给的字段真的不一样，预览得照着来。

    自判模式（self）：有摘要和排序，**scores 全 None**（judge.py 不给概率），answers 里也没有
    best_reply（那道题被排序取代了）。起草模式（none）：什么都没判断。
    """
    base = {"candidates": list(_CANDIDATES), "usage": {}, "reply_to": _REPLY_TO,
            "judge_engine": engine, "judge_error": None}
    if judge_error:
        return {**base, "judged": False, "best_index": None, "best_reply": None,
                "scores": [None] * len(_CANDIDATES), "answers": {}, "ranking": None,
                "judge_error": {"message": "判断服务返回 502", "status": 502,
                                "hint": "对端暂时不可用", "retries": 2}}
    if engine == "self":
        return {**base, "judged": True, "best_index": 2, "best_reply": _CANDIDATES[2],
                "scores": [None] * len(_CANDIDATES), "ranking": [2, 0, 1],
                "answers": {k: v for k, v in _ANSWERS.items() if k != "best_reply"}}
    if engine == "none":
        return {**base, "judged": False, "best_index": None, "best_reply": None,
                "scores": [None] * len(_CANDIDATES), "answers": {}, "ranking": None}
    return {**base, "judged": True, "best_index": 1, "best_reply": _CANDIDATES[1],
            "scores": [0.21, 0.66, 0.13], "ranking": None, "answers": dict(_ANSWERS)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用合成聊天预览 Qt UI；绝不采集、联网或填入真实微信。"
    )
    parser.add_argument("--state", choices=_STATES, default="ready", help="预览界面状态")
    parser.add_argument("--screenshot", metavar="PATH", help="将演示界面保存为 PNG 后退出（合成数据，不含微信内容）")
    parser.add_argument("--judge-engine", choices=("openrouter", "self", "none"),
                        default="openrouter", help="预览哪一档判断（决定卡片有没有百分比）")
    parser.add_argument("--judge-error", action="store_true",
                        help="预览「判断失败但候选保住」那一种界面")
    parser.add_argument("--scroll-bottom", action="store_true",
                        help="截图前把当前页滚到底部（判断卡在设置页下半部分，默认拍不到）")
    parser.add_argument("--scroll", type=int, metavar="PCT",
                        help="截图前把当前页滚到 0~100 的位置。设置页很长，"
                             "「保存聊天记录到本地」这类在中间偏下的行 --scroll-bottom 是到不了的")
    parser.add_argument("--auto-send", choices=("off", "dm", "group", "any", "both", "all"),
                        default="off",
                        help="自动发送那几个开关的演示状态（决定倒计时/发送键/群昵称露不露出来）："
                             "dm=单聊、group=群里@我、any=群里不@我也回、both=dm+group、all=三条路全开")
    args = parser.parse_args()
    if args.state == "auto" and args.auto_send == "off":
        args.auto_send = "dm"  # 倒计时条只在开了自动发送时才有意义，别拍出一张自相矛盾的图
    dm_on = args.auto_send in ("dm", "both", "all")
    group_on = args.auto_send in ("group", "both", "all")
    any_on = args.auto_send in ("any", "all")
    target = Path(args.screenshot).expanduser() if args.screenshot else None
    result = _result(args.judge_engine, args.judge_error)

    demo_settings = {"has_key": args.state != "setup", "relationship": "friends", "context": 10,
                     "has_deepseek_key": False, "draft_provider": "openrouter", "reply_target": True,
                     "style": "话少，基本不用标点，急了才发感叹号", "thinking": False, "check_update": True,
                     "retries": 2, "has_custom_key": False, "draft_base_url": "", "draft_model": "",
                     "judge_base_url": "", "judge_model": "",
                     # 判断引擎按命令行给；密钥状态跟着引擎走，否则说明文字会和实际档位打架
                     "judge_engine": args.judge_engine, "has_judge_key": args.judge_engine == "openrouter",
                     "auto_send_dm": dm_on, "auto_send_group": group_on, "auto_send_delay": 5,
                     "auto_send_group_any": any_on, "auto_send_any_wait": 8,
                     "send_key": "enter", "my_name": "王五" if group_on else "",
                     "draft_timeout": 20, "judge_timeout": 15,
                     "candidate_count": 3,
                     # 聊天记录导出。**路径必须给假的**：真值是本机的绝对路径，截图要进 README，
                     # 把自己机器上的目录结构贴到网上没有任何好处。
                     "save_history": True,
                     "history_dir": r"C:\Apps\jev-chat-windows\聊天记录"}

    # 形参必须跟 settings.save() 一一对应：少一个，设置页一点「保存设置」就会 TypeError，
    # 然后被 _save() 的 except 吞成「保存失败，请检查配置文件是否可写」——一个很难查的假故障。
    def save_demo_settings(key, relationship_text, context_n=None,
                           deepseek_key_text=None, draft_provider=None, reply_target_on=None,
                           style_text=None, thinking_on=None, check_update_on=None,
                           custom_key_text=None, draft_base_url_text=None, draft_model_text=None,
                           judge_base_url_text=None, judge_model_text=None, retries_n=None,
                           judge_engine_text=None, judge_key_text=None,
                           auto_send_dm_on=None, auto_send_group_on=None, auto_send_delay_n=None,
                           send_key_text=None, my_name_text=None, draft_timeout_n=None,
                           judge_timeout_n=None, candidate_count_n=None,
                           auto_send_group_any_on=None, auto_send_any_wait_n=None,
                           save_history_on=None):
        if key:
            demo_settings["has_key"] = True
        demo_settings["relationship"] = relationship_text
        if context_n is not None:
            demo_settings["context"] = context_n
        if deepseek_key_text:
            demo_settings["has_deepseek_key"] = True
        if custom_key_text:
            demo_settings["has_custom_key"] = True
        if draft_provider is not None:
            demo_settings["draft_provider"] = draft_provider
        if reply_target_on is not None:
            demo_settings["reply_target"] = bool(reply_target_on)
        if style_text is not None:
            demo_settings["style"] = style_text
        if thinking_on is not None:
            demo_settings["thinking"] = bool(thinking_on)
        if check_update_on is not None:
            demo_settings["check_update"] = bool(check_update_on)
        if draft_base_url_text is not None:
            demo_settings["draft_base_url"] = draft_base_url_text
        if draft_model_text is not None:
            demo_settings["draft_model"] = draft_model_text
        if judge_base_url_text is not None:
            demo_settings["judge_base_url"] = judge_base_url_text
        if judge_model_text is not None:
            demo_settings["judge_model"] = judge_model_text
        if retries_n is not None:
            demo_settings["retries"] = retries_n
        if judge_engine_text is not None:
            demo_settings["judge_engine"] = judge_engine_text
        if judge_key_text:
            demo_settings["has_judge_key"] = True
        if auto_send_dm_on is not None:
            demo_settings["auto_send_dm"] = bool(auto_send_dm_on)
        if auto_send_group_on is not None:
            demo_settings["auto_send_group"] = bool(auto_send_group_on)
        if auto_send_delay_n is not None:
            demo_settings["auto_send_delay"] = int(auto_send_delay_n)
        if send_key_text is not None:
            demo_settings["send_key"] = send_key_text
        if my_name_text is not None:
            demo_settings["my_name"] = my_name_text
        if draft_timeout_n is not None:
            demo_settings["draft_timeout"] = int(draft_timeout_n)
        if judge_timeout_n is not None:
            demo_settings["judge_timeout"] = int(judge_timeout_n)
        if candidate_count_n is not None:
            demo_settings["candidate_count"] = int(candidate_count_n)
        if auto_send_group_any_on is not None:
            demo_settings["auto_send_group_any"] = bool(auto_send_group_any_on)
        if auto_send_any_wait_n is not None:
            demo_settings["auto_send_any_wait"] = int(auto_send_any_wait_n)
        if save_history_on is not None:
            demo_settings["save_history"] = bool(save_history_on)

    # 在创建 Overlay 前替换设置接口，整个事件循环期间都保持隔离。
    with patch.multiple(
        settings,
        has_key=lambda: demo_settings["has_key"],
        relationship=lambda: demo_settings["relationship"],
        context=lambda: demo_settings["context"],
        deepseek_key=lambda: "",
        has_deepseek_key=lambda: demo_settings["has_deepseek_key"],
        draft_provider=lambda: demo_settings["draft_provider"],
        reply_target=lambda: demo_settings["reply_target"],
        style=lambda: demo_settings["style"],
        thinking=lambda: demo_settings["thinking"],
        check_update=lambda: demo_settings["check_update"],
        # 聊天记录导出这两个也必须桩掉：history_dir() 的真值是本机绝对路径，不桩的话
        # 截图里会把自己的目录结构印出来（那正是要公开出去的图），开关状态也会跟着真实配置走。
        save_history=lambda: demo_settings["save_history"],
        history_dir=lambda: demo_settings["history_dir"],
        # 这几个也必须桩掉：不桩的话 _load_settings() 会去读真实的 config.json 和注册表，
        # 违背本脚本「不读取真实密钥，也不修改环境变量或 config.json」的承诺。
        retries=lambda: demo_settings["retries"],
        custom_key=lambda: "",
        has_custom_key=lambda: demo_settings["has_custom_key"],
        draft_base_url=lambda: demo_settings["draft_base_url"],
        draft_model=lambda: demo_settings["draft_model"],
        judge_base_url=lambda: demo_settings["judge_base_url"],
        judge_model=lambda: demo_settings["judge_model"],
        # 判断引擎那一组也必须桩掉：stored_judge_engine / judge_problem 不桩就会去读真实
        # config.json，跟上面那几个地址是同一个理由。
        stored_judge_engine=lambda: demo_settings["judge_engine"],
        judge_engine=lambda: demo_settings["judge_engine"],
        judge_problem=lambda: "",
        judge_key=lambda: "",
        has_judge_key=lambda: demo_settings["has_judge_key"],
        # 自动发送和超时这一组同理：不桩就会去读真实 config.json，把用户自己的开关状态
        # 拍进演示图里——那既不是演示数据，也可能泄露他本机的配置。
        auto_send_dm=lambda: demo_settings["auto_send_dm"],
        auto_send_group=lambda: demo_settings["auto_send_group"],
        auto_send_group_any=lambda: demo_settings["auto_send_group_any"],
        auto_send_any_wait=lambda: demo_settings["auto_send_any_wait"],
        auto_send_on=lambda: (demo_settings["auto_send_dm"] or demo_settings["auto_send_group"]
                              or demo_settings["auto_send_group_any"]),
        auto_send_delay=lambda: demo_settings["auto_send_delay"],
        send_key=lambda: demo_settings["send_key"],
        my_name=lambda: demo_settings["my_name"],
        draft_timeout=lambda: demo_settings["draft_timeout"],
        judge_timeout=lambda: demo_settings["judge_timeout"],
        candidate_count=lambda: demo_settings["candidate_count"],
        save=save_demo_settings,
    ):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from app.overlay import Overlay

        # 必须在 Overlay 之前建好 app：字体得在控件构造前进字体库
        _ensure_cjk_fonts(QApplication.instance() or QApplication([]))

        def simulate_fill(text):
            # 等 Overlay 自身的点击反馈结束后，再显示明确的演示提示。
            QTimer.singleShot(0, lambda: ov.set_status(
                f"演示模式：已模拟填入「{text}」；未操作微信。", kind="success"
            ))

        def simulate_auto(text):
            """倒计时走完时的桩。真程序这里会去按发送键，演示里只报一句。"""
            QTimer.singleShot(0, lambda: ov.set_status(
                f"演示模式：倒计时结束，本该自动发送「{text}」；未操作微信。", kind="success"
            ))

        # 只有当前会话有结果，切到另一个会话就是空态——跟真实情况一致
        ov = Overlay(on_fill=simulate_fill, result_of=lambda t: result if t == _CHAT else None,
                     on_auto_send=simulate_auto, on_settings_change=lambda: None,
                     on_toggle_judge=lambda on: ov.set_status(
                         "演示模式：判断已" + ("开启" if on else "关闭") + "（未写配置）。", "success"))
        ov.win.setWindowTitle("WeChatJev · 界面演示（合成数据）")

        if args.state == "setup":
            ov.set_status("演示模式：请填写示例密钥，设置仅保存在本次预览内。", kind="warning")
            ov.open_settings()
        elif args.state == "waiting":
            ov.set_status("演示模式：等待对方的新消息；当前未连接微信。")
        else:
            for chat, who, text, name, timestamp in _MESSAGES:
                ov.log_message(who, text, name, timestamp=timestamp, chat=chat)
            ov.set_targets(_GROUP, _SENDERS, _SENDERS[0])  # 群聊才有回复对象这一行
            ov.set_chat(_CHAT)
            ov.show(result)
            ov.set_status("演示模式：已生成 3 条建议，点击填入仅模拟操作。", kind="success")
            # 演示用的「有新版本」提示。地址指向本项目自己的仓库，别写成上游——
            # 截图里虽然只渲染「去下载」四个字，但源码里的地址会被读者当成真实更新源。
            ov.set_update("9.9.9", "https://github.com/caizili999/jev-chat-windows/releases/latest")
            if args.state == "loading":
                ov.set_busy(True)
                ov.set_status("演示模式：正在为最新消息生成建议…", kind="busy")
            elif args.state == "error":
                ov.set_busy(True)
                ov.set_status("演示模式：分析失败，请检查网络和密钥，等待下一条消息后重试。", kind="error")
            elif args.state == "settings":
                ov.open_settings()
            elif args.state == "paused":
                ov.set_capture(False)
            elif args.state == "auto":
                # 直接调 begin_auto，把「N 秒后自动发送」那条摆出来（真程序里由 main.start_auto 调）。
                # 倒计时是真跑的：截图在 500ms 时按下，界面上是「5 秒后自动发送」。
                # 不判断档 best_index=None；演示时按真实 auto_pick 的口径取第一条，并把说明带进条里。
                auto_index = result.get("best_index")
                auto_note = ""
                if not isinstance(auto_index, int) or not 0 <= auto_index < len(result["candidates"]):
                    auto_index = 0
                    auto_note = ("没有判断结果（判断关着或失败了），直接发第一条候选，"
                                 "不一定是最合适的那条。")
                ov.begin_auto(result["candidates"][auto_index], 5, auto_note)

        exit_code = 0
        if target is not None:
            def save_screenshot():
                nonlocal exit_code
                try:
                    if args.scroll_bottom or args.scroll is not None:
                        bar = ov.pages.currentWidget().verticalScrollBar()
                        if args.scroll_bottom:
                            bar.setValue(bar.maximum())
                        else:
                            bar.setValue(int(bar.maximum() * max(0, min(100, args.scroll)) / 100))
                        ov.app.processEvents()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not ov.win.grab().save(str(target), "PNG"):
                        raise OSError(f"无法保存截图：{target}")
                    print(f"已保存合成界面截图：{target}")
                except OSError as exc:
                    print(str(exc))
                    exit_code = 1
                finally:
                    ov.app.quit()

            QTimer.singleShot(500, save_screenshot)
        ov.run()
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
