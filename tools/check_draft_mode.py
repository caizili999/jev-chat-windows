# -*- coding: utf-8 -*-
"""三档模式（完整 / 自判 / 起草）的离线回归检查。

守的是几个容易静默退化的约定：
1. `core.engine.analyze()` 按 judge_engine 分流，且**自判模式一律不给概率**（scores 全 None）；
2. **判断失败不丢候选**：起草已经成功，第二步出问题不该让用户什么都拿不到；
3. `app/overlay.py` 的 `_rank()`：未判断时不排序不标推荐；自判模式按模型给的 ranking 排，
   脏索引丢掉、漏排的补齐——**一条候选都不能丢**；
4. `app.settings.draft_problem()` / `judge_engine()` 的判定与降级：选了 OpenRouter 却没密钥
   退回自判，但**降级结果不能污染用户存下的选择**（否则补上密钥也回不去）。

为什么用 ast 抽函数体跑：`app/overlay.py` 模块级 import PySide6，没装 Qt 的机器上根本 import 不了，
只能把纯函数源码抠出来单独 exec。跑法（项目根在 cwd）：

    python tools/check_draft_mode.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import textwrap

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_pure(path: str, name: str):
    """从源码里抠出一个纯函数来跑（不 import 模块，绕开模块级的 PySide6 依赖）。"""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        raise AssertionError(f"{path} 里找不到函数 {name}（改名了？同步更新本脚本）")
    ns: dict = {}
    exec(textwrap.dedent(ast.get_source_segment(src, fn)), ns)
    return ns[name]


def check_rank() -> None:
    rank = _load_pure(os.path.join(_ROOT, "app", "overlay.py"), "_rank")

    # 完整模式：推荐位强制第一，其余按概率降序，同分按原索引
    assert rank(3, True, 1, [0.2, 0.5, 0.3]) == (1, [1, 2, 0])
    assert rank(3, True, 0, [0.1, 0.1, 0.1]) == (0, [0, 1, 2])
    assert rank(3, True, 2, [0.4, 0.3, 0.2]) == (2, [2, 0, 1])
    assert rank(3, True, 9, [0.4, 0.3, 0.2]) == (0, [0, 1, 2])      # 越界 best 退回 0
    assert rank(3, True, "b", [0.4, 0.3, 0.2]) == (0, [0, 1, 2])    # 非 int 的 best 也退回 0
    assert rank(0, True, 0, []) == (None, [])                       # 空候选不炸
    assert rank(3, True, 0, [None, None, None]) == (0, [0, 1, 2])
    assert rank(3, True, 1, []) == (1, [1, 0, 2])                   # scores 比 count 短
    assert rank(3, True, 0, [0.1, "脏", None]) == (0, [0, 1, 2])    # 脏数据当 0

    # 起草模式：不排序、不标推荐——哪怕传了「最高分」那条也保持生成顺序
    assert rank(3, False, None, [None, None, None]) == (None, [0, 1, 2])
    assert rank(3, False, 0, [0.9, 0.1, 0.0]) == (None, [0, 1, 2])
    assert rank(1, False, 0, [None]) == (None, [0])
    assert rank(0, False, None, []) == (None, [])

    # 自判模式：模型给的是**完整排序**，照它排（没有概率，所以界面不会显示百分比）
    assert rank(3, True, 2, [None, None, None], [2, 0, 1]) == (2, [2, 0, 1])
    assert rank(3, True, 0, [None] * 3, [1, 2]) == (1, [1, 2, 0])      # 漏排的按生成顺序补末尾
    assert rank(3, True, 0, [None] * 3, [9, 1]) == (1, [1, 0, 2])      # 越界索引丢掉
    assert rank(3, True, 0, [None] * 3, [True, 1]) == (1, [1, 0, 2])   # 布尔不算 int
    assert rank(3, True, 0, [None] * 3, ["1"]) == (0, [0, 1, 2])       # 非整数丢掉
    assert rank(3, True, 0, [None] * 3, [2, 2, 0]) == (2, [2, 0, 1])   # 重复编号只算一次
    assert rank(3, True, 0, [None] * 3, []) == (0, [0, 1, 2])          # 空 ranking 退回普通路径
    assert rank(0, True, None, [], []) == (None, [])
    # 无论 ranking 多脏，候选**一条都不能少**
    for dirty in ([9], ["x"], [True], [], [2, 2], None):
        _, order = rank(3, True, 0, [None] * 3, dirty)
        assert sorted(order) == [0, 1, 2], f"ranking={dirty!r} 时丢了候选：{order}"
    print("overlay._rank ok")


def check_engine() -> None:
    import core.engine as engine

    engine.draft_candidates = lambda *a, **k: ["甲", "乙", "丙"]
    called = []
    engine.ask = lambda *a, **k: called.append("openrouter")

    # ── 起草模式（不判断）────────────────────────────────────────────────
    r = engine.analyze([("her", "在吗")], "romantic partners", judge=False)
    assert r["candidates"] == ["甲", "乙", "丙"]
    assert r["judged"] is False and r["best_index"] is None and r["best_reply"] is None
    assert r["scores"] == [None, None, None], r["scores"]
    assert r["answers"] == {} and r["ranking"] is None
    assert r["judge_engine"] == "none" and r["judge_error"] is None
    assert not called, "judge=False 时不该调判断接口"

    r = engine.analyze([("her", "在吗")], "romantic partners", judge_engine="none")
    assert r["judged"] is False and r["judge_engine"] == "none"

    # 引擎值不认识 → 归一到 none；judge=False 必须压过 judge_engine
    r = engine.analyze([("her", "在吗")], "romantic partners", judge_engine="乱写的")
    assert r["judged"] is False and r["judge_engine"] == "none", "认不出的引擎值该退成 none"
    r = engine.analyze([("her", "在吗")], "romantic partners", judge=False, judge_engine="self")
    assert r["judged"] is False and r["judge_engine"] == "none", "judge=False 该压过 judge_engine"

    # ── 自判模式：用起草那套模型判断，有摘要和排序、**一律不给概率** ────────
    seen = {}

    def fake_self_judge(state, questions, candidates, **kw):
        seen.update(kw)
        seen["candidates"] = candidates
        return {"answers": {"true_intent": {"type": "choice", "choice": "casual_chat"}},
                "ranking": [2, 0, 1]}

    engine.self_judge = fake_self_judge
    r = engine.analyze([("her", "在吗")], "romantic partners", provider="custom",
                       draft_base_url="https://api.example.com/v1",
                       judge_engine="self", judge_key="sk-j")
    assert r["judged"] is True and r["judge_engine"] == "self" and r["judge_error"] is None
    assert r["ranking"] == [2, 0, 1]
    assert r["best_index"] == 2 and r["best_reply"] == "丙", "自判的推荐位该取 ranking 第一条"
    assert r["scores"] == [None, None, None], f"自判模式绝不能给概率：{r['scores']}"
    assert r["answers"]["true_intent"]["choice"] == "casual_chat"
    assert seen["key"] == "sk-j" and seen["provider"] == "custom", seen
    assert seen["candidates"] == ["甲", "乙", "丙"], "判断要拿到全部候选"
    assert not called, "自判模式不该走 OpenRouter 的 decisions 协议"

    # ── 判断失败**不丢候选**：起草已经花钱拿到 3 条，第二步出问题不该全扔 ────
    def boom(*a, **k):
        raise engine.JevError("判断服务返回 502", status=502, hint="对端暂时不可用", retries=2)

    engine.self_judge = boom
    r = engine.analyze([("her", "在吗")], "romantic partners", judge_engine="self")
    assert r["candidates"] == ["甲", "乙", "丙"], "判断失败把候选丢了"
    assert r["judged"] is False and r["judge_engine"] == "self"
    assert r["scores"] == [None, None, None] and r["best_index"] is None
    err = r["judge_error"] or {}
    assert err.get("status") == 502 and err.get("retries") == 2, err
    assert "不可用" in err.get("hint", "") and "502" in err.get("message", ""), err

    # 起草一条都没出（出口过滤全清了）：判断那步压根不该跑
    engine.draft_candidates = lambda *a, **k: []
    for eng in ("openrouter", "self", "none"):
        r = engine.analyze([("her", "在吗")], "romantic partners", judge_engine=eng)
        assert r["candidates"] == [] and r["scores"] == [] and r["judged"] is False
        assert r["judge_engine"] == eng, f"{eng}: 空候选时引擎值该照实带出去"

    # candidate_count 必须真透传到起草——这是「候选条数」这个提速开关唯一有意义的证明。
    # 透传断了的话设置页能调、号也存下来，但发起请求还是会要 3 条（改了等于没改）。
    got = {}

    def fake_draft(*a, **k):
        got.update(k)
        return ["甲", "乙", "丙"]

    engine.draft_candidates = fake_draft
    engine.analyze([("her", "在吗")], "romantic partners", judge=False, candidate_count=1)
    assert got.get("count") == 1, f"candidate_count 该透传成 count=1，实际 {got.get('count')!r}（说明没接上）"
    engine.analyze([("her", "在吗")], "romantic partners", judge=False)
    assert got.get("count") == 3, f"不传时该默认 3，实际 {got.get('count')!r}"
    print("engine.analyze() 三档 ok")


def check_settings() -> None:
    from app import settings

    settings._CONFIG = os.path.join(tempfile.mkdtemp(), "config.json")
    keys = {"OPENROUTER_API_KEY": "", "DEEPSEEK_API_KEY": "", "CUSTOM_API_KEY": "",
            "JUDGE_API_KEY": ""}
    settings._get_key = lambda name: keys.get(name, "")

    def cfg(**kw):
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(kw, f)

    cfg(draft_provider="openrouter")
    assert settings.draft_problem() == "起草选了 OpenRouter 但没填 OpenRouter 密钥"
    assert settings.draft_ready() is False
    keys["OPENROUTER_API_KEY"] = "sk-or"
    assert settings.draft_ready() is True

    cfg(draft_provider="deepseek")
    keys["OPENROUTER_API_KEY"] = ""
    assert "DeepSeek 密钥" in settings.draft_problem()
    keys["DEEPSEEK_API_KEY"] = "sk-ds"
    assert settings.draft_ready() is True

    cfg(draft_provider="custom", draft_base_url="")
    keys["DEEPSEEK_API_KEY"] = ""
    assert "基础地址不可用" in settings.draft_problem()
    cfg(draft_provider="custom", draft_base_url="https://api.example.com/v1")
    assert settings.draft_problem() == "选了自定义地址但没填自定义密钥"
    keys["CUSTOM_API_KEY"] = "sk-c"
    # 判断那半缺 OpenRouter 密钥不算问题：自定义齐了就是 ready
    assert settings.draft_ready() is True and settings.has_key() is False

    cfg(draft_provider="乱写的")  # 脏值退回 openrouter，别把起草打死
    assert settings.draft_provider() == "openrouter"

    with open(settings._CONFIG, "w", encoding="utf-8") as f:
        f.write("{ 不是 json")  # config.json 坏掉也要降级，不抛
    assert settings.draft_provider() == "openrouter" and settings.draft_base_url() == ""
    print("settings.draft_problem() 三档 ok")


def check_judge_engine() -> None:
    from app import settings

    settings._CONFIG = os.path.join(tempfile.mkdtemp(), "config.json")
    keys = {"OPENROUTER_API_KEY": "", "DEEPSEEK_API_KEY": "", "CUSTOM_API_KEY": "",
            "JUDGE_API_KEY": ""}
    settings._get_key = lambda name: keys.get(name, "")

    def cfg(**kw):
        with open(settings._CONFIG, "w", encoding="utf-8") as f:
            json.dump(kw, f)

    def keys_reset(**kw):
        for k in keys:
            keys[k] = ""
        keys.update(kw)

    # 显式选了不判断：照用户说的来，不降级、也不报问题
    keys_reset()
    cfg(draft_provider="openrouter", judge_engine="none")
    assert settings.stored_judge_engine() == "none"
    assert settings.judge_engine() == "none" and settings.judge_problem() == ""

    # 选了 OpenRouter 但没密钥 → 运行时降到自判，但**存值仍是用户选的 openrouter**
    keys_reset(DEEPSEEK_API_KEY="sk-ds")
    cfg(draft_provider="deepseek", judge_engine="openrouter")
    assert settings.stored_judge_engine() == "openrouter", "存值不该被降级改写"
    assert settings.judge_engine() == "self", "选了 openrouter 没密钥要退到自判"
    assert "退回用起草模型判断" in settings.judge_problem()

    # 关键：保存一次之后 config.json 里还得是 openrouter，否则补上密钥也回不去完整模式
    settings.save(None, "friends")
    assert settings.stored_judge_engine() == "openrouter", "save() 把用户选的 openrouter 改写成了降级结果"
    assert settings.draft_provider() == "deepseek", "save() 不该顺手把起草来源也改了"
    assert settings.judge_engine() == "self"

    # 有 OpenRouter 密钥 = 完整模式
    keys_reset(OPENROUTER_API_KEY="sk-or")
    cfg(draft_provider="openrouter", judge_engine="openrouter")
    assert settings.judge_engine() == "openrouter" and settings.judge_problem() == ""

    # 选了自判但起草压根没配好 → 退成 none，不能靠一个跑不起来的起草服务做判断
    keys_reset()
    cfg(draft_provider="openrouter", judge_engine="self")
    assert settings.judge_engine() == "none" and "判断先跳过" in settings.judge_problem()

    keys_reset(DEEPSEEK_API_KEY="sk-ds")
    cfg(draft_provider="deepseek", judge_engine="self")
    assert settings.judge_engine() == "self" and settings.judge_problem() == ""

    # 老用户升级上来（config.json 里没有 judge_engine）：按有没有 OpenRouter 密钥推断，
    # 保存一次之后就固定成显式值，不再随密钥变化漂移
    keys_reset(DEEPSEEK_API_KEY="sk-ds")
    cfg(draft_provider="deepseek")
    assert settings.stored_judge_engine() == ""
    assert settings.judge_engine() == "self", "没 OpenRouter 密钥时推断为自判"
    settings.save(None, "friends")
    assert settings.stored_judge_engine() == "self", "首次保存要把推断结果固定下来"

    keys_reset(OPENROUTER_API_KEY="sk-or")
    cfg(draft_provider="openrouter")
    assert settings.stored_judge_engine() == "" and settings.judge_engine() == "openrouter"

    # 脏值当没存过（stored 返回空串），别把引擎打死
    keys_reset(DEEPSEEK_API_KEY="sk-ds")
    cfg(draft_provider="deepseek", judge_engine="乱写的")
    assert settings.stored_judge_engine() == "" and settings.judge_engine() == "self"

    # 判断专用密钥跟起草那把分开；不填就是空（= 用起草那家）
    assert settings.judge_key() == "" and settings.has_judge_key() is False
    keys["JUDGE_API_KEY"] = "sk-j"
    assert settings.judge_key() == "sk-j" and settings.has_judge_key() is True
    print("settings.judge_engine() 三档 ok")


def main() -> int:
    check_rank()
    check_engine()
    check_settings()
    check_judge_engine()
    print("三档模式检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
