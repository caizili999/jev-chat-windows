# -*- coding: utf-8 -*-
"""静态检查 app/overlay.py：有没有「构造了控件、但忘了加进布局」的属性。

Qt 里这种错误是**静默**的——控件没有父窗口，setVisible(True) 会把它提升成独立顶层窗口
（飘在桌面上的「弹窗」），设置页里反而看不见，也不报任何错。装不了 Qt 的环境下（比如
没有图形界面、或者依赖还没装），静态检查是唯一能自动拦住它的手段。

跑法：python tools/check_ui_layout.py   （通过 exit 0，发现问题 exit 1 并列出位置）
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 不在布局里也正常的：顶层窗口自己 show()，布尔标志根本不是控件，定时器不是控件
# kbWindow 跟 win 同理——知识库是一个**独立的顶层窗口**（长文本编辑，需要宽度和高度，
# 塞进悬浮窗那个 440 宽的页面栈里没法用），所以它本来就不该进任何布局。
_ALLOWED = {"win", "kbWindow", "_current", "_autoTimer"}
_LAYOUT_CALLS = ("addWidget", "addLayout", "addItem")


def _is_self_attr(node) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self")


def check(path: Path, class_name: str = "Overlay") -> list[tuple[int, str]]:
    """→ [(行号, 属性名)]：用 Call 构造、却从未传给 addWidget/addLayout/addItem 的 self.<attr>。

    只看 Call 形式的赋值，所以元组/列表/字典（_dsWidgets、_customWidgets、_hintLabels 之类）
    会被自动跳过——它们不是控件。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == class_name)
    assigned, added = {}, set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_self_attr(target) and isinstance(node.value, ast.Call):
                    assigned[target.attr] = node.lineno
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LAYOUT_CALLS):
            for arg in node.args:
                if _is_self_attr(arg):
                    added.add(arg.attr)
    return sorted((line, name) for name, line in assigned.items()
                  if name not in added and name not in _ALLOWED)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    target = root / "app" / "overlay.py"
    missing = check(target)
    if missing:
        for line, name in missing:
            print(f"{target}:{line}  self.{name} 构造了但没进任何布局"
                  f"（会变成飘在桌面上的顶层窗口，设置页里反而看不见）")
        raise SystemExit(1)
    print("app/overlay.py 布局检查通过")
