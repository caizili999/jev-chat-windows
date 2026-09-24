# -*- coding: utf-8 -*-
"""整条链的唯一入口：对话 → 起草 3 条 → 判断+排序 → 结构化结果。

平台无关。SSE 消费者、悬浮窗、命令行 demo 都只调 analyze()。

判断分三档（界面上的「完整模式 / 自判模式 / 起草模式」）：
- judge_engine="openrouter"：OpenRouter 的 decisions 协议，**有校准过的概率**，卡片显示百分比；
- judge_engine="self"：用任意 OpenAI 兼容模型（默认就是起草那家）做判断，**只给排序不给概率**；
- judge_engine="none"（或 judge=False）：不判断，只要候选。
详见 core/judge.py 开头为什么自判模式不给概率。
"""
from __future__ import annotations

try:
    from .draft import draft_candidates
    from .jev_client import DEFAULT_RETRIES, JevError, ask
    from .judge import judge as self_judge
    from .questions import JUDGE_QUESTIONS, build_rank_question, build_state
except ImportError:
    from draft import draft_candidates
    from jev_client import DEFAULT_RETRIES, JevError, ask
    from judge import judge as self_judge
    from questions import JUDGE_QUESTIONS, build_rank_question, build_state

_REPLY_IDX = {"reply_a": 0, "reply_b": 1, "reply_c": 2}

# 判断那一步最多看最近几条对话。起草要模仿口吻、要接得住上下文，看 10 条有用；
# 判断只需要判断「对方这句是什么意思」，再往前的旧消息对结论几乎没贡献，白白多花输入 token、
# 多等一次往返。用户设的「参考上下文」比这短就听用户的。
_JUDGE_CONTEXT_CAP = 6


def _phase(on_phase, name: str) -> None:
    """报一下现在在哪一步，给状态栏做分阶段进度用。

    回调是调用方给的，它出问题（写队列失败之类）不该影响生成本身——这里吞掉异常，
    因为「进度没显示出来」远比「生成失败」轻。
    """
    if on_phase is None:
        return
    try:
        on_phase(name)
    except Exception:
        pass


def _no_judge(candidates: list, reply_to, engine: str) -> dict:
    """不判断时的结果（起草模式）。best_index=None、scores 全 None，**不编排序**——
    编出来的「推荐」没有依据，是假信号，比不标更糟。judge_engine 照实带出去，界面按它说文案。"""
    return {
        "candidates": candidates,
        "best_index": None,
        "best_reply": None,
        "scores": [None] * len(candidates),
        "answers": {},
        "ranking": None,
        "usage": {},
        "reply_to": reply_to,
        "judged": False,
        "judge_engine": engine,
        "judge_error": None,
    }


def _judge_via_openrouter(messages, relationship, context, candidates, reply_to, timeout,
                          judge_base_url, judge_model, retries) -> dict:
    """完整模式：走 OpenRouter 的 decisions 协议。返回 {answers, ranking, scores, best_index}。"""
    questions = dict(JUDGE_QUESTIONS)
    if len(candidates) >= 2:  # 起草只给了 1 条就没什么可排的，判断题照问
        questions.update(build_rank_question(candidates))
    result = ask(build_state(messages, relationship, keep=context, reply_to=reply_to),
                 questions, timeout=timeout, base_url=judge_base_url, model=judge_model,
                 retries=retries)

    answers = result.get("answers") or {}
    best_key = (answers.get("best_reply") or {}).get("choice")
    best_index = _REPLY_IDX.get(best_key, 0)  # 解析不出就退第一条
    if best_index >= len(candidates):
        best_index = 0

    probabilities = (answers.get("best_reply") or {}).get("probabilities") or {}
    scores = [0.0] * len(candidates)
    for key, idx in _REPLY_IDX.items():
        if idx >= len(candidates):
            continue
        try:
            scores[idx] = float(probabilities.get(key, 0.0))
        except (TypeError, ValueError):
            scores[idx] = 0.0  # 脏数据一律按 0 处理
    return {"answers": answers, "ranking": None, "scores": scores,
            "best_index": best_index, "usage": result.get("usage") or {}}


def analyze(messages: list, relationship: str, model: str | None = None,
            timeout: float = 30, context: int = 10, provider: str = "openrouter",
            reply_to: str | None = None, style: str = "", thinking: bool = False,
            draft_base_url: str = "", judge_base_url: str = "",
            judge_model: str = "", judge: bool = True,
            retries: int = DEFAULT_RETRIES, judge_engine: str = "openrouter",
            judge_key: str = "", judge_timeout: float | None = None,
            on_phase=None, candidate_count: int = 3) -> dict:
    """messages: [(from, text)] from ∈ {her, me}，最新一条在最后；
    群聊里可以带第三项 name（说这句话的人），单聊不带。
    context: 起草和判断各看最近多少条消息（用户设置里的「参考上下文」）。
    provider: 起草走哪家（openrouter / deepseek 直连 / custom 自定义 OpenAI 兼容地址）。
    reply_to: 群聊里指定回复给谁；None = 正常回复。
    style: 用户自己描述的说话风格，只影响起草。
    thinking: 起草时是否开思考模式，只影响起草，默认关。
    model: 起草用的模型名覆盖；None = 该来源的默认模型（判断那个用 judge_model，两个是分开的）。
    draft_base_url: 只在 provider="custom" 时用；空/非法值退回 OpenRouter 默认。
    judge_base_url / judge_model: 判断的地址和模型；留空 = 跟起草那套完全相同
    （self 引擎）或官方端点（openrouter 引擎）。
    judge_key: 判断专用密钥；空 = 用起草那家的。
    retries: 失败后最多再试几次（设置页可配，0 = 不重试）。起草和判断共用这一个值——
    两个环节都是「对端偶发不可用」，没有分开配的理由。重试条件见 jev_client.post_json。
    judge_engine: "openrouter" / "self" / "none"。judge=False 等价于 "none"
    （调用方按有没有密钥、用户在设置里选了什么来传）。
    judge_timeout: 判断那一次的超时；None = 跟起草同一个 timeout。判断比起草轻得多，
    分开配能把「对端抽风时最坏等多久」收住一截；超时现在不重试，到了上限就直接报出来。
    on_phase: 进度回调，生成过程中会收到 "draft" / "judge"，给界面做「正在起草…/正在判断…」用。
    回调抛异常会被吞掉——进度没显示出来，不该让生成失败。
    candidate_count: 起草要几条候选，1~3，默认 3。只要 1 条时模型输出短、不用在多个版本间权衡，
    生成时间会短一截——这是「我想更快」最直接的一个口子。脏值当 3（见 draft_candidates）。
    **判断失败不会连候选一起丢掉**：起草已经花钱拿到了 3 条能用的回复，因为第二步出问题就全扔，
    用户什么都得不到。这时如实返回 judged=False + judge_error（带状态码和提示），候选照常给。

    返回 {candidates, best_index, best_reply, scores, answers, ranking, usage, reply_to,
          judged, judge_engine, judge_error}。
    scores 是每条候选的胜出概率（0~1，取自 best_reply.probabilities）；**自判模式和起草模式
    一律 None**，界面因此不显示百分比。ranking 是候选的展示顺序（原始索引列表），只有自判模式给。
    只有对方最新说话时才有意义调它——是不是该触发由调用方判断（看 latest_from）。
    """
    _phase(on_phase, "draft")
    candidates = draft_candidates(messages, relationship, provider=provider,
                                  model=model, timeout=timeout, keep=context, reply_to=reply_to,
                                  style=style, thinking=thinking, base_url=draft_base_url,
                                  retries=retries, count=candidate_count)

    engine = judge_engine if judge else "none"
    if engine not in ("openrouter", "self"):
        engine = "none"
    if not candidates:
        return _no_judge(candidates, reply_to, engine)

    j_timeout = timeout if judge_timeout is None else judge_timeout
    judge_keep = min(context, _JUDGE_CONTEXT_CAP)  # 判断不需要看那么远，见 _JUDGE_CONTEXT_CAP
    if engine != "none":
        _phase(on_phase, "judge")  # 起草成功之后、判断之前——两档判断都从这儿开始
    try:
        if engine == "self":
            got = self_judge(build_state(messages, relationship, keep=judge_keep, reply_to=reply_to),
                             JUDGE_QUESTIONS, candidates,
                             provider=provider, draft_base_url=draft_base_url,
                             draft_model=model or "", base_url=judge_base_url,
                             model=judge_model, key=judge_key, timeout=j_timeout, retries=retries)
            got["scores"] = [None] * len(candidates)  # 自判不给概率：模型自评的百分比是编的
            got["best_index"] = got["ranking"][0]
            got["usage"] = {}
        elif engine == "openrouter":
            got = _judge_via_openrouter(messages, relationship, judge_keep, candidates, reply_to,
                                        j_timeout, judge_base_url, judge_model, retries)
        else:
            return _no_judge(candidates, reply_to, engine)
    except JevError as e:
        # 判断失败，但候选保住了。judge_error 三件套跟起草失败那条路一个形状，
        # 状态栏可以复用同一套 error_status 文案。
        return {**_no_judge(candidates, reply_to, engine),
                "judge_error": {"message": str(e), "status": e.status,
                                "hint": e.hint, "retries": e.retries}}

    return {
        "candidates": candidates,
        "best_index": got["best_index"],
        # candidates 可能为空（起草被出口过滤全清了）：别让这里 IndexError
        "best_reply": candidates[got["best_index"]] if candidates else None,
        "scores": got["scores"],
        "answers": got["answers"],
        "ranking": got["ranking"],
        "usage": got["usage"],
        "reply_to": reply_to,
        "judged": True,
        "judge_engine": engine,
        "judge_error": None,
    }
