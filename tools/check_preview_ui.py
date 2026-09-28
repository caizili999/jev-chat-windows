# -*- coding: utf-8 -*-
"""静态检查 tools/preview_ui.py 跟 app/settings.py 的接口对不对得上。

`preview_ui.py` 是**唯一**会把自己的界面截图公开出去的东西（那些图进 README）。它有两条硬承诺，
两条都靠「桩」实现，而桩漏一个是**静默**的：

1. **「不读取真实密钥，也不修改环境变量或 config.json」。** 它靠 `patch.multiple(settings, ...)`
   把要用的那几个函数换掉。overlay 里每多一处**会读真实配置**的 `settings.xxx()` 调用，就多一处
   可能把用户自己的开关状态、甚至本机路径拍进公开的截图里——表现不是报错，而是截图不对。
2. **形参必须跟 `settings.save()` 对得上。** 少一个，设置页一点「保存设置」就 TypeError，
   然后被 `_save()` 的 except 吞成「保存失败，请检查配置文件是否可写」——一个很难查的假故障。

判断「会不会读真实配置」不能只看调用名，要看它**走到底会不会碰到** `_cfg()` / `_get_key()`；
而且**遇到已经被桩掉的函数就停**——那一段在运行时已经被换掉了，走不到真实数据。
（`draft_problem()` 就是个例子：它本身不读配置，只是把一堆已经被桩掉的读函数拼起来。）

第二个检查不看名字、看**调用点**：`_save()` 里前 5 个是位置参数（名字不一样无所谓），
其余全是关键字（名字对不上就会 TypeError）。所以判据是「桩至少收得下那几个位置参数」
+「`_save()` 传的每个关键字桩都得有」。

这个脚本不需要 Qt、不联网、不碰任何真实配置，纯 AST + inspect。

跑法：python tools/check_preview_ui.py   （通过 exit 0，发现问题 exit 1）
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app import settings  # noqa: E402

# 真正的「读真实数据」的落点。两类：
#   - 函数调用：_cfg() 读 config.json，_get_key() 读进程环境 + 注册表；
#   - 名字引用：_ROOT 是从 __file__ / sys.executable 算出来的**本机绝对路径**——
#     history_dir() / kb_dir() 就靠它，桩不掉的话截图里会把自己的目录结构印出来。
_SINK_CALLS = {"_cfg", "_get_key"}
_SINK_NAMES = {"_ROOT", "_CONFIG"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _calls_in(node) -> set:
    """node 子树里所有「名字形式的函数调用」——只看模块内互调，属性和别的都跳过。"""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            out.add(sub.func.id)
    return out


def _names_in(node) -> set:
    """node 子树里引用到的模块级名字（含 `os.path.join(_ROOT, …)` 这种嵌套的）。"""
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _module_functions(tree: ast.Module) -> dict:
    """模块里每个顶层函数 → (它调用的其它顶层函数, 它引用的模块级名字)。"""
    funcs = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = (_calls_in(node), _names_in(node))
    return funcs


def _reads_real_state(name: str, funcs: dict, stubbed: set, seen=None) -> bool:
    """这个函数（在 preview 的桩之下）走到底会不会碰到真实数据。

    已经被桩掉的函数直接返回 False——运行时它已经被替换了。
    """
    if name in stubbed:
        return False
    if name in _SINK_CALLS or name in _SINK_NAMES:
        return True
    seen = seen or set()
    if name in seen or name not in funcs:
        return False
    seen.add(name)
    calls, names = funcs[name]
    if names & _SINK_NAMES:
        return True
    return any(_reads_real_state(callee, funcs, stubbed, seen) for callee in calls)


def _settings_calls(path: Path) -> set:
    """源码里所有 `settings.<name>(...)` 用到的名字。"""
    names = set()
    for node in ast.walk(_tree(path)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "settings"):
            names.add(node.func.attr)
    return names


def _stubbed(path: Path) -> set:
    """`patch.multiple(settings, **kw)` 里桩掉的那批名字。"""
    for node in ast.walk(_tree(path)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "multiple" and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id == "settings"):
            return {kw.arg for kw in node.keywords}
    return set()


def _demo_save_params(path: Path) -> list:
    """preview_ui 里那个 save_demo_settings 的形参名（按顺序）。"""
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == "save_demo_settings":
            args = node.args
            return [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    return []


def _save_call(path: Path) -> tuple:
    """overlay 里那处 `settings.save(...)` 的 (位置参数个数, 关键字名集合)。"""
    for node in ast.walk(_tree(path)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "save"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "settings"):
            return len(node.args), {kw.arg for kw in node.keywords}
    return 0, set()


def main() -> int:
    overlay = _ROOT / "app" / "overlay.py"
    preview = _ROOT / "tools" / "preview_ui.py"
    problems = []

    # 1. overlay 调到的每个**会读真实配置**的 settings 函数，都必须在 preview 里被桩掉
    stubbed = _stubbed(preview)
    funcs = _module_functions(_tree(_ROOT / "app" / "settings.py"))
    called = _settings_calls(overlay)
    leaks = sorted(n for n in called
                   if n in funcs and _reads_real_state(n, funcs, stubbed))
    if leaks:
        problems.append(
            "preview_ui 没桩这几个会读真实配置的 settings 调用，预览会去读真实 config.json / 注册表"
            f"（把用户自己的配置拍进公开截图里）：{', '.join(leaks)}")

    # 2. save_demo_settings 要收得下 _save() 传的东西
    demo = _demo_save_params(preview)
    real = list(settings.save.__code__.co_varnames[:settings.save.__code__.co_argcount])
    positional, keywords = _save_call(overlay)
    if len(demo) < positional:
        problems.append(
            f"save_demo_settings 只有 {len(demo)} 个形参，收不下 _save() 传的 {positional} 个位置参数"
            "（点「保存设置」会 TypeError，然后被吞成「保存失败」）")
    missing_kw = sorted(keywords - set(demo))
    if missing_kw:
        problems.append(
            f"save_demo_settings 少了这几个关键字形参：{missing_kw}"
            "（点「保存设置」会 TypeError，然后被吞成「保存失败」）")
    if len(demo) != len(real):
        problems.append(
            f"save_demo_settings 的形参个数（{len(demo)}）跟 settings.save()（{len(real)}）对不上，"
            "多半是 settings.save 加了新参数而预览脚本没跟上")

    if problems:
        for line in problems:
            print(f"tools/preview_ui.py: {line}")
        return 1
    print(f"preview_ui 与 settings 的接口检查通过"
          f"（{len(stubbed)} 个桩覆盖 overlay 的 {len(called)} 处 settings 调用，"
          f"其中会读真实配置的 {len(leaks)} 处；save 形参 {len(demo)} 个收得下"
          f" {positional} 个位置参数 + {len(keywords)} 个关键字）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
