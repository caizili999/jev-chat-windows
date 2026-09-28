# -*- coding: utf-8 -*-
"""浅色置顶回复助手：回复建议和独立设置页。默认发送由用户确认；显式开启自动发送后才会按安全闸门发送。"""
from datetime import datetime
from math import isfinite

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QSizeGrip, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, FluentIcon as FIF, HyperlinkButton,
    IndeterminateProgressBar, LineEdit, PasswordLineEdit, PlainTextEdit,
    PrimaryPushButton, PushButton, ScrollArea, SpinBox, SwitchButton, Theme, TransparentToolButton,
    setCustomStyleSheet, setFont, setTheme, setThemeColor,
)

from app import settings
from app.kb import selfcheck as kb_selfcheck
from app.kb import ui as kb_ui
from app.version import VERSION
from core.draft import CHAT_PATH
from core.jev_client import DECISIONS_PATH, normalize_endpoint

_LOG_LINES = 300
_MUTED = "#68776f"
_GREEN = "#18794e"
_RED = "#b44832"
# 下拉框除了文字还要占的内边距 + 箭头宽度。minimumSizeHint 是「文字宽 + 这一圈」，
# 直接拿可用宽度去省略，结果会比可用宽度多出这几十像素，照样把页面撑出窗口。
_COMBO_CHROME = 48
_CHOICES = {
    "true_intent": {
        "confirm_you_care": "希望确认你在意", "vent_anger": "表达不满或受伤",
        "request_action": "希望你采取行动", "seek_explanation": "希望了解原因",
        "casual_chat": "轻松交流", "close_topic": "平和结束话题",
    },
    "best_action": {
        "check_history": "先核对聊天记录", "apologize": "为已知问题道歉",
        "give_commitment": "给出具体承诺", "explain": "说明事实与原因",
        "acknowledge": "回应并表达理解", "say_less": "简短回应或留白",
        "make_plan": "商量具体安排",
    },
    "she_needs": {
        "apology": "真诚道歉", "action": "具体行动或安排", "explanation": "清楚的解释",
        "care": "关注与在意", "nothing": "可能无需补充回应",
    },
}
_RELATIONSHIPS = [
    ("恋人", "romantic partners"), ("朋友", "friends"), ("同事", "colleagues"),
    ("家人", "family"), ("自定义", None),
]
# 判断引擎。选项文字把差别（有没有胜出概率）直接写出来——三档给的东西真的不一样，
# 光叫「完整/自判/起草」用户分不清少了什么。
_JUDGE_ENGINES = [
    ("OpenRouter（有胜出概率，需要 OpenRouter 密钥）", "openrouter"),
    ("用起草模型判断（有摘要和排序，无概率）", "self"),
    ("不判断（只要候选回复）", "none"),
]
_JUDGE_ENGINE_ITEMS = tuple(name for name, _ in _JUDGE_ENGINES)
# 起草来源下拉框的**完整**选项文字。单独拎成常量，是因为界面上的显示文本会被省略成
# 「OpenRouter（DeepSeek V4.1…」，而省略的源文本必须是这里的原文（见 _elide_items）。
_PROVIDER_ITEMS = (
    "OpenRouter（DeepSeek V4.1 Flash，需要 OpenRouter 密钥）",
    "DeepSeek 直连（更快，需要 DeepSeek key）",
    "自定义（任意 OpenAI 兼容地址）",
)


def _choice(answers, name):
    return _CHOICES[name].get((answers.get(name) or {}).get("choice"), "暂未判断")


def _endpoint_note(text, path, what, fallback):
    """地址框下面那行实时说明：空 = 用官方，拼得出 = 显示实际请求，拼不出 = 提示会退回。

    用的是 core 里同一个 normalize_endpoint，所以这行显示的就是真正会请求的地址——
    地址填错时 core 会静默退回官方，用户需要一个看得见的确认点。"""
    text = (text or "").strip()
    if not text:
        return f"{what}留空，用 {fallback}"
    url = normalize_endpoint(text, path)
    return f"实际请求：{url}" if url else f"地址无效，{what}会退回 {fallback}"


def _mode_note():
    """空态卡片上「现在是哪一档」的提示尾巴；完整模式返回空串。

    降级是正式支持的一档，但必须让用户看见——否则他以为自己在用完整功能，
    少了摘要/概率/排序都不知道。三档给的东西真的不一样，文案也得分开说清。
    """
    engine = settings.judge_engine()
    if engine == "openrouter":
        return ""
    if engine == "self":
        return ("\n当前是自判模式：用你自己的模型做判断，有摘要、紧张度和推荐顺序，"
                "但没有胜出概率——自评的百分比没有校准依据，所以不给。")
    return "\n当前是不判断：只给候选回复，没有判断摘要、紧张度和排序。"


def _rank(count, judged, best, scores, ranking=None):
    """候选的展示顺序 + 归一化后的推荐位。返回 (best, order)。

    完整模式：推荐位（API 给的 choice）强制第一，其余按概率降序，同分按原索引。
    自判模式：模型给的是一个**完整排序**（ranking），照它排；没有概率，所以不显示百分比。
    起草模式：没有判断就没有排序——保持生成顺序，且**不标推荐**。编一个「推荐」出来
    没有校准依据，是假信号，比不标更糟。

    纯函数、不碰 Qt，单独拎出来是为了能在没有 PySide6 的机器上离线验证。
    """
    if not judged:
        return None, list(range(count))
    if ranking:
        # 脏索引（越界、非整数、布尔）一律丢掉；重复编号只算一次——不去重的话
        # `show()` 会照着 order 建卡片，同一条候选会冒出两张，还会顶掉别的候选的位置。
        # 模型漏排的按生成顺序补在末尾：**一条候选都不能丢**，宁可顺序不完美，
        # 也不能让用户少看到一条能用的回复。
        order = []
        for i in ranking:
            if (isinstance(i, int) and not isinstance(i, bool)
                    and 0 <= i < count and i not in order):
                order.append(i)
        order += [i for i in range(count) if i not in order]
        return (order[0] if order else None), order
    if not isinstance(best, int) or not 0 <= best < count:
        best = 0 if count else None
    scores = list(scores or [])
    def weight(i):
        """概率缺失/脏数据一律当 0 排，别让 None 把排序炸掉。"""
        v = scores[i] if i < len(scores) else None
        return -v if isinstance(v, (int, float)) else 0
    return best, sorted(range(count), key=lambda i: (i != best, weight(i), i))


def at_me(text, name):
    """这条消息是不是在 @我。只认「@ + 昵称」，**不认裸昵称**。

    群里随口提到我的名字（「这事张三知道」）不该被当成「在叫我」——那是自动发送最容易
    误判的一类，而误判的代价是把一句没人看过的回复发进群里。OCR 偶尔把「@张三」读成
    「@ 张三」，所以只在这一处把空格归一掉。

    **公开**（原来叫 _at_me）：main 排「群里安静窗口」时要判断这条是不是 @我，
    必须用同一个判定，不能各写一份——两边一旦不一致，就会出现「它等安静、它不等」这种
    谁也说不清的行为。
    """
    name = (name or "").replace(" ", "")
    if not name:
        return False
    return f"@{name}" in (text or "").replace(" ", "")


# auto_pick 里那些「不发」的原因中，哪些属于**配置问题**（用户该去改设置），
# 哪些属于**按设计跳过**（正常过滤）。main 用这个区分决定状态栏要不要用警示色：
# 「这条没有 @我」是群里的正常过滤，报警示色反而像出了故障；而「还没填群昵称」是真的配错了。
# 判据放在这里而不是 main 里，是因为这些字符串由 auto_pick 产生，改一处就够了。
AUTO_CONFIG_REASONS = (
    "自动发送没开",
    "还没填群昵称，群里不自动发送",
    "单聊没有开自动发送",
    "群里没有开自动发送",
)


def auto_pick(result, is_group, last_text, my_name, dm_on, group_on, group_any_on=False):
    """要不要自动发送这份结果、发哪一条。返回 (index, note)。

    index 为 None = 不发，note 是原因；index 不是 None = 发，note 是可选的一句提示
    （目前只有「判断关着，发的是第一条」这一种）。

    **没有判断结果时发第一条**，这是刻意的口径（不是漏写）：判断关着就没有「哪条最合适」的
    依据，但用户是**显式**打开了自动发送的——那是他的知情同意。禁止它等于替他否掉自己做的
    决定。候选本身都是模型写的像样回复，发第一条不是发垃圾，只是不保证是最合适的那条；
    想要质量就把判断打开。**但必须说破**：note 会被倒计时条原样显示出来，
    让他知道这次发的不是「判断过的推荐」。

    其余每一条都是「不满足就不发」：
    - 三个开关都关着 → 不发。默认状态，跟升级前一模一样。
    - 单聊看 auto_send_dm，群聊看 auto_send_group——群里的 @ 是靠文本匹配认出来的，
      误判面明显更大，值得让人单独决定。
    - 「群里不@我也回」（group_any_on）**包含** @我 那一半：开着它就等于群里都回，
      @我的消息当然也在内，不必另外再开 group_on。它也不要求填群昵称——那条路不匹配 @。
      之所以是个独立开关，是因为「没被点名」的误判面比 @我 大得多，得让人能单独关掉。
    - 只走 @我 那条路时，必须 @我，且「我的群昵称」填了。留空 = 那条路一律不自动发：
      宁可不发，不可发错。

    纯函数、不碰 Qt，跟 _rank 一样单独拎出来，好在没有界面的机器上离线验证。
    """
    if not (dm_on or group_on or group_any_on):
        return None, "自动发送没开"
    candidates = result.get("candidates") or []
    if not candidates:
        return None, "这一轮没有生成出候选回复"
    index = result.get("best_index")
    note = ""
    if not result.get("judged"):
        # 判断关着（或判断那一步失败了）：没有依据挑最好的那条，发第一条，并明说。
        # 这里不区分「用户关了判断」和「判断失败」——两种情况下拿到的信息是一样的
        # （都没有排序），措辞用中性的「没有判断结果」。
        index = 0
        note = ("没有判断结果（判断关着或失败了），直接发第一条候选，"
                "不一定是最合适的那条。")
    if (not isinstance(index, int) or isinstance(index, bool)
            or not 0 <= index < len(candidates)):
        return None, "没有可用的推荐回复，不自动发送"
    if is_group:
        if group_any_on:
            # 「群里不@我也回」开着 = 群里都回。@我的消息是其中一种，不该反过来被漏掉，
            # 所以这里不要求另外开 group_on；也不查昵称——这条路压根不匹配 @。
            return index, note
        if not group_on:
            return None, "群里没有开自动发送"
        name = (my_name or "").strip()
        if not name:
            return None, "还没填群昵称，群里不自动发送"
        if not at_me(last_text, name):
            return None, "这条没有 @我"
    elif not dm_on:
        return None, "单聊没有开自动发送"
    return index, note


def _label(text="", size=14, color=None, bold=False, parent=None):
    label = BodyLabel(text, parent)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    setFont(label, size, QFont.DemiBold if bold else QFont.Normal)
    if color:
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(label, qss, qss)
    return label


def _tool(icon, title, callback, parent=None):
    button = TransparentToolButton(icon, parent)
    button.setFixedSize(32, 32)
    button.setToolTip(title)
    button.setAccessibleName(title)
    button.clicked.connect(callback)
    return button


class _Surface(CardWidget):
    def __init__(self, parent=None, accent=False, warn=False):
        # warn 用在「N 秒后自动发送」那条上：它是唯一不可逆的动作，配色得跟推荐卡的浅绿分开，
        # 一眼就能看出「这条不是建议，是即将发生的事」。
        self.accent = accent
        self.warn = warn
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def _normalBackgroundColor(self):
        if self.warn:
            return QColor("#fdf1e0")
        return QColor("#edf7f0" if self.accent else "#ffffff")

    def _hoverBackgroundColor(self):
        return self._normalBackgroundColor()

    def _pressedBackgroundColor(self):
        return self._normalBackgroundColor()


class _TitleBar(QWidget):
    """只有标题栏可拖动，选择正文或按按钮不会意外移动窗口。"""
    def __init__(self, parent):
        super().__init__(parent)
        self._drag = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)


class _MainWindow(QWidget):
    """窗口大小变了就叫 Overlay 重新排布；断点没跨过时 _relayout 自己不做事，这里不用防抖。"""
    def __init__(self, relayout):
        super().__init__()
        self._relayout = relayout

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout(event.size().width(), event.size().height())


class _ReplyCard(_Surface):
    def __init__(self, owner, index, recommended=False, number=1, score=None, judged=True):
        super().__init__(accent=recommended)
        box = QVBoxLayout(self)
        self.box = box
        box.setSpacing(10)
        top = QHBoxLayout()
        if recommended:
            label = "推荐回复"
        elif judged:
            label = f"备选 {number}"
        else:
            # 起草模式没有排序，别用「备选」——那会暗示存在一个并不存在的排序
            label = f"候选 {number + 1}"
        tag = label  # 百分比只进显示文本，不进无障碍名称
        if score is not None:
            label += f" · {round(score * 100)}%"
        top.addWidget(_label(label, 12, _GREEN if recommended else _MUTED, True))
        self.copyButton = _tool(FIF.COPY, "复制这条回复", lambda: owner._copy(index), self)
        self.copyButton.setFixedSize(24, 24)
        top.addWidget(self.copyButton)
        box.addLayout(top)
        self.text = _label(owner.cands[index], 15)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.text)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.fillButton = (PrimaryPushButton if recommended else PushButton)("填入微信", self)
        self.fillButton.setAccessibleName(f"填入{tag}到微信")
        self.fillButton.clicked.connect(lambda: owner._fill(index))
        bottom.addWidget(self.fillButton)
        box.addLayout(bottom)
        self.set_compact(owner._compact)

    def set_available(self, enabled):
        self.fillButton.setEnabled(enabled)
        self.copyButton.setEnabled(enabled)

    def set_compact(self, compact):
        self.box.setContentsMargins(12, 8, 12, 8) if compact else self.box.setContentsMargins(16, 12, 16, 12)
        self.fillButton.setMinimumWidth(80 if compact else 100)


class Overlay:
    def __init__(self, on_fill, on_toggle_capture=None, on_target_change=None, result_of=None,
                 on_auto_send=None, on_settings_change=None, on_toggle_judge=None,
                 kb=None, on_kb_change=None):
        """result_of(会话名) → 那个会话上次的结果或 None；切着看别的会话时用它把旧结果放回来。
        on_target_change(会话名, 人名) → 用户在群里挑了回复对象。
        on_auto_send(文本) → 倒计时走完、该按发送键了。**界面只负责倒计时和取消**，
        真正发不发由 main 在那一下重新确认（输入框空不空、会话有没有被切走）——两处都过了才发。
        on_settings_change() → 设置存下来了。main 用它同步子进程要不要盯输入框（见 sync_watch_input）。
        on_toggle_judge(要不要判断) → 标题栏那个「判断」开关被拨动了。**由 main 负责存**，
        界面不直接写配置文件——开关的持久化只有一条路，省得以后两处写法不一致。
        kb → KbStore。**不给就不出现任何知识库控件**（设置页那一整块、首页那行计数、
        「存为联系人」按钮全都藏起来），行为跟加这个功能之前一模一样——几个离线工具
        构造 Overlay 时就是这么用的。
        on_kb_change() → 知识库内容变了（新建/编辑/删除/清空/一键存联系人）。main 用它刷新计数。
        """
        self.app = QApplication.instance() or QApplication([])
        setTheme(Theme.LIGHT)
        setThemeColor(_GREEN, save=False)
        self.on_fill = on_fill
        self.on_toggle_capture = on_toggle_capture
        self.on_target_change = on_target_change
        self.result_of = result_of
        self.on_auto_send = on_auto_send
        self.on_settings_change = on_settings_change
        self.on_toggle_judge = on_toggle_judge
        self.kb = kb
        self.on_kb_change = on_kb_change
        self.kbWindow = None   # 懒建：用户不点「知识库与联系人」就不构造那个窗口
        self.cands = []
        self.cards = []
        self._busy = False
        self._current = False
        self._compact = None  # 断点模式：None 保证 _relayout 第一次调用必定生效
        self._pageLayouts = []
        self._hintLabels = []
        self.feeds = {}  # {会话名: [排好版的记录]}
        self.counts = {}  # {会话名: 消息条数}
        self.hers = {}  # {会话名: 对方最近一句}
        self.targets = {}  # {会话名: ([发言人], 当前回复对象)}
        self._chat = ""  # 微信当前开着的会话
        self._shown = ""  # 界面上正在看的会话（浏览时和上面不一样）
        # 自动发送的倒计时。_autoPending 是「这一枪还开着吗」——取消和到点之间有个 150ms 的
        # 空档，没有它就会出现「点了取消照样发出去」。
        self._autoText = ""
        self._autoLeft = 0
        self._autoPending = False
        # 每一轮倒计时一个序号。取消后留下的 150ms singleShot 不能误伤下一轮：
        # 旧回调如果在新 begin_auto() 之后才到，会把**新**的倒计时条收掉、甚至触发新消息发送。
        # 这就是「旧定时器串台」——布尔 _autoPending 不够区分两轮，必须带代数。
        self._autoSerial = 0
        self._autoTimer = QTimer()
        self._autoTimer.setInterval(1000)
        self._autoTimer.timeout.connect(self._auto_tick)
        self.win = _MainWindow(self._relayout)
        self.win.setObjectName("assistantWindow")
        self.win.setWindowTitle("Jev · 微信回复助手")
        self.win.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.win.setStyleSheet(
            "QWidget#assistantWindow { background: #f5f7f6; border: 1px solid #dce3de; border-radius: 14px; }"
        )
        self.win.setMinimumWidth(320)
        self.win.setMaximumWidth(640)
        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header = _TitleBar(self.win)
        title = QHBoxLayout(header)
        title.setContentsMargins(18, 12, 10, 10)
        title.setSpacing(8)
        name = _label("Jev", 20, "#233c2f", True)
        name.setFixedWidth(40)
        name.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(name)
        self.subtitle = _label("微信回复助手", 12, _MUTED)
        self.subtitle.setAttribute(Qt.WA_TransparentForMouseEvents)
        title.addWidget(self.subtitle, 1)
        # 「判断」一键开关：点一下立刻生效，不用进设置页、不用点保存。
        # 它跟设置页的「判断引擎」是同一个配置项（复用 judge_engine，不新增键）——
        # 这里关掉 = 存 none，打开 = 恢复你上次存的那一档（openrouter / self）。
        # 之所以做成按钮而不是又加一个开关项：用户抱怨的是「慢」，要的是随手关掉，
        # 而设置页那个下拉要点三次 + 滚到底 + 保存。
        self.judgeSwitch = SwitchButton(header)
        self.judgeSwitch.setOnText("判断")
        self.judgeSwitch.setOffText("不判断")
        self.judgeSwitch.setToolTip("开启或关闭意图判断和排序（关掉只给候选回复，更快）")
        self.judgeSwitch.setAccessibleName("开启或关闭意图判断和排序")
        self.judgeSwitch.checkedChanged.connect(self._judge_toggled)
        title.addWidget(self.judgeSwitch)
        self.captureSwitch = SwitchButton(header)
        self.captureSwitch.setOnText("采集中")
        self.captureSwitch.setOffText("已暂停")
        self.captureSwitch.setToolTip("开启或暂停微信采集")
        self.captureSwitch.setAccessibleName("开启或暂停微信采集")
        self.captureSwitch.setChecked(True)
        self.captureSwitch.checkedChanged.connect(self._capture_toggled)
        title.addWidget(self.captureSwitch)
        self.settingsButton = _tool(FIF.SETTING, "设置", self.open_settings, header)
        title.addWidget(self.settingsButton)
        title.addWidget(_tool(FIF.REMOVE, "最小化", self.win.showMinimized, header))
        title.addWidget(_tool(FIF.CLOSE, "关闭助手", self.win.close, header))
        outer.addWidget(header)
        self.updateBar = QWidget(self.win)
        update_row = QHBoxLayout(self.updateBar)
        update_row.setContentsMargins(18, 4, 8, 4)
        update_row.setSpacing(8)
        self.updateLabel = _label("", 12, _GREEN, True)
        update_row.addWidget(self.updateLabel, 1)
        self.updateLink = HyperlinkButton("", "去下载", self.updateBar)
        self.updateLink.setFixedHeight(24)
        update_row.addWidget(self.updateLink)
        closeUpdate = TransparentToolButton(FIF.CLOSE, self.updateBar)
        closeUpdate.setFixedSize(20, 20)
        closeUpdate.setToolTip("关闭更新提示")
        closeUpdate.setAccessibleName("关闭更新提示")
        closeUpdate.clicked.connect(lambda: self.updateBar.hide())
        update_row.addWidget(closeUpdate)
        self.updateBar.setFixedHeight(32)
        self.updateBar.hide()
        outer.addWidget(self.updateBar)
        self.pages = QStackedWidget(self.win)
        outer.addWidget(self.pages, 1)
        # 页脚在 _build_settings 之前就建好：_load_settings()（在 _build_settings 末尾）要按存下的
        # 开关刷这行字。控件先建、布局后挂，顺序上页脚仍在最下面，样子没变。
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 9, 8, 8)
        self.footerNote = _label("", 11, _MUTED)
        footer.addWidget(self.footerNote, 1)
        grip = QSizeGrip(self.win)
        grip.setFixedSize(16, 16)
        footer.addWidget(grip, 0, Qt.AlignBottom)
        outer.addLayout(footer)
        self._build_home()
        self._build_settings()
        screen = self.app.primaryScreen().availableGeometry()
        self.win.setMinimumHeight(min(360, screen.height() - 32))
        self.win.resize(min(440, screen.width() - 32), min(820, screen.height() - 48))
        self.win.move(screen.right() - self.win.width() - 20, screen.top() + 24)
        self._relayout(self.win.width(), self.win.height())  # resizeEvent 补不到构造时这一次
        if settings.draft_ready():
            self.set_status("等待新消息", "idle")
        else:
            self.set_status(settings.draft_problem() + "，去设置里补上", "warning")
        self.win.show()

    def _scroll_page(self):
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("pageContent")
        content.setStyleSheet("QWidget#pageContent { background: transparent; }")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 8, 20, 12)
        layout.setSpacing(14)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        self._pageLayouts.append(layout)
        return scroll, layout

    def _relayout(self, w, h):
        """宽度跨过断点才重摆布局（省事）；但下拉框的文字省略要跟着每次宽度重算，
        高度也每次都重算，反正只是设个定高。"""
        compact = w < 400
        if compact != self._compact:
            self._compact = compact
            self._apply_compact(compact)
        else:
            # 没跨断点也得刷一次：两个下拉框的文字是按窗口宽度省略的（见 _elide_items），
            # 窗口变宽了不重算，就会一直显示着刚才那个更短的省略结果。
            self._fit_combo_text()
        self.feed.setFixedHeight(max(100, min(240, int(h * 0.25))))

    def _avail_width(self):
        """设置页里一个控件最多能有多宽：窗口宽减掉页边距和卡片内边距。

        刻意只看**窗口宽度**，不看滚动区的 viewport：viewport 宽度取决于内容宽度，而内容宽度
        又取决于这里算出来的省略长度，两边互相咬就会抖。窗口宽度不依赖内容，没有这个环。
        """
        return max(160, self.win.width() - 72)

    def _elide_items(self, box, items):
        """把下拉框的显示文字按当前可用宽度省略。

        ComboBox 是 QPushButton 的子类，minimumSizeHint 照整段文字算、不会自己省略，而设置页的
        横向滚动条是关着的——不省略的话，「OpenRouter（DeepSeek V4.1 Flash，需要 OpenRouter 密钥）」
        会把整页撑到 700 多像素宽（窗口最宽才 640），右边那一列连同「保存设置」按钮被静默裁掉，
        用户根本点不到它，界面也不报任何错。

        源文本取自 items（原文），**不取 currentText()**——后者已经是上一轮省略过的结果，
        窗口变宽时回不去，会越省略越短。_COMBO_CHROME 是下拉框自己的内边距和箭头宽度，
        不减掉的话省略完还是比可用宽度多出几十像素。
        """
        index = max(0, min(box.currentIndex(), len(items) - 1))
        box.setText(box.fontMetrics().elidedText(
            items[index], Qt.ElideRight, self._avail_width() - _COMBO_CHROME))

    def _fit_combo_text(self):
        """两个文字最长的下拉框：窗口宽度变了就重新省略一遍。"""
        self._elide_items(self.providerBox, _PROVIDER_ITEMS)
        self._elide_items(self.engineBox, _JUDGE_ENGINE_ITEMS)

    def _apply_compact(self, compact):
        """紧凑/常规两套间距和可见性；断点没变时不会被调用。"""
        self.subtitle.setVisible(not compact)
        self.captureSwitch.setOnText("" if compact else "采集中")
        self.captureSwitch.setOffText("" if compact else "已暂停")
        self.judgeSwitch.setOnText("" if compact else "判断")
        self.judgeSwitch.setOffText("" if compact else "不判断")
        for label in self._hintLabels:
            label.setVisible(not compact)
        self.referenceNote.setVisible(bool(self.cands) and not compact)
        self._sync_ds_fields()
        self._sync_engine_fields()
        self._sync_auto_fields()
        margins = (12, 8, 12, 12) if compact else (20, 8, 20, 12)
        for layout in self._pageLayouts:
            layout.setContentsMargins(*margins)
        for card in self.cards:
            card.set_compact(compact)

    def _build_home(self):
        self.home, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_label("回复建议", 23, "#24382d", True), 1)
        self.updated = _label("", 11, _MUTED)
        self.updated.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading.addWidget(self.updated)
        body.addLayout(heading)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        prefix = _label("当前会话", 12, _MUTED)
        prefix.setFixedWidth(56)
        chat_row.addWidget(prefix)
        self.chatBox = ComboBox()
        self.chatBox.setMinimumWidth(0)  # 别让会话名的长度撑开整行，宽度交给 stretch
        self.chatBox.setPlaceholderText("尚未识别到会话")
        self.chatBox.setAccessibleName("当前会话")
        self.chatBox.setToolTip("微信切到哪个会话这里就跟到哪个；也可以自己选一个，只看它的记录和建议")
        self.chatBox.currentIndexChanged.connect(self._on_chat_selected)
        chat_row.addWidget(self.chatBox, 1)
        self.chatFollow = _label("", 11, _MUTED)
        self.chatFollow.setFixedWidth(52)
        self.chatFollow.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        chat_row.addWidget(self.chatFollow)
        body.addLayout(chat_row)
        self.targetRow = QWidget()  # 只有开了「群聊指定回复对象」且这个会话是群聊才露出来
        target_row = QHBoxLayout(self.targetRow)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        target_prefix = _label("回复对象", 12, _MUTED)
        target_prefix.setFixedWidth(56)
        target_row.addWidget(target_prefix)
        self.targetBox = ComboBox()
        self.targetBox.setMinimumWidth(0)  # 人名长度不定，别让它撑开整行
        self.targetBox.setAccessibleName("回复对象")
        self.targetBox.setToolTip("三条候选都按这个人来写；不选就跟着最近说话的那位")
        self.targetBox.currentIndexChanged.connect(self._on_target_selected)
        target_row.addWidget(self.targetBox, 1)
        self.atCheck = CheckBox("填入时带 @")
        self.atCheck.setChecked(True)
        self.atCheck.setToolTip("填入时在开头加「@名字 」。只是普通文字，微信不会认成真正的 @")
        target_row.addWidget(self.atCheck)
        self.targetRow.hide()
        body.addWidget(self.targetRow)
        self.status = _label("", 12, _MUTED)
        body.addWidget(self.status)
        # 「这一轮到底带了什么」——知识库命中几条笔记、带了几条历史。用户开了知识库之后，
        # 唯一能当场确认「它真的用上了」的地方就是这一行；两条都是 0 时写「未用知识库」，
        # 免得让人以为带了东西其实没有。没有知识库（kb=None）时整行不出现。
        self.kbLine = _label("", 11, _MUTED)
        body.addWidget(self.kbLine)
        if self.kb is None:
            self.kbLine.hide()
        self.progress = IndeterminateProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.hide()
        body.addWidget(self.progress)
        self.context = QWidget()
        context_box = QVBoxLayout(self.context)
        context_box.setContentsMargins(0, 0, 0, 0)
        context_box.setSpacing(5)
        context_box.addWidget(_label("对方最近说", 11, _MUTED))
        self.latest = _label("", 14, "#42574a")
        self.latest.setTextInteractionFlags(Qt.TextSelectableByMouse)
        context_box.addWidget(self.latest)
        self.context.hide()
        body.addWidget(self.context)

        self.insight = _Surface()
        insight_box = QVBoxLayout(self.insight)
        insight_box.setContentsMargins(14, 12, 14, 12)
        insight_box.setSpacing(7)
        row = QHBoxLayout()
        self.insightTitle = _label("对话参考", 12, _MUTED)
        row.addWidget(self.insightTitle, 1)
        self.tension = _label("", 11)
        self.tension.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.tension)
        insight_box.addLayout(row)
        self.summary = _label("", 14, "#304c3c", True)
        insight_box.addWidget(self.summary)
        self.intent = _label("", 12, _MUTED)
        insight_box.addWidget(self.intent)
        # 自判模式那行「自评、未校准」，以及判断失败时的原因。空串就整行不占地方。
        self.judgeNote = _label("", 11, _MUTED)
        self.judgeNote.setWordWrap(True)
        insight_box.addWidget(self.judgeNote)
        self.insight.setToolTip("根据当前聊天片段推测，可能理解有偏差。紧张度为 0–9 的参考评分。")
        self.insight.hide()
        body.addWidget(self.insight)

        self.empty = _Surface()
        empty_box = QVBoxLayout(self.empty)
        empty_box.setContentsMargins(24, 36, 24, 36)
        empty_box.setSpacing(14)
        symbol = _label("…", 30, _GREEN, True)
        symbol.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(symbol)
        self.emptyTitle = _label("等待对方的新消息", 17, "#304c3c", True)
        self.emptyTitle.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyTitle)
        self.emptyHint = _label("保持微信聊天窗口打开。\n收到新消息后，回复建议会出现在这里。", 13, _MUTED)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(self.emptyHint)
        self.setupButton = PrimaryPushButton("前往设置")
        self.setupButton.clicked.connect(self.open_settings)
        empty_box.addWidget(self.setupButton, 0, Qt.AlignHCenter)
        self._empty_text()  # 起草跑不起来 / 起草模式 / 完整模式，三套文案都在这里出
        body.addWidget(self.empty)
        # 「N 秒后自动发送」摆在候选**上方**：这是自动发送唯一能后悔的窗口，必须落在不用滚动就
        # 看得见的位置，取消按钮更是。它说的就是下面第一张卡（推荐那条），顺序上也这么读。
        self.autoBar = _Surface(warn=True)
        auto_box = QVBoxLayout(self.autoBar)
        auto_box.setContentsMargins(14, 12, 14, 12)
        auto_box.setSpacing(8)
        auto_row = QHBoxLayout()
        self.autoLabel = _label("", 13, "#8a4b12", True)
        auto_row.addWidget(self.autoLabel, 1)
        self.autoCancel = PushButton("取消发送")
        self.autoCancel.setAccessibleName("取消这次自动发送")
        self.autoCancel.clicked.connect(self._cancel_auto)
        auto_row.addWidget(self.autoCancel)
        auto_box.addLayout(auto_row)
        self.autoText = _label("", 14, "#5a4326")
        self.autoText.setWordWrap(True)
        auto_box.addWidget(self.autoText)
        # 「这次为什么发这条」。只有判断关着（或判断失败）时才有内容——那时发的不是
        # 「判断过的推荐」，而是第一条。用户以为自己开的是「判断过的推荐自动发送」，
        # 不写出来他永远不会知道，等发现时话已经发出去了。
        # **名字必须跟设置页那个 autoNote 区分开**：那是「保存后：…」的总结句，
        # 两个都用 self.autoNote 的话后建的那个会覆盖前一个，结果是把倒计时的说明
        # 写进设置页的总结里（这个 bug 真出现过，被 check_auto_send 抓出来）。
        self.autoBarNote = _label("", 12, "#8a4b12")
        self.autoBarNote.setWordWrap(True)
        self.autoBarNote.hide()
        auto_box.addWidget(self.autoBarNote)
        self.autoBar.hide()
        body.addWidget(self.autoBar)
        self.replyBox = QVBoxLayout()
        self.replyBox.setSpacing(10)
        body.addLayout(self.replyBox)
        self.referenceNote = _label("AI 建议仅供参考，按你的语气调整后再发送。", 11, _MUTED)
        self.referenceNote.hide()
        body.addWidget(self.referenceNote)

        self.historyButton = PushButton(FIF.HISTORY, "聊天记录")
        self.historyButton.clicked.connect(self._toggle_history)
        self.historyButton.setAccessibleName("展开或收起聊天记录")
        body.addWidget(self.historyButton)
        # 上游是「长按悬浮球 → 把当前会话存为联系人」。桌面端没有长按，做成一键按钮，
        # 放在「聊天记录」旁边——同一类「当前会话」的动作。
        self.saveContactButton = PushButton(FIF.PEOPLE, "存为联系人")
        self.saveContactButton.setAccessibleName("把当前会话存为知识库联系人")
        self.saveContactButton.setToolTip(
            "把这个会话存成联系人，之后就能给它填关系、备注和别名")
        self.saveContactButton.clicked.connect(self._save_contact)
        body.addWidget(self.saveContactButton)
        if self.kb is None:
            self.saveContactButton.hide()
        self.feed = PlainTextEdit()
        self.feed.setReadOnly(True)
        self.feed.setPlaceholderText("识别到的聊天内容会显示在这里")
        self.feed.setMaximumBlockCount(_LOG_LINES)
        self.feed.setFixedHeight(160)
        self.feed.hide()
        body.addWidget(self.feed)
        self._history_title()
        body.addStretch(1)

    def _build_settings(self):
        self.settingsPage, body = self._scroll_page()
        heading = QHBoxLayout()
        heading.addWidget(_tool(FIF.RETURN, "返回回复建议", self._back_home))
        heading.addWidget(_label("设置", 23, "#24382d", True), 1)
        body.addLayout(heading)
        body.addWidget(_label("调整关系背景，连接你的回复服务。", 13, _MUTED))
        preference = _Surface()
        box = QVBoxLayout(preference)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        box.addWidget(_label("回复偏好", 16, "#304c3c", True))
        relation_label = _label("你们的关系", 13)
        box.addWidget(relation_label)
        self.relationshipBox = ComboBox()
        self.relationshipBox.setMinimumWidth(0)
        self.relationshipBox.addItems([name for name, value in _RELATIONSHIPS])
        self.relationshipBox.setAccessibleName("你们的关系")
        relation_label.setBuddy(self.relationshipBox)
        box.addWidget(self.relationshipBox)
        self.relEdit = LineEdit()
        self.relEdit.setPlaceholderText("例如：刚认识的朋友，正在慢慢熟悉")
        self.relEdit.setAccessibleName("自定义关系背景")
        box.addWidget(self.relEdit)
        self.relationshipBox.currentIndexChanged.connect(
            lambda index: self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        )
        box.addWidget(self._hint("帮助助手把握称呼、语气和回应分寸。"))
        style_label = _label("说话风格（可选）", 13)
        box.addWidget(style_label)
        self.styleEdit = LineEdit()
        self.styleEdit.setPlaceholderText("例如：话少、不用标点、偶尔用 doge、不说客套话")
        self.styleEdit.setAccessibleName("说话风格")
        style_label.setBuddy(self.styleEdit)
        box.addWidget(self.styleEdit)
        box.addWidget(self._hint("候选本来就照着你最近发的消息模仿；这里可以再补一句你自己的口吻。"))
        context_label = _label("参考上下文", 13)
        box.addWidget(context_label)
        self.contextBox = SpinBox()
        self.contextBox.setRange(3, 30)
        self.contextBox.setAccessibleName("参考的最近消息条数")
        context_label.setBuddy(self.contextBox)
        box.addWidget(self.contextBox)
        box.addWidget(self._hint(
            "生成和判断时看最近这么多条消息。太少会丢上下文，太多会稀释重点，建议 6–12。"
        ))
        target_row = QHBoxLayout()
        target_row.addWidget(_label("群聊指定回复对象", 13), 1)
        self.targetSwitch = SwitchButton()
        self.targetSwitch.setOnText("开")
        self.targetSwitch.setOffText("关")
        self.targetSwitch.setAccessibleName("群聊指定回复对象")
        target_row.addWidget(self.targetSwitch)
        box.addLayout(target_row)
        box.addWidget(self._hint(
            "开了以后群聊里可以选回复给谁，候选会针对 TA 写，填入时可带 @。关了就正常回复。"
        ))
        update_row = QHBoxLayout()
        update_row.addWidget(_label("启动时检查更新", 13), 1)
        self.updateSwitch = SwitchButton()
        self.updateSwitch.setOnText("开")
        self.updateSwitch.setOffText("关")
        self.updateSwitch.setAccessibleName("启动时检查更新")
        update_row.addWidget(self.updateSwitch)
        box.addLayout(update_row)
        box.addWidget(self._hint(
            "只向 GitHub 查最新版本号，不发送任何数据。国内访问 GitHub 慢的话关掉也行。"
        ))
        hist_row = QHBoxLayout()
        hist_row.addWidget(_label("保存聊天记录到本地", 13), 1)
        self.historySwitch = SwitchButton()
        self.historySwitch.setOnText("开")
        self.historySwitch.setOffText("关")
        self.historySwitch.setAccessibleName("保存聊天记录到本地")
        hist_row.addWidget(self.historySwitch)
        box.addLayout(hist_row)
        # 存放路径必须写出来：开了这个开关，用户第一个想知道的就是「文件去哪了」。但它**不能进
        # 灰字说明**——QLabel 的最小宽度按最长不可断开的词算，那串没有空格的长路径实测要 180px，
        # 窗口一窄整页就被撑开，「保存设置」按钮会被裁到可视区外面。只读单行框的最小宽度只有
        # 十几像素，内容自己滚动，还能选中复制去资源管理器里粘。
        self.historyPathEdit = LineEdit()
        self.historyPathEdit.setText(settings.history_dir())
        self.historyPathEdit.setReadOnly(True)
        self.historyPathEdit.setAccessibleName("聊天记录存放位置")
        box.addWidget(self.historyPathEdit)
        box.addWidget(self._hint(
            "把每个会话的对话按「会话名 / 日期」存成 CSV，放在上面那个位置。时间是聊天时间——"
            "从微信里那行时间戳认出来的，所以补录的旧消息会归到它真实的那一天。"
            "只在本机写文件，不联网。关掉开关时会把已经攒下的那批先写完，不会丢。"))
        body.addWidget(preference)

        # ── 知识库（可选的一整块）─────────────────────────────────────────────
        # 没有 store（几个离线工具构造 Overlay 时不传 kb）就整张卡都不出现，
        # 设置页跟加这个功能之前完全一样。
        self.kbCard = _Surface()
        kbox = QVBoxLayout(self.kbCard)
        kbox.setContentsMargins(16, 16, 16, 18)
        kbox.setSpacing(12)
        kbox.addWidget(_label("知识库", 16, "#304c3c", True))
        kbox.addWidget(_label(
            "只存在本机，不上传。写下的笔记和联系人会在分析时按会话标题与关键词带上。", 12, _MUTED))
        kb_hist_row = QHBoxLayout()
        kb_hist_row.addWidget(_label("记录聊天历史（只存本机）", 13), 1)
        self.kbHistorySwitch = SwitchButton()
        self.kbHistorySwitch.setOnText("开")
        self.kbHistorySwitch.setOffText("关")
        self.kbHistorySwitch.setAccessibleName("记录聊天历史到知识库")
        kb_hist_row.addWidget(self.kbHistorySwitch)
        kbox.addLayout(kb_hist_row)
        self.kbHistHint = _label("", 12, _MUTED)
        kbox.addWidget(self.kbHistHint)
        kb_count_label = _label("注入最近历史条数（0–100）", 13)
        kbox.addWidget(kb_count_label)
        self.kbCountBox = SpinBox()
        self.kbCountBox.setRange(0, 100)
        self.kbCountBox.setAccessibleName("注入最近历史条数")
        kb_count_label.setBuddy(self.kbCountBox)
        kbox.addWidget(self.kbCountBox)
        # 「记录历史开着、条数却是 0」这种组合必须当场说：两个控件各自看都正常，
        # 凑一起才「只记录、永不注入」，用户自己发现不了。改动即刷新提示。
        self.kbHistorySwitch.checkedChanged.connect(self._kb_hint)
        self.kbCountBox.valueChanged.connect(self._kb_hint)
        kb_actions = QHBoxLayout()
        kb_actions.setSpacing(8)
        self.kbOpenButton = PushButton(FIF.LIBRARY, "知识库与联系人")
        self.kbOpenButton.setAccessibleName("打开知识库与联系人管理")
        self.kbOpenButton.clicked.connect(self._open_kb)
        kb_actions.addWidget(self.kbOpenButton)
        self.kbClearButton = PushButton("清空知识库与历史")
        self.kbClearButton.setAccessibleName("清空知识库与历史")
        self.kbClearButton.clicked.connect(self._clear_kb)
        kb_actions.addWidget(self.kbClearButton)
        kb_actions.addStretch(1)
        kbox.addLayout(kb_actions)
        # 自检：刻意做得低调，它是排查用的开发辅助，不是用户功能（上游也是这个态度）。
        self.kbCheckButton = PushButton("自检")
        self.kbCheckButton.setAccessibleName("运行知识库自检")
        self.kbCheckButton.setToolTip("用临时数据跑一遍匹配、命中和去重，跑完把自己造的东西删掉")
        self.kbCheckButton.clicked.connect(self._selfcheck_kb)
        kbox.addWidget(self.kbCheckButton, 0, Qt.AlignLeft)
        self.kbResult = _label("", 12, _MUTED)
        kbox.addWidget(self.kbResult)
        self.kbResult.hide()  # 没跑过自检就别留一段空行（空标签仍占布局间距）
        body.addWidget(self.kbCard)
        if self.kb is None:
            self.kbCard.hide()

        connection = _Surface()
        box = QVBoxLayout(connection)
        box.setContentsMargins(16, 16, 16, 18)
        box.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(_label("起草服务", 16, "#304c3c", True), 1)
        box.addLayout(heading)
        provider_label = _label("起草模型来源", 13)
        box.addWidget(provider_label)
        self.providerBox = ComboBox()
        self.providerBox.setMinimumWidth(0)  # 选项文字很长，别让它撑开设置页
        self.providerBox.addItems(list(_PROVIDER_ITEMS))
        self.providerBox.setAccessibleName("起草模型来源")
        provider_label.setBuddy(self.providerBox)
        box.addWidget(self.providerBox)
        # ── DeepSeek 直连那一组：只有选中它才显示
        ds_heading = QHBoxLayout()
        ds_label = _label("DeepSeek API 密钥", 13)
        ds_heading.addWidget(ds_label, 1)
        self.dsKeyState = _label("", 12, _GREEN)
        self.dsKeyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ds_heading.addWidget(self.dsKeyState)
        box.addLayout(ds_heading)
        self.dsKeyEdit = PasswordLineEdit()
        self.dsKeyEdit.setAccessibleName("DeepSeek API 密钥")
        ds_label.setBuddy(self.dsKeyEdit)
        self.dsKeyEdit.returnPressed.connect(self._save)
        box.addWidget(self.dsKeyEdit)
        self.dsHint = _label("platform.deepseek.com 申请。已配置时留空保留当前密钥。", 12, _MUTED)
        box.addWidget(self.dsHint)
        # 只有选了直连才显示这一组；dsHint 额外还要看紧凑模式，单独存，不进 _hintLabels
        self._dsWidgets = (ds_label, self.dsKeyState, self.dsKeyEdit)
        # ── 自定义（OpenAI 兼容）那一组：同样只有选中才显示。
        # 切回别的来源时这几个值留在 config.json 里但不生效，切回来会自动填回。
        url_label = _label("基础地址", 13)
        self.draftUrlEdit = LineEdit()
        self.draftUrlEdit.setPlaceholderText("例如：https://api.moonshot.cn/v1")
        self.draftUrlEdit.setAccessibleName("自定义起草服务基础地址")
        url_label.setBuddy(self.draftUrlEdit)
        self.draftUrlNote = _label("", 12, _MUTED)  # 实时显示拼出来的完整地址，别让用户猜
        model_label = _label("模型名", 13)
        self.draftModelEdit = LineEdit()
        self.draftModelEdit.setPlaceholderText("例如：kimi-k2；留空用该地址的默认模型")
        self.draftModelEdit.setAccessibleName("自定义起草模型名")
        model_label.setBuddy(self.draftModelEdit)
        ck_heading = QHBoxLayout()
        ck_label = _label("自定义 API 密钥", 13)
        ck_heading.addWidget(ck_label, 1)
        self.ckKeyState = _label("", 12, _GREEN)
        self.ckKeyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ck_heading.addWidget(self.ckKeyState)
        self.ckKeyEdit = PasswordLineEdit()
        self.ckKeyEdit.setAccessibleName("自定义 API 密钥")
        ck_label.setBuddy(self.ckKeyEdit)
        self.ckKeyEdit.returnPressed.connect(self._save)
        self._customWidgets = (url_label, self.draftUrlEdit, self.draftUrlNote, model_label,
                               self.draftModelEdit, ck_label, self.ckKeyState, self.ckKeyEdit)
        self.customHint = _label(
            "填到 /v1 就行，程序自己补 /chat/completions。地址必须是 http:// 或 https:// 开头，"
            "填错会自动退回 OpenRouter 官方地址（不会崩，但也不再是你的地址）。\n"
            "注意：填了自定义地址后，对话文本会发到该地址——出网范围不再只有 OpenRouter 和 DeepSeek。",
            12, _MUTED)
        # 这几个控件必须进布局：没进布局的 Qt 控件没有父窗口，setVisible(True) 会把它们
        # 提升成独立的顶层窗口（飘在桌面上的「弹窗」），设置页里反而看不见。
        box.addWidget(url_label)
        box.addWidget(self.draftUrlEdit)
        box.addWidget(self.draftUrlNote)
        box.addWidget(model_label)
        box.addWidget(self.draftModelEdit)
        box.addLayout(ck_heading)
        box.addWidget(self.ckKeyEdit)
        box.addWidget(self.customHint)
        self.providerBox.currentIndexChanged.connect(lambda index: self._sync_ds_fields())
        # ── 起草的思考开关
        think_row = QHBoxLayout()
        think_row.addWidget(_label("起草时开启思考模式", 13), 1)
        self.thinkingSwitch = SwitchButton()
        self.thinkingSwitch.setOnText("开")
        self.thinkingSwitch.setOffText("关")
        self.thinkingSwitch.setAccessibleName("起草时开启思考模式")
        think_row.addWidget(self.thinkingSwitch)
        box.addLayout(think_row)
        box.addWidget(self._hint(
            "关：秒回，够用。开：模型先想再写，更斟酌但慢好几倍、贵一些。三种来源都生效；"
            "自定义地址只在开启时才带 reasoning 字段，对端不认这个字段就关掉它。"
        ))
        # ── 候选条数。放在这一张卡的最前面：它是「想更快」最直接的一个口子，
        #    要 1 条时模型输出短一截、也不用在多个版本之间权衡取舍，生成时间跟着短。
        cand_label = _label("生成几条候选", 13)
        box.addWidget(cand_label)
        self.candidateBox = SpinBox()
        self.candidateBox.setRange(settings._MIN_CANDIDATES, settings._MAX_CANDIDATES)
        self.candidateBox.setAccessibleName("生成几条候选回复")
        cand_label.setBuddy(self.candidateBox)
        box.addWidget(self.candidateBox)
        box.addWidget(self._hint(
            "默认 3 条，可以调到 1。调小是真能变快：只要 1 条时模型不用写三个版本、"
            "输出也短得多，生成时间会少一截——不只是界面上少几张卡。"
            "调成 1 条就没有「推荐排序」可言了（只有一条，无从比较）。"
        ))
        # ── 失败重试次数。一个值管起草和判断两边：两边都是「对端偶发不可用」，没有分开配的理由。
        #    文案必须是「最多再试几次」而不是「尝试次数」——后者会让人以为总共只打 N 次。
        retry_label = _label("失败后最多再试几次", 13)
        box.addWidget(retry_label)
        self.retriesBox = SpinBox()
        self.retriesBox.setRange(0, 5)
        self.retriesBox.setAccessibleName("失败后最多再试几次")
        retry_label.setBuddy(self.retriesBox)
        box.addWidget(self.retriesBox)
        box.addWidget(self._hint(
            "0 = 失败就算了。默认 2。服务端返回 5xx、连接被中断、超时、以及对端返回的不是"
            "预期格式，都会自动重试；密钥错、地址错这类 4xx 不重试（重试也没用，只会让你多等）。"
            "起草和判断共用这个值。每次重试间隔 1~4 秒，所以填太大最坏情况会等很久。"
        ))
        # ── 单次请求超时。起草和判断分开配：判断读的上下文短得多、模型也常常更小，
        #    给它跟起草一样长的超时是白等——而超时会按上面的重试次数再打，最坏情况翻好几倍。
        dto_label = _label("起草超时（秒）", 13)
        self.draftTimeoutBox = SpinBox()
        self.draftTimeoutBox.setRange(settings._MIN_TIMEOUT, settings._MAX_TIMEOUT)
        self.draftTimeoutBox.setAccessibleName("起草单次请求超时秒数")
        dto_label.setBuddy(self.draftTimeoutBox)
        box.addWidget(dto_label)
        box.addWidget(self.draftTimeoutBox)
        jto_label = _label("判断超时（秒）", 13)
        self.judgeTimeoutBox = SpinBox()
        self.judgeTimeoutBox.setRange(settings._MIN_TIMEOUT, settings._MAX_TIMEOUT)
        self.judgeTimeoutBox.setAccessibleName("判断单次请求超时秒数")
        jto_label.setBuddy(self.judgeTimeoutBox)
        box.addWidget(jto_label)
        box.addWidget(self.judgeTimeoutBox)
        box.addWidget(self._hint(
            "单次请求最多等多久，不是整轮的上限——超时算一次失败，会按上面那个次数再打。"
            "默认起草 20 秒、判断 15 秒；线路慢就调大，服务端快就调小，让卡住的那一次早点暴露。"
        ))
        body.addWidget(connection)

        # ── 第二张卡：判断与排序。协议是 OpenRouter 私有的，所以这里只能换 host（代理场景），
        # 跟起草那张卡分开——上一轮已经确定起草和判断是两个独立端点，UI 不该把它们挤在一起。
        judging = _Surface()
        jbox = QVBoxLayout(judging)
        jbox.setContentsMargins(16, 16, 16, 18)
        jbox.setSpacing(12)
        jhead = QHBoxLayout()
        jhead.addWidget(_label("判断与排序", 16, "#304c3c", True), 1)
        self.keyState = _label("", 12, _GREEN)
        self.keyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        jhead.addWidget(self.keyState)
        jbox.addLayout(jhead)
        engine_label = _label("判断引擎", 13)
        self.engineBox = ComboBox()
        self.engineBox.setMinimumWidth(0)  # 选项文字长，别撑开设置页
        self.engineBox.addItems([name for name, _ in _JUDGE_ENGINES])
        self.engineBox.setAccessibleName("判断引擎")
        engine_label.setBuddy(self.engineBox)
        self.engineNote = _label("", 12, _MUTED)  # 随引擎实时变：这一档给什么、要什么、缺了会怎样
        self.engineNote.setWordWrap(True)
        self.engineBox.currentIndexChanged.connect(lambda _i: self._sync_engine_fields())
        jbox.addWidget(engine_label)
        jbox.addWidget(self.engineBox)
        jbox.addWidget(self.engineNote)

        key_label = _label("OpenRouter API 密钥（选填）", 13)
        jbox.addWidget(key_label)
        self.keyEdit = PasswordLineEdit()
        self.keyEdit.setAccessibleName("OpenRouter API 密钥")
        key_label.setBuddy(self.keyEdit)
        self.keyEdit.returnPressed.connect(self._save)
        jbox.addWidget(self.keyEdit)
        self.keyHint = _label(
            "只有「OpenRouter」引擎和「起草模型来源 = OpenRouter」会用到它。"
            "判断引擎选 OpenRouter 时，这里填了才有校准过的胜出概率。已配置时留空保留。", 12, _MUTED)
        self.keyHint.setWordWrap(True)
        jbox.addWidget(self.keyHint)

        judge_url_label = _label("判断服务地址（可选）", 13)
        self.judgeUrlEdit = LineEdit()
        self.judgeUrlEdit.setPlaceholderText("留空跟起草用同一个地址")
        self.judgeUrlEdit.setAccessibleName("判断服务地址")
        judge_url_label.setBuddy(self.judgeUrlEdit)
        self.judgeUrlNote = _label("", 12, _MUTED)
        judge_model_label = _label("判断模型（可选）", 13)
        self.judgeModelEdit = LineEdit()
        self.judgeModelEdit.setPlaceholderText("留空跟起草用同一个模型")
        self.judgeModelEdit.setAccessibleName("判断模型")
        judge_model_label.setBuddy(self.judgeModelEdit)
        jk_heading = QHBoxLayout()
        jk_label = _label("判断 API 密钥（选填）", 13)
        jk_heading.addWidget(jk_label, 1)
        self.judgeKeyState = _label("", 12, _GREEN)
        self.judgeKeyState.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        jk_heading.addWidget(self.judgeKeyState)
        self.judgeKeyEdit = PasswordLineEdit()
        self.judgeKeyEdit.setAccessibleName("判断 API 密钥")
        jk_label.setBuddy(self.judgeKeyEdit)
        self.judgeKeyEdit.returnPressed.connect(self._save)
        self.judgeHint = _label("", 12, _MUTED)  # 随引擎变，单独存（条件显隐的说明文字不进 _hintLabels）
        self.judgeHint.setWordWrap(True)
        # 条件显隐的两组。_selfJudgeWidgets 是「用起草模型判断」才需要的（另配地址/密钥）；
        # _judgeWidgets 是只要判断开着就有意义的。
        self._selfJudgeWidgets = (jk_label, self.judgeKeyState, self.judgeKeyEdit)
        self._judgeWidgets = (judge_url_label, self.judgeUrlEdit, self.judgeUrlNote,
                              judge_model_label, self.judgeModelEdit) + self._selfJudgeWidgets
        jbox.addWidget(judge_url_label)
        jbox.addWidget(self.judgeUrlEdit)
        jbox.addWidget(self.judgeUrlNote)
        jbox.addWidget(judge_model_label)
        jbox.addWidget(self.judgeModelEdit)
        jbox.addLayout(jk_heading)
        jbox.addWidget(self.judgeKeyEdit)
        jbox.addWidget(self.judgeHint)
        body.addWidget(judging)

        # ── 第三张卡：自动发送。全项目唯一不可逆的动作，所以放在最靠下、保存键上方——
        # 改到这里的人已经看过前面所有配置，心里有数；卡片顶上也先把代价说清楚。
        autosend = _Surface()
        abox = QVBoxLayout(autosend)
        abox.setContentsMargins(16, 16, 16, 18)
        abox.setSpacing(12)
        abox.addWidget(_label("自动发送", 16, "#304c3c", True))
        abox.addWidget(_label(
            "默认关闭。开启后，满足条件的消息会由程序替你按发送键——发出去就收不回来。"
            "生成完会先给你一段时间，点「取消发送」就能拦下；输入框里已经有你打的字时，"
            "程序一律不发。", 12, "#8a4b12"))
        # 判断关着 + 自动发送开着 = 会发，但发的是**第一条**候选。这是最容易踩的坑：
        # 用户以为「自动发送」发的是判断过的推荐，其实判断关了就没有推荐可言。
        # 放在卡片顶部（开关之前），让他在拨开关之前就看到。
        self.autoJudgeWarn = _label(
            "判断关着（设置页上面「判断引擎」选的是「不判断」）：自动发送仍然会发，"
            "但发的是第一条候选，不是判断出来的推荐。想要质量就打开判断，"
            "或者把「生成几条候选」调成 1——只有一条时本来就没什么可挑的。",
            12, "#a4471a", True)
        self.autoJudgeWarn.setWordWrap(True)
        abox.addWidget(self.autoJudgeWarn)
        dm_row = QHBoxLayout()
        dm_row.addWidget(_label("单聊里对方直接找我", 13), 1)
        self.autoDmSwitch = SwitchButton()
        # 开关上的字跟这一页其他开关统一用「开/关」：SwitchButton 的 minimumSizeHint 是按整段
        # 文字算的，写「自动发」会把设置页撑宽一截，右边那列跟着被裁掉。
        self.autoDmSwitch.setOnText("开")
        self.autoDmSwitch.setOffText("关")
        self.autoDmSwitch.setAccessibleName("单聊里对方直接找我时自动发送")
        dm_row.addWidget(self.autoDmSwitch)
        abox.addLayout(dm_row)
        group_row = QHBoxLayout()
        group_row.addWidget(_label("群里 @我", 13), 1)
        self.autoGroupSwitch = SwitchButton()
        self.autoGroupSwitch.setOnText("开")
        self.autoGroupSwitch.setOffText("关")
        self.autoGroupSwitch.setAccessibleName("群里 @我 时自动发送")
        group_row.addWidget(self.autoGroupSwitch)
        abox.addLayout(group_row)
        any_row = QHBoxLayout()
        any_row.addWidget(_label("群里不@我也回", 13), 1)
        self.autoAnySwitch = SwitchButton()
        self.autoAnySwitch.setOnText("开")
        self.autoAnySwitch.setOffText("关")
        self.autoAnySwitch.setAccessibleName("群里不@我也自动回复")
        any_row.addWidget(self.autoAnySwitch)
        abox.addLayout(any_row)
        abox.addWidget(self._hint(
            "拆成三个开关：群里认 @ 靠的是文本匹配（消息里出现「@你的群昵称」），昵称 OCR 抖一下"
            "就会漏认——漏认只是「该自动发的没发」，比误发安全，但值得单独决定。"
            "「不@我也回」是另一回事：它不认 @，群里任何新消息都会触发，误判面大得多，"
            "所以单独一个开关、默认关；开了它，@你的消息当然也回，不用再开上面那个。"
            "程序启动后收到的第一批消息都不会自动发送（那批可能是你不在时攒下的）。"))
        # 「不@我也回」是三条路里唯一**不看有没有人叫你**的，代价必须写在开关正下方，
        # 不能塞进上面那段通用说明里——拨开关的人眼睛就在这里。
        # 秒数不写死在这句里：它跟着下面那个「先安静几秒」的框变，所以正文在 _sync_auto_fields 里生成。
        self.autoAnyWarn = _label("", 12, "#a4471a", True)
        self.autoAnyWarn.setWordWrap(True)
        abox.addWidget(self.autoAnyWarn)
        # ── 倒计时秒数和发送键：开了任意一个开关才有意义，关着时整组藏起来
        delay_label = _label("生成完等几秒再发", 13)
        self.autoDelayBox = SpinBox()
        self.autoDelayBox.setRange(0, settings._MAX_AUTO_DELAY)  # 上限跟 settings.save 的夹取一致
        self.autoDelayBox.setAccessibleName("生成完等几秒再自动发送")
        delay_label.setBuddy(self.autoDelayBox)
        abox.addWidget(delay_label)
        abox.addWidget(self.autoDelayBox)
        # 「不@我也回」那条路的**安静窗口**单独配：群里连着聊的时候，它靠一直往后等来避免插话。
        # 这个框以前写的是「等几秒」、配的却是倒计时，而真正的安静窗口硬编码在 main 里——
        # 用户填的数不生效。现在标签和行为对齐：这里填的就是「先安静几秒」。
        any_wait_label = _label("群里不@我时先安静几秒", 13)
        self.autoAnyWaitBox = SpinBox()
        self.autoAnyWaitBox.setRange(0, settings._MAX_AUTO_DELAY)
        self.autoAnyWaitBox.setAccessibleName("群里不@我时先安静几秒再生成")
        any_wait_label.setBuddy(self.autoAnyWaitBox)
        abox.addWidget(any_wait_label)
        abox.addWidget(self.autoAnyWaitBox)
        self._autoAnyWaitWidgets = (any_wait_label, self.autoAnyWaitBox)
        key_label = _label("按哪个键发送", 13)
        self.sendKeyBox = ComboBox()
        self.sendKeyBox.setMinimumWidth(0)
        self.sendKeyBox.addItems(["Enter（微信默认）", "Ctrl+Enter"])
        self.sendKeyBox.setAccessibleName("按哪个键发送")
        key_label.setBuddy(self.sendKeyBox)
        abox.addWidget(key_label)
        abox.addWidget(self.sendKeyBox)
        self._autoKeyWidgets = (delay_label, self.autoDelayBox, key_label, self.sendKeyBox)
        self.autoKeyHint = _label(
            "发送键必须跟微信「设置 → 通用 → 快捷键 → 按 Enter 发送消息」一致，否则不是发不出去、"
            "就是在输入框里插一个换行。等待秒数是留给你后悔的窗口：填 0 = 生成完立刻发，没有后悔机会。",
            12, _MUTED)
        self.autoKeyHint.setWordWrap(True)
        abox.addWidget(self.autoKeyHint)
        # ── 群昵称：只有开了群里那一半才需要
        name_label = _label("我在群里的昵称", 13)
        self.myNameEdit = LineEdit()
        self.myNameEdit.setPlaceholderText("例如：张三（要填群昵称，不一定是微信昵称）")
        self.myNameEdit.setAccessibleName("我在群里的昵称")
        name_label.setBuddy(self.myNameEdit)
        abox.addWidget(name_label)
        abox.addWidget(self.myNameEdit)
        self._autoGroupWidgets = (name_label, self.myNameEdit)
        # 开着「群里 @我」但昵称空着 = 一次都不会自动发。这是**静默失效**：开关显示「开」，
        # 用户以为已经在用，实际 auto_pick() 缺昵称就直接拦下了。所以这里显式说破，
        # 用警示色（跟倒计时条同一族），别让他以为是自己配好了却没收到消息。
        self.autoNameWarn = _label("群昵称空着，群里一次都不会自动发送——请填上再保存。", 12, "#a4471a", True)
        self.autoNameWarn.setWordWrap(True)
        abox.addWidget(self.autoNameWarn)
        self.autoGroupHint = _label(
            "用来认「@我」。群昵称常常跟微信昵称不一样，每个群还可能不同——本版只能填一个，"
            "填你最常被 @ 的那个；留空 = 群里一律不自动发送。昵称只有一个字的话很容易跟别人的"
            "昵称撞上，建议填完整一些。", 12, _MUTED)
        self.autoGroupHint.setWordWrap(True)
        abox.addWidget(self.autoGroupHint)
        self.autoNote = _label("", 12, _MUTED)
        self.autoNote.setWordWrap(True)
        abox.addWidget(self.autoNote)
        self.autoDmSwitch.checkedChanged.connect(lambda _on: self._sync_auto_fields())
        self.autoGroupSwitch.checkedChanged.connect(lambda _on: self._sync_auto_fields())
        self.autoAnySwitch.checkedChanged.connect(lambda _on: self._sync_auto_fields())
        self.autoDelayBox.valueChanged.connect(lambda _v: self._sync_auto_fields())
        self.autoAnyWaitBox.valueChanged.connect(lambda _v: self._sync_auto_fields())
        self.myNameEdit.textChanged.connect(lambda _t: self._sync_auto_fields())
        body.addWidget(autosend)
        self.settingsFeedback = _label("", 13, _GREEN)
        self.settingsFeedback.hide()
        body.addWidget(self.settingsFeedback)
        actions = QHBoxLayout()
        back = PushButton("返回")
        back.clicked.connect(self._back_home)
        actions.addWidget(back)
        actions.addStretch(1)
        self.saveButton = PrimaryPushButton("保存设置")
        self.saveButton.clicked.connect(self._save)
        actions.addWidget(self.saveButton)
        body.addLayout(actions)
        body.addWidget(self._hint("保存后用于下一次生成的回复。"))
        body.addStretch(1)
        self._load_settings()
        # 两个地址框的实时说明：连接放在两个 note 都建好之后，免得构造期的回调打到还没建的控件
        self.draftUrlEdit.textChanged.connect(self._sync_url_notes)
        self.judgeUrlEdit.textChanged.connect(self._sync_url_notes)
        self._sync_url_notes()

    def _hint(self, text):
        """设置页字段下面的灰字说明：记下来，紧凑模式一起隐藏。"""
        label = _label(text, 12, _MUTED)
        self._hintLabels.append(label)
        return label

    def _sync_ds_fields(self):
        """按「起草模型来源」显示对应那一组字段：0=OpenRouter 没有额外字段、1=DeepSeek 密钥、
        2=自定义（地址 / 模型名 / 密钥）。两组的说明文字额外还要看紧凑模式，单独存，不进 _hintLabels。
        下拉框那行文字按当前可用宽度省略，理由见 _elide_items。"""
        index = self.providerBox.currentIndex()
        for w in self._dsWidgets:
            w.setVisible(index == 1)
        self.dsHint.setVisible(index == 1 and not self._compact)
        for w in self._customWidgets:
            w.setVisible(index == 2)
        self.customHint.setVisible(index == 2 and not self._compact)
        self._elide_items(self.providerBox, _PROVIDER_ITEMS)

    def _sync_engine_fields(self):
        """按判断引擎显隐字段、刷新说明。

        这不是「藏几个框」——三档给的东西真的不一样，所以要把「你会得到什么、要付什么代价」
        写在引擎下面。尤其自判模式会**多一次模型调用**，偷偷多花钱是我不愿意做的事。

        条件显隐的说明文字（engineNote / judgeHint / judgeUrlNote）不进 _hintLabels：
        它们要同时看「引擎选了什么」和「紧凑模式」，两个条件都得管，单独算。
        """
        engine = _JUDGE_ENGINES[max(0, self.engineBox.currentIndex())][1]
        for w in self._judgeWidgets:
            w.setVisible(engine != "none")
        for w in self._selfJudgeWidgets:  # 另配地址/密钥只有自判模式才用得上
            w.setVisible(engine == "self")
        self.judgeUrlNote.setVisible(engine != "none")
        self.judgeHint.setVisible(engine != "none" and not self._compact)
        # 注意：这些标签是 PlainText 渲染的，写 markdown 的 ** 会原样显示出来。
        note = {
            "openrouter": "走 OpenRouter 私有的判断协议，会给校准过的胜出概率"
                          "（卡片上那个百分比）。需要 OpenRouter 密钥。",
            "self": "用你自己的模型做判断：会多一次模型调用（多花一点钱、多等一两秒），"
                    "给出摘要、紧张度和推荐顺序，但没有胜出概率——自评的百分比是编的，不给。",
            "none": "不做判断，只给候选回复。最省事，也最省钱。",
        }[engine]
        problem = settings.judge_problem()
        if problem:  # 选了 OpenRouter 却没密钥这类，必须当场说清，别让他以为正在用完整模式
            note += "\n" + problem + "。"
        self.engineNote.setText(note)
        self._elide_items(self.engineBox, _JUDGE_ENGINE_ITEMS)  # 理由见 _elide_items
        self._sync_url_notes()

    def _sync_url_notes(self):
        """两个地址框下面的实时说明。判断那行**随引擎变**——同一个输入框，两条不同的协议。"""
        self.draftUrlNote.setText(_endpoint_note(
            self.draftUrlEdit.text(), CHAT_PATH, "起草", "OpenRouter 官方地址"))
        engine = _JUDGE_ENGINES[max(0, self.engineBox.currentIndex())][1]
        if engine == "openrouter":
            self.judgeUrlNote.setText(_endpoint_note(
                self.judgeUrlEdit.text(), DECISIONS_PATH, "判断", "OpenRouter 官方端点"))
            self.judgeHint.setText(
                "判断和排序走 OpenRouter 私有的 decisions 协议，不是 OpenAI 的 /chat/completions——"
                "填 https://xxx/v1 这类 OpenAI 兼容地址一定会 404。留空走官方；只在你确认某个代理"
                "确实实现了 /api/alpha/decisions 时才填。")
        elif engine == "self":
            self.judgeUrlNote.setText(_endpoint_note(
                self.judgeUrlEdit.text(), CHAT_PATH, "判断", "跟起草同一个地址"))
            self.judgeHint.setText(
                "判断走普通 /chat/completions，填任意 OpenAI 兼容地址（填到 /v1 就行）。"
                "地址和模型都留空 = 跟起草完全相同，这才是「用我配的起草模型来判」。"
                "判断密钥留空就用起草那把；只有判断地址跟起草不是同一家时才需要单独填。\n"
                "注意：判断比起草难得多（要读潜台词、要把紧张度分档），用小模型判断结论会偏。")
        else:
            self.judgeUrlNote.setText("")
            self.judgeHint.setText("")

    def _sync_footer(self):
        """页脚那句承诺必须跟着开关变：开着自动发送还写「发送由你确认」就是骗人。

        说的是**存下来**的状态，跟设置页里 autoNote 那句「保存后：…」分开措辞——
        同一个界面上出现两句互相矛盾的话，比不写还糟。
        """
        if not settings.auto_send_on():
            self.footerNote.setText(f"仅填入输入框 · 发送由你确认 · v{VERSION}")
            return
        # 倒计时三条路共用一个值了，页脚可以放心把这个数写死——上一版两条路不同，写哪个都错一半。
        # 安静窗口不进页脚：它只在群里、只在那条路上生效，写进来会让人以为单聊也要先等。
        anyg = settings.auto_send_group_any()
        extra = "（含群里不@我）" if anyg else ""
        d = settings.auto_send_delay()
        window = f"发送前 {d} 秒内可取消" if d else "发送前可取消"
        if settings.judge_engine() == "none":
            # 判断关着时自动发送**仍然会发**（见 auto_pick），但发的是第一条候选，不是判断过的
            # 推荐。原来这里无条件写「自动发送已开启 · 发送前 N 秒内可取消」——在判断关着的
            # 配置下那句话是**假话**：用户以为自己开的是「推荐自动发送」，实际发的是第一条。
            # 页脚是常驻可见的，说破它的成本最低、被看到的概率最高。
            self.footerNote.setText(f"自动发送已开启{extra}（判断关着，发第一条候选） · v{VERSION}")
            return
        self.footerNote.setText(f"自动发送已开启{extra} · {window} · v{VERSION}")

    def _sync_auto_fields(self):
        """按三个开关显隐字段，并把「保存后到底会怎样」写成一句人话。

        自动发送是唯一不可逆的动作，用户不该靠拼几个开关自己推断现在的行为——把结论直接写出来：
        开没开、开的是哪几条路、各自等几秒。群昵称空着时也要说破：只走 @我 那条路时，
        那一刻他以为群里会自动发，实际一次都不会发（宁可不发，不可发错）。
        """
        dm = self.autoDmSwitch.isChecked()
        group = self.autoGroupSwitch.isChecked()
        anyg = self.autoAnySwitch.isChecked()
        on = dm or group or anyg
        for w in self._autoKeyWidgets:
            w.setVisible(on)
        self.autoKeyHint.setVisible(on and not self._compact)
        for w in self._autoAnyWaitWidgets:
            w.setVisible(anyg)
        # 群昵称只在「只回 @我 的」那条路上有用：「不@我也回」开着时群里一律都回，认不认得出 @
        # 已经不影响发不发，再把昵称栏摆在那儿只会让人以为「填了才生效」。
        for w in self._autoGroupWidgets:
            w.setVisible(group and not anyg)
        self.autoGroupHint.setVisible(group and not anyg and not self._compact)
        # 开着 @我 那一半、昵称却是空的：显式警告。判据跟 auto_pick 完全一致
        # （group 开但 my_name 空 → 那条路不自动发），不能让开关和实际行为对不上。
        # anyg 开着时那条路根本不跑，这条警告会说反话，所以一并收掉。
        self.autoNameWarn.setVisible(group and not anyg and not self.myNameEdit.text().strip())
        self.autoAnyWarn.setVisible(anyg)
        # 那句警告里的秒数跟着框走：写死在构造里的话，改了框不改字，用户会以为自己填错了。
        q = self.autoAnyWaitBox.value()
        if q:
            quiet = f"所以这条路会先等群里安静 {q} 秒才动手，这是它唯一的保险。"
        else:
            quiet = ("所以这条路会立刻动手（填 0 = 不等安静），群里有人说话就回，"
                     "很容易变成刷屏。")
        self.autoAnyWarn.setText(
            "「不@我也回」开着：群里任何新消息都会自动回复，包括别人之间的对话——"
            f"程序判断不出哪条是在叫你。{quiet}")
        # 判断关着 + 自动发送开着：会发，但发的是第一条候选。也要说破，理由同上——
        # 用户以为发的是「判断过的推荐」，而实际不是。判据同样跟 auto_pick 对齐。
        self.autoJudgeWarn.setVisible(on and settings.judge_engine() == "none")
        parts = []
        # 倒计时三条路共用一个值（d）；「不@我也回」那条额外多一道安静窗口（q），两个都得写出来，
        # 否则用户改了一个框、看到的行为却是另一个框决定的——上一版就是这么让人以为设置没保存。
        def wait(n):
            return "立刻发" if not n else f"等 {n} 秒"
        d = self.autoDelayBox.value()
        if dm:
            parts.append(f"单聊里对方直接找你（{wait(d)}）")
        if group and not anyg:
            name = self.myNameEdit.text().strip()
            parts.append(f"群里 @{name}（{wait(d)}）" if name
                         else "群里 @我（群昵称还空着，这条暂时不会发）")
        if anyg:
            lead = f"先安静 {q} 秒，" if q else ""
            parts.append(f"群里任何新消息、包括没@你的（{lead}{'再' if q else ''}{wait(d)}）")
        if not on:
            text, color = "保存后：不会自动发送，回复都由你自己确认。", _MUTED
        else:
            # 判断关着时「点取消就能拦下」不是重点，重点是**发的是第一条**——那句话更该出现在
            # 这句总结里，因为它是「保存后到底会怎样」的唯一权威说法。
            tail = ("；判断关着，发的是第一条候选。" if settings.judge_engine() == "none"
                    else "；这期间点「取消发送」就能拦下。")
            text = f"保存后：{'、'.join(parts)}时自动发送{tail}"
            color = "#8a4b12"
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.autoNote, qss, qss)
        self.autoNote.setText(text)

    def _load_settings(self):
        relationship = settings.relationship()
        index = next((i for i, (_, value) in enumerate(_RELATIONSHIPS) if value == relationship),
                     len(_RELATIONSHIPS) - 1)
        self.relationshipBox.setCurrentIndex(index)
        self.relEdit.setText(relationship if _RELATIONSHIPS[index][1] is None else "")
        self.relEdit.setVisible(_RELATIONSHIPS[index][1] is None)
        self.styleEdit.setText(settings.style())
        self.contextBox.setValue(settings.context())
        self.targetSwitch.setChecked(settings.reply_target())
        self.keyEdit.clear()
        # 没填 OpenRouter 密钥不等于「只能起草」了——判断那半会退回自判模式（用起草模型）。
        # 这行字必须说清退到哪一档，否则用户以为自己少了判断，其实只是少了概率。
        self.keyEdit.setPlaceholderText(
            "已配置，留空保留" if settings.has_key() else "选填：判断退回自判模式（无概率）")
        self.keyState.setText("已配置" if settings.has_key() else "未配置 · 判断用起草模型")
        self.providerBox.setCurrentIndex({"openrouter": 0, "deepseek": 1, "custom": 2}
                                        .get(settings.draft_provider(), 0))
        self.dsKeyEdit.clear()
        self.dsKeyEdit.setPlaceholderText(
            "已配置，留空保留" if settings.has_deepseek_key() else "输入你的 DeepSeek 密钥")
        self.dsKeyState.setText("已配置" if settings.has_deepseek_key() else "未配置")
        # 自定义地址 / 模型名存在 config.json，切回别的来源也不清空（只是不生效）
        self.draftUrlEdit.setText(settings.draft_base_url())
        self.draftModelEdit.setText(settings.draft_model())
        self.ckKeyEdit.clear()
        self.ckKeyEdit.setPlaceholderText(
            "已配置，留空保留" if settings.has_custom_key() else "输入该地址对应的密钥")
        self.ckKeyState.setText("已配置" if settings.has_custom_key() else "未配置")
        self.judgeUrlEdit.setText(settings.judge_base_url())
        self.judgeModelEdit.setText(settings.judge_model())
        # 装的是**用户存下的选择**，不是 judge_engine() 那个降级后的结果——否则「选了 OpenRouter
        # 但还没填密钥」会被悄悄改成 self，等他哪天补上密钥也回不到完整模式。降级原因由
        # _sync_engine_fields() 追加在说明里。
        stored = settings.stored_judge_engine() or ("openrouter" if settings.has_key() else "self")
        self.engineBox.setCurrentIndex(
            next((i for i, (_, value) in enumerate(_JUDGE_ENGINES) if value == stored), 0))
        self.judgeKeyEdit.clear()
        self.judgeKeyEdit.setPlaceholderText(
            "已配置，留空保留" if settings.has_judge_key() else "留空用起草那把密钥")
        self.judgeKeyState.setText("已配置" if settings.has_judge_key() else "未配置 · 用起草的")
        self.thinkingSwitch.setChecked(settings.thinking())
        self.retriesBox.setValue(settings.retries())
        self.candidateBox.setValue(settings.candidate_count())
        self.draftTimeoutBox.setValue(settings.draft_timeout())
        self.judgeTimeoutBox.setValue(settings.judge_timeout())
        self.updateSwitch.setChecked(settings.check_update())
        self.historySwitch.setChecked(settings.save_history())
        self.autoDmSwitch.setChecked(settings.auto_send_dm())
        self.autoGroupSwitch.setChecked(settings.auto_send_group())
        self.autoAnySwitch.setChecked(settings.auto_send_group_any())
        self.autoDelayBox.setValue(settings.auto_send_delay())
        self.autoAnyWaitBox.setValue(settings.auto_send_any_wait())
        # 存的是 send_key() 归一后的值（脏值已经退回 enter），所以这里两档一定对得上
        self.sendKeyBox.setCurrentIndex(0 if settings.send_key() == "enter" else 1)
        self.myNameEdit.setText(settings.my_name())
        # 知识库那两格。kb=None 时整张卡藏着，控件照样填——不然将来谁把那块露出来会看到空格子。
        self.kbHistorySwitch.setChecked(settings.kb_history_enabled())
        self.kbCountBox.setValue(settings.kb_history_count())
        self._kb_hint()
        self._sync_ds_fields()  # setCurrentIndex 没变就不发信号，这里补一次
        self._sync_auto_fields()
        self._sync_footer()
        # 标题栏那个「判断」开关跟设置页的引擎是同一个东西，必须跟着刷新：
        # 用户在设置页选了「不判断」再回到首页，开关不能还显示「判断」。
        self.set_judge(settings.stored_judge_engine() != "none")
        self.settingsFeedback.hide()

    def _save(self):
        relationship = _RELATIONSHIPS[self.relationshipBox.currentIndex()][1]
        relationship = relationship or self.relEdit.text().strip()
        key = self.keyEdit.text().strip()
        # max(0, ...) 兜住 currentIndex() == -1：直接拿 tuple[-1] 会静默取到 "custom"
        provider = ("openrouter", "deepseek", "custom")[
            max(0, min(self.providerBox.currentIndex(), 2))]
        # max(0, ...) 同理兜住 currentIndex() == -1
        engine = _JUDGE_ENGINES[max(0, min(self.engineBox.currentIndex(),
                                           len(_JUDGE_ENGINES) - 1))][1]
        deepseek_key = self.dsKeyEdit.text().strip()
        custom_key = self.ckKeyEdit.text().strip()
        judge_key = self.judgeKeyEdit.text().strip()
        draft_url = self.draftUrlEdit.text().strip()
        judge_url = self.judgeUrlEdit.text().strip()
        if not relationship:
            self._settings_feedback("请填写关系背景，或选择一个已有选项。", error=True)
            self.relEdit.setFocus()
            return
        # 「缺密钥」一律不拦着不让存：判断那半本来就可选（缺了就是起草模式），而保存闸会把用户
        # 刚填的地址、模型、密钥一起丢掉——比让他带着不完整的配置走下去更糟。缺什么由
        # settings.draft_problem() 在保存后统一说清。
        # 只留两类当场就能改的校验：地址必须填、格式得能拼出 URL。
        # 自定义地址保存时不联网探测（探测要真发一次请求，等于白花钱）；运行时 core 还会再兜一次底
        # ——地址不可用就退回官方默认，绝不把整条链打死。
        if provider == "custom":
            if not (draft_url or settings.draft_base_url()):
                self._settings_feedback("选了自定义地址，请填写基础地址。", error=True)
                self.draftUrlEdit.setFocus()
                return
            if draft_url and not normalize_endpoint(draft_url, CHAT_PATH):
                self._settings_feedback("基础地址格式不对，要用 http:// 或 https:// 开头。", error=True)
                self.draftUrlEdit.setFocus()
                return
        # 判断地址的合法性只跟 scheme 有关，但用哪条路径要跟引擎一致——顺便让报错文案说得准
        judge_path = DECISIONS_PATH if engine == "openrouter" else CHAT_PATH
        if judge_url and not normalize_endpoint(judge_url, judge_path):
            self._settings_feedback("判断服务地址格式不对，要用 http:// 或 https:// 开头。", error=True)
            self.judgeUrlEdit.setFocus()
            return
        try:
            settings.save(key or None, relationship, self.contextBox.value(),
                          deepseek_key or None, provider,
                          reply_target_on=self.targetSwitch.isChecked(),
                          style_text=self.styleEdit.text().strip(),
                          thinking_on=self.thinkingSwitch.isChecked(),
                          check_update_on=self.updateSwitch.isChecked(),
                          save_history_on=self.historySwitch.isChecked(),
                          custom_key_text=custom_key or None, draft_base_url_text=draft_url,
                          draft_model_text=self.draftModelEdit.text().strip(),
                          judge_base_url_text=judge_url,
                          judge_model_text=self.judgeModelEdit.text().strip(),
                          retries_n=self.retriesBox.value(),
                          judge_engine_text=engine,
                          judge_key_text=judge_key or None,
                          auto_send_dm_on=self.autoDmSwitch.isChecked(),
                          auto_send_group_on=self.autoGroupSwitch.isChecked(),
                          auto_send_delay_n=self.autoDelayBox.value(),
                          auto_send_group_any_on=self.autoAnySwitch.isChecked(),
                          auto_send_any_wait_n=self.autoAnyWaitBox.value(),
                          # 索引 → 值：sendKeyBox 只有两项，越界只可能是 -1（没选中），按 Enter 兜底
                          send_key_text=("enter" if self.sendKeyBox.currentIndex() <= 0 else "ctrl_enter"),
                          my_name_text=self.myNameEdit.text().strip(),
                          draft_timeout_n=self.draftTimeoutBox.value(),
                          judge_timeout_n=self.judgeTimeoutBox.value(),
                          candidate_count_n=self.candidateBox.value(),
                          kb_history_enabled_on=self.kbHistorySwitch.isChecked(),
                          kb_history_count_n=self.kbCountBox.value())
        except Exception:
            self._settings_feedback("保存失败，请检查配置文件是否可写后重试。", error=True)
            return
        self._load_settings()
        self._render_targets()  # 开关刚改过，回到首页时这一行该显该藏得重算一次
        if self.on_settings_change:
            # 让 main 知道设置变了（目前只有一件事：自动发送开关决定子进程要不要盯输入框）。
            # 回调炸了不该让「已保存」这件事看起来失败，所以单独包一层。
            try:
                self.on_settings_change()
            except Exception:
                pass
        problem = settings.draft_problem()
        if problem:
            # 存下来了，但跑不起来。说清楚缺哪一格，别让用户以为一切就绪。
            self._settings_feedback(f"已保存，但还不能开始：{problem}。", error=True)
        elif settings.judge_problem():
            # 起草能跑，但判断那半降级了（比如选了 OpenRouter 却没密钥）——必须当场说，
            # 否则用户以为自己正在用完整模式。
            self._settings_feedback(f"设置已保存。注意：{settings.judge_problem()}。", error=True)
        else:
            note = {"openrouter": "",
                    "self": "（自判模式：有摘要和排序，没有胜出概率）",
                    "none": "（不判断：没有判断摘要、紧张度和排序）"}[settings.judge_engine()]
            if settings.auto_send_on():
                # 开了自动发送就必须在这里再说一遍——用户关掉设置页之后，页脚那行是唯一
                # 一直挂在眼前的提醒，而这一步是他刚做出的、唯一不可逆的选择。
                note += "自动发送已开启：发送前会在候选上方显示倒计时，可点「取消发送」。"
            self._settings_feedback("设置已保存，将用于下一次回复。" + note)
        self.setupButton.hide()
        if not self.cands and not self._busy:
            self._empty_text()
            self.set_status("设置已就绪，等待新消息", "idle")

    # ── 知识库 ──────────────────────────────────────────────────────────────
    # kb=None（几个离线工具构造 Overlay 时不传）时这些入口都不会被调到——按钮和整张卡
    # 都已经藏起来了。但每个入口仍先判一次，免得以后谁接错线时炸在更奇怪的地方。
    # ⚠️ 这里 `self` 是 **Overlay 这个普通 Python 对象，不是控件**。凡是要给弹窗/提示条
    # 当 parent 的地方，一律写 `self.win`——写成 `self` 会让 QFrame.__init__ 直接 ValueError，
    # 而且炸在「联系人已经存好了」之后，用户看到的是一个堆栈而不是「已存为联系人」。

    def _kb_hint(self):
        """设置页知识库那块下面那行说明。跨字段的组合要在这里点破：
        「记录历史」开着但条数是 0 = 只往磁盘记、一条都不注入，用户看着两个控件都正常。"""
        if self.kb is None:
            return
        if not self.kbHistorySwitch.isChecked():
            self.kbHistHint.setText("关着：手写的笔记和联系人照常生效，只是不带聊天历史。")
            self.kbHistHint.show()
        elif self.kbCountBox.value() == 0:
            self.kbHistHint.setText("条数为 0：历史只记录到本机、不会注入到分析里。")
            self.kbHistHint.show()
        else:
            self.kbHistHint.hide()

    def _open_kb(self):
        """打开知识库窗口。懒建——用户不点就不构造那个顶层窗口。"""
        if self.kb is None:
            return
        if self.kbWindow is None:
            self.kbWindow = kb_ui.KnowledgeWindow(self.kb, on_change=self._kb_changed)
        self.kbWindow.show_and_raise()

    def _kb_changed(self):
        """知识库里动过东西（新建/编辑/删除/清空/一键存联系人）。
        通知 main 刷新计数那行；顺手把窗口里的列表也重建一次。"""
        if self.kbWindow is not None:
            try:
                self.kbWindow.refresh()
            except Exception:  # noqa: BLE001 —— 刷新失败不该让「已经存好了」看起来失败
                pass
        if self.on_kb_change:
            try:
                self.on_kb_change()
            except Exception:  # noqa: BLE001
                pass

    def _clear_kb(self):
        if self.kb is None:
            return
        counts = self.kb.counts()
        if counts.notes == 0 and counts.contacts == 0:
            kb_ui.toast(self.win, "知识库本来就是空的")
            return
        if not kb_ui.confirm(
                self.win, "清空知识库",
                f"删掉全部 {counts.notes} 条笔记、{counts.contacts} 个联系人"
                f"（含 {counts.log_lines} 条历史）？不可恢复。密钥和设置不受影响。",
                "清空", danger=True):
            return
        self.kb.clear_all()
        # 窗口是建在这个 store 上的，clear_all 之后它可能还挂着已经删掉的条目。
        # 直接销毁、下次点开重建，比逐个列表去对账可靠。
        if self.kbWindow is not None:
            self.kbWindow.close()
            self.kbWindow.deleteLater()
            self.kbWindow = None
        self.kbResult.setText("")
        self._kb_changed()
        kb_ui.toast(self.win, "知识库已清空")

    def _selfcheck_kb(self):
        """跑一遍知识库自检（临时数据，跑完自删），把结果写在那行小字上。"""
        if self.kb is None:
            return
        try:
            text = kb_selfcheck.run(self.kb)
        except Exception as e:  # noqa: BLE001 —— 自检自己都不该炸出去
            text = f"自检异常：{type(e).__name__} {e}"
        ok = text.startswith("自检通过")
        qss = f"BodyLabel {{ color: {_GREEN if ok else _RED}; background: transparent; }}"
        setCustomStyleSheet(self.kbResult, qss, qss)
        self.kbResult.setText(text)
        self.kbResult.show()
        self._kb_changed()  # 自检会删掉自己造的数据，计数仍值得刷一次

    def _save_contact(self):
        """把界面上正在看的会话存成知识库联系人（上游是长按悬浮球）。"""
        if self.kb is None:
            return
        title = self.current_chat()
        if not title:
            self.set_status("还没识别到会话，先把微信切到要存的那个聊天再试。", "warning")
            return
        try:
            message = self.kb.save_or_merge_contact(title, "wechat")
        except Exception as e:  # noqa: BLE001
            self.log(f"[存联系人失败] {type(e).__name__}: {e}")
            kb_ui.toast(self.win, "存联系人失败，详见聊天记录")
            return
        self._kb_changed()
        kb_ui.toast(self.win, message)
        self.set_status(message + "，可在设置 → 知识库与联系人里补关系和备注。", "success")

    def set_context_info(self, notes, history):
        """这一轮到底带了什么：命中几条笔记、注入几条历史。main 在每次分析前调一次。
        两条都是 0 时明确写「未使用」，免得用户以为带了东西其实没有。"""
        if self.kb is None:
            return
        if not notes and not history:
            self.kbLine.setText("本轮未使用知识库")
        else:
            self.kbLine.setText(f"本轮已带上：{notes} 条笔记、{history} 条历史")
        self.kbLine.show()

    def _settings_feedback(self, text, error=False):
        color = "#b44832" if error else _GREEN
        qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
        setCustomStyleSheet(self.settingsFeedback, qss, qss)
        self.settingsFeedback.setText(text)
        self.settingsFeedback.show()

    def _focus_gap(self):
        """打开设置页时焦点落在真正缺的那一格上：能跑就落在关系背景，缺什么落在什么。"""
        if settings.draft_ready():
            return self.relationshipBox
        provider = ("openrouter", "deepseek", "custom")[
            max(0, min(self.providerBox.currentIndex(), 2))]
        if provider == "deepseek":
            return self.dsKeyEdit
        if provider == "custom":
            if not normalize_endpoint(self.draftUrlEdit.text().strip(), CHAT_PATH):
                return self.draftUrlEdit
            return self.ckKeyEdit
        return self.keyEdit

    def open_settings(self):
        if self._autoPending:
            # 倒计时条只在首页看得见，进了设置页就点不到「取消」了——先收掉，方向永远是「不发」。
            self._stop_auto()
            self.set_status("已暂停这次自动发送（你打开了设置页），回复仍在候选里。", "idle")
        if self.pages.currentWidget() != self.settingsPage:
            self._load_settings()
        self.pages.setCurrentWidget(self.settingsPage)
        self.settingsButton.setEnabled(False)
        self._focus_gap().setFocus()

    def _back_home(self):
        self.keyEdit.clear()
        self.dsKeyEdit.clear()
        self.judgeKeyEdit.clear()  # 密钥框一律不留在界面上，返回首页就清
        self.pages.setCurrentWidget(self.home)
        self.settingsButton.setEnabled(True)

    def _fill(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        # 用户自己动手填了，就说明他要亲自处理：正在跑的倒计时立刻收掉，别在他改字的时候
        # 把「推荐那条」按发送键发出去。
        self._stop_auto()
        try:
            self.on_fill(self.cands[index])
        except Exception as e:
            # 状态栏保持友好文案；真实原因和压缩堆栈进聊天记录，认得出是哪一步炸的
            import traceback
            self.set_status("未能填入，请确认微信窗口可用后重试，或复制回复。", "error")
            self.log(f"[填入失败] {type(e).__name__}: {e}")
            self.log(f"[填入失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
            return
        self.set_status("已尝试填入，请在微信确认内容后发送。", "success")

    def _copy(self, index):
        if self._busy or not self._current or index >= len(self.cands):
            return
        self._stop_auto()  # 同 _fill：手动接管 = 撤销这次自动发送
        self.app.clipboard().setText(self.cands[index])
        self.set_status("回复已复制，可在微信中粘贴并修改。", "success")

    def can_auto_send(self):
        """界面上这份结果现在能不能自动发送。**只看界面这一侧**：正看着的就是微信当前会话、
        候选还有效。输入框里有没有字、会话有没有被切走，得看子进程和 main 的状态——那些由
        main 在真正按发送键之前再确认一遍，两边都过了才发。"""
        return bool(self.cands) and self._current and self._shown == self._chat

    def begin_auto(self, text, seconds, note=""):
        """把「N 秒后自动发送」摆出来并开始倒计时；到点回调 on_auto_send(text)。

        这是自动发送唯一能后悔的窗口，所以它必须显眼、必须一键能取消。seconds <= 0 表示用户
        自己把等待填成了 0——那就不再给窗口，但发送前的确认一步不少（见 main 那边的确认）。

        note 是「这次为什么发这条」的一句提示（目前只有「判断关着，发的是第一条」）。
        它必须跟着倒计时一起显示：用户以为自己开的是「判断过的推荐自动发送」，
        实际发的是第一条——不写出来他永远不会知道，等发现时话已经发出去了。
        """
        self._autoSerial += 1
        serial = self._autoSerial
        self._autoText = text
        # 拆成两行：self.x = max(...) 会被 tools/check_ui_layout.py 当成「构造了控件」，
        # 那是个 AST 启发式（只看右值是不是 Call），没必要为它去扩白名单。
        left = max(0, int(seconds))
        self._autoLeft = left
        self._autoPending = True
        self.autoText.setText(text if len(text) <= 90 else text[:90] + "…")
        self.autoText.setToolTip(text)
        self.autoBarNote.setText(note)
        self.autoBarNote.setVisible(bool(note))
        self.autoBar.show()
        self._auto_paint()
        if self._autoLeft <= 0:
            self.after(0, lambda serial=serial: self._auto_fire(serial))
        else:
            self._autoTimer.start()

    def _auto_paint(self):
        self.autoLabel.setText(f"{self._autoLeft} 秒后自动发送" if self._autoLeft > 0
                               else "正在自动发送…")

    def _auto_tick(self):
        self._autoLeft -= 1
        self._auto_paint()
        if self._autoLeft > 0:
            return
        self._autoTimer.stop()
        # 让「正在自动发送…」露一下脸再动手：按发送键要抢前台、点输入框，中间那几百毫秒
        # 界面看起来不该像什么都没发生。
        serial = self._autoSerial
        self.after(150, lambda serial=serial: self._auto_fire(serial))

    def _auto_fire(self, serial=None):
        # 带序号的回调来自某一轮；如果这一轮已经被取消并且又开始了新一轮，旧回调必须失效。
        # 不带序号是测试/内部立即触发的当前轮回调，保留这个形式也方便手动收尾。
        if serial is not None and serial != self._autoSerial:
            return
        if not self._autoPending:  # 这 150ms 里被取消/作废了
            return
        self._autoPending = False
        self._autoTimer.stop()
        self.autoBar.hide()
        if self.on_auto_send:
            self.on_auto_send(self._autoText)

    def _stop_auto(self):
        """内部收尾：停表、收条。不动状态栏——该说什么由调用方决定。

        序号递增会让已经排进事件队列的旧 singleShot 失效；只 stop QTimer 不够，
        150ms 的 lambda 已经在队列里了，下一轮 begin_auto() 后它仍然会回来。
        """
        self._autoSerial += 1
        self._autoPending = False
        self._autoTimer.stop()
        self.autoBar.hide()

    def _cancel_auto(self):
        """用户点了「取消发送」。这是这个功能里最重要的一次点击，所以要把结果说清楚。"""
        self._stop_auto()
        self.set_status("已取消这次自动发送。回复还在候选里，你确认后再发。", "idle")

    def auto_pending(self):
        """倒计时是不是还开着。main 用它决定要不要因为「用户开始打字了」而当场收掉。"""
        return self._autoPending

    def cancel_auto(self, reason):
        """外部（main）要求取消：输入框里已经有字、会话被切走之类。reason 写进状态栏。
        没在倒计时就什么都不做——别让一句「已取消」盖掉更重要的状态。"""
        if not self._autoPending:
            return
        self._stop_auto()
        self.set_status(reason, "warning")

    def _capture_toggled(self, on):
        """用户自己拨的开关：界面先改，再通知父进程去开/停采集。"""
        self._capture_text(on)
        if self.on_toggle_capture:
            self.on_toggle_capture(on)

    def _judge_toggled(self, on):
        """标题栏「判断」开关。先让 main 存，再刷新依赖它的显示 —— 不在这里写配置文件。

        顺序很要紧：页脚那句「自动发送已开启（判断关着…）」和设置页那条警告都读
        `settings.judge_engine()`。先存再读，它们才是刚生效的值；反过来的话要等下次刷新才对，
        用户看到的就是「关掉了判断，页脚却还说没事」。
        """
        if self.on_toggle_judge:
            self.on_toggle_judge(on)
        self._sync_footer()
        self._sync_auto_fields()
        if not on:
            self.set_status("已关闭判断，只给候选回复（更快）。", "success")
            return
        # 打开时显示「实际会走哪一档」：用户存的是 openrouter 但没密钥的话，实际是自判。
        if settings.judge_engine() == "self":
            self.set_status("已开启判断（用你自己的模型做判断和排序，不给概率）。", "success")
        else:
            self.set_status("已开启判断。", "success")

    def set_judge(self, on):
        """父进程回报的状态：只改界面，不回调。跟 set_capture 一个做法，免得来回打架。"""
        self.judgeSwitch.blockSignals(True)
        self.judgeSwitch.setChecked(on)
        self.judgeSwitch.blockSignals(False)

    def set_update(self, latest, url):
        """main.py 后台线程查到比当前新的版本才会调这个。只显示版本号和 Release 链接，别的什么都没有。"""
        self.updateLabel.setText(f"有新版本 v{latest}")
        self.updateLink.setUrl(url)
        self.updateBar.show()

    def set_capture(self, on, reason=""):
        """父进程回报的状态：只改界面，不回调（不然和父进程来回打架）。reason 为空用默认说明。"""
        if not on:
            # 采集停了就再也确认不了输入框里有没有字，倒计时留着只会把话发错。
            self._stop_auto()
        self.captureSwitch.blockSignals(True)
        self.captureSwitch.setChecked(on)
        self.captureSwitch.blockSignals(False)
        self._capture_text(on, reason)

    def _capture_text(self, on, reason=""):
        """开关状态对应的状态行和空态文案。已有的候选不受影响，暂停了照样能填入/复制。"""
        configured = settings.draft_ready()
        if not on:
            self.set_status(reason or "采集已暂停，微信内容不再读取", "warning")
        elif configured:
            self.set_status("等待新消息", "idle")
        else:
            self.set_status("请先在设置中配置回复服务", "warning")
        if self._busy or self.cands:  # 正在生成或已有候选时，空态卡片本来就看不见
            return
        if not on:
            self.emptyTitle.setText("采集已暂停")
            self.emptyHint.setText("微信里的内容暂时不再读取。\n打开标题栏的开关，继续接收新消息。")
            self.setupButton.setVisible(not configured)
        else:
            self._empty_text()

    def set_busy(self, busy):
        self._busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.invalidate_replies()
            self.progress.start()
            self.set_status("正在根据新消息整理回复…", "busy")
            if not self.cands:
                self.emptyTitle.setText("正在想一句合适的回复")
                self.emptyHint.setText("正在结合上下文生成建议，稍等一下。")
                self.setupButton.hide()
        else:
            self.progress.stop()
            if not self.cands:
                self._empty_text()
        for card in self.cards:
            card.set_available(self._current and not busy)

    def _empty_text(self):
        """空态卡片的默认文案：起草跑不起来 / 起草模式 / 完整模式，三套说法。"""
        ready = settings.draft_ready()
        if ready:
            self.emptyTitle.setText("等待对方的新消息")
            self.emptyHint.setText("保持微信聊天窗口打开。\n收到新消息后，回复建议会出现在这里。"
                                   + _mode_note())
        else:
            self.emptyTitle.setText("先设置，再开始")
            self.emptyHint.setText(settings.draft_problem() + "。\n在设置里补上就能开始。")
        self.setupButton.setVisible(not ready)

    def invalidate_replies(self):
        self._stop_auto()  # 候选都要作废了，那条「N 秒后自动发送」自然也不作数
        self._current = False
        if self.cands:
            self.updated.setText("上次建议")
        for card in self.cards:
            card.set_available(False)

    def set_status(self, text, kind="idle"):
        colors = {"idle": _MUTED, "busy": _GREEN, "success": _GREEN,
                  "warning": "#93611d", "error": "#b44832"}
        markers = {"idle": "●", "busy": "●", "success": "✓", "warning": "!", "error": "!"}
        qss = f"BodyLabel {{ color: {colors.get(kind, _MUTED)}; background: transparent; }}"
        setCustomStyleSheet(self.status, qss, qss)
        self.status.setText(f"{markers.get(kind, '●')}  {text}")
        if kind == "error" and self._busy:
            self.set_busy(False)
        if kind == "error" and not self.cands:
            self.emptyTitle.setText("暂时没有可用的回复")
            self.emptyHint.setText("请按上方提示处理。收到新的对方消息后会再次尝试。")
            self.setupButton.setVisible(not settings.draft_ready())

    def set_failed(self, text):
        """生成失败的收尾。三件事，缺一件用户就会卡住：

        1. **把已有的候选恢复成可点。** 失败的是「这一次生成」，不是「上次的结果」——
           set_busy(True) 会把旧候选置灰（标「上次建议」），失败后不恢复的话，用户手上
           明明还有几条能用的回复，按钮却是灰的。没理由锁住他。
        2. **把真因摆到眼前。** 错误详情一直只写在聊天记录面板里，而那个面板默认折叠、
           又和 OCR 文本混在一起，用户根本找不到——「生成失败，请检查网络和服务设置」
           之所以让人抓狂，就是因为看不见原因。失败时替他把面板展开。
        3. 状态栏那行交给调用方给的文案（带状态码和重试次数）。
        """
        self.set_status(text, "error")  # 内部会 set_busy(False)，卡片按 _current 重新算可用性
        if self.cands:
            self._current = True  # 必须在 set_status 之后：它会经 set_busy 用 _current 刷卡片
            for card in self.cards:
                card.set_available(True)
        self.feed.show()
        self._history_title()  # 按钮文案跟着变（展开/收起）

    def _toggle_history(self):
        self.feed.setVisible(self.feed.isHidden())
        self._history_title()

    def _history_title(self):
        action = "展开" if self.feed.isHidden() else "收起"
        count = self.counts.get(self._shown, 0)
        self.historyButton.setText(f"{action}聊天记录" + (f" · {count}" if count else ""))

    def log(self, line):
        """采集状态行：只进正在看的那个会话，不按会话存。"""
        bar = self.feed.verticalScrollBar()
        follow = self.feed.isHidden() or bar.value() >= bar.maximum() - 4
        self.feed.appendPlainText(line)
        if follow:
            bar.setValue(bar.maximum())

    def log_message(self, who, text, name="", timestamp=None, chat=None):
        """按会话存一份；只有正在看的那个会往显示区里写。"""
        chat = chat or self._shown
        speaker = (name or "对方") if who == "her" else "我"
        timestamp = timestamp or datetime.now().strftime("%H:%M")
        self.counts[chat] = self.counts.get(chat, 0) + 1
        lines = self.feeds.setdefault(chat, [])
        lines.append(f"{timestamp}  {speaker}\n{text}\n")
        del lines[:-_LOG_LINES]
        if who == "her":
            self.hers[chat] = text
        self._add_chat(chat)
        if chat != self._shown:
            return
        self.log(lines[-1])
        if who == "her":
            self._show_latest(text)
        self._history_title()

    def _show_latest(self, text):
        self.latest.setText(text if len(text) <= 120 else text[:120] + "…")
        self.latest.setToolTip(text)
        self.context.show()

    def current_chat(self):
        """界面上正在看的会话（不一定是微信当前开着的那个）。"""
        return self._shown

    def set_chat(self, title):
        """微信切到了哪个会话：登记进下拉框并自动跟过去，不触发用户选择的回调。"""
        if not title:
            return
        browsing = self._shown != self._chat  # 正看着的就是它、但之前是「浏览中」：也得重画，把填入放开
        self._chat = title
        self._add_chat(title)
        if title != self._shown or browsing:
            self.chatBox.blockSignals(True)
            self.chatBox.setCurrentIndex(self.chatBox.findText(title))
            self.chatBox.blockSignals(False)
            self._switch_to(title)
        self._follow_text()

    def _add_chat(self, title):
        """新会话自动进下拉框；addItem 添第一条时会自己选中，别让它触发切换。"""
        if not title or self.chatBox.findText(title) >= 0:
            return
        self.chatBox.blockSignals(True)
        self.chatBox.addItem(title)
        self.chatBox.blockSignals(False)

    def _on_chat_selected(self, index):
        """用户自己挑了一个会话：只换看的内容，微信那边不动。"""
        title = self.chatBox.itemText(index)
        if title and title != self._shown:
            self._switch_to(title)

    def _switch_to(self, title):
        """换正在看的会话：记录、对方最近说、条数、上次的建议一起换过去。"""
        self._shown = title
        self.feed.clear()
        for line in self.feeds.get(title, []):
            self.feed.appendPlainText(line)
        her = self.hers.get(title)
        if her:
            self._show_latest(her)
        else:
            self.context.hide()
        self._history_title()
        self._follow_text()
        self._render_targets()
        self.show_cached(self.result_of(title) if self.result_of else None)

    def set_targets(self, chat, senders, current):
        """某个会话的发言人名单（最近的在前）和当前回复对象；正看着它才重画。"""
        self.targets[chat] = (list(senders), current)
        if chat == self._shown:
            self._render_targets()

    def _render_targets(self):
        """开关关着、或这个会话没有发言人（单聊），这一行就不出现。
        重填下拉框时屏蔽信号，别把自己的填充当成用户挑的。"""
        senders, current = self.targets.get(self._shown, ([], None))
        visible = bool(senders) and settings.reply_target()
        self.targetRow.setVisible(visible)
        if not visible:
            return
        self.targetBox.blockSignals(True)
        self.targetBox.clear()
        self.targetBox.addItems(senders)
        self.targetBox.setCurrentIndex(senders.index(current) if current in senders else 0)
        self.targetBox.blockSignals(False)

    def _on_target_selected(self, index):
        """用户挑了回复对象。浏览别的会话时改的就是那个会话的对象——记录、候选也都按会话走，口径一致。"""
        name = self.targetBox.itemText(index)
        if not name:
            return
        senders, _ = self.targets.get(self._shown, ([], None))
        self.targets[self._shown] = (senders, name)
        self.set_status(f"按「{name}」重新生成…", "busy")
        if self.on_target_change:
            self.on_target_change(self._shown, name)

    def at_prefix_enabled(self):
        """填入时要不要带「@名字 」前缀（只记在界面上，不落盘）。"""
        return self.atCheck.isChecked()

    def _follow_text(self):
        self.chatFollow.setText(("跟随微信" if self._shown == self._chat else "浏览中") if self._chat else "")

    def show_cached(self, result):
        """把某个会话上次的结果放回界面；没有就回到空态。浏览别的会话时只给看不给填——
        微信当前开着的不是它，填进去就串会话了。"""
        if result:
            self.show(result)
        else:
            self._stop_auto()  # 切到一个没有结果的会话：正在跑的倒计时属于别的会话，必须收掉
            self.cands = []
            self._clear_cards()
            self.insight.hide()
            self.referenceNote.hide()
            self.empty.show()
            self.updated.setText("")
            self._empty_text()
        if self._shown != self._chat:
            self.invalidate_replies()
            self.set_status(f"正在浏览「{self._shown}」，只看不填；微信切回它才能用。")

    def show(self, result):
        """按推荐顺序展示，按钮始终绑定 candidates 的原始索引。

        三档的区别全在这里：
        - 完整模式（openrouter）：有概率 → 卡片带百分比、按概率排。
        - 自判模式（self）：有排序、**没有概率** → 按模型给的排序排、标推荐，但不显示百分比。
        - 起草模式（none）：没有判断 → 不标推荐、卡片保持生成顺序。
        共同底线：宁可不给信号，也不编一个假信号。
        """
        self._stop_auto()  # 换了一批候选，上一批的倒计时不作数（main 会按新结果重新起一个）
        self.cands = result["candidates"]
        judged = bool(result.get("judged", True))
        engine = result.get("judge_engine") or ("openrouter" if judged else "none")
        judge_error = result.get("judge_error") or None
        self.set_busy(False)
        self._current = bool(self.cands)
        self._clear_cards()
        raw_scores = result.get("scores") or []
        scores = [raw_scores[i] if i < len(raw_scores) else None for i in range(len(self.cands))]
        if not judged or not any(scores):  # 未判断，或全 0/None（旧结果或接口未返回）就不展示百分比
            scores = [None] * len(self.cands)
        best, order = _rank(len(self.cands), judged, result.get("best_index"), scores,
                            result.get("ranking"))
        for position, index in enumerate(order):
            card = _ReplyCard(self, index, recommended=index == best and best is not None,
                              number=position, score=scores[index], judged=judged)
            self.replyBox.addWidget(card)
            self.cards.append(card)
        reply_to = result.get("reply_to")
        self.insightTitle.setText(f"对话参考 · 回复给 {reply_to}" if reply_to else "对话参考")
        answers = result.get("answers") or {}
        if judged:
            self.summary.setText("建议：" + _choice(answers, "best_action"))
            self.intent.setText("可能意图 · " + _choice(answers, "true_intent") +
                                "\n可能需要 · " + _choice(answers, "she_needs"))
            self.intent.setVisible(True)
            score = (answers.get("danger_level") or {}).get("score")
            valid_score = isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 9
            self.tension.setVisible(True)
            self.tension.setText(f"紧张度 {score:.0f}/9" if valid_score else "紧张度待判断")
            color = "#996819" if valid_score and score >= 3 else _MUTED
            if valid_score and score >= 6:
                color = "#b44832"
            qss = f"BodyLabel {{ color: {color}; background: transparent; }}"
            setCustomStyleSheet(self.tension, qss, qss)
            # 自判模式的摘要和排序是模型真做了的判断，但紧张度是它自己评的、概率更是没有——
            # 这句话必须说出来，否则用户分不清哪个数字有校准依据。
            self.judgeNote.setText(
                "由你自己的模型自评，未经校准；因此只给推荐顺序，不给胜出概率。"
                if engine == "self" else "")
            self.judgeNote.setVisible(bool(self.judgeNote.text()))
        elif judge_error:
            # 判断失败但候选保住了（起草已经成功）。把原因摆出来，用户能据此自己修
            # （换个模型、改地址），而不是只看到「未判断」不知道为什么。
            head = judge_error.get("hint") or "判断这一步没成功"
            if judge_error.get("status"):
                head = f"{judge_error['status']} {head}"
            self.summary.setText("判断失败 · 候选回复照常可用")
            self.intent.setText(head + "\n候选回复不受影响，可以直接填入；详情见下方聊天记录。")
            self.intent.setVisible(True)
            self.tension.setVisible(False)
            self.judgeNote.setVisible(False)
        else:
            # 没判断。少了哪一半按引擎说清楚，别装作正常。
            self.summary.setText({
                "openrouter": "未判断 · 缺 OpenRouter 密钥",
                "self": "未判断 · 起草服务还没配好",
                "none": "未判断 · 已在设置里关掉判断",
            }[engine])
            self.intent.setText("只生成了候选回复，没有意图判断、紧张度评分和排序。\n"
                                "在设置页「判断与排序」里选一个判断引擎就能补上。")
            self.intent.setVisible(True)
            self.tension.setVisible(False)
            self.judgeNote.setVisible(False)
        self.empty.setVisible(not self.cands)
        self.insight.setVisible(bool(self.cands))
        self.referenceNote.setVisible(bool(self.cands) and not self._compact)
        self.updated.setText(datetime.now().strftime("%H:%M") + " 更新")
        if self.cands:
            if judged:
                self.set_status("建议已更新，选一句适合你的回复" if engine != "self"
                                else "已生成候选回复（自判模式，有排序无概率）", "success")
            elif judge_error:
                self.set_status("候选回复已生成，但判断那一步失败了；详情见聊天记录", "warning")
            else:
                self.set_status("已生成候选回复（不判断，未排序）", "success")
        else:
            self.set_status("未生成可用回复，请等待下一条新消息。", "error")

    def _clear_cards(self):
        for card in self.cards:
            self.replyBox.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []

    def after(self, ms, fn):
        QTimer.singleShot(ms, fn)

    def run(self):
        self.app.exec()
