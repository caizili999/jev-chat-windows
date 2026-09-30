# -*- coding: utf-8 -*-
"""把「悬浮窗里的知识库接线」真的跑一遍验证（离屏，不需要显示器）。

为什么要单独一个脚本：知识库这条线**必须对没有知识库的用户完全无感**——
不给 kb（几个离线工具就是这么构造 Overlay 的）时，设置页那块、首页那行计数、
「存为联系人」按钮一个都不能出现，行为要跟加这个功能之前一模一样。
而「藏起来了」这件事只有真构造一遍才看得见：hide() 忘了调、或者 hide 了却
parent 还挂在布局里（占着位置留一块空白），静态检查都发现不了。

同时钉住三件容易悄悄错的事：
  - _load_settings / _save 有没有真的读写这两个新字段（漏一个 = 设置存了不生效）；
  - 「记录历史开着、条数是 0」这条跨字段的静默失效有没有被说出来（_kb_hint）；
  - 清空知识库之后那个已经建好的窗口有没有被销毁（不然它显示的还是已删掉的条目）。

还有一条**多独立窗口专属**的：「当前看到的窗口」那栏（check_windows_column）。
会话名从 OCR 头部改成 Windows 窗口标题之后，现有联系人一个都对不上，不配一次那些
历史会静默失效——所以「点一下把窗口标题写进别名」这条链路必须真的通，而且
**不给 windows_of 时那栏一个像素都不能露**（离线工具和单窗口用户看到的是老界面）。

跑法：python tools/check_kb_ui.py   （通过 exit 0，失败抛 AssertionError 并 exit 1）

**绝不碰用户真实数据**：config.json 指到临时文件；知识库 store 建在临时目录上；
确认框和 toast 都被替换掉，不会弹窗也不会阻塞。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在 import PySide6 之前
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QWidget  # noqa: E402

from app import settings  # noqa: E402
from app.kb import KbStore  # noqa: E402
from app.kb.models import Contact, LogEntry, Note  # noqa: E402
from app.overlay import Overlay  # noqa: E402
from app import overlay as overlay_mod  # noqa: E402


def _tmp_config(initial: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json", prefix="jev_kbui_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(initial, f, ensure_ascii=False)
    settings._CONFIG = path
    return path


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 下面两个由 __main__ 填好：桩不能只记文案，还得记 parent 才能查出「传错了对象」这类 bug。
_toast_parents: list = []
_real_toast = None


def _on_settings_page(ov, widget) -> bool:
    """这个控件的父链通不通到设置页。构造了却没进布局的控件，父链是断的。"""
    node = widget.parentWidget()
    while node is not None and node is not ov.settingsPage:
        node = node.parentWidget()
    return node is ov.settingsPage


# ── 1. 没有知识库：整条线一个控件都不露 ──────────────────────────────────────

def check_absent(ov) -> None:
    """kb=None：三个入口全藏起来，设置页里不留一块空白。

    这是「不改动现有行为」最直接的证据——几个离线工具（preview_ui / check_auto_send）
    构造 Overlay 时不传 kb，它们的界面必须跟加这个功能之前一个像素都不差。
    """
    ov.win.show()
    ov.open_settings()
    ov.app.processEvents()
    assert ov.kb is None, "这条用例的 Overlay 不该有 store"
    assert ov.kbCard.isHidden(), "没有 store 时设置页那张知识库卡必须藏起来"
    assert not ov.kbCard.isVisible(), "藏了就该真的看不见（不是被别的控件盖住）"
    ov._back_home()
    ov.app.processEvents()
    assert ov.kbLine.isHidden(), "没有 store 时首页那行计数必须藏起来"
    assert ov.saveContactButton.isHidden(), "没有 store 时「存为联系人」必须藏起来"
    # 藏起来的控件不能还占着高度（isHidden 的控件不参与布局，这里顺手确认一遍）
    assert ov.kbLine.height() == 0 or ov.kbLine.isHidden()


# ── 2. 有知识库：控件真的挂上去了、并且能看见 ────────────────────────────────

def check_present(ov) -> None:
    """kb=store：整张卡在设置页里、首页两行也露出来。"""
    assert ov.kb is not None
    assert not ov.kbCard.isHidden(), "有 store 时知识库卡该露出来"
    assert _on_settings_page(ov, ov.kbCard), "知识库卡没进设置页的布局"
    assert _on_settings_page(ov, ov.kbOpenButton), "「知识库与联系人」按钮没进布局"
    assert _on_settings_page(ov, ov.kbCheckButton), "「自检」按钮没进布局"
    ov.open_settings()
    ov.app.processEvents()
    for widget, name in ((ov.kbHistorySwitch, "记录历史开关"), (ov.kbCountBox, "历史条数"),
                         (ov.kbOpenButton, "打开按钮"), (ov.kbClearButton, "清空按钮")):
        assert widget.isVisible(), f"设置页打开着，{name} 却不可见（多半没进布局）"
    assert (ov.kbCountBox.minimum(), ov.kbCountBox.maximum()) == (0, settings._MAX_KB_HISTORY), \
        f"条数上限该跟 settings 一致，实际 {ov.kbCountBox.maximum()}"
    assert settings._MAX_KB_HISTORY == 100, "上游 ContextBuilder 是 coerceIn(0, 100)"
    assert ov.kbResult.isHidden(), "没跑过自检时那行结果不该占位置"
    ov._back_home()
    ov.app.processEvents()
    assert not ov.kbLine.isHidden(), "有 store 时首页该有那行「本轮带了什么」"
    assert not ov.saveContactButton.isHidden(), "有 store 时「存为联系人」该露出来"


# ── 3. 设置往返：两个新字段真被读写 ──────────────────────────────────────────

def check_roundtrip(ov, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友", "kb_history_enabled": True,
                   "kb_history_count": 7}, f, ensure_ascii=False)
    ov._load_settings()
    assert ov.kbHistorySwitch.isChecked() is True, "_load_settings 没读到开关"
    assert ov.kbCountBox.value() == 7, f"_load_settings 没读到 7，读到 {ov.kbCountBox.value()}"

    ov.kbHistorySwitch.setChecked(False)
    ov.kbCountBox.setValue(12)
    ov._save()
    saved = _read(path)
    assert saved["kb_history_enabled"] is False, f"开关没写回：{saved.get('kb_history_enabled')}"
    assert saved["kb_history_count"] == 12, f"条数没写回：{saved.get('kb_history_count')}"

    # 0 是合法值（只记录、不注入），不能被当成「没填」而退回默认 30
    ov.kbHistorySwitch.setChecked(True)
    ov.kbCountBox.setValue(0)
    ov._save()
    saved = _read(path)
    assert saved["kb_history_count"] == 0, f"0 被吃掉了：{saved.get('kb_history_count')}"
    assert saved["kb_history_enabled"] is True, "开关被 0 带偏了"
    # 顺手确认没把别的键弄丢（save() 是全量写，漏一个键就是一次静默丢设置）
    for key in ("relationship", "context", "retries", "judge_engine", "auto_send_delay"):
        assert key in saved, f"save() 漏了已有键 {key}"


def check_clamp() -> None:
    """settings 那两个新读函数对脏值的处理，跟界面的值域必须一致。"""
    path = settings._CONFIG
    for raw, want in ((999, 100), (-5, 0), ("abc", 30), (None, 30), (0, 0), (55, 55)):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"relationship": "朋友", "kb_history_count": raw}, f, ensure_ascii=False)
        got = settings.kb_history_count()
        assert got == want, f"kb_history_count={raw!r} 应为 {want}，实际 {got}"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"relationship": "朋友"}, f, ensure_ascii=False)
    assert settings.kb_history_enabled() is False, "缺这一项时默认必须是关的"


# ── 4. 跨字段的静默失效必须被说出来 ──────────────────────────────────────────

def check_hint(ov) -> None:
    """「记录历史开着、条数是 0」= 只记录、永不注入。两个控件各自看都正常，
    凑一起才失效，用户自己发现不了——所以必须有一行字点破。"""
    ov.open_settings()
    ov.kbHistorySwitch.setChecked(True)
    ov.kbCountBox.setValue(0)
    ov.app.processEvents()
    assert ov.kbHistHint.isVisible(), "「开着 + 0 条」必须给出提示"
    assert "0" in ov.kbHistHint.text(), f"提示得说清是 0 条：{ov.kbHistHint.text()!r}"
    assert "不" in ov.kbHistHint.text(), f"提示得说清不会注入：{ov.kbHistHint.text()!r}"

    ov.kbCountBox.setValue(30)
    ov.app.processEvents()
    assert ov.kbHistHint.isHidden(), "正常组合下不该留一行多余的话"

    ov.kbHistorySwitch.setChecked(False)
    ov.app.processEvents()
    assert ov.kbHistHint.isVisible(), "关着的时候也该说清「笔记和联系人照常生效」"
    assert "笔记" in ov.kbHistHint.text(), f"得说明关掉历史不影响笔记：{ov.kbHistHint.text()!r}"
    ov._back_home()


# ── 5. 首页那行「本轮带了什么」 ──────────────────────────────────────────────

def check_context_line(ov) -> None:
    # 什么都没识别到：老文案，不能变
    ov.set_context_info(0, 0)
    assert "未使用" in ov.kbLine.text(), f"0/0 时要明确说没用到：{ov.kbLine.text()!r}"

    # 识别到联系人、但确实没东西可补 —— **不能说「未使用」**。
    # 用户就是这么被误导的：他存了联系人、知识库里也有十几条记录，却看到「未使用」，
    # 以为功能坏了；其实是那一屏的消息全在屏幕上、被去重剔干净了。
    ov.set_context_info(0, 0, contact_matched=True)
    text = ov.kbLine.text()
    assert "未使用" not in text, f"联系人命中了就不该说没用到：{text!r}"
    assert "联系人" in text, f"该告诉用户联系人已经认出来了：{text!r}"

    # 只有联系人背景（关系/备注）被注入时，不能退化成「0 条笔记、0 条历史」
    ov.set_context_info(0, 0, background="关系：同事\n关于KK：说话客气些")
    text = ov.kbLine.text()
    assert "未使用" not in text, f"背景发出去了就不能说没用到：{text!r}"
    assert "背景" in text, f"该说清带上的是联系人背景：{text!r}"

    # 纯空白 background 不算背景，别被它骗成「已带上」
    ov.set_context_info(0, 0, background="   ")
    assert "未使用" in ov.kbLine.text(), f"空白背景不算数：{ov.kbLine.text()!r}"

    ov.set_context_info(2, 5)
    text = ov.kbLine.text()
    assert "2" in text and "5" in text, f"两个数都要写出来：{text!r}"
    assert "笔记" in text and "历史" in text, f"得说清带的是什么：{text!r}"
    assert "背景" not in text, f"没背景时别多嘴：{text!r}"

    # 有笔记/历史、又有背景：三样都要在，且原来的两个数还在原位
    ov.set_context_info(2, 5, background="关系：同事")
    text = ov.kbLine.text()
    assert "2" in text and "5" in text and "背景" in text, f"三样都该写出来：{text!r}"
    assert text.startswith("本轮已带上：2 条笔记、5 条历史"), f"原有的两个数不能变形：{text!r}"


# ── 6. 一键存联系人 ─────────────────────────────────────────────────────────

def check_save_contact(ov, store, toasts) -> None:
    """界面上正看着的会话 → 知识库联系人。没有会话时只提示，不写东西。"""
    # 先在没有会话的情况下点：不该凭空造出一个联系人
    ov._chat = ""
    ov._shown = ""
    before = len(store.contacts())
    ov._save_contact()
    assert len(store.contacts()) == before, "没有当前会话时不该写任何东西"
    assert "warning" in ov.status.text() or "还没" in ov.status.text(), \
        f"该提示用户先切到会话：{ov.status.text()!r}"

    ov.set_chat("测试群(12)")
    ov._save_contact()
    names = [c.name for c in store.contacts()]
    assert "测试群" in names, f"该把去人数后的名字存进去，实际 {names}"
    assert toasts, "存成功该给一句 toast（非阻塞提示）"
    assert any("联系人" in t for t in toasts), f"toast 文案不对：{toasts}"
    # 再存一次：是「已存在」，不能变成两条
    ov._save_contact()
    assert len(store.contacts()) == 1, f"重复存不该造出第二条：{[c.name for c in store.contacts()]}"
    assert any("已存在" in t or "并入" in t for t in toasts), f"第二次该说已存在：{toasts}"


# ── 7. 打开 / 清空 ───────────────────────────────────────────────────────────

def check_open_and_clear(ov, store, toasts) -> None:
    """窗口懒建、只建一次；清空之后必须销毁重建（否则它还显示已删掉的条目）。"""
    assert ov.kbWindow is None, "没点过就不该先建出那个顶层窗口"
    ov._open_kb()
    assert ov.kbWindow is not None, "点了要建出来"
    first = ov.kbWindow
    ov._open_kb()
    assert ov.kbWindow is first, "第二次点该复用，不该又建一个"
    ov.kbWindow.close()

    # 空库时点清空：只提示，不弹确认框
    store.clear_all()
    toasts.clear()
    ov._clear_kb()
    assert any("空" in t for t in toasts), f"空库该提示「本来就是空的」：{toasts}"
    assert ov.kbWindow is first, "空库时没真删东西，窗口不该被销毁"

    # 有内容时点清空：真删、销毁窗口、刷新计数
    store.save_note(Note(id="n1", title="口味", content="不吃香菜", tags=["吃饭"]))
    store.save_contact(Contact(id="c1", name="测试群", apps=["wechat"]))
    changed = []
    ov.on_kb_change = lambda: changed.append(1)
    toasts.clear()
    ov._clear_kb()
    assert store.counts().notes == 0 and store.counts().contacts == 0, \
        f"该清干净，实际 {store.counts()}"
    assert ov.kbWindow is None, "清空后必须销毁那个窗口，否则它还显示已删掉的条目"
    assert changed, "清空后该通知外面刷新计数"
    assert any("已清空" in t for t in toasts), f"toast 文案不对：{toasts}"


# ── 7b. 「当前看到的窗口」那栏：窗口标题 → 联系人别名 ─────────────────────────
#
# 这一栏是**多独立窗口这条路最要紧的一步**，所以单独钉住。会话名从「OCR 主窗口头部」
# 改成「Windows 窗口标题」之后，窗口标题跟现有联系人一个都对不上——真实例子里是
# `1群` vs `1群(4)Q`（那个 Q 是 OCR 把 🔍 搜索图标认成的字）。不配一次，那几十上百条
# 历史会**静默**对不上：首页只是从「用了知识库」变成「本轮未使用知识库」，
# 用户只会以为功能坏了。点一下把窗口标题写进别名，两条路就统一到同一个 key 了。
#
# 顺带钉住三条容易改坏的：
#   - 不给 windows_of（离线工具、单窗口用户）时整栏**不能出现**，否则界面就变了；
#   - 给了但一个独立窗口都没开 → 同样不出现，不留一块空卡片；
#   - 拿窗口列表时抛异常 → 窗口照样打得开（那栏只是锦上添花，不该拖垮整个窗口）。

def _texts(root) -> list:
    """把一棵控件树里所有静态文案收出来（标签、按钮都算），用来断言「那栏在不在」。"""
    out = []
    for widget in root.findChildren(QWidget):
        getter = getattr(widget, "text", None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except TypeError:      # 少数控件 text() 要参数，跳过
            continue
        if isinstance(value, str):
            out.append(value)
    return out


def _by_accessible(root, name: str):
    """按 accessibleName 找控件。**不靠下标**——布局顺序改一次，下标断言就假绿了。"""
    for widget in root.findChildren(QWidget):
        if widget.accessibleName() == name:
            return widget
    return None


def _open_contacts(ov):
    """打开知识库窗口并切到「联系人」页。"""
    ov._open_kb()
    ov.kbWindow._select(1)
    ov.app.processEvents()
    return ov.kbWindow


def _close_ov(ov) -> None:
    try:
        if ov.kbWindow is not None:
            ov.kbWindow.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        ov.win.close()
    except Exception:  # noqa: BLE001
        pass


def check_windows_column(store, toasts) -> None:
    store.clear_all()
    # 「1群(4)Q」是真实存在的旧名字（Q 是 OCR 认错的 🔍）；「老婆」两个名字本来就对得上。
    store.save_contact(Contact(id="w1", name="1群(4)Q", apps=["wechat"]))
    store.save_contact(Contact(id="w2", name="老婆", apps=["wechat"]))

    titles = ["1群", "老婆"]
    changed = []
    ov = Overlay(on_fill=lambda *a: None, kb=store,
                 on_kb_change=lambda: changed.append(1),
                 windows_of=lambda: list(titles))
    try:
        ov.win.show()
        win = _open_contacts(ov)

        texts = _texts(win)
        assert "当前看到的窗口" in texts, f"有独立窗口时该有那栏：{texts}"
        assert "1群" in texts and "老婆" in texts, f"两个窗口标题都该列出来：{texts}"
        # 名字就对得上的（老婆）→ 已配到；对不上的（1群）→ 还没配到
        assert any("已配到「老婆」" in t for t in texts), f"该认出「老婆」：{texts}"
        assert sum(1 for t in texts if "还没配到联系人" in t) == 1, \
            f"只有「1群」还没配到：{texts}"
        # 已经配上的那个，下拉框要**预选**在它身上：不然一边写着「已配到『老婆』」、
        # 下拉里却是别人，看着像显示错了
        paired = _by_accessible(win, "把「老婆」配到哪个联系人")
        assert paired is not None and paired.currentText() == "老婆", \
            f"已配上的窗口该预选在那个联系人上，实际 {getattr(paired, 'currentText', lambda: '?')()!r}"

        # 点「配到…」，把窗口标题写进「1群(4)Q」的别名
        pick = _by_accessible(win, "把「1群」配到哪个联系人")
        button = _by_accessible(win, "把「1群」配到选中的联系人")
        assert pick is not None and button is not None, "每个窗口都该有下拉和「配到…」按钮"
        index = pick.findText("1群(4)Q")
        assert index >= 0, \
            f"下拉里该有「1群(4)Q」，实际 {[pick.itemText(i) for i in range(pick.count())]}"
        pick.setCurrentIndex(index)
        toasts.clear()
        button.click()
        ov.app.processEvents()

        hit = store.find_contact("1群", "wechat")
        assert hit is not None and hit.name == "1群(4)Q", \
            f"配完「1群」该命中「1群(4)Q」，实际 {getattr(hit, 'name', None)}"
        assert "1群" in hit.aliases, f"窗口标题该进别名：{hit.aliases}"
        assert changed, "配对之后该通知外面刷新"
        assert any("已把「1群」配到" in t for t in toasts), f"该给一句 toast：{toasts}"

        # 界面上那行也要跟着变成「已配到」
        texts = _texts(win)
        assert any("已配到「1群(4)Q」" in t for t in texts), f"配完该显示已配到：{texts}"
        assert not any("还没配到联系人" in t for t in texts), f"都配完了不该还剩：{texts}"
        repick = _by_accessible(win, "把「1群」配到哪个联系人")
        assert repick is not None and repick.currentText() == "1群(4)Q", \
            "配完之后下拉该预选在那个联系人上（不然用户以为没配上）"

        # 再点一次：是「已经在」，不能堆第二条别名（refresh 会重建控件，得重新找）
        before = list(store.find_contact("1群", "wechat").aliases)
        pick = _by_accessible(win, "把「1群」配到哪个联系人")
        button = _by_accessible(win, "把「1群」配到选中的联系人")
        pick.setCurrentIndex(pick.findText("1群(4)Q"))
        toasts.clear()
        button.click()
        ov.app.processEvents()
        after = store.find_contact("1群", "wechat").aliases
        assert after == before, f"重复配不该堆别名：{before} → {after}"
        assert any("已经在" in t for t in toasts), f"第二次该说已经在：{toasts}"
    finally:
        _close_ov(ov)

    # ① 不给 windows_of：整栏不出现（离线工具 / 单窗口用户看到的界面一个像素都不能变）
    ov2 = Overlay(on_fill=lambda *a: None, kb=store)
    try:
        win2 = _open_contacts(ov2)
        assert "当前看到的窗口" not in _texts(win2), "没给 windows_of 时那栏不该出现"
    finally:
        _close_ov(ov2)

    # ② 给了但一个独立窗口都没开：同样不出现，不留一块空卡片
    ov3 = Overlay(on_fill=lambda *a: None, kb=store, windows_of=lambda: [])
    try:
        win3 = _open_contacts(ov3)
        assert "当前看到的窗口" not in _texts(win3), "没有独立窗口时不该留一块空卡片"
    finally:
        _close_ov(ov3)

    # ③ 拿窗口列表时抛异常：窗口照样要打得开（这栏只是锦上添花）
    def _boom():
        raise OSError("拿不到窗口列表")

    ov4 = Overlay(on_fill=lambda *a: None, kb=store, windows_of=_boom)
    try:
        win4 = _open_contacts(ov4)
        assert "当前看到的窗口" not in _texts(win4), "拿不到窗口列表时那栏该安静地不出现"
        assert "联系人" in _texts(win4) or "新建联系人" in _texts(win4), \
            "那栏出不来也不该把整个联系人页带塌"
    finally:
        _close_ov(ov4)


# ── 7c. 「有个窗口还没配到联系人」那条提示 ────────────────────────────────────
#
# 这条提示**必须非模态**：调用它的是 main 的 tick()，而 `ov.after(50, tick)` 是 tick() 的
# **最后一行**。在 tick() 里弹模态框（MessageBoxBase.exec()）会让父进程彻底停死——
# 子进程还在截图、队列一直堆、没人消费（DESIGN_MULTIWINDOW §6.3）。
# 「它是不是模态的」光看代码看不出来，只能真构造一遍再问 isModal()。

def check_unpaired_hint(ov, store) -> None:
    from PySide6.QtWidgets import QDialog, QPushButton
    from qfluentwidgets import InfoBar

    kb_ui = overlay_mod.kb_ui

    # ① 真的把它建出来（不是桩），确认不是模态框
    ov.win.show()
    opened = []
    bar = kb_ui.toast_unpaired(ov.win, "1群", on_open=lambda: opened.append(1))
    assert bar is not None, "提示条该建得出来"
    assert not isinstance(bar, QDialog), "提示条**绝不能**是模态对话框——它是在主循环里弹的"
    assert not bar.isModal(), "提示条不能是模态的"
    ov.app.processEvents()

    # ② 得有一个「去配对」按钮，点了会回调出去
    buttons = [b for b in bar.findChildren(QPushButton) if b.text() == "去配对"]
    assert buttons, "提示条上该有「去配对」——光说一句对不上，用户还是不知道点哪儿"
    buttons[0].click()
    ov.app.processEvents()
    assert opened == [1], "点了「去配对」该回调出去"
    # 点完不用再 close()：qfluentwidgets 的 InfoBar 关掉会自己析构，
    # 再碰 `bar` 会 `RuntimeError: Internal C++ object already deleted`（正好也证明了它真关了）。

    # ③ 传错 parent 不该崩（跟 toast 一个口径：它只负责好看，出不来也不能把采集拖垮）
    try:
        kb_ui.toast_unpaired(object(), "1群")
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"toast_unpaired 收到非控件 parent 时抛了 {type(e).__name__}: {e}") from e
    ov.app.processEvents()

    # ④ 有知识库、标题对不上 → 真冒一条；没有知识库 → 一条都不冒
    #    （没有 store 时根本没有「配到联系人」这回事，凭空冒提示就破坏了
    #     「不加知识库就逐字节不变」这条最硬的约束）
    before = len(ov.win.findChildren(InfoBar))
    ov.notify_unpaired("1群")
    ov.app.processEvents()
    assert len(ov.win.findChildren(InfoBar)) > before, "有知识库时该冒一条提示"

    bare = Overlay(on_fill=lambda *a: None)
    try:
        bare.win.show()
        bare.app.processEvents()
        before = len(bare.win.findChildren(InfoBar))
        bare.notify_unpaired("1群")
        bare.app.processEvents()
        assert len(bare.win.findChildren(InfoBar)) == before, "没有知识库时不该冒提示条"
    finally:
        bare.win.hide()

    # ⑤ 真正的接线：回调是 _open_kb_for_pairing —— 开窗口 **并停在联系人页**
    #    （「当前看到的窗口」那一栏在联系人页里，停在笔记页用户还是找不到）
    ov.kbWindow = None
    ov._open_kb_for_pairing()
    ov.app.processEvents()
    assert ov.kbWindow is not None, "「去配对」该把知识库窗口开出来"
    assert ov.kbWindow._tab == 1, "而且要直接停在联系人页"
    ov.kbWindow.close()
    ov.kbWindow = None


# ── 8. 自检那条线真的通 ─────────────────────────────────────────────────────

def check_selfcheck(ov, store) -> None:
    store.save_note(Note(id="keep", title="用户的笔记", content="别动我", tags=["x"]))
    ov._selfcheck_kb()
    text = ov.kbResult.text()
    assert text, "自检必须给出结果"
    assert not ov.kbResult.isHidden(), "跑过自检那行结果要露出来"
    assert "自检" in text, f"文案不对：{text!r}"
    # 自检造的东西必须自己清掉，用户那条笔记原封不动
    names = [n.title for n in store.notes()]
    assert names == ["用户的笔记"], f"自检留了残渣或动了用户数据：{names}"


# ── 8b. 提示条 / 确认框的 parent 必须是真控件 ────────────────────────────────
#
# 这条是**真出过事**才加的：`_save_contact` 里写成了 `kb_ui.toast(self, …)`，而 Overlay
# 的 `self` 是个普通 Python 对象、不是控件 → `QFrame.__init__(parent=<Overlay>)` 直接
# ValueError。更糟的是它炸在**联系人已经写进磁盘之后**，用户看到的是堆栈而不是「已存为联系人」。
#
# 上一版的检查为什么没抓到：它用 `lambda parent, message: toasts.append(message)` 把 toast
# 整个换掉了——**把被测的东西桩掉，就等于没测**。现在桩里会校验 parent 的类型，
# 另外再拿真的 InfoBar 跑一次，确认那条路本身是通的。

def check_toast_parent(ov, store, fake_toast) -> None:
    """① 被记录下来的 parent 都必须是 QWidget；② 真的 InfoBar 能建起来；
    ③ **拿真的 toast 把用户那次报错的场景原样重放一遍**。"""
    from PySide6.QtWidgets import QWidget

    bad = [(type(p).__name__) for p in _toast_parents if not isinstance(p, QWidget)]
    assert not bad, (
        f"toast/confirm 收到过不是控件的 parent：{bad}——"
        "多半是把 Overlay 的 `self` 传进去了，应该写 `self.win`")

    real_toast = _real_toast
    ov.win.show()
    ov.app.processEvents()

    # 把真 toast 装回去，跑一遍真实的 _save_contact —— 这正是用户报错的那条路：
    # 联系人是**先存好、再弹提示**的，所以提示条一崩，用户看到的就是「已经存好了」+ 一个堆栈。
    overlay_mod.kb_ui.toast = real_toast
    try:
        ov.set_chat("检查群(3)")
        try:
            ov._save_contact()
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"用真 toast 跑 _save_contact 抛了 {type(e).__name__}: {e}——"
                "联系人都已经写进磁盘了，这一步不该崩") from e
        assert "检查群" in [c.name for c in store.contacts()], \
            f"联系人没存进去：{[c.name for c in store.contacts()]}"
        assert "联系人" in ov.status.text(), f"状态栏该给出反馈：{ov.status.text()!r}"
        ov.app.processEvents()

        # 确认框同理：只构造、不 exec（exec 是模态的，会把脚本卡住）
        from app.kb.ui import _ConfirmDialog
        try:
            dialog = _ConfirmDialog(ov.win, "检查用", "这条只是构造一下，不弹。")
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"真的 _ConfirmDialog(ov.win, …) 构造失败：{type(e).__name__}: {e}") from e
        dialog.deleteLater()
        ov.app.processEvents()

        # 兜底：万一以后又有人传了非控件，提示条该降级、不该崩
        try:
            real_toast(object(), "传错 parent 也不该崩")
        except Exception as e:  # noqa: BLE001
            raise AssertionError(
                f"kb_ui.toast 收到非控件 parent 时抛了 {type(e).__name__}: {e}——"
                "它只负责好看，出不来也不该把调用方那件「已经做完的事」变成一次崩溃") from e
        ov.app.processEvents()
    finally:
        overlay_mod.kb_ui.toast = fake_toast


# ── 9. main 那根线：app/kb → core 的唯一交接面 ──────────────────────────────

def check_main_wiring(ov, store, path: str) -> None:
    """main.build_knowledge 是 app/kb 与 core 之间**唯一**的那根线，必须两头都对：
    没 store → None（core 走老路，请求里一个字段都不多）；有 store → 真带上 background/history，
    并且把条数推给首页那行「本轮带了什么」。

    「没 store 返回 None」这一条是「不改动现有行为」的最后一环：core.engine 收到 None 时，
    请求体跟加这个功能之前逐字节相同（这条由 tools/check_kb_inject.py 钉着）。
    """
    import main as main_mod

    # ov / kb_store 正常由 main.py 的 __main__ 段建好；这里是直接 import 的，
    # 所以可能还不存在——用 getattr 兜住，并在最后原样还原。
    real_ov = getattr(main_mod, "ov", None)
    real_store = main_mod.kb_store
    try:
        main_mod.ov = ov
        main_mod.kb_store = None
        assert main_mod.build_knowledge("测试群", [("her", "在吗，这周末有空吗")]) is None, \
            "没有 store 时必须返回 None"

        store.clear_all()
        store.save_contact(Contact(id="c9", name="测试群", apps=["wechat"],
                                   relationship="同事，带我做项目的组长"))
        store.save_note(Note(id="n9", title="口味忌口", content="不吃香菜",
                             tags=["吃饭"], always_on=True))
        store.append_log("c9", [LogEntry("her", "上周说好周五交稿", store.now() - 86_400_000,
                                         "wechat")], screen_batch=False)
        main_mod.kb_store = store

        # (a) 历史关着：笔记和联系人照常带上，历史一条不带
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"relationship": "朋友", "kb_history_enabled": False}, f, ensure_ascii=False)
        knowledge = main_mod.build_knowledge("测试群", [("her", "在吗，这周末有空吗")])
        assert isinstance(knowledge, dict), f"有 store 时该返回 dict，实际 {type(knowledge)}"
        assert "组长" in knowledge["background"], f"联系人关系没进去：{knowledge['background']!r}"
        assert "香菜" in knowledge["background"], f"常驻笔记没进去：{knowledge['background']!r}"
        assert knowledge["history"] == [], f"历史关着不该带历史：{knowledge['history']!r}"
        assert "1" in ov.kbLine.text() and "0" in ov.kbLine.text(), \
            f"首页那行该报「1 条笔记、0 条历史」：{ov.kbLine.text()!r}"

        # (b) 历史开着：那条更早的消息要出现在 history 里，形状跟 core 认的一致
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"relationship": "朋友", "kb_history_enabled": True,
                       "kb_history_count": 30}, f, ensure_ascii=False)
        knowledge = main_mod.build_knowledge("测试群", [("her", "在吗，这周末有空吗")])
        assert len(knowledge["history"]) == 1, f"该带上那 1 条旧消息：{knowledge['history']!r}"
        assert knowledge["history"][0] == {"from": "her", "text": "上周说好周五交稿"}, \
            f"history 的形状必须是 {{from, text}}（core 只认这个）：{knowledge['history']!r}"
        assert "1" in ov.kbLine.text(), f"首页那行该报带上了历史：{ov.kbLine.text()!r}"

        # (c) 用户实际踩到的坑：联系人命中了、历史也开着，但**这一屏的消息全都还在屏幕上**，
        #     去重（app/kb/context.py 的 on_screen）把它们剔干净 → 什么都不注入。
        #     这时界面必须说「识别到了，但没有可补的内容」，**不能说「未使用知识库」**——
        #     那会让人以为知识库根本没生效，而实际上联系人早认出来了、这一屏也记进去了。
        store.clear_all()
        store.save_contact(Contact(id="c10", name="测试群", apps=["wechat"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"relationship": "朋友", "kb_history_enabled": True,
                       "kb_history_count": 30}, f, ensure_ascii=False)
        knowledge = main_mod.build_knowledge("测试群", [("her", "在吗，这周末有空吗")])
        assert knowledge == {"background": "", "history": []}, \
            f"屏上已有的消息不重复注入，这一轮该是空的：{knowledge!r}"
        text = ov.kbLine.text()
        assert "未使用" not in text, f"联系人命中了就不该说没用到：{text!r}"
        assert "联系人" in text, f"该告诉用户联系人已经认出来了：{text!r}"
    finally:
        main_mod.ov, main_mod.kb_store = real_ov, real_store


if __name__ == "__main__":
    path = _tmp_config({"relationship": "朋友"})
    kb_root = tempfile.mkdtemp(prefix="jev_kbui_store_")
    real_confirm, real_toast = overlay_mod.kb_ui.confirm, overlay_mod.kb_ui.toast
    _real_toast = real_toast          # check_toast_parent 要拿真的那个再跑一遍
    toasts: list = []
    try:
        store = KbStore(kb_root)

        # 桩**不能只记文案**：上一版就是 `lambda parent, message: toasts.append(message)`，
        # 把 parent 整个丢掉了，于是「传了 Overlay 而不是 self.win」这种 bug 从桩底下溜过去了。
        # 现在把 parent 一起记下来，check_toast_parent 会校验它是不是真控件。
        def fake_confirm(parent, *args, **kwargs):
            _toast_parents.append(parent)
            return True          # 一律当「点了确定」，并且不弹窗（离屏下模态框会把脚本卡死）

        def fake_toast(parent, message):
            _toast_parents.append(parent)
            toasts.append(message)

        overlay_mod.kb_ui.confirm = fake_confirm
        overlay_mod.kb_ui.toast = fake_toast

        bare = Overlay(on_fill=lambda *a: None)
        check_absent(bare)

        ov = Overlay(on_fill=lambda *a: None, kb=store, on_kb_change=lambda: None)
        check_present(ov)
        check_roundtrip(ov, path)
        check_clamp()
        check_hint(ov)
        check_context_line(ov)
        check_save_contact(ov, store, toasts)
        check_open_and_clear(ov, store, toasts)
        check_windows_column(store, toasts)
        check_unpaired_hint(ov, store)
        check_selfcheck(ov, store)
        check_main_wiring(ov, store, path)
        check_toast_parent(ov, store, fake_toast)
    finally:
        overlay_mod.kb_ui.confirm, overlay_mod.kb_ui.toast = real_confirm, real_toast
        try:
            os.unlink(path)
        except OSError:
            pass
        shutil.rmtree(kb_root, ignore_errors=True)
    print("悬浮窗知识库接线检查通过"
          "（无库时全隐藏 / 有库时可见 / 设置往返 / 跨字段提示 / 一键存 / 清空 / 自检 / "
          "main 接线 / 提示条的 parent 是真控件 / 「当前看到的窗口」配对 / 未配对提示非模态）",
          flush=True)
    os._exit(0)
