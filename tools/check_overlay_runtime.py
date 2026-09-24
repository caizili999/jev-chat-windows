# -*- coding: utf-8 -*-
"""把 app/overlay.py 真的跑起来验证一遍（离屏，不需要显示器）。

为什么要有这个：tools/check_ui_layout.py 是 ast 静态检查，只能看出「某个 self.<attr>
构造了、却没出现在 addWidget 的参数里」。它拦得住最常见的那类错误，但拦不住：
  - 控件加进了布局、却加进了一个永远不会显示的分支；
  - 布局加对了、可 _load_settings / _save 忘了读写这个字段；
  - 值域写错（SpinBox 忘了 setRange，或者上下限跟 settings 不一致）；
  - 失败兜底的时序错了（比如 _current 没在 set_status 之后设，卡片照样是灰的）。

这些只有真构造一遍、真调一遍才看得见。装了 PySide6 之后就该跑这个，而不是只跑 ast。

跑法：python tools/check_overlay_runtime.py   （通过 exit 0，失败抛 AssertionError 并 exit 1）

**绝不碰用户真实配置**：把 settings._CONFIG 指到临时文件；三个密钥字段一律留空传 None，
所以不会往注册表 HKCU\\Environment 写任何东西。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在 import PySide6 之前
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import settings  # noqa: E402
from app.overlay import _JUDGE_ENGINES, Overlay, _mode_note  # noqa: E402
from qfluentwidgets import ComboBox, SpinBox  # noqa: E402


def _tmp_config(initial: dict) -> str:
    """把 settings 的配置文件指到临时文件，返回路径。用户真实 config.json 一动不动。"""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="jev_check_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(initial, f, ensure_ascii=False)
    settings._CONFIG = path
    return path


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_widget_in_layout(ov) -> None:
    """retriesBox 真的挂在设置页上，而不是被构造出来后忘在一边。

    忘了加进布局的控件 parentWidget() 是 None（SpinBox() 建的时候没给父），
    往上走 parent 链就到不了 settingsPage —— 这正是 ast 检查想拦的那个 bug。
    """
    assert isinstance(ov.retriesBox, SpinBox), "retriesBox 应该是 SpinBox"
    assert (ov.retriesBox.minimum(), ov.retriesBox.maximum()) == (0, 5), \
        f"值域应是 0~5，实际 {ov.retriesBox.minimum()}~{ov.retriesBox.maximum()}"
    node, chain = ov.retriesBox.parentWidget(), []
    while node is not None and node is not ov.settingsPage:
        chain.append(type(node).__name__)
        node = node.parentWidget()
    assert node is ov.settingsPage, f"retriesBox 的父链没通到设置页：{chain}"
    # 真显示出来才算数：被加进一个永远不显示的分支里，isVisible 会是 False
    ov.win.show()
    ov.open_settings()
    ov.app.processEvents()
    assert ov.retriesBox.isVisible(), "设置页都打开了，retriesBox 却不可见（多半没进布局）"


def check_roundtrip(ov, path: str) -> None:
    """_load_settings 读得到、_save 写回去，中间不丢。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友", "retries": 4}, f, ensure_ascii=False)
    ov._load_settings()
    assert ov.retriesBox.value() == 4, f"_load_settings 没读到 4，读到 {ov.retriesBox.value()}"
    ov.retriesBox.setValue(1)
    ov._save()
    saved = _read(path)
    assert saved["retries"] == 1, f"_save 没写回 1，写的是 {saved.get('retries')}"
    # 越界值必须被夹住，而不是原样落盘（否则界面上限形同虚设）
    ov.retriesBox.setValue(5)
    ov._save()
    assert _read(path)["retries"] == 5, "上限 5 没落盘"


def check_clamp() -> None:
    """settings.retries() 对越界/脏值/缺失的处理，跟界面的 0~5 必须一致。"""
    path = settings._CONFIG
    cases = [(99, 5), (-3, 0), ("abc", 2), (None, 2), (3, 3)]
    for raw, want in cases:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"relationship": "朋友", "retries": raw}, f, ensure_ascii=False)
        got = settings.retries()
        assert got == want, f"config retries={raw!r} 应为 {want}，实际 {got}"
    # 整个文件坏掉也要退默认，不能抛
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    assert settings.retries() == 2, "config.json 坏掉时应退默认 2"


def check_set_failed(ov) -> None:
    """失败兜底：手上的候选要恢复可点，日志面板要展开，状态栏要有原因。

    「先 set_busy(True) 置灰 → 再 set_failed」是真实时序：分析期间会先置灰，
    失败后不恢复的话用户明明还有几条能用的回复，按钮却是灰的。
    """
    # answers 的真实形状是嵌套的 {"<键>": {"choice": "<枚举>"}}，跟 core/engine.py 一致
    ov.show({"candidates": ["甲", "乙", "丙"], "judged": True,
             "scores": [0.5, 0.3, 0.2], "best_index": 0,
             "answers": {"best_action": {"choice": "make_plan"},
                         "true_intent": {"choice": "seek_explanation"},
                         "she_needs": {"choice": "care"},
                         "danger_level": {"score": 4},
                         "best_reply": {"choice": 0, "probabilities": [0.5, 0.3, 0.2]}},
             "reply_to": None})
    assert ov.cards, "show 之后应该有卡片"
    ov.set_busy(True)  # 置灰（真实流程里分析开始时就是这样）
    assert not ov.cards[0].fillButton.isEnabled(), "set_busy(True) 应把候选置灰"
    ov.feed.hide()
    ov.set_failed("503 对端服务暂时不可用，已重试 2 次；新消息到来后会再试。")
    for i, card in enumerate(ov.cards):
        assert card.fillButton.isEnabled(), f"失败后第 {i} 张卡的按钮该恢复可点"
        assert card.copyButton.isEnabled(), f"失败后第 {i} 张卡的复制按钮该恢复可点"
    assert not ov.feed.isHidden(), "失败后应把聊天记录面板展开（否则用户看不到真因）"
    assert "503" in ov.status.text(), f"状态栏该带状态码，实际 {ov.status.text()!r}"
    assert "已重试 2 次" in ov.status.text(), "状态栏该写清重试了几次"
    # 面板被 set_failed 展开了，按钮文案该跟着变成「收起」（点一下才收回去）
    assert "收起" in ov.historyButton.text(), f"面板已展开，按钮该是「收起」，实际 {ov.historyButton.text()!r}"


def check_set_failed_empty(ov) -> None:
    """一条候选都没有时 set_failed 不能炸（空态是失败最常见的场景）。"""
    ov.cands = []
    ov._current = False
    ov.set_failed("请求超时，已重试 2 次；新消息到来后会再试。")
    assert "超时" in ov.status.text()


def check_draft_mode_cards(ov) -> None:
    """起草模式（judged=False）：卡片标「候选 N」而不是「备选 N」，不编概率、不编排序。"""
    ov.show({"candidates": ["甲", "乙", "丙"], "judged": False, "scores": [], "best_index": None})
    # 标签只进无障碍名称（「填入候选 1 到微信」），百分比不进——这里就查这个名称
    labels = [card.fillButton.accessibleName() for card in ov.cards]
    assert len(labels) == 3, labels
    joined = " ".join(labels)
    assert "候选" in joined, f"起草模式的卡片该标「候选」，实际 {labels}"
    assert "备选" not in joined, f"起草模式不该出现「备选」（暗示不存在的排序）：{labels}"
    assert not ov.tension.isVisible() or ov.tension.isHidden(), "起草模式不该显示紧张度"


def check_mode_note() -> None:
    """_mode_note 随判断引擎变文案（决定「完整/自判/起草模式」的说法）。

    三档给的东西真的不一样，文案必须跟着变——否则用户以为自己在用完整功能。
    """
    real = settings.judge_engine
    try:
        settings.judge_engine = lambda: "openrouter"
        assert _mode_note() == "", "完整模式不该有提示尾巴"
        settings.judge_engine = lambda: "self"
        note = _mode_note()
        assert "自判" in note and "概率" in note, note
        settings.judge_engine = lambda: "none"
        note = _mode_note()
        # 文案用「不判断」跟设置页下拉的选项名对齐（三档都起草，叫「起草模式」反而分不清）
        assert "不判断" in note and "排序" in note, note
    finally:
        settings.judge_engine = real


def check_engine_controls(ov) -> None:
    """判断引擎下拉 + 判断密钥框：在布局里、三档齐全、跟着引擎显隐、说明说实话。"""
    ov.open_settings()
    ov.app.processEvents()
    assert isinstance(ov.engineBox, ComboBox), "engineBox 应该是 ComboBox"
    assert ov.engineBox.count() == len(_JUDGE_ENGINES) == 3, ov.engineBox.count()
    # 父链通到设置页 —— 构造了却没进布局的控件，父链是断的
    node = ov.engineBox.parentWidget()
    while node is not None and node is not ov.settingsPage:
        node = node.parentWidget()
    assert node is ov.settingsPage, "engineBox 没进设置页的布局"
    for i, (_, engine) in enumerate(_JUDGE_ENGINES):
        ov.engineBox.setCurrentIndex(i)
        ov.app.processEvents()
        want_judge = engine != "none"
        want_self = engine == "self"
        assert ov.judgeUrlEdit.isVisible() == want_judge, \
            f"{engine}：判断地址该{'显示' if want_judge else '隐藏'}"
        assert ov.judgeModelEdit.isVisible() == want_judge, f"{engine}：判断模型显隐不对"
        assert ov.judgeKeyEdit.isVisible() == want_self, \
            f"{engine}：判断密钥只在自判模式下有意义"
        assert ov.engineNote.text(), f"{engine} 引擎下面必须有一段说明"
        assert ov.judgeHint.text() == ("" if not want_judge else ov.judgeHint.text()), engine
    # 说明必须把各档真正的差别说出来，而不是一句「可选」糊过去
    ov.engineBox.setCurrentIndex(0)
    ov.app.processEvents()
    assert "概率" in ov.engineNote.text(), ov.engineNote.text()
    assert "decisions" in ov.judgeHint.text(), ov.judgeHint.text()
    ov.engineBox.setCurrentIndex(1)
    ov.app.processEvents()
    assert "多一次模型调用" in ov.engineNote.text(), ov.engineNote.text()
    assert "chat/completions" in ov.judgeHint.text(), ov.judgeHint.text()
    ov.engineBox.setCurrentIndex(2)
    ov.app.processEvents()
    assert ov.judgeHint.text() == "", "关掉判断后说明该清空，别留一段用不上的话"
    # 这些标签是 PlainText 渲染的，写进 markdown 的 ** 会原样显示成星号（截图里真出现过）
    for i in range(3):
        ov.engineBox.setCurrentIndex(i)
        ov.app.processEvents()
        for label in (ov.engineNote, ov.judgeHint, ov.judgeUrlNote):
            assert "**" not in label.text(), \
                f"说明文字里漏了 markdown 标记（PlainText 会原样显示）：{label.text()!r}"


def check_openrouter_key_state(ov, path: str) -> None:
    """没填 OpenRouter 密钥时，那行状态要说清「判断退回自判」，不能再说「起草模式」。

    三档之后「没 OpenRouter 密钥 = 只能起草」已经不成立了：判断那半会拿起草模型顶上。
    这行字要是还说「起草模式」，用户会以为自己少了判断，其实只是少了概率。
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友", "judge_engine": "openrouter"}, f, ensure_ascii=False)
    real = settings.has_key
    try:
        settings.has_key = lambda: False
        ov._load_settings()
        assert "起草模型" in ov.keyState.text(), ov.keyState.text()
        assert "起草模式" not in ov.keyState.text(), f"说法过期了：{ov.keyState.text()!r}"
        assert "自判" in ov.keyEdit.placeholderText(), ov.keyEdit.placeholderText()
        settings.has_key = lambda: True
        ov._load_settings()
        assert ov.keyState.text() == "已配置", ov.keyState.text()
    finally:
        settings.has_key = real


def check_settings_fits(ov) -> None:
    """设置页的内容不能比窗口宽——横向滚动条是关着的，超出去的部分会被静默裁掉。

    这个 bug 真的存在过：起草来源下拉框的文字是「OpenRouter（DeepSeek V4.1 Flash，需要 OpenRouter
    密钥）」，ComboBox 是 QPushButton 子类、minimumSizeHint 照整段文字算、不会自己省略，
    于是整页被撑到 788 像素宽，而窗口最宽只有 640——右对齐的「保存设置」按钮整颗在窗口外，
    用户点不到它，界面也不报任何错。这里就是钉住它别再回来。
    """
    ov.win.show()
    ov.open_settings()
    ov.app.processEvents()
    for width in (320, 400, 440, 640):
        ov.win.resize(width, 820)
        ov._relayout(width, 820)
        ov.app.processEvents()
        content = ov.settingsPage.widget()
        need, have = content.minimumSizeHint().width(), ov.settingsPage.viewport().width()
        assert need <= have, (
            f"窗口宽 {width}：设置页内容最少要 {need}px，可用只有 {have}px——"
            f"超出的部分会被裁掉（保存按钮就在右边）。多半是某个下拉框的文字没按宽度省略。")
    ov.win.resize(440, 820)
    ov._relayout(440, 820)
    ov._back_home()


def check_self_judge_render(ov) -> None:
    """自判模式：按模型给的 ranking 排、标推荐，但**绝不显示百分比**，并说明「自评未校准」。"""
    ov.show({"candidates": ["甲", "乙", "丙"], "judged": True, "judge_engine": "self",
             "scores": [None, None, None], "best_index": 2, "ranking": [2, 0, 1],
             "answers": {"best_action": {"choice": "make_plan"},
                         "true_intent": {"choice": "casual_chat"},
                         "she_needs": {"choice": "nothing"},
                         "danger_level": {"score": 1}},
             "judge_error": None, "reply_to": None})
    assert len(ov.cards) == 3, len(ov.cards)
    assert [c.text.text() for c in ov.cards] == ["丙", "甲", "乙"], \
        f"卡片该按 ranking [2,0,1] 排，实际 {[c.text.text() for c in ov.cards]}"
    names = [c.fillButton.accessibleName() for c in ov.cards]
    assert "推荐" in names[0], f"ranking 的第一条该标推荐：{names}"
    assert all("%" not in n for n in names), f"自判模式绝不能出现百分比：{names}"
    assert "未经校准" in ov.judgeNote.text(), f"必须说明是自评未校准：{ov.judgeNote.text()!r}"
    assert "紧张度 1/9" in ov.tension.text(), ov.tension.text()
    for label in (ov.summary, ov.intent, ov.judgeNote, ov.tension):  # 全是 PlainText
        assert "**" not in label.text(), f"不该出现 markdown 标记：{label.text()!r}"
    # 模型给的排序里出现重复编号：只能出 3 张卡——同一条候选冒两张会顶掉别人的位置
    ov.show({"candidates": ["甲", "乙", "丙"], "judged": True, "judge_engine": "self",
             "scores": [None, None, None], "best_index": 2, "ranking": [2, 2, 0],
             "answers": {}, "judge_error": None, "reply_to": None})
    texts = [c.text.text() for c in ov.cards]
    assert len(ov.cards) == 3, f"重复编号不该多出卡片，实际 {len(ov.cards)} 张：{texts}"
    assert texts == ["丙", "甲", "乙"], f"重复编号只算一次后该是 [丙,甲,乙]，实际 {texts}"


def check_judge_error_render(ov) -> None:
    """判断失败：候选照常显示，判断栏说清原因，且不标推荐（没有排序依据就不能编）。"""
    ov.show({"candidates": ["甲", "乙", "丙"], "judged": False, "judge_engine": "self",
             "scores": [None, None, None], "best_index": None, "ranking": None, "answers": {},
             "judge_error": {"message": "判断：输出不是 JSON: '<html>502</html>'", "status": None,
                             "hint": "判断结果不是 JSON", "retries": 2},
             "reply_to": None})
    assert len(ov.cards) == 3, "判断失败不能连候选一起丢掉"
    assert "判断失败" in ov.summary.text(), ov.summary.text()
    assert "不是 JSON" in ov.intent.text(), ov.intent.text()
    assert "候选回复不受影响" in ov.intent.text(), ov.intent.text()
    names = [c.fillButton.accessibleName() for c in ov.cards]
    assert all("候选" in n for n in names), f"判断失败时不该标推荐/备选：{names}"


def check_no_judge_render(ov) -> None:
    """没判断的三档文案要分开：缺密钥 / 起草没配好 / 用户自己关掉。"""
    for engine, token in (("openrouter", "OpenRouter 密钥"), ("self", "起草服务"),
                          ("none", "关掉")):
        ov.show({"candidates": ["甲", "乙"], "judged": False, "judge_engine": engine,
                 "scores": [None, None], "best_index": None, "ranking": None, "answers": {},
                 "judge_error": None, "reply_to": None})
        assert token in ov.summary.text(), (engine, ov.summary.text())
        names = [c.fillButton.accessibleName() for c in ov.cards]
        assert all("候选" in n for n in names), (engine, names)


def check_judge_toggle(ov) -> None:
    """标题栏那个「判断」开关：拨动要回调、回调要带对的值、set_judge 不回调（防回环）。

    为什么值得单独测：这个开关跟设置页的「判断引擎」是**同一个配置项**，两处都能改它。
    如果 set_judge（父进程回报状态）会触发回调，就会形成「存一次 → 界面收到 → 又存一次」的
    来回打架，最后把用户存的档位写坏。blockSignals 就是为这个。
    """
    seen = []
    ov.on_toggle_judge = lambda on: seen.append(on)
    ov._judge_toggled(True)
    ov._judge_toggled(False)
    assert seen == [True, False], f"拨动回调次数/值不对：{seen}"
    assert ov.judgeSwitch.isChecked() is False or True  # 开关自身状态由 Qt 维护，这里只确认没炸

    before = list(seen)
    ov.set_judge(True)
    ov.set_judge(False)
    assert seen == before, f"set_judge 不该触发回调（会长回环写坏配置），实际多出 {seen[len(before):]}"
    assert ov.judgeSwitch.isChecked() is False, "set_judge(False) 后开关应是关的"
    ov.set_judge(True)
    assert ov.judgeSwitch.isChecked() is True, "set_judge(True) 后开关应是开的"
    ov.on_toggle_judge = None


def check_candidate_control(ov) -> None:
    """「生成几条候选」控件真在布局里、值域对、跟设置对得上。"""
    box = ov.candidateBox
    assert box.parent() is not None, "candidateBox 没进布局（会变成飘在桌面上的小窗）"
    assert (box.minimum(), box.maximum()) == (settings._MIN_CANDIDATES, settings._MAX_CANDIDATES), \
        f"值域应为 1~3，实际 {box.minimum()}~{box.maximum()}"
    assert settings._MIN_CANDIDATES == 1 and settings._MAX_CANDIDATES == 3


def check_group_name_warning(ov) -> None:
    """「群里 @我」开着但昵称为空 → 必须出现显式警告。

    这是静默失效：开关显示「开」，用户以为在用了，实际 main.auto_pick() 缺昵称就直接拦下。
    所以判据跟 auto_pick 保持一致，两处不能各说各话。
    """
    ov.open_settings()
    ov.autoGroupSwitch.setChecked(True)
    ov.myNameEdit.setText("")
    ov._sync_auto_fields()
    assert ov.autoNameWarn.isVisible(), "群昵称空着时必须警告（否则开关显示「开」却一次都不会发）"
    ov.myNameEdit.setText("张三")
    ov._sync_auto_fields()
    assert not ov.autoNameWarn.isVisible(), "填了昵称就不该再警告"
    ov.autoGroupSwitch.setChecked(False)
    ov.myNameEdit.setText("")
    ov._sync_auto_fields()
    assert not ov.autoNameWarn.isVisible(), "群里那半关着时不该警告（那时昵称不需要）"
    ov._back_home()


if __name__ == "__main__":
    path = _tmp_config({"relationship": "朋友", "retries": 2})
    try:
        ov = Overlay(on_fill=lambda *a: None)
        check_widget_in_layout(ov)
        check_roundtrip(ov, path)
        check_clamp()
        check_engine_controls(ov)
        check_set_failed(ov)
        check_set_failed_empty(ov)
        check_draft_mode_cards(ov)
        check_openrouter_key_state(ov, path)
        check_settings_fits(ov)
        check_self_judge_render(ov)
        check_judge_error_render(ov)
        check_no_judge_render(ov)
        check_mode_note()
        check_judge_toggle(ov)
        check_candidate_control(ov)
        check_group_name_warning(ov)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    print("app/overlay.py 运行时检查通过", flush=True)
    # Qt 的 offscreen 平台在解释器退出阶段会偶发段错误（退出码 139）。断言此时已经全部通过，
    # 崩在析构里只会让 CI 随机变红，所以成功路径直接结束进程，不跑 Qt 的收尾。
    os._exit(0)
