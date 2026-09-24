# -*- coding: utf-8 -*-
"""自动发送这条链的离线回归检查。

自动发送是这个工具里**唯一不可逆**的动作，所以链上每一道闸都是「不满足就不发」。
这道脚本守的就是每一道闸，任何一道静默失效，用户就会在聊天记录里看到自己没发过的消息：

1. `app.capture.input_has_text()`：空框（灰字占位符）判 False、真打的字判 True、
   只有光标判 False；区域认不出来时**一律 True**（方向永远是宁可不发）。
2. `app.overlay.auto_pick()` / `at_me()`：开关、单聊还是群聊、@我、群昵称、推荐位脏值，逐条收口。
   **判断关着时发第一条候选**（附一句说明），这是刻意口径而不是漏判——见那个函数的 docstring。
   「群里不@我也回」这条路的边界（包含 @我、不要求群昵称、单聊不受影响）也在这里。
3. `app.overlay.Overlay` 的倒计时：能取消、能被作废、到点只回调一次。
4. `app.settings` 那 7 个新配置项：往返、夹取、脏值、默认值。
5. `main.start_auto()` / `main.auto_send_reply()`：第一批不自动发、发送前把状态重新确认一遍。

跑法：python tools/check_auto_send.py   （通过 exit 0，失败抛 AssertionError 并 exit 1）

**绝不碰用户真实配置**：把 settings._CONFIG 指到临时文件；所有密钥字段一律留空传 None，
所以不会往注册表 HKCU\\Environment 写任何东西。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在 import PySide6 之前
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from app import settings  # noqa: E402
from app.capture import input_has_text  # noqa: E402
from app.overlay import Overlay, at_me, auto_pick  # noqa: E402

# 输入框检测的合成帧：400x800，输入框顶分隔线在 y=300。area 的形状跟 chat_area() 一致：
# (x0, y_top, x1, y_in, 面板底色, y_pane)。input_has_text 只用得到 x0/x1/y_in 三项。
_BG = (245, 247, 246)
_AREA = (100, 100, 700, 300, _BG, 60)
# 底色亮度 ≈ 246，所以门槛 120 换算成灰度：亮于 126 的算「灰字」，暗于 126 的算「真字」。
_GREY = (160, 160, 160)  # 占位符灰字：对比度 86
_INK = (55, 55, 55)      # 真打的字：对比度 191


def _frame():
    return np.full((400, 800, 3), _BG, dtype=np.uint8)


def check_input_has_text() -> None:
    """纯像素判断的三档：灰字占位符不算字、真字算、光标不算，认不出来一律算「有字」。"""
    # 空框：整行都是占位符灰字。像素很多，但对比度只有 86，够不着 120 的门槛
    empty = _frame()
    empty[320:340, 130:660] = _GREY
    assert input_has_text(empty, _AREA) is False, "灰字占位符不该被当成「用户打了字」"

    # 真打的字：深色、高对比度。一个汉字就有一两百像素，远超 100 的下限
    typed = _frame()
    typed[318:344, 130:400] = _INK
    assert input_has_text(typed, _AREA) is True, "真打的字必须判成「有字」"

    # 只有光标：2px 宽、26px 高的高对比度竖线 ≈ 52 像素，不能算「有字」
    caret = _frame()
    caret[318:344, 200:202] = (30, 30, 30)
    assert input_has_text(caret, _AREA) is False, "光标不该被当成「用户打了字」"

    # 光标 + 真字：字才是决定性的，光标不该把结论带偏
    both = _frame()
    both[318:344, 200:202] = (30, 30, 30)
    both[318:344, 260:400] = _INK
    assert input_has_text(both, _AREA) is True

    # ── 认不出来时必须返回 True。这四条是这套判断里最重要的方向：拿不准就不发，
    #    误判成「空框」的后果是把别人的草稿拼上去发出去，那比不发严重得多。
    assert input_has_text(_frame(), (100, 100, 140, 300, _BG, 60)) is True, "区域太窄该判「有字」"
    assert input_has_text(_frame(), (100, 100, 700, 395, _BG, 60)) is True, "区域越界该判「有字」"
    assert input_has_text(_frame(), (100, 100, 700, 300, _BG, 60)) is False, "整块纯底色 = 空框"
    assert input_has_text(np.zeros((0, 0, 3), dtype=np.uint8), _AREA) is True, "空帧该判「有字」"
    print("capture.input_has_text() ok")


def check_at_me() -> None:
    """@ 的识别口径：认「@昵称」，不认裸昵称，忽略 OCR 多出来的空格。"""
    assert at_me("@小金 在吗", "小金") is True
    assert at_me("@ 小金 在吗", "小金") is True, "OCR 在 @ 后面多插一个空格不该漏认"
    assert at_me("请问小金在吗", "小金") is False, "只是提到名字不算「叫我」"
    assert at_me("", "小金") is False
    assert at_me("@小金 在吗", "") is False, "昵称没填就谁都认不出来"
    assert at_me("@小金 在吗", "  小金  ") is True, "昵称两边的空格该忽略"
    assert at_me(None, "小金") is False, "文本为 None 不能炸"
    print("overlay.at_me() ok")


def check_group_any() -> None:
    """「群里不@我也回」的边界。这条路是三条里误判面最大的，口径必须钉死。

    核心是一条**包含**关系：开着它 = 群里都回，@我的消息当然也在内，不用另外再开
    auto_send_group。反过来（只开它却漏掉 @我 的消息）就成「有人点名找你反而不回」了。
    """
    r = {"candidates": ["甲", "乙", "丙"], "best_index": 1, "judged": True}

    # 默认（第 7 个参数不传）= 关着，行为跟升级前一模一样
    assert auto_pick(r, True, "今天天气不错", "小金", False, True)[0] is None, \
        "没开「不@我也回」时，没 @我的消息不该发"
    assert auto_pick(r, True, "今天天气不错", "小金", False, True) == (None, "这条没有 @我")

    # 开了：不@我也发，而且是发判断出来的那条，不是第一条
    assert auto_pick(r, True, "今天天气不错", "小金", False, False, True) == (1, "")
    # 开了：@我的消息当然也发——不需要另外开 group_on（包含关系）
    assert auto_pick(r, True, "@小金 在吗", "小金", False, False, True) == (1, ""), \
        "开了「都回」却漏掉 @我的消息，就成了「点名找你反而不回」"
    # 开了：不依赖群昵称。这条路不匹配 @，昵称空着照样跑（@我 那条路仍然要求填）
    assert auto_pick(r, True, "今天天气不错", "", False, False, True) == (1, ""), \
        "「不@我也回」不认 @，不该因为群昵称空着就失效"
    assert auto_pick(r, True, "@小金 在吗", "", False, False, True) == (1, ""), \
        "开了「都回」时，昵称空着也不该拦——那条路根本不看昵称"

    # 单聊不受它影响：它只管群聊
    assert auto_pick(r, False, "在吗", "", False, False, True)[0] is None, \
        "「群里不@我也回」管不到单聊"

    # 三个开关全关：跟升级前一模一样
    assert auto_pick(r, True, "今天天气不错", "小金", False, False, False) == (None, "自动发送没开")

    # 判断关着也不豁免这条路的前置条件（没有候选、推荐位脏了，照样不发）
    assert auto_pick({"candidates": [], "best_index": 0, "judged": True},
                     True, "今天天气不错", "小金", False, False, True)[0] is None
    assert auto_pick({"candidates": ["甲", "乙"], "best_index": 9, "judged": True},
                     True, "今天天气不错", "小金", False, False, True)[0] is None
    print("overlay.auto_pick() 群里不@我也回 ok")


def check_auto_pick() -> None:
    """自动发送的决策矩阵。每一条「不发」都对应一个真实的误发场景。"""
    r = {"candidates": ["甲", "乙", "丙"], "best_index": 1, "judged": True}

    # 两个开关都关着（默认状态）：跟升级前完全一样，一条都不发
    assert auto_pick(r, False, "", "", False, False) == (None, "自动发送没开")
    assert auto_pick(r, True, "@小金 在吗", "小金", False, False)[0] is None

    # 单聊：开了就发推荐那条（不是第一条）
    assert auto_pick(r, False, "在吗", "", True, False) == (1, "")
    assert auto_pick(r, False, "在吗", "", False, True)[0] is None, "群里那个开关管不到单聊"

    # 群聊：必须开了群里那个开关，且这条确实 @了我，且群昵称填了
    assert auto_pick(r, True, "@小金 在吗", "小金", True, True) == (1, "")
    assert auto_pick(r, True, "@小金 在吗", "小金", True, False)[0] is None, "群里那个开关没开"
    assert auto_pick(r, True, "小金你来说说", "小金", True, True)[0] is None, "裸昵称不算 @我"
    assert auto_pick(r, True, "@小金 在吗", "", True, True) == (None, "还没填群昵称，群里不自动发送")
    assert auto_pick(r, True, "@小金 在吗", "小金", False, True) == (1, ""), "单聊那个开关管不到群里"

    # 判断关着（或判断失败）：**发第一条**，并附一句说明。这是刻意的口径——
    # 用户显式开了自动发送，那是知情同意；禁止它等于替他否掉自己做的两个决定
    # （开自动发送 + 关判断提速）。但必须说破，否则他以为发的是「判断过的推荐」。
    got = auto_pick({"candidates": ["甲", "乙", "丙"], "best_index": None, "judged": False},
                    False, "", "", True, False)
    assert got[0] == 0, f"判断关着时该发第一条，实际 {got}"
    assert "第一条" in got[1], f"必须说明发的是第一条，实际 {got[1]!r}"

    # 判断关着时就算给了 best_index 也忽略它——judged=False 时那个字段本来就没有意义，
    # 拿它当依据等于用了一个我们明确不相信的值。
    assert auto_pick({"candidates": ["甲", "乙"], "best_index": 1, "judged": False},
                     False, "", "", True, False)[0] == 0, "judged=False 时该忽略 best_index"

    # 判断关着**不豁免**群聊那几道门：昵称空着、没 @我，照样不发
    assert auto_pick({"candidates": ["甲"], "best_index": None, "judged": False},
                     True, "@小金 在吗", "", True, True)[0] is None, "群昵称空着不该因为「无判断」就放行"
    assert auto_pick({"candidates": ["甲"], "best_index": None, "judged": False},
                     True, "小金你来说说", "小金", True, True)[0] is None, "没 @我照样不发"

    # 推荐位脏了（越界 / 非整数 / 布尔）→ 宁可不发，不能拿别的候选顶替
    for bad in (9, -1, None, "0", True, 1.0):
        got = auto_pick({"candidates": ["甲", "乙"], "best_index": bad, "judged": True},
                        False, "", "", True, False)
        assert got[0] is None, f"best_index={bad!r} 时不该发，实际 {got}"
    # 一条候选都没有
    assert auto_pick({"candidates": [], "best_index": 0, "judged": True},
                     False, "", "", True, False)[0] is None
    assert auto_pick({}, False, "", "", True, False)[0] is None, "结果字段缺失也不能炸"

    # 配置问题 vs 按设计跳过：状态栏要不要报警示色靠这个集合区分。
    # 「这条没有 @我」必须**不在**里面（那是群里的正常过滤，报警示色像出了故障），
    # 而真正配错的几条必须在里面——否则用户又回到「开了却没反应，也不知道为什么」。
    from app.overlay import AUTO_CONFIG_REASONS
    for must in ("还没填群昵称，群里不自动发送", "单聊没有开自动发送", "群里没有开自动发送"):
        assert must in AUTO_CONFIG_REASONS, f"{must!r} 是配置问题，该在警示集合里"
    assert "这条没有 @我" not in AUTO_CONFIG_REASONS, "正常过滤不该报警示色"
    assert "没有可用的推荐回复，不自动发送" not in AUTO_CONFIG_REASONS
    print("overlay.auto_pick() ok")


def _tmp_config(initial: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="jev_auto_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(initial, f, ensure_ascii=False)
    settings._CONFIG = path
    return path


def _write(path: str, **kw) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友", **kw}, f, ensure_ascii=False)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_settings_defaults(path: str) -> None:
    """默认值必须是「跟以前一模一样」：升级上来的人不该看到自己没发过的消息。"""
    _write(path)
    assert settings.auto_send_dm() is False, "单聊自动发送默认必须是关的"
    assert settings.auto_send_group() is False, "群 @我 自动发送默认必须是关的"
    assert settings.auto_send_group_any() is False, "群里不@我也回默认必须是关的（误判面最大）"
    assert settings.auto_send_on() is False
    assert settings.auto_send_delay() == 5, "默认 5 秒——自动和可控唯一能共存的方式"
    assert settings.auto_send_any_wait() == 8, "「不@我也回」那条路的安静窗口默认 8 秒"
    assert settings.AUTO_QUIET_SECONDS == 8, "安静窗口默认值 8 秒：短了挡不住连续对话，长了显得迟钝"
    assert settings.send_key() == "enter", "默认发送键跟微信默认值一致"
    assert settings.my_name() == ""
    assert settings.draft_timeout() == 20 and settings.judge_timeout() == 15, \
        "超时默认该是收紧后的 20/15，不是原来那个统一的 30"
    assert settings.judge_timeout() < settings.draft_timeout(), "判断该比起草等得短"
    assert settings.candidate_count() == 3, "候选条数默认必须是 3（升级上来行为不变）"

    # 安静窗口必须**跟着设置页那个框走**，不能硬编码。上一版它写死在 8 秒，用户在设置页填的
    # 数根本不生效——「填了 5、界面还是等 8 秒」那个 bug 的根就在这里。
    _write(path, auto_send_any_wait=5)
    assert settings.auto_send_any_wait() == 5, "该读用户填的值"
    # 老键仍然认：语义从「那条路的倒计时」改成「安静窗口」了，但值照搬最贴近用户当时填它的本意
    # （他填的时候想的就是「群里没@我时先等几秒」），退回默认 8 等于把他的设置丢掉。
    _write(path, auto_send_any_delay=5)
    assert settings.auto_send_any_wait() == 5, "老键该被兼容读取，不能退回默认 8"
    _write(path, auto_send_any_wait=11, auto_send_any_delay=5)
    assert settings.auto_send_any_wait() == 11, "新键在时以新键为准"

    # 读得到
    _write(path, auto_send_dm=True, auto_send_group=True, auto_send_delay=7,
           send_key="ctrl_enter", my_name=" 小金 ", draft_timeout=45, judge_timeout=8)
    assert settings.auto_send_dm() is True and settings.auto_send_group() is True
    assert settings.auto_send_on() is True, "两个开关的并集决定子进程要不要盯输入框"
    assert settings.auto_send_delay() == 7 and settings.send_key() == "ctrl_enter"
    assert settings.my_name() == "小金", "群昵称该去掉两边空格"
    assert settings.draft_timeout() == 45 and settings.judge_timeout() == 8

    # 「不@我也回」自己就能把 auto_send_on() 顶起来——漏了它，那条路会「开关开着却从来不发」
    # （子进程不盯输入框 → input_has 永远不是 False → auto_send_reply 每次都取消）。
    _write(path, auto_send_group_any=True)
    assert settings.auto_send_group_any() is True
    assert settings.auto_send_on() is True, "只开「不@我也回」也必须让子进程盯输入框"

    # 夹取：界面的 SpinBox 上下限和这里必须一致，否则「填了但不生效」很难查
    for key, raw, want in (("auto_send_delay", 99, 30), ("auto_send_delay", -1, 0),
                           ("auto_send_any_wait", 99, 30), ("auto_send_any_wait", -1, 0),
                           ("draft_timeout", 999, 120), ("draft_timeout", 1, 5),
                           ("judge_timeout", 0, 5), ("judge_timeout", 60, 60),
                           ("candidate_count", 99, 3), ("candidate_count", 0, 1),
                           ("candidate_count", -5, 1)):
        _write(path, **{key: raw})
        got = {"auto_send_delay": settings.auto_send_delay,
               "auto_send_any_wait": settings.auto_send_any_wait,
               "draft_timeout": settings.draft_timeout,
               "judge_timeout": settings.judge_timeout,
               "candidate_count": settings.candidate_count}[key]()
        assert got == want, f"{key}={raw!r} 应夹到 {want}，实际 {got}"

    # 脏值：数字项退默认，枚举项退 enter（微信的默认值），都不能把功能打死
    for key, want in (("auto_send_delay", 5), ("auto_send_any_wait", 8),
                      ("draft_timeout", 20), ("judge_timeout", 15),
                      ("candidate_count", 3)):
        _write(path, **{key: "abc"})
        got = {"auto_send_delay": settings.auto_send_delay,
               "auto_send_any_wait": settings.auto_send_any_wait,
               "draft_timeout": settings.draft_timeout,
               "judge_timeout": settings.judge_timeout,
               "candidate_count": settings.candidate_count}[key]()
        assert got == want, f"{key} 是脏值时该退默认 {want}，实际 {got}"
    _write(path, send_key="乱写的")
    assert settings.send_key() == "enter", "认不出的发送键该退 enter"
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    assert settings.auto_send_delay() == 5 and settings.send_key() == "enter"
    assert settings.draft_timeout() == 20 and settings.judge_timeout() == 15
    assert settings.candidate_count() == 3
    print("settings 自动发送/超时/候选条数 默认与夹取 ok")


def check_settings_save(path: str) -> None:
    """save() 的往返：传了写进去、传 None 保留、空串清掉、越界夹住、脏枚举不动原值。"""
    _write(path)
    settings.save(None, "朋友", auto_send_dm_on=True, auto_send_group_on=False,
                  auto_send_delay_n=9, send_key_text="ctrl_enter", my_name_text="小金",
                  draft_timeout_n=25, judge_timeout_n=6, candidate_count_n=1,
                  auto_send_group_any_on=True, auto_send_any_wait_n=15)
    saved = _read(path)
    assert saved["auto_send_dm"] is True and saved["auto_send_group"] is False
    assert saved["auto_send_group_any"] is True, "「不@我也回」该写进去"
    assert saved["auto_send_any_wait"] == 15, "那条路自己的安静窗口该独立落盘"
    assert "auto_send_any_delay" not in saved, "旧键要顺手清掉，别留两个同义项让人猜哪个生效"
    assert saved["auto_send_delay"] == 9 and saved["send_key"] == "ctrl_enter"
    assert saved["my_name"] == "小金"
    assert saved["draft_timeout"] == 25 and saved["judge_timeout"] == 6
    assert saved["candidate_count"] == 1, "候选条数该写进去"

    # 不传 = 保留。_save() 每次都会把全部字段带齐，但别的调用方（比如只改关系的）不该顺手清掉
    settings.save(None, "朋友", auto_send_delay_n=99)
    saved = _read(path)
    assert saved["auto_send_delay"] == 30, "越界值该夹到 30 再落盘"
    assert saved["my_name"] == "小金" and saved["send_key"] == "ctrl_enter", "没传的字段必须保留"
    assert saved["auto_send_dm"] is True, "没传的开关必须保留"
    assert saved["auto_send_group_any"] is True, "没传的开关必须保留"
    assert saved["auto_send_any_wait"] == 15, "没传的安静窗口必须保留"
    assert saved["candidate_count"] == 1, "没传的候选条数必须保留"

    # 候选条数越界要夹住（SpinBox 上限是 3，手改 config.json 才会越界）
    settings.save(None, "朋友", candidate_count_n=99)
    assert _read(path)["candidate_count"] == 3, "越界候选条数该夹到 3"
    settings.save(None, "朋友", candidate_count_n=0)
    assert _read(path)["candidate_count"] == 1, "0 条候选没有意义，该夹到 1"

    # save_judge_engine：只换 judge_engine 一个键，别的设置一个字都不能动。
    # 这是标题栏那个一键开关的持久化路径，写坏了会连关系、密钥配置一起丢。
    settings.save(None, "朋友", candidate_count_n=2, style_text="话少")
    before = _read(path)
    settings.save_judge_engine("none")
    after = _read(path)
    assert after["judge_engine"] == "none", "该切成 none"
    assert {k for k in set(before) | set(after) if before.get(k) != after.get(k)} == {"judge_engine"}, \
        f"只该动 judge_engine，实际动了 {[k for k in set(before) | set(after) if before.get(k) != after.get(k)]}"
    assert after["candidate_count"] == 2 and after["style"] == "话少", "别的设置必须原样保留"
    settings.save_judge_engine("self")
    assert _read(path)["judge_engine"] == "self", "切回来该恢复"
    settings.save_judge_engine("乱写的")
    assert _read(path)["judge_engine"] == "self", "脏档位该被忽略，不能写进去"

    # 脏枚举 = 保留原来的，不写一个认不出的值进去
    settings.save(None, "朋友", send_key_text="乱写的")
    assert _read(path)["send_key"] == "ctrl_enter", "脏 send_key 该保留原值"

    # 空串 = 清掉（群昵称是可以被清空的，清了就等于「群里不自动发」）
    settings.save(None, "朋友", my_name_text="")
    assert _read(path)["my_name"] == ""

    # 群昵称清了之后，群里那一半就发不出去了——这条链要闭得上
    settings.save(None, "朋友", auto_send_group_on=True, auto_send_dm_on=False)
    assert settings.auto_send_group() is True and settings.my_name() == ""
    r = {"candidates": ["甲"], "best_index": 0, "judged": True}
    assert auto_pick(r, True, "@小金 在吗", settings.my_name(),
                     settings.auto_send_dm(), settings.auto_send_group())[0] is None
    print("settings.save() 自动发送往返 ok")


def check_countdown(ov) -> None:
    """倒计时：显示、逐秒走、到点回调一次、能被用户和程序两边取消。"""
    fired = []
    ov.on_auto_send = lambda text: fired.append(text)
    ov.win.show()
    ov.cands, ov._current, ov._shown, ov._chat = ["甲"], True, "小分队", "小分队"
    ov.app.processEvents()
    assert ov.can_auto_send() is True, "正看着的就是微信当前会话、有候选，该允许自动发送"

    # 正常走完：3 秒 → 2 → 1 → 到点
    ov.begin_auto("甲", 3)
    ov.app.processEvents()
    assert ov.auto_pending() is True and ov.autoBar.isVisible(), "倒计时条该露出来"
    assert ov.autoLabel.text() == "3 秒后自动发送", ov.autoLabel.text()
    assert ov.autoText.text() == "甲", ov.autoText.text()
    assert fired == [], "还没到点就发了"
    ov._auto_tick()
    assert ov.autoLabel.text() == "2 秒后自动发送", ov.autoLabel.text()
    ov._auto_tick()
    assert ov.autoLabel.text() == "1 秒后自动发送", ov.autoLabel.text()
    ov._auto_tick()
    assert ov.autoLabel.text() == "正在自动发送…", ov.autoLabel.text()
    ov._auto_fire()
    assert fired == ["甲"], f"该回调一次：{fired}"
    assert ov.auto_pending() is False and not ov.autoBar.isVisible(), "发完该把倒计时条收掉"
    ov._auto_fire()  # 到点后那个 150ms 的定时器可能再来一次：必须已经无害
    assert fired == ["甲"], "到点只能回调一次"

    # 用户点「取消发送」
    ov.begin_auto("乙", 3)
    ov.app.processEvents()
    ov._cancel_auto()
    assert ov.auto_pending() is False and not ov.autoBar.isVisible()
    assert "已取消" in ov.status.text(), ov.status.text()
    ov._auto_tick()
    ov._auto_fire()
    assert fired == ["甲"], "取消了还发出去——这是这个功能最不能出的错"

    # 等待时间设成 0：立刻发，但倒计时条仍然要露一下脸（否则用户不知道刚才发生了什么）
    ov.begin_auto("丙", 0)
    assert ov.auto_pending() is True and ov.autoLabel.text() == "正在自动发送…"
    ov._auto_fire()
    assert fired == ["甲", "丙"], fired

    # 程序侧取消：输入框里已经有字（main 发现用户在打字）
    ov.begin_auto("丁", 5)
    ov.cancel_auto("输入框里已经有内容（你可能正在打字），已取消这次自动发送。")
    assert ov.auto_pending() is False and "输入框里已经有内容" in ov.status.text()
    ov._auto_tick()
    ov._auto_fire()
    assert fired == ["甲", "丙"], "程序侧取消之后也不能发"
    # 没在倒计时时 cancel_auto 什么都不做，别让一句「已取消」盖掉更重要的状态
    ov.cancel_auto("不该出现的一句")
    assert "不该出现的一句" not in ov.status.text()

    # 候选被作废（来了新消息）→ 正在跑的倒计时必须一起作废
    ov.cands, ov._current = ["戊"], True
    ov.begin_auto("戊", 5)
    ov.invalidate_replies()
    assert ov.auto_pending() is False and not ov.autoBar.isVisible()

    # note（「判断关着，发的是第一条」）要跟着倒计时一起显示，也得跟着一起收掉。
    # 这是用户唯一能知道「发的不是判断过的推荐」的地方——不显示他就永远不会知道，
    # 等发现时话已经发出去了。
    ov.cands, ov._current = ["壬"], True
    ov.begin_auto("壬", 5, "没有判断结果（判断关着或失败了），直接发第一条候选，不一定是最合适的那条。")
    ov.app.processEvents()
    assert ov.autoBarNote.isVisible(), "有 note 时必须显示出来"
    assert "第一条" in ov.autoBarNote.text(), ov.autoBarNote.text()
    ov._stop_auto()
    assert not ov.autoBar.isVisible(), "收掉倒计时条时 note 跟着一起收（它是条里的子控件）"
    # 没有 note 时（正常判断过的那种）那一行必须藏起来，不能留着上一条的说明
    ov.begin_auto("癸", 5)
    ov.app.processEvents()
    assert not ov.autoBarNote.isVisible(), "没有 note 时不该显示空白行占地方"
    ov._stop_auto()

    # 关键回归：旧一轮在取消后留下的 150ms singleShot，不能在新一轮 begin_auto() 后
    # 把新条收掉或触发新发送。此前只有 _autoPending 布尔值时，这里会误伤新一轮。
    old_serial = ov._autoSerial
    ov.begin_auto("旧", 5)
    stale_serial = ov._autoSerial
    ov._auto_tick()  # 为旧轮排一个 150ms 回调
    ov._stop_auto()
    ov.begin_auto("新", 5)
    assert ov.auto_pending() is True and ov.autoBar.isVisible()
    ov._auto_fire(stale_serial)
    assert ov.auto_pending() is True and ov.autoBar.isVisible(), "旧回调不该收掉新倒计时"
    assert ov._autoSerial > old_serial
    ov._stop_auto()

    # 打开设置页 → 倒计时条看不见了，那一下必须收掉（方向永远是「不发」）
    ov.cands, ov._current = ["己"], True
    ov.begin_auto("己", 5)
    ov.open_settings()
    assert ov.auto_pending() is False, "进了设置页就点不到「取消」，不能留着它继续跑"
    assert "设置页" in ov.status.text(), ov.status.text()
    ov._back_home()

    # 用户自己点了「填入微信」= 手动接管，撤销这次自动发送
    ov.cands, ov._current, ov._shown, ov._chat = ["庚"], True, "小分队", "小分队"
    ov.on_fill = lambda text: None
    ov.begin_auto("庚", 5)
    ov._fill(0)
    assert ov.auto_pending() is False, "用户自己动手了，就不该再自动发"
    ov.begin_auto("辛", 5)
    ov._copy(0)
    assert ov.auto_pending() is False

    # 正在浏览别的会话（微信当前开的不是它）→ 只看不填，更不能自动发
    ov._shown, ov._chat = "别的会话", "小分队"
    assert ov.can_auto_send() is False, "浏览中的会话不该允许自动发送"
    ov._shown, ov._chat = "小分队", "小分队"
    ov._current = False
    assert ov.can_auto_send() is False, "候选已作废时不该允许自动发送"
    ov.cands = []
    ov._current = True
    assert ov.can_auto_send() is False, "没有候选时不该允许自动发送"
    assert fired == ["甲", "丙"], f"全程只有那两次该真的发：{fired}"
    print("Overlay 倒计时与取消 ok")


def check_auto_fields(ov, path: str) -> None:
    """设置页那三个联动：开关决定字段显隐、说明句随开关变、页脚承诺跟着存下的值变。"""
    _write(path, auto_send_dm=False, auto_send_group=False, auto_send_delay=5)
    ov._load_settings()
    ov.open_settings()
    ov.app.processEvents()
    assert not ov.autoDelayBox.isVisible(), "两个开关都关着时，倒计时秒数不该露出来"
    assert not ov.sendKeyBox.isVisible(), "两个开关都关着时，发送键不该露出来"
    assert not ov.myNameEdit.isVisible(), "没开群里那一半时，群昵称不该露出来"
    assert "不会自动发送" in ov.autoNote.text(), ov.autoNote.text()
    assert "发送由你确认" in ov.footerNote.text(), ov.footerNote.text()
    assert (ov.autoDelayBox.minimum(), ov.autoDelayBox.maximum()) == (0, settings._MAX_AUTO_DELAY), \
        f"倒计时值域该跟 settings 的上限一致，实际 {ov.autoDelayBox.minimum()}~{ov.autoDelayBox.maximum()}"

    # 只开单聊：秒数和发送键露出来，群昵称仍然藏着
    ov.autoDmSwitch.setChecked(True)
    ov.app.processEvents()
    assert ov.autoDelayBox.isVisible() and ov.sendKeyBox.isVisible()
    assert not ov.myNameEdit.isVisible()
    assert "单聊里对方直接找你" in ov.autoNote.text(), ov.autoNote.text()
    assert "群里" not in ov.autoNote.text(), ov.autoNote.text()

    # 再开群里：群昵称露出来，但没填的时候必须说破「暂时不会发」
    ov.autoGroupSwitch.setChecked(True)
    ov.app.processEvents()
    assert ov.myNameEdit.isVisible()
    assert "群昵称还空着" in ov.autoNote.text(), ov.autoNote.text()
    ov.myNameEdit.setText("小金")
    ov.app.processEvents()
    assert "群里 @小金" in ov.autoNote.text(), ov.autoNote.text()

    # 再开「不@我也回」：群昵称栏该收起来（那条路不认 @，摆着只会让人以为「填了才生效」），
    # 昵称那条警告更要收掉——它写的是「群里一次都不会自动发送」，在这个配置下是句反话。
    # 代价改由 autoAnyWarn 顶上，它必须就在开关正下方。
    ov.autoAnySwitch.setChecked(True)
    ov.app.processEvents()
    assert ov.autoAnyWaitBox.isVisible(), "开了不@我也回，它自己那条安静窗口该露出来"
    assert not ov.myNameEdit.isVisible(), "不@我也回不认 @，群昵称栏不该再占位"
    assert not ov.autoNameWarn.isVisible(), "那条路根本不跑，昵称警告会说反话"
    assert ov.autoAnyWarn.isVisible(), "这条路误判面最大，代价必须写在开关下面"
    assert "任何新消息" in ov.autoNote.text(), ov.autoNote.text()
    # 这条路有两个等待，两个都得写出来、而且各自是各自的值：安静窗口 8（默认）、倒计时 5（这个
    # 测试配置里的 auto_send_delay）。上一版只写倒计时，用户根本看不出安静窗口被写死在 8 秒。
    assert "先安静 8 秒" in ov.autoNote.text(), ov.autoNote.text()
    assert "再等 5 秒" in ov.autoNote.text(), ov.autoNote.text()
    # 开关下面那句警告里的秒数必须跟框一致：写死的话，改了框不改字，用户会以为自己填错了
    assert "安静 8 秒" in ov.autoAnyWarn.text(), ov.autoAnyWarn.text()
    ov.autoAnyWaitBox.setValue(3)
    ov.app.processEvents()
    assert "先安静 3 秒" in ov.autoNote.text(), ov.autoNote.text()
    assert "安静 3 秒" in ov.autoAnyWarn.text(), ov.autoAnyWarn.text()
    # 填 0 = 不等安静：那时不能再写「等 0 秒」这种话，得把代价说破
    ov.autoAnyWaitBox.setValue(0)
    ov.app.processEvents()
    assert "等 0 秒" not in ov.autoNote.text(), ov.autoNote.text()
    assert "立刻动手" in ov.autoAnyWarn.text(), ov.autoAnyWarn.text()
    ov.autoAnyWaitBox.setValue(8)
    ov.app.processEvents()
    ov.autoAnySwitch.setChecked(False)
    ov.app.processEvents()
    assert ov.myNameEdit.isVisible(), "关掉之后群昵称栏该回来"

    # 说明句说的是「保存后」，页脚说的是「已经存下的」——两边措辞不能打架
    assert ov.autoNote.text().startswith("保存后："), ov.autoNote.text()
    assert "自动发送已开启" not in ov.footerNote.text(), "还没保存，页脚不该说已开启"
    ov._save()
    assert "自动发送已开启" in ov.footerNote.text(), ov.footerNote.text()

    # 页脚有两种说法，取决于判断开没开。**判断关着时不能再写「发送前 N 秒内可取消」**——
    # 那时发的不是判断过的推荐，那句承诺在「判断关着」的配置下是假话。
    assert settings.judge_engine() == "none", "这个测试配置里起草没配好，判断该是 none"
    assert "判断关着" in ov.footerNote.text(), ov.footerNote.text()
    assert "5 秒内可取消" not in ov.footerNote.text(), "判断关着时不该再承诺「可取消」那套"
    assert "发第一条" in ov.footerNote.text(), ov.footerNote.text()
    # 判断开着时才是原来那句（临时换掉 judge_engine，测完还原）
    real_engine = settings.judge_engine
    settings.judge_engine = lambda: "self"
    try:
        ov._sync_footer()
        assert "5 秒内可取消" in ov.footerNote.text(), ov.footerNote.text()
        assert "判断关着" not in ov.footerNote.text(), ov.footerNote.text()
    finally:
        settings.judge_engine = real_engine
    ov._sync_footer()  # 还原成真实口径，后面的断言都基于它

    # 判断关着时设置页卡内也要有一条警告（跟群昵称那条同一形式），否则用户
    # 在设置页看到的全是「会发」的好消息，没人告诉他发的是第一条
    ov.autoGroupSwitch.setChecked(True)
    ov.myNameEdit.setText("小金")
    ov.autoDmSwitch.setChecked(True)
    ov.app.processEvents()
    assert ov.autoJudgeWarn.isVisible(), "判断关着 + 自动发送开着时该有警告"
    assert "第一条" in ov.autoJudgeWarn.text(), ov.autoJudgeWarn.text()
    assert "**" not in ov.autoJudgeWarn.text(), "PlainText 渲染，不能带 markdown 标记"

    # 存下来的开关真的落盘了，并且能从盘上读回来
    saved = _read(path)
    assert saved["auto_send_dm"] is True and saved["auto_send_group"] is True
    assert saved["my_name"] == "小金"
    assert settings.auto_send_on() is True

    # 「不@我也回」落盘 + 页脚口径：倒计时三条路共用一个值了，页脚可以放心把这个数写死——
    # 上一版两条路秒数不同，写哪个都错一半，所以那时只能含糊地写「发送前可取消」。
    ov.autoAnySwitch.setChecked(True)
    ov.app.processEvents()
    ov._save()
    assert _read(path)["auto_send_group_any"] is True, "该落盘"
    assert _read(path)["auto_send_any_wait"] == settings.auto_send_any_wait(), "该落盘"
    assert "含群里不@我" in ov.footerNote.text(), ov.footerNote.text()
    # 判断关着时页脚走的是另一句（「判断关着，发第一条候选」），带秒数的那句只在判断开着时出现
    real_engine = settings.judge_engine
    settings.judge_engine = lambda: "self"
    try:
        ov._sync_footer()
        assert f"发送前 {settings.auto_send_delay()} 秒内可取消" in ov.footerNote.text(), \
            ov.footerNote.text()
    finally:
        settings.judge_engine = real_engine
    ov._sync_footer()
    ov.autoAnySwitch.setChecked(False)
    ov.app.processEvents()

    # 关回去：页脚要退回那句「发送由你确认」，不能留着一句过期承诺
    ov.autoDmSwitch.setChecked(False)
    ov.autoGroupSwitch.setChecked(False)
    ov._save()
    assert "发送由你确认" in ov.footerNote.text(), ov.footerNote.text()
    assert "自动发送已开启" not in ov.footerNote.text()

    # 说明文字是 PlainText 渲染的，写 markdown 的 ** 会原样显示成星号（截图里真出现过）
    for label in (ov.autoNote, ov.autoKeyHint, ov.autoGroupHint, ov.autoAnyWarn):
        assert "**" not in label.text(), f"不该出现 markdown 标记：{label.text()!r}"
    ov._back_home()
    print("设置页自动发送字段联动 ok")


def check_main_gates() -> None:
    """main 那一侧的门禁：第一批不发、用户自己要求重生成的不发、界面不允许的不发。"""
    import main

    calls = []

    class FakeOv:
        def __init__(self, can=True):
            self.can = can

        def log(self, line):
            calls.append(("log", line))

        def set_status(self, text, kind="idle"):
            calls.append(("status", text, kind))

        def can_auto_send(self):
            return self.can

        def begin_auto(self, text, seconds, note=""):
            calls.append(("begin", text, seconds, note))

    result = {"candidates": ["甲", "乙", "丙"], "best_index": 1, "judged": True}
    no_judge = {"candidates": ["甲", "乙", "丙"], "best_index": None, "judged": False}
    real = (settings.auto_send_on, settings.auto_send_dm, settings.auto_send_group,
            settings.auto_send_group_any, settings.auto_send_delay, settings.my_name)
    settings.auto_send_on = lambda: True
    settings.auto_send_dm = lambda: True
    settings.auto_send_group = lambda: False
    settings.auto_send_group_any = lambda: False
    settings.auto_send_delay = lambda: 5
    settings.my_name = lambda: ""
    try:
        def run(batches, triggered_by_message, can=True, group=False, my_name="",
                res=None, last_text="@小金 在吗"):
            calls.clear()
            main.chats = {}
            main.state["batches"] = {"小分队": batches}
            chat = main.chat_of("小分队")
            chat["auto"] = triggered_by_message
            if group:
                chat["senders"] = ["阿杰"]
                chat["history"].append(("her", last_text, "阿杰"))
            else:
                chat["history"].append(("her", last_text, None))
            main.ov = FakeOv(can)
            settings.my_name = lambda: my_name
            main.start_auto("小分队", result if res is None else res)
            return list(calls)

        def begins(got):
            return [c for c in got if c[0] == "begin"]

        def statuses(got):
            return [c for c in got if c[0] == "status"]

        # 开关没开：连算都不算，也不往聊天记录里写
        settings.auto_send_on = lambda: False
        assert run(9, True) == [], "开关没开时不该有任何动作"
        settings.auto_send_on = lambda: True

        # 第一批不自动发：那批可能是用户不在时攒下的
        got = run(1, True)
        assert not begins(got), f"第一批不该自动发：{got}"
        assert any("第一批" in c[1] for c in got if c[0] == "log"), got
        # **拦截原因也要进状态栏**——只写进「聊天记录」面板等于没写（那个面板默认折叠，
        # 用户开了自动发送却没发出去时，第一反应就是「怎么没反应」）
        assert any("第一批" in c[1] for c in statuses(got)), got

        # 不是「对方来了新消息」触发的（用户自己改了回复对象要求重生成）→ 不发
        assert run(5, False) == [], "用户自己要求重生成时不该自动发"

        # 界面不允许（正浏览别的会话 / 候选已作废）→ 不发
        got = run(5, True, can=False)
        assert not begins(got), got
        assert any("正看着的" in c[1] for c in got if c[0] == "log"), got
        assert any("别的会话" in c[1] for c in statuses(got)), got

        # 全都满足 → 发的是**推荐那条**（best_index=1），不是第一条；note 为空
        assert run(2, True) == [("begin", "乙", 5, "")], run(2, True)

        # 判断关着 → 发**第一条**，且 note 必须说破（用户以为发的是判断过的推荐）
        got = run(2, True, res=no_judge)
        assert len(begins(got)) == 1, f"判断关着也该发（发第一条），实际 {got}"
        assert begins(got)[0][1] == "甲", f"判断关着时该发第一条，实际 {begins(got)[0][1]!r}"
        assert "第一条" in begins(got)[0][3], f"note 必须说破发的是第一条：{begins(got)[0][3]!r}"

        # 群聊：开关没开 / 昵称没填 / 没 @我，三种都不发
        settings.auto_send_group = lambda: False
        got = run(2, True, group=True, my_name="小金")
        assert not begins(got), got
        settings.auto_send_group = lambda: True

        # 昵称没填 = 配置问题 → 状态栏要用警示色，用户才知道要去填
        got = run(2, True, group=True, my_name="")
        assert not begins(got), got
        assert any(c[2] == "warning" and "群昵称" in c[1] for c in statuses(got)), got

        # 没 @我 = 按设计过滤 → 中性色。报警示色会像「出了故障」，而它其实是正常工作
        got = run(2, True, group=True, my_name="小金", last_text="今天天气不错")
        assert not begins(got), got
        assert any(c[2] == "idle" and "没有 @我" in c[1] for c in statuses(got)), got

        # 该发的时候发，而且是推荐那条
        got = run(2, True, group=True, my_name="小金")
        assert ("begin", "乙", 5, "") in got, got
        settings.auto_send_group = lambda: False

        # 「不@我也回」：群里没@我也发，而且**倒计时跟其他两条路共用一个值**（都是 5）。
        # 上一版这条路单独取「群里不@我时等几秒」，于是用户在设置页改「生成完等几秒再发」时
        # 会发现群里根本不听——那个框只管单聊和 @我，两条路各读一个值，改哪个都只对一半。
        settings.auto_send_group_any = lambda: True
        got = run(2, True, group=True, my_name="小金", last_text="今天天气不错")
        assert begins(got) == [("begin", "乙", 5, "这条没@你，是「群里不@我也回」触发的。")], got
        # @我的消息也走这条路（包含关系），只是不该带「没@你」那句
        got = run(2, True, group=True, my_name="小金", last_text="@小金 在吗")
        assert begins(got) == [("begin", "乙", 5, "")], got
        # 不认昵称：这条路压根不匹配 @，昵称空着照样发（@我 那条路才要求填）
        got = run(2, True, group=True, my_name="", last_text="今天天气不错")
        assert begins(got) == [("begin", "乙", 5, "这条没@你，是「群里不@我也回」触发的。")], got
        settings.auto_send_group_any = lambda: False
    finally:
        (settings.auto_send_on, settings.auto_send_dm, settings.auto_send_group,
         settings.auto_send_group_any, settings.auto_send_delay, settings.my_name) = real
    print("main.start_auto() 门禁 ok")


def check_send_gate() -> None:
    """真正按发送键那一下：任何一个条件变了都要取消，而且要说清为什么。"""
    import main

    sent, statuses, logs = [], [], []
    main.ov = SimpleNamespace(set_status=lambda text, kind="idle": statuses.append((text, kind)),
                              log=lambda line: logs.append(line),
                              after=lambda ms, fn: None,
                              current_chat=lambda: main.state["chat"])
    main.send_text = lambda hwnd, area, text, key: (sent.append((text, key)), "Enter")[1]
    real_key = settings.send_key
    settings.send_key = lambda: "ctrl_enter"

    def run(**over):
        sent.clear()
        statuses.clear()
        main.state.update({"hwnd": 1, "area": (0, 0, 10, 10), "chat": "小分队",
                           "auto_title": "小分队", "input_has": False, "busy": False})
        main.state.update(over)
        main.auto_send_reply("甲")

    try:
        run()
        assert sent == [("甲", "ctrl_enter")], f"该按设置里的键发出去：{sent}"
        assert any("已自动发送" in s[0] for s in statuses), statuses

        # 每一条「不发」都对应一个真实的误发场景
        for over, token in (({"input_has": True}, "输入框里已经有内容"),
                            ({"input_has": None}, "输入框里已经有内容"),
                            ({"chat": "别的会话"}, "切到别的会话"),
                            ({"busy": True}, "又在生成"),
                            ({"hwnd": None}, "不可用"),
                            ({"area": None}, "不可用"),
                            ({"auto_title": ""}, "不知道这条回复属于哪个会话")):
            run(**over)
            assert sent == [], f"{over} 时不该发送，实际发了 {sent}"
            assert any(token in s[0] for s in statuses), (over, statuses)
            assert any("取消" in line for line in logs), logs

        # 界面自己切走了（微信还开着那个会话）也要拦
        main.state.update({"hwnd": 1, "area": (0, 0, 10, 10), "chat": "小分队",
                           "auto_title": "小分队", "input_has": False, "busy": False})
        main.ov = SimpleNamespace(set_status=lambda text, kind="idle": statuses.append((text, kind)),
                                  log=lambda line: logs.append(line),
                                  after=lambda ms, fn: None,
                                  current_chat=lambda: "别的会话")
        sent.clear()
        statuses.clear()
        main.auto_send_reply("甲")
        assert sent == [] and any("界面已经切到别的会话" in s[0] for s in statuses), statuses

        # 发完之后那一眼：输入框里还有内容 = 发送键多半跟微信设置不一致
        statuses.clear()
        main.state["chat"] = "小分队"
        main.state["input_has"] = True
        main.check_auto_sent("小分队")
        assert any("发送键" in s[0] for s in statuses), statuses
        statuses.clear()
        main.state["input_has"] = False  # 发出去了，框是空的 → 不该报警
        main.check_auto_sent("小分队")
        assert statuses == [], statuses
        statuses.clear()
        main.state["input_has"] = True
        main.check_auto_sent("别的会话")  # 已经切走了，那一眼看的不是同一件事
        assert statuses == [], statuses
    finally:
        settings.send_key = real_key
    print("main.auto_send_reply() 发送前确认 ok")


def check_quiet_window() -> None:
    """群里「不@我也回」的安静窗口：该等的等、不该等的一点都不等。

    这是那条路唯一的节奏控制器（用户明确不要冷却），判错了两头都难受：该等不等 → 群里
    连着聊它一句句插；不该等却等 → 单聊和 @我 变得迟钝。

    定时器那半边更要紧：QTimer.singleShot 取消不了，只能靠令牌让旧回调自己失效。
    这一步错了会变成「群里每说一句就生成一次」——正好是这个功能最该避免的失败模式。
    """
    import main

    class FakeOv:
        def __init__(self):
            self.timers = []

        def set_status(self, text, kind="idle"):
            pass

        def after(self, ms, fn):
            self.timers.append((ms, fn))

    real = (settings.auto_send_group_any, settings.my_name, main.start_analyze)
    ov = FakeOv()
    try:
        settings.auto_send_group_any = lambda: True
        settings.my_name = lambda: "小金"
        main.ov = ov
        main.state["busy"] = False
        main.state["quiet"] = {}

        def chat_with(senders, last):
            main.chats = {}
            c = main.chat_of("小分队")
            c["senders"] = list(senders)
            c["history"].append(("her", last, "阿杰" if senders else None))
            return c

        # 群聊 + 没@我 → 等
        assert main.should_wait_quiet(chat_with(["阿杰"], "今天天气不错")) is True
        # 群聊 + @我 → 不等（明确信号，立刻生成，跟升级前一模一样）
        assert main.should_wait_quiet(chat_with(["阿杰"], "@小金 在吗")) is False
        # 单聊 → 不等（那边对方说完就在等你）
        assert main.should_wait_quiet(chat_with([], "在吗")) is False
        # 开关关着 → 一律不等，行为跟升级前一模一样
        settings.auto_send_group_any = lambda: False
        assert main.should_wait_quiet(chat_with(["阿杰"], "今天天气不错")) is False
        settings.auto_send_group_any = lambda: True
        # 群昵称空着：「不@我也回」不认 @，照样要等——不能因为没填昵称就退化成「不等」
        settings.my_name = lambda: ""
        assert main.should_wait_quiet(chat_with(["阿杰"], "今天天气不错")) is True
        settings.my_name = lambda: "小金"

        # 令牌：每来一条新消息就重排，旧回调必须失效
        fired = []
        main.start_analyze = lambda t, m, auto_ok=False: fired.append((t, auto_ok))
        main.chats = {}
        c = main.chat_of("小分队")
        c["senders"] = ["阿杰"]
        c["history"].append(("her", "今天天气不错", "阿杰"))
        c["rev"] += 1
        main.arm_quiet("小分队")
        rev1, (ms1, fn1) = c["rev"], ov.timers[-1]
        assert ms1 == settings.auto_send_any_wait() * 1000, f"延时该是安静窗口，实际 {ms1}"
        assert main.state["quiet"]["小分队"] == rev1

        # 群里又来一条（rev 变了）→ 重排，这时**旧的**回调到点必须什么都不做
        c["history"].append(("her", "那我们去哪", "阿杰"))
        c["rev"] += 1
        main.arm_quiet("小分队")
        rev2, (_, fn2) = c["rev"], ov.timers[-1]
        assert rev2 != rev1 and main.state["quiet"]["小分队"] == rev2
        fn1()
        assert not fired, "群里又来消息了，旧的安静定时器必须失效，否则每句都会生成一次"
        fn2()
        assert fired == [("小分队", True)], f"最新那一轮才该生成，实际 {fired}"
        assert "小分队" not in main.state["quiet"], "生成过了就把令牌收掉"

        # 用户自己说话了：rev 一样会变，等着的那次作废（他已经在参与，不该替他插话）
        c["history"].append(("me", "我来说两句", None))
        c["rev"] += 1
        main.state["quiet"]["小分队"] = rev2
        fired.clear()
        main.fire_quiet("小分队", rev2)
        assert not fired, "用户已经自己说话了，不该再替他生成"

        # 安静窗口必须**跟着设置页那个框走**。上一版这里读的是硬编码的 AUTO_QUIET_SECONDS，
        # 用户在设置页填 5 也没用，界面上永远「等安静 8 秒」——这正是用户报的那个 bug。
        real_wait = settings.auto_send_any_wait
        settings.auto_send_any_wait = lambda: 3
        try:
            main.arm_quiet("小分队")
            assert ov.timers[-1][0] == 3000, f"该用用户填的值，实际 {ov.timers[-1][0]}"
            main.state["quiet"].clear()
        finally:
            settings.auto_send_any_wait = real_wait
        print("main 群里安静窗口 ok")
    finally:
        settings.auto_send_group_any, settings.my_name, main.start_analyze = real


def main_check() -> int:
    check_input_has_text()
    check_at_me()
    check_auto_pick()
    check_group_any()
    check_quiet_window()
    path = _tmp_config({"relationship": "朋友"})
    try:
        check_settings_defaults(path)
        check_settings_save(path)
        # 造 Overlay 之前先把配置复位，免得 _load_settings 读到上一个检查留下的开关
        _write(path)
        ov = Overlay(on_fill=lambda *a: None)
        check_countdown(ov)
        check_auto_fields(ov, path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    check_main_gates()
    check_send_gate()
    print("自动发送检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main_check())
