# -*- coding: utf-8 -*-
"""知识库与联系人管理窗口（对齐上游 KnowledgeActivity）。

上游是个独立 Activity；Windows 这边对应**一个独立的顶层窗口**——比往悬浮窗那个
440 宽的页面栈里再塞一页合适得多（这里是长文本编辑，需要宽度和高度）。

窗口里显示的一切都只来自 `知识库/` 目录，别处没有。纯代码搭界面，
跟 app/overlay.py 用同一套卡片/文字的观感。

**依赖 Qt**，所以跟 app/kb 的其它模块分开：models / store / context / selfcheck / text
都是 Qt-free 的，只有这个文件和界面有关。
"""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CardWidget, FluentIcon as FIF, InfoBar, InfoBarPosition, LineEdit,
    MessageBoxBase, PlainTextEdit, PrimaryPushButton, PushButton, SubtitleLabel,
    SwitchButton, TransparentToolButton, setCustomStyleSheet, setFont,
)

from app.kb.models import Contact, Note
from app.kb.store import new_id
from app.kb.text import parse_import_blocks, split_tags

_INK = "#233c2f"
_SUB = "#68776f"
_GREEN = "#18794e"
_RED = "#b44832"
_ACCENT = "#2f6b4f"
_TAB_OFF = "#e8ede9"


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


def _card(parent=None):
    card = CardWidget(parent)
    card.setBorderRadius(12)
    card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return card


def _icon_button(icon, title, callback, parent=None, danger=False):
    button = TransparentToolButton(icon, parent)
    button.setFixedSize(30, 30)
    button.setToolTip(title)
    button.setAccessibleName(title)
    if danger:
        qss = f"TransparentToolButton:hover {{ background: rgba(180,72,50,0.12); }}"
        setCustomStyleSheet(button, qss, qss)
    button.clicked.connect(callback)
    return button


class _ClickCard(CardWidget):
    """点一下就能编辑的卡片（上游是「点条目编辑，长按删除」；桌面端把删除做成了按钮）。"""

    def __init__(self, on_click, parent=None):
        self._on_click = on_click
        super().__init__(parent)
        self.setBorderRadius(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):  # noqa: N802 —— Qt 的约定
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self._on_click()
        super().mouseReleaseEvent(event)


# ── 对话框 ──────────────────────────────────────────────────────────────────
# 都用 qfluentwidgets 的 MessageBoxBase：它自带「确定/取消」和遮罩，跟应用其它部分一致。
# 每个对话框把校验放在 validate() 里——不合法就**不关闭**，而不是关掉之后再弹一句提示。

class _BaseDialog(MessageBoxBase):
    def __init__(self, parent, title, width=460):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title, self)
        self.viewLayout.addWidget(self.titleLabel)
        self.warn = _label("", 12, _RED)
        self.widget.setMinimumWidth(width)

    def _field(self, caption, widget, hint=""):
        self.viewLayout.addWidget(_label(caption, 13, _INK, True))
        self.viewLayout.addWidget(widget)
        if hint:
            self.viewLayout.addWidget(_label(hint, 12, _SUB))
        return widget

    def _warn(self, text):
        self.warn.setText(text)
        self.warn.show()

    def _finish(self):
        """把标题/正文那类「先建后填」的控件装进来，并给按钮换中文。"""
        self.viewLayout.addWidget(self.warn)
        self.warn.hide()
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")

    def exec(self) -> bool:
        """跑对话框，返回用户是否确认。"""
        self.warn.hide()
        return super().exec()


class _NoteDialog(_BaseDialog):
    def __init__(self, parent, note: Note | None = None):
        super().__init__(parent, "新建笔记" if note is None else "编辑笔记")
        self.titleEdit = LineEdit(self)
        self.titleEdit.setPlaceholderText("标题，例如：口味忌口")
        self.titleEdit.setAccessibleName("笔记标题")
        self.titleEdit.setText(note.title if note else "")
        self._field("标题", self.titleEdit)

        self.contentEdit = PlainTextEdit(self)
        self.contentEdit.setPlaceholderText("正文，写清楚事实本身")
        self.contentEdit.setAccessibleName("笔记正文")
        self.contentEdit.setMinimumHeight(96)
        self.contentEdit.setPlainText(note.content if note else "")
        self._field("正文", self.contentEdit)

        self.tagsEdit = LineEdit(self)
        self.tagsEdit.setPlaceholderText("逗号分隔，例如：吃饭，周末")
        self.tagsEdit.setAccessibleName("笔记标签")
        self.tagsEdit.setText("，".join(note.tags) if note else "")
        self._field("标签", self.tagsEdit, "命中规则：任一标签或标题出现在会话标题或最近 6 条消息里。")

        self.alwaysRow = self._switch_row("常驻（每次分析都带上）", note.always_on if note else False)
        self.enabledRow = self._switch_row("启用", note.enabled if note else True)
        self._finish()

    def _switch_row(self, text, initial):
        row = QWidget(self)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(_label(text, 13), 1)
        switch = SwitchButton(row)
        switch.setOnText("开")
        switch.setOffText("关")
        switch.setAccessibleName(text)
        switch.setChecked(bool(initial))
        box.addWidget(switch)
        self.viewLayout.addWidget(row)
        return switch

    def validate(self) -> bool:
        if not self.titleEdit.text().strip() and not self.contentEdit.toPlainText().strip():
            self._warn("标题和正文不能都空着。")
            return False
        return True

    def value(self, note_id: str | None) -> Note:
        return Note(id=note_id or new_id(),
                    title=self.titleEdit.text().strip(),
                    content=self.contentEdit.toPlainText().strip(),
                    tags=split_tags(self.tagsEdit.text()),
                    always_on=self.alwaysRow.isChecked(),
                    enabled=self.enabledRow.isChecked())


class _ContactDialog(_BaseDialog):
    def __init__(self, parent, contact: Contact | None = None):
        super().__init__(parent, "新建联系人" if contact is None else "编辑联系人")
        self.nameEdit = LineEdit(self)
        self.nameEdit.setPlaceholderText("名字，一般就是会话标题")
        self.nameEdit.setAccessibleName("联系人名字")
        self.nameEdit.setText(contact.name if contact else "")
        self._field("名字", self.nameEdit)

        self.aliasEdit = PlainTextEdit(self)
        self.aliasEdit.setPlaceholderText("每行一个，例如另一个写法或群名")
        self.aliasEdit.setAccessibleName("联系人别名")
        self.aliasEdit.setMinimumHeight(72)
        self.aliasEdit.setPlainText("\n".join(contact.aliases) if contact else "")
        self._field("别名（每行一个）", self.aliasEdit,
                    "会话标题等于名字或任一别名即算命中（忽略大小写与群人数后缀）。")

        self.relEdit = LineEdit(self)
        self.relEdit.setPlaceholderText("例如：同事，带我做项目的组长")
        self.relEdit.setAccessibleName("联系人关系")
        self.relEdit.setText(contact.relationship if contact else "")
        self._field("关系", self.relEdit, "填了会作为「关系：…」带进分析，覆盖不了上面那个全局关系选项。")

        self.notesEdit = PlainTextEdit(self)
        self.notesEdit.setPlaceholderText("关于这个人要记住的事")
        self.notesEdit.setAccessibleName("联系人备注")
        self.notesEdit.setMinimumHeight(72)
        self.notesEdit.setPlainText(contact.notes if contact else "")
        self._field("备注", self.notesEdit)
        self._finish()

    def validate(self) -> bool:
        if not self.nameEdit.text().strip():
            self._warn("名字不能空着。")
            return False
        return True

    def value(self, existing: Contact | None) -> Contact:
        aliases = [ln.strip() for ln in self.aliasEdit.toPlainText().split("\n")]
        return Contact(id=existing.id if existing else new_id(),
                       name=self.nameEdit.text().strip(),
                       aliases=[a for a in aliases if a],
                       apps=list(existing.apps) if existing else [],
                       relationship=self.relEdit.text().strip(),
                       notes=self.notesEdit.toPlainText().strip(),
                       auto_summary=existing.auto_summary if existing else "")


class _ImportDialog(_BaseDialog):
    _SAMPLE = "口味忌口\n不吃香菜，海鲜过敏\n\n项目代号\n内部叫小蓝"

    def __init__(self, parent):
        super().__init__(parent, "从文本导入")
        self.viewLayout.addWidget(_label("按空行分段，每段第一行当标题，其余当正文。", 12, _SUB))
        self.input = PlainTextEdit(self)
        self.input.setPlaceholderText(self._SAMPLE)
        self.input.setAccessibleName("要导入的文本")
        self.input.setMinimumHeight(150)
        self.viewLayout.addWidget(self.input)
        self._finish()
        self.yesButton.setText("导入")

    def blocks(self) -> list:
        return parse_import_blocks(self.input.toPlainText())


class _ConfirmDialog(_BaseDialog):
    def __init__(self, parent, title, message, ok_text="确定", danger=False):
        super().__init__(parent, title, width=400)
        self.viewLayout.addWidget(_label(message, 13, _INK))
        self.viewLayout.addWidget(self.warn)
        self.warn.hide()
        self.yesButton.setText(ok_text)
        self.cancelButton.setText("取消")
        if danger:
            self.yesButton.setStyleSheet(
                "PrimaryPushButton { background: #b44832; border: 1px solid #b44832; "
                "color: #ffffff; }")

    def validate(self) -> bool:
        return True

    def exec(self) -> bool:
        self.warn.hide()
        return super().exec()


# ── 主窗口 ──────────────────────────────────────────────────────────────────

class KnowledgeWindow(QWidget):
    """知识库与联系人。`on_change()` 在每次改动之后调一次，让外面刷新计数那行。"""

    def __init__(self, store, on_change=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.on_change = on_change
        self._tab = 0
        self.setWindowTitle("Jev · 知识库与联系人")
        self.setMinimumSize(520, 560)
        self.resize(640, 720)
        self.setStyleSheet("QWidget { background: #f5f7f6; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)
        outer.addWidget(_label("知识库与联系人", 22, _INK, True))
        outer.addWidget(_label(
            "只存在本机，不上传。分析时按会话标题匹配联系人、按关键词命中笔记。", 12, _SUB))
        tabs = QHBoxLayout()
        tabs.setSpacing(8)
        self.notesTab = PushButton("笔记")
        self.contactsTab = PushButton("联系人")
        for button, index in ((self.notesTab, 0), (self.contactsTab, 1)):
            button.setAccessibleName(button.text())
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _c=False, i=index: self._select(i))
            tabs.addWidget(button)
        tabs.addStretch(1)
        outer.addLayout(tabs)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("kbPage")
        content.setStyleSheet("QWidget#kbPage { background: transparent; }")
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(0, 4, 0, 8)
        self.body.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self._render()

    # ── 对外 ────────────────────────────────────────────────────────────────

    def refresh(self):
        """数据被别处改过（比如悬浮窗的「存为联系人」）之后重建列表。"""
        self._render()

    def show_and_raise(self):
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    # ── 渲染 ────────────────────────────────────────────────────────────────

    def _select(self, index):
        if index == self._tab:
            return
        self._tab = index
        self._render()

    def _render(self):
        self._style_tabs()
        _clear(self.body)
        if self._tab == 0:
            self._render_notes()
        else:
            self._render_contacts()

    def _style_tabs(self):
        for button, index in ((self.notesTab, 0), (self.contactsTab, 1)):
            active = index == self._tab
            bg = _ACCENT if active else _TAB_OFF
            fg = "#ffffff" if active else _SUB
            qss = (f"PushButton {{ background: {bg}; color: {fg}; border: none; "
                   f"border-radius: 9px; padding: 6px 18px; font-weight: "
                   f"{'600' if active else '400'}; }}"
                   f"PushButton:hover {{ background: {bg}; }}")
            setCustomStyleSheet(button, qss, qss)

    # ── 笔记 ────────────────────────────────────────────────────────────────

    def _render_notes(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        new_button = PrimaryPushButton(FIF.ADD, "新建笔记")
        new_button.setAccessibleName("新建笔记")
        new_button.clicked.connect(lambda: self._edit_note(None))
        row.addWidget(new_button)
        import_button = PushButton("从文本导入")
        import_button.setAccessibleName("从文本导入笔记")
        import_button.clicked.connect(self._import_notes)
        row.addWidget(import_button)
        row.addStretch(1)
        self.body.addLayout(row)

        notes = sorted(self.store.notes(), key=lambda n: n.updated_at, reverse=True)
        if not notes:
            self.body.addWidget(_empty_card(
                "还没有笔记。写点该记住的事实：习惯、忌口、项目代号、约定过的时间。"))
        else:
            for note in notes:
                self.body.addWidget(self._note_card(note))
            self.body.addWidget(_label(
                "点条目编辑，右边的开关控制这条要不要参与命中。", 11, _SUB))
        self.body.addStretch(1)

    def _note_card(self, note: Note):
        card = _ClickCard(lambda: self._edit_note(note))
        box = QHBoxLayout(card)
        box.setContentsMargins(14, 12, 10, 12)
        box.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(3)
        head = note.title or "（无标题）"
        if note.always_on:
            head += "  · 常驻"
        left.addWidget(_label(head, 15, _INK, True))
        left.addWidget(_label("标签：" + "、".join(note.tags) if note.tags else "无标签", 12, _SUB))
        preview = note.content.replace("\n", " ")[:46]
        if preview:
            left.addWidget(_label(preview, 12, _SUB))
        box.addLayout(left, 1)

        enable = SwitchButton(card)
        enable.setOnText("开")
        enable.setOffText("关")
        enable.setAccessibleName(f"启用笔记「{note.title}」")
        enable.setChecked(note.enabled)
        enable.checkedChanged.connect(lambda on, n=note: self._toggle_note(n, on))
        box.addWidget(enable, 0, Qt.AlignTop)
        box.addWidget(_icon_button(FIF.DELETE, "删除这条笔记",
                                   lambda: self._delete_note(note), card, danger=True),
                      0, Qt.AlignTop)
        return card

    def _toggle_note(self, note: Note, on: bool):
        self.store.save_note(_with_enabled(note, on))
        self._changed()

    def _edit_note(self, note: Note | None):
        dialog = _NoteDialog(self, note)
        if not dialog.exec():
            return
        self.store.save_note(dialog.value(note.id if note else None))
        self._render()
        self._changed()

    def _delete_note(self, note: Note):
        if not _ConfirmDialog(self, "删除笔记",
                              f"删除「{note.title or '（无标题）'}」？不可恢复。",
                              "删除", danger=True).exec():
            return
        self.store.delete_note(note.id)
        self._render()
        self._changed()

    def _import_notes(self):
        dialog = _ImportDialog(self)
        if not dialog.exec():
            return
        blocks = dialog.blocks()
        for title, content in blocks:
            self.store.save_note(Note(id=new_id(), title=title, content=content))
        self._render()
        self._changed()
        toast(self, f"已导入 {len(blocks)} 条" if blocks else "没解析出内容")

    # ── 联系人 ──────────────────────────────────────────────────────────────

    def _render_contacts(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        new_button = PrimaryPushButton(FIF.ADD, "新建联系人")
        new_button.setAccessibleName("新建联系人")
        new_button.clicked.connect(lambda: self._edit_contact(None))
        row.addWidget(new_button)
        row.addStretch(1)
        self.body.addLayout(row)

        contacts = sorted(self.store.contacts(), key=lambda c: c.updated_at, reverse=True)
        if not contacts:
            self.body.addWidget(_empty_card(
                "还没有联系人。也可以在回复建议页点「存为联系人」，把当前会话一键存下来。"))
        else:
            for contact in contacts:
                self.body.addWidget(self._contact_card(contact))
            self.body.addWidget(_label(
                "点条目编辑。会话标题等于名字或任一别名即算命中（忽略大小写与群人数后缀）。",
                11, _SUB))
        self.body.addStretch(1)

    def _contact_card(self, contact: Contact):
        card = _ClickCard(lambda: self._edit_contact(contact))
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(3)
        head = QHBoxLayout()
        head.addWidget(_label(contact.name or "（无名）", 15, _INK, True), 1)
        head.addWidget(_icon_button(FIF.DELETE, "删除这个联系人及其历史",
                                    lambda: self._delete_contact(contact), card, danger=True))
        box.addLayout(head)
        if contact.aliases:
            box.addWidget(_label("别名：" + "、".join(contact.aliases), 12, _SUB))
        if contact.apps:
            box.addWidget(_label("来源：" + "、".join(_app_label(a) for a in contact.apps), 12, _SUB))
        if contact.relationship:
            box.addWidget(_label("关系：" + contact.relationship.replace("\n", " ")[:40], 12, _SUB))
        if contact.notes:
            box.addWidget(_label("备注：" + contact.notes.replace("\n", " ")[:40], 12, _SUB))

        count = self.store.log_size(contact.id)
        clear = PushButton(f"清空此人历史（{count} 条）")
        clear.setAccessibleName(f"清空「{contact.name}」的聊天历史")
        clear.setMinimumHeight(26)
        qss = (f"PushButton {{ background: transparent; color: {_RED}; border: none; "
               "padding: 4px 0; font-weight: 600; text-align: left; }"
               f"PushButton:hover {{ color: {_RED}; text-decoration: underline; }}")
        setCustomStyleSheet(clear, qss, qss)
        clear.clicked.connect(lambda: self._clear_contact_log(contact, count))
        box.addWidget(clear, 0, Qt.AlignLeft)
        return card

    def _edit_contact(self, contact: Contact | None):
        dialog = _ContactDialog(self, contact)
        if not dialog.exec():
            return
        self.store.save_contact(dialog.value(contact))
        self._render()
        self._changed()

    def _delete_contact(self, contact: Contact):
        if not _ConfirmDialog(
                self, "删除联系人",
                f"删除「{contact.name}」及其全部历史？不可恢复。密钥等设置不受影响。",
                "删除", danger=True).exec():
            return
        self.store.delete_contact(contact.id)
        self._render()
        self._changed()

    def _clear_contact_log(self, contact: Contact, count: int):
        if count == 0:
            toast(self, "本来就没有历史")
            return
        if not _ConfirmDialog(
                self, "清空历史",
                f"删掉「{contact.name}」的 {count} 条聊天历史？联系人档案保留。",
                "清空", danger=True).exec():
            return
        self.store.clear_log(contact.id)
        self._render()
        self._changed()

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 —— 界面刷新失败不该让「已经存好了」看起来失败
                pass


# ── 给悬浮窗用的两个小入口 ────────────────────────────────────────────────────
# 悬浮窗不该去碰 MessageBoxBase / InfoBar 的细节，所以在这里包一层。
#
# ⚠️ 两个都**不能往外抛**，两个 parent 都必须是真 QWidget。
# 踩过的坑：调用方传了 `self`——在 app/overlay.py 里 `self` 是 Overlay 那个**普通 Python
# 对象**（不是控件），于是 `QFrame.__init__(parent=<Overlay>)` 直接 ValueError。
# 更糟的是它炸在**事情已经做完之后**：联系人已经存进磁盘了，用户看到的却是一个堆栈。
# 所以这里两层防护：parent 不是控件就退回 None；整个调用再包一层 try。
# 提示条和确认框都只负责「好看 / 好问」，不负责「正确」。

def confirm(parent, title: str, message: str, ok_text: str = "确定", danger: bool = False) -> bool:
    """确认框，返回用户是否点了确定。

    弹不出来时返回 **False**（= 当成用户没确认）。这个方向永远是安全的：调用它的地方
    问的都是「要不要删」，答不上来就不删。
    """
    if not isinstance(parent, QWidget):
        parent = None
    try:
        return bool(_ConfirmDialog(parent, title, message, ok_text, danger).exec())
    except Exception:  # noqa: BLE001 —— 问不出来就不做，绝不往外抛
        return False


def toast(parent, message: str):
    """一句非阻塞提示（浮一下就走，不打断操作）。

    出不来就算了——调用它的地方那件事**都已经做完了**（联系人已存、知识库已清空），
    一个提示条弹不出来绝不能反过来让「已经存好了」看起来失败。
    """
    if not isinstance(parent, QWidget):
        parent = None
    try:
        InfoBar.success(title="知识库", content=message, orient=Qt.Horizontal,
                        isClosable=True, position=InfoBarPosition.TOP_RIGHT,
                        duration=2500, parent=parent)
    except Exception:  # noqa: BLE001 —— 提示弹不出来不是故障
        pass


# ── 小工具 ──────────────────────────────────────────────────────────────────

def _with_enabled(note: Note, enabled: bool) -> Note:
    """只改 enabled 的那一版。"""
    return replace(note, enabled=enabled)


def _empty_card(message: str):
    card = _card()
    box = QVBoxLayout(card)
    box.setContentsMargins(16, 18, 16, 18)
    box.addWidget(_label(message, 13, _SUB))
    return card


def _app_label(app: str) -> str:
    """来源标识 → 人看得懂的名字。本项目只有微信，但存下来的值可能是老版本写的别的。"""
    return {"wechat": "微信", "com.tencent.mm": "微信"}.get(app, app)


def _clear(layout):
    """把一个布局里的东西全撤掉。嵌套布局和弹簧也要一起处理，否则会越堆越多。"""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            _clear(child)
