# -*- coding: utf-8 -*-
"""把版本号写进 app/version.py 的那一行。

CI 打包前用 tag 名调它（见 .github/workflows/release.yml），本地想打一个带版本号的包也能手动跑。

跑法：
    python tools/set_version.py 1.2.3        # 写入
    python tools/set_version.py v1.2.3       # 开头的 v 自动去掉
    python tools/set_version.py --show       # 只看当前值，不写
    python tools/set_version.py --check 1.2.3  # 只断言，不符就 exit 1（CI 用这个当门禁）

只动 `VERSION = "..."` 那一行，文件里其他内容（包括注释）原样保留。
版本号必须是 `数字.数字...` 或 `数字.数字-后缀` 这种形态：`app/update.py` 的
`parse_version()` 只认纯数字段，认不出的版本号不会弹更新提示——所以 `0.0.0-dev`
（源码直跑）永远不会去查 GitHub，这是有意的。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "app" / "version.py"
LINE_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']*)["\']\s*$', re.M)


def read_version() -> str:
    m = LINE_RE.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"ERROR: {VERSION_FILE} 里找不到 VERSION = \"...\" 那一行")
    return m.group(1)


def write_version(new: str) -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    if not LINE_RE.search(text):
        raise SystemExit(f"ERROR: {VERSION_FILE} 里找不到 VERSION = \"...\" 那一行")
    # 用函数替换，避免 new 里的反斜杠被当成转义（re.sub 的 repl 会解释 \g 之类）
    updated = LINE_RE.sub(lambda _m: f'VERSION = "{new}"', text, count=1)
    if updated != text:
        VERSION_FILE.write_text(updated, encoding="utf-8")
    return new


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="设置 / 查看 / 校验 app/version.py 里的版本号")
    ap.add_argument("version", nargs="?", help="要写入的版本号；开头的 v 会被去掉")
    ap.add_argument("--show", action="store_true", help="只打印当前值")
    ap.add_argument("--check", metavar="X.Y.Z", help="断言当前值等于它，不符 exit 1")
    args = ap.parse_args(argv)

    if args.check:
        got = read_version()
        if got != args.check:
            print(f"版本号不符：app/version.py 里是 {got!r}，期望 {args.check!r}")
            return 1
        print(f"版本号正确：{got}")
        return 0

    if args.show or not args.version:
        print(read_version())
        return 0

    ver = args.version[1:] if args.version.startswith("v") else args.version
    if not ver:
        print("ERROR: 版本号是空的（只有一个 v？）")
        return 1
    old = read_version()
    write_version(ver)
    print(f"版本号：{old} -> {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
