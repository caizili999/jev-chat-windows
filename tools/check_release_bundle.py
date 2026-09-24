# -*- coding: utf-8 -*-
"""发布包体检：确认要传上去的东西里没有你自己的数据。

跑法：
    python tools/check_release_bundle.py                          # 体检 dist/jev-chat-windows
    python tools/check_release_bundle.py path/to/解压出来的目录      # 体检别处（比如下载回来的 zip 解开）
    python tools/check_release_bundle.py --expect-version 1.2.3    # 顺带断言版本号已经盖过章

三件事：
  1. **源码树里没混进你自己的东西**（git ls-files，只查被跟踪的文件）——
     config.json / 聊天记录 / .workbuddy-ai / .env 一旦进了 index，打包时就会跟着走。
  2. **打出来的目录里没混进你自己的东西**（config.json / 聊天记录 / *.csv / *.log / .env）——
     config.json 是你本机跑过这个 exe 之后留下的，里面有 my_name 和中转地址。
  3. **关键载荷还在**（OCR 模型、onnxruntime、qfluentwidgets 资源、windows_capture）——
     少一个就是「双击 exe 立刻炸」，别等用户来报。

为什么不写在 workflow 里：规则里全是中文（"聊天记录"），而 workflow 在 Windows runner 上
默认走 pwsh，非 ASCII 字面量容易在解码那一步出岔子。放在 .py 里（Python 源码默认 UTF-8），
YAML 里只留一行命令，最不容易坏。本地也能跑同一条，所见即 CI 所见。

只读，不改任何文件；不联网。通过 exit 0，失败打印问题清单并 exit 1。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = ROOT / "dist" / "jev-chat-windows"

# 这些东西出现在仓库或发布包里都是事故。用文件名/目录名匹配，不用全文扫描。
FORBIDDEN_NAMES = {
    "config.json",       # 本机设置：my_name、中转地址、关系背景……全是私事
    ".env",
    ".workbuddy-ai",
    ".claude",
    ".cursor",
    "聊天记录",           # 导出的对话内容本体
}
FORBIDDEN_SUFFIXES = {".csv", ".log"}
FORBIDDEN_PREFIXES = (".env.",)  # .env.local / .env.production

# 密钥长得像什么。只用来「提醒」，命中不等于一定泄露（第三方包里有随机串很正常）。
KEY_PATTERNS = [
    ("OpenRouter/OpenAI 风格", re.compile(rb"sk-(?:or-)?v?\d?-?[A-Za-z0-9_\-]{24,}")),
    ("DeepSeek/通用 sk-", re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b")),
]
# 只看小文本文件。二进制里凑出 32 位随机串太容易，扫了全是噪声。
SCAN_SUFFIXES = {".py", ".json", ".txt", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".md", ".qss", ".js"}
SCAN_MAX_BYTES = 2 * 1024 * 1024

# 少一个就「双击 exe 立刻炸」。相对 bundle 根的路径。
REQUIRED = [
    ("jev-chat-windows.exe", "主程序"),
    ("_internal/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx", "OCR 检测模型"),
    ("_internal/rapidocr_onnxruntime/models/ch_PP-OCRv4_rec_infer.onnx", "OCR 识别模型"),
    ("_internal/rapidocr_onnxruntime/config.yaml", "OCR 配置"),
]


def _bad_name(name: str) -> str | None:
    """命中就返回原因，否则 None。大小写不敏感（Windows 上本来也不分）。"""
    low = name.lower()
    if low in FORBIDDEN_NAMES:
        return f"文件名是 {name}"
    if any(low.startswith(p) for p in FORBIDDEN_PREFIXES):
        return f"文件名是 {name}"
    if os.path.splitext(low)[1] in FORBIDDEN_SUFFIXES:
        return f"后缀是 {os.path.splitext(low)[1]}"
    return None


def check_git_index() -> list[str]:
    """被 git 跟踪的文件里有没有不该有的。不是 git 仓库就跳过（比如 CI 里解压出来的源码包）。"""
    if not (ROOT / ".git").exists():
        print("  [跳过] 不是 git 仓库，没法查 index")
        return []
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError) as e:
        return [f"git ls-files 跑不起来（{e}）：这一条没查成，别当成通过"]

    problems = []
    tracked = [p for p in out.split("\0") if p]
    for path in tracked:
        for part in Path(path).parts:
            why = _bad_name(part)
            if why:
                problems.append(f"git 跟踪了 {path}（{why}）")
                break
    print(f"  [OK] index 干净：{len(tracked)} 个被跟踪文件，无 config.json / 聊天记录 / .env")
    return problems


def check_bundle(bundle: Path) -> tuple[list[str], list[str]]:
    """返回 (硬问题, 提醒)。硬问题必须为空才能发。"""
    problems: list[str] = []
    notes: list[str] = []

    if not bundle.is_dir():
        return [f"目录不存在：{bundle}"], []

    n_files = 0
    for dirpath, dirnames, filenames in os.walk(bundle):
        rel_dir = os.path.relpath(dirpath, bundle)
        # 目录名本身也要查：聊天记录/ 这种是一整个目录
        for d in list(dirnames):
            why = _bad_name(d)
            if why:
                problems.append(f"混进了目录 {os.path.join(rel_dir, d)}（{why}）")
        for f in filenames:
            n_files += 1
            why = _bad_name(f)
            if why:
                problems.append(f"混进了文件 {os.path.join(rel_dir, f)}（{why}）")

    if not problems:
        print(f"  [OK] 包里干净：{n_files} 个文件，无 config.json / 聊天记录 / csv / log / .env")

    # 关键载荷
    missing = [(rel, desc) for rel, desc in REQUIRED if not (bundle / rel).exists()]
    for rel, desc in missing:
        problems.append(f"缺关键文件：{rel}（{desc}）")
    if not missing:
        print(f"  [OK] 关键载荷齐：exe + OCR 模型 + 配置")

    # windows_capture 是 Rust 编译的 .pyd，名字带 ABI tag，只能按后缀找
    if not any(p.suffix == ".pyd" and "windows_capture" in p.name
               for p in bundle.rglob("*.pyd")):
        problems.append("缺 windows_capture 的 .pyd（采集层，少了它启动就炸）")
    else:
        print("  [OK] windows_capture 已打进包")

    # qfluentwidgets 不发 .qss：全部样式/图标编译进了 _rc/resource.py（3MB 上下）。
    # 少了它界面会退回素颜 Qt——不崩，但一眼就不是这个程序了，所以按大小查。
    qss = bundle / "_internal" / "qfluentwidgets" / "_rc" / "resource.py"
    if not qss.exists():
        problems.append("缺 qfluentwidgets 的 _rc/resource.py（界面样式会全丢）")
    elif qss.stat().st_size < 1_000_000:
        problems.append(f"qfluentwidgets 的 _rc/resource.py 只有 {qss.stat().st_size} 字节，"
                        "看着不完整（正常 3MB 上下）")
    else:
        print(f"  [OK] qfluentwidgets 样式资源已打进包（{qss.stat().st_size // 1024 // 1024}MB）")

    return problems, notes


def scan_keys(bundle: Path) -> list[str]:
    """在文本类文件里找密钥形状的串。命中只提醒不拦——第三方包里凑出随机串很正常。"""
    hits: list[str] = []
    for p in bundle.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIXES:
            continue
        try:
            if p.stat().st_size > SCAN_MAX_BYTES:
                continue
            blob = p.read_bytes()
        except OSError:
            continue
        for label, pat in KEY_PATTERNS:
            if pat.search(blob):
                hits.append(f"{p.relative_to(bundle)} 里有像「{label}」的串")
                break
    return hits


def check_version(expected: str) -> list[str]:
    """版本号盖章了没。CI 打包前会把 tag 写进 app/version.py，这里查源码那份——
    冻进 exe 里的版本读不出来，但源码盖章是打包的前一步，盖没盖是同一件事。"""
    vfile = ROOT / "app" / "version.py"
    if not vfile.exists():
        return [f"找不到 {vfile}"]
    text = vfile.read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']*)["\']', text, re.M)
    if not m:
        return ["app/version.py 里读不出 VERSION"]
    got = m.group(1)
    if got != expected:
        return [f"版本号没盖章：app/version.py 里是 {got!r}，期望 {expected!r}"]
    print(f"  [OK] 版本号已盖章：{got}")
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布包体检：确认没有你自己的数据混进去")
    ap.add_argument("bundle", nargs="?", default=str(DEFAULT_BUNDLE),
                    help=f"要体检的目录，默认 {DEFAULT_BUNDLE}")
    ap.add_argument("--expect-version", metavar="X.Y.Z",
                    help="断言 app/version.py 里的 VERSION 等于它")
    ap.add_argument("--skip-git", action="store_true", help="不查 git index")
    args = ap.parse_args(argv)

    bundle = Path(args.bundle)
    print(f"体检发布包：{bundle}")

    problems: list[str] = []
    if not args.skip_git:
        print("1) 源码树（git index）")
        problems += check_git_index()

    print("2) 打出来的目录")
    p2, _ = check_bundle(bundle)
    problems += p2

    if args.expect_version:
        print("3) 版本号")
        problems += check_version(args.expect_version)

    notes = scan_keys(bundle) if bundle.is_dir() else []

    print()
    if notes:
        print("提醒（不拦，自己确认一下）：")
        for n in notes:
            print(f"  - {n}")
        print()

    if problems:
        print(f"体检不通过，{len(problems)} 个问题——这些不能跟着发布：")
        for p in problems:
            print(f"  ✗ {p}")
        print()
        print("怎么修：")
        print("  - config.json / 聊天记录：从源码树和 dist 里删掉。dist 那份是本机跑过 exe 留下的，")
        print("    重新打一次包就没有了（PyInstaller 不会复制它）。")
        print("  - 已进 git index：git rm --cached <路径>，再确认 .gitignore 里有对应规则。")
        return 1

    print("发布包体检通过：没有你自己的数据混进去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
