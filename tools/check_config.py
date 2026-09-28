# -*- coding: utf-8 -*-
"""配置持久化的离线回归检查：原子写、坏文件不静默、密钥加密存储。

这三件事都是**静默失效**型的问题——出事了不会报错，只会让用户看到「我的设置全没了」
或者「密钥明明填过却一直 401」，而两者都极难反查。所以每一条都得钉住：

1. **原子写**（settings._write_config）：临时文件 + fsync + os.replace。
   直接覆盖写会在中途崩溃/断电/磁盘满时留下半截 JSON，而坏 JSON 会让所有设置退回默认值。
   这里验证：正常写完内容对、不留 .tmp 残渣、旧 .tmp 会被顶掉、非 ASCII 不乱码。
2. **坏文件不被无声销毁**：保存前先把解析不了的旧文件备份成 config.json.bad-<时间戳>，
   且备份内容**原样**保留（用户可能手工修得回来）。
3. **坏文件要说出来**：getter 仍然退回默认值（这是刻意的 fail-safe 口径，不能改），
   但同时要记一条告警，且**只说一次**——_cfg() 一次生成要调十几次，不封口就是刷屏。
   首次运行（文件根本不存在）必须**不**报警告，否则新用户一上来就被吓一跳。
4. **密钥加密存储**：注册表里放 DPAPI 密文（dpapi: 前缀），进程环境里放明文（core/ 只认它）。
   验证：往返一致、密文里不含明文、无前缀的历史明文照读、密文解不开时返回空串
   （**绝不能把密文当密钥发出去**——那只会换回一个 401，比「没填密钥」更难查）。

跑法：python tools/check_config.py   （通过 exit 0，失败抛 AssertionError 并 exit 1）

**绝不碰用户真实配置与注册表**：settings._CONFIG 全程指向临时目录，
所有密钥字段一律传 None（save() 里 if key_text 为假就完全跳过 _set_key），
所以注册表 HKCU\\Environment 一个字节都不会动。
"""
from __future__ import annotations

import glob
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# stdout 强制 UTF-8：Windows 控制台跟着代码页走（中文系统 cp936、CI runner 是 cp1252），
# cp1252 编不出中文，脚本会在 print 那一行直接 UnicodeEncodeError。stderr 一起包上——
# 断言失败时 AssertionError 里的中文是从 stderr 出去的，漏了它真错反而被编码错误盖住。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from app import settings  # noqa: E402


def _fresh_dir() -> str:
    """一个干净的临时目录，并让 settings 只认它里面的 config.json。"""
    d = tempfile.mkdtemp(prefix="jev_cfg_")
    settings._CONFIG = os.path.join(d, "config.json")
    settings._config_noted.clear()
    settings._config_problems.clear()
    # 密钥也必须 stub：_get_key() 会读真实进程环境和注册表 HKCU\Environment，作者本机配过
    # OpenRouter key 的话，judge_engine() 之类的推断就跟着变，测试不再有确定行为。
    # check_auto_send 就是栽在这上面（只有干净的 CI runner 能过），别重犯。
    settings._get_key = lambda name: ""
    return d


def _corrupt(text: str = "{ 半截 JSON") -> None:
    with open(settings._CONFIG, "w", encoding="utf-8") as f:
        f.write(text)


def check_first_run_is_not_a_fault() -> None:
    """还没建过配置 = 正常首次运行，不能报警告，getter 走默认值。"""
    _fresh_dir()
    assert settings.retries() == 2, "没有配置文件时该退默认 2"
    assert settings.relationship() == settings._DEFAULT_RELATIONSHIP
    assert settings.take_config_problem() == [], \
        "首次运行（文件不存在）不该报「配置坏了」——那会把新用户吓一跳"
    print("首次运行不报警告 ok")


def check_atomic_write() -> None:
    """正常写：内容对、不留 .tmp、旧 .tmp 被顶掉、非 ASCII 不乱码。"""
    d = _fresh_dir()
    settings._write_config({"relationship": "恋人", "retries": 4, "my_name": "小金"})
    with open(settings._CONFIG, encoding="utf-8") as f:
        got = json.load(f)
    assert got == {"relationship": "恋人", "retries": 4, "my_name": "小金"}, got
    assert not glob.glob(os.path.join(d, "*.tmp")), "写完不该留下临时文件"

    # 上一次崩溃留下的 .tmp 不该让这次写入失败（os.replace 会顶掉它）
    with open(os.path.join(d, "config.json.tmp"), "w", encoding="utf-8") as f:
        f.write("上个进程留下的垃圾")
    settings._write_config({"relationship": "朋友"})
    assert json.load(open(settings._CONFIG, encoding="utf-8"))["relationship"] == "朋友"
    assert not glob.glob(os.path.join(d, "*.tmp")), "陈旧的 .tmp 该被顶掉，不该留着"
    print("原子写 ok（无 .tmp 残渣，非 ASCII 正常）")


def check_save_roundtrip() -> None:
    """走公开入口 settings.save()：落盘的是完整配置，且同样不留临时文件。

    密钥字段全部传 None —— save() 里 `if key_text:` 为假就跳过 _set_key，
    所以这一趟不会碰注册表。
    """
    d = _fresh_dir()
    settings.save(None, "同事", 7, None, "deepseek", auto_send_dm_on=True,
                  my_name_text="小金", candidate_count_n=1)
    with open(settings._CONFIG, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["relationship"] == "同事" and saved["context"] == 7, saved
    assert saved["draft_provider"] == "deepseek" and saved["auto_send_dm"] is True, saved
    assert saved["my_name"] == "小金" and saved["candidate_count"] == 1, saved
    assert not glob.glob(os.path.join(d, "*.tmp")), "save() 之后不该留下临时文件"
    # 再读一遍，确认落盘的和读出来的是一回事
    assert settings.relationship() == "同事" and settings.retries() == settings._DEFAULT_RETRIES
    print("settings.save() 原子写往返 ok")


def check_save_judge_engine_is_atomic() -> None:
    """标题栏那个一键开关的持久化路径也必须原子，且不能动别的键。

    这条路径是「读文件 → 换一个键 → 写回去」，写坏了会连关系、地址一起丢。
    """
    d = _fresh_dir()
    settings._write_config({"relationship": "朋友", "retries": 3, "draft_provider": "deepseek"})
    settings.save_judge_engine("none")
    got = json.load(open(settings._CONFIG, encoding="utf-8"))
    assert got["judge_engine"] == "none", got
    assert got["relationship"] == "朋友" and got["retries"] == 3, f"不该动别的键：{got}"
    assert got["draft_provider"] == "deepseek", f"不该动别的键：{got}"
    assert not glob.glob(os.path.join(d, "*.tmp")), "不该留下临时文件"
    settings.save_judge_engine("乱写的")  # 脏值一律忽略，宁可不动也别写坏
    assert json.load(open(settings._CONFIG, encoding="utf-8"))["judge_engine"] == "none"
    print("save_judge_engine() 原子写 + 只动一个键 ok")


def check_corrupt_is_backed_up_not_destroyed() -> None:
    """坏文件在下次保存前必须被备份，且内容原样保留。"""
    d = _fresh_dir()
    _corrupt("{ 半截 JSON，里面有用户唯一一份配置")
    settings._write_config({"relationship": "朋友"})
    baks = glob.glob(os.path.join(d, "config.json.bad-*"))
    assert len(baks) == 1, f"坏文件该被备份一次，实际 {baks}"
    with open(baks[0], encoding="utf-8") as f:
        assert f.read() == "{ 半截 JSON，里面有用户唯一一份配置", "备份必须原样保留，不能改写"
    # 能正常解析的文件不该被备份（否则每次保存都堆一个 .bad）
    settings._write_config({"relationship": "同事"})
    assert len(glob.glob(os.path.join(d, "config.json.bad-*"))) == 1, "好文件不该被备份"
    print("坏文件备份（原样保留）+ 好文件不备份 ok")


def check_corrupt_config_falls_back_to_defaults() -> None:
    """坏配置仍然退默认值（刻意口径），但必须报出来，且只报一次。"""
    _fresh_dir()
    _corrupt("{ 又坏了")
    # 一次生成里 _cfg() 会被调十几次，全都得退回默认、全都不能抛
    for _ in range(20):
        assert settings.retries() == 2
        assert settings.draft_provider() == "openrouter"
        assert settings.auto_send_dm() is False, "读不出配置时自动发送必须是关的（安全方向）"
    problems = settings.take_config_problem()
    assert len(problems) == 1, f"20 次读取该合并成 1 条告警，实际 {len(problems)} 条：{problems}"
    assert "config.json" in problems[0], problems[0]
    assert settings.take_config_problem() == [], "取过一次就该清空"
    print("坏配置退默认 + 只报一次 ok")


def check_key_encryption() -> None:
    """密钥：注册表存密文、往返一致、兼容历史明文、解不开时返回空串。"""
    plain = "sk-or-v1-abcdefghijklmnopqrstuvwxyz"

    # 历史遗留：升级上来的老用户，注册表里还是明文，必须照读（否则密钥凭空消失）
    assert settings._unprotect_key("sk-legacy-plaintext") == "sk-legacy-plaintext"
    assert settings._unprotect_key("") == ""
    # 空串加密仍是空串：没填过的 key 不该在注册表里留下一个空密文
    assert settings._protect_key("") == ""

    if settings._crypt(b"probe", decrypt=False) is None:
        # DPAPI 拿不到（非 Windows）。那就必须退回明文存储——存储方式出问题绝不能
        # 让用户填的密钥直接消失，那比明文存着严重得多。
        assert settings._protect_key(plain) == plain, "DPAPI 不可用时应退回明文，不能丢 key"
        assert settings._unprotect_key(plain) == plain
        print("密钥存储 ok（本机无 DPAPI，已验证退回明文且不丢 key）")
        return

    enc = settings._protect_key(plain)
    assert enc.startswith(settings._KEY_PREFIX), f"必须带 {settings._KEY_PREFIX} 前缀：{enc[:20]}"
    assert plain not in enc, "明文不该出现在密文里"
    assert settings._unprotect_key(enc) == plain, "往返不一致"

    # 密文坏掉（换过 Windows 账户 / 配置被搬到别的机器）：返回空串，**不能把密文当密钥发出去**
    settings._config_noted.clear()
    settings._config_problems.clear()
    assert settings._unprotect_key("dpapi:bm90LWEtcmVhbC1ibG9i") == "", \
        "解不开必须返回空串；把密文当密钥发出去只会换回一个更难查的 401"
    assert settings._unprotect_key("dpapi:!!!不是base64!!!") == ""
    problems = settings.take_config_problem()
    assert len(problems) == 1 and "重新填" in problems[0], f"解不开要说清怎么办：{problems}"

    # 加密中途不可用（API 调用失败）也要退回明文，不能丢 key
    real = settings._crypt
    settings._crypt = lambda data, decrypt: None
    try:
        assert settings._protect_key(plain) == plain, "DPAPI 调用失败时应退回明文存储"
    finally:
        settings._crypt = real
    print("密钥加密存储 ok（密文往返 / 明文兼容 / 解不开退空串 / 降级不丢 key）")


def main() -> None:
    check_first_run_is_not_a_fault()
    check_atomic_write()
    check_save_roundtrip()
    check_save_judge_engine_is_atomic()
    check_corrupt_is_backed_up_not_destroyed()
    check_corrupt_config_falls_back_to_defaults()
    check_key_encryption()
    print("配置持久化检查全部通过（原子写 / 坏文件备份与告警 / 密钥加密）")


if __name__ == "__main__":
    main()
