"""Fixed Jev question set. Instructions/criteria in English; chat text stays Chinese."""

from __future__ import annotations

# 追加到每道判断题 instructions 末尾的一句话。作用是：当 state 里带了 background
# （关系 / 联系人备注 / 命中的知识库笔记）时，告诉模型那些是**给定的上下文**，
# 而不是该被扣分的跑题内容。
#
# ⚠️ 跟上游的一处刻意差异：上游**无条件**给每道题都加上这句（JevQuestions.judge() 在构造
# 时就拼进去）。这里只在**真的带了 background 时**才加——因为本项目要保证「没有知识库的用户，
# 请求体与加这个功能之前逐字节一致」。上游那句话本身也是为 background 服务的，条件化之后
# 意图不变，而且不会去动已经过校准的那套题干措辞。
BACKGROUND_NOTE = " Facts given in background are provided context, not off-topic."


def judge_questions(with_background: bool = False) -> dict:
    """判断题集合的一份副本。

    with_background=True 时给每道题的 instructions 追加 BACKGROUND_NOTE。
    返回副本，不原地改 JUDGE_QUESTIONS——那个模块级 dict 是校准过的原文，谁都别动它。
    """
    if not with_background:
        return dict(JUDGE_QUESTIONS)
    return {name: {**spec, "instructions": str(spec.get("instructions") or "") + BACKGROUND_NOTE}
            for name, spec in JUDGE_QUESTIONS.items()}


def knowledge_parts(knowledge) -> tuple:
    """知识库注入的入参 → (background, history)。

    knowledge 是**普通 dict**，形状：
        {"background": "关系：恋人\\n…", "history": [{"from": "her", "text": "…"}, …]}
    None / 空 / 脏值一律当成「没有知识库」——绝不能因为调用方传错东西就让整条链失败。

    为什么 core/ 收 dict 而不是收 app.kb 的对象：依赖方向是 app → core，core 不能反向 import app。
    调用方（main.py）负责把 ChatContext 摊平成这个 dict，边界因此是显式的。
    """
    if not isinstance(knowledge, dict):
        return "", []
    background = str(knowledge.get("background") or "").strip()
    raw = knowledge.get("history")
    history = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            msg = _history_msg(item)
            if msg is not None:
                history.append(msg)
    return background, history


def _history_msg(item):
    """一条历史 → {"from":…, "text":…}；认不出的返回 None（丢掉，不猜）。"""
    if isinstance(item, dict):
        who, text = item.get("from"), item.get("text")
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        who, text = item[0], item[1]
    else:
        return None
    if text is None:
        return None
    # 上游那边历史里的 side 是 "me"/"other"；本项目全程用 "me"/"her"。
    # 非 "me" 一律算对方——跟上游 `if (side == "me") 我 else 对方` 同一个口径，
    # 顺带保证注进去的历史一定过得了 build_state 的校验。
    return {"from": "me" if who == "me" else "her", "text": str(text)}


JUDGE_QUESTIONS: dict = {
    "literal_question": {
        "type": "noul",
        "instructions": (
            "Is the other person's latest message meant purely literally, with no subtext? "
            "Judge from the whole thread, not one sentence in isolation."
        ),
        "criteria": {
            "true": (
                "The latest message is a straightforward statement, question, or plan "
                "with no implied accusation, test, sarcasm, hint, or unsaid request."
            ),
            "false": (
                "There is subtext: a test of whether you remember or care, sarcasm, "
                "an implied complaint, a hint they will not say outright, a trap question, "
                "an accusation dressed as a question, or a cold/short line that really means blame."
            ),
        },
    },
    "true_intent": {
        "type": "choice",
        "instructions": (
            "What is the other person's true intent in the latest message, given the full conversation? "
            "Prefer tone and context over surface wording. "
            "If they are checking whether you remember something or still care, choose confirm_you_care "
            "even if the words look like a request to 'say it' or to do something. "
            "If they already accepted and closed the matter peacefully, choose close_topic. "
            "Ending the relationship, deleting you, or 'don't talk to me' is vent_anger, never close_topic."
        ),
        "criteria": {
            "confirm_you_care": (
                "They are testing whether you remember, pay attention, or still care. "
                "Signals: 'did you forget again', 'then say it', 'you better', sarcastic 'busy person', "
                "asking you to prove you know a past conversation. "
                "If they mainly want a new deliverable or a yes on a time, do not use this."
            ),
            "vent_anger": (
                "They are angry or hurt and mainly want the feeling acknowledged. "
                "They are blaming or raising the temperature; a specific plan is not the main point yet."
            ),
            "request_action": (
                "They want a concrete action, time, deliverable, or commitment from you now, "
                "and this is a real ask, not a loyalty test."
            ),
            "seek_explanation": (
                "They want a factual explanation of why something happened. "
                "They asked why or what is going on, not mainly for an apology or a new plan."
            ),
            "casual_chat": (
                "Light talk, banter, sharing, teasing with a laugh, or friendly logistics "
                "with no emotional test and no conflict. A friend suggesting a meal time can be this "
                "if the thread is warm."
            ),
            "close_topic": (
                "Peaceful wrap-up only: they accepted an apology, confirmed a happy plan, said thanks, "
                "or clearly signaled they need nothing more. "
                "Not a breakup, not 'don't contact me', not sarcastic 'I'm used to it'."
            ),
        },
    },
    "danger_level": {
        "type": "score",
        "instructions": (
            "How close is this conversation to a fight or to hurting the relationship? "
            "Match the current scene. "
            "If they genuinely accepted an apology or confirmed a happy plan, score the cooled-down present, "
            "not an earlier complaint. "
            "If an ultimatum (break up, report to the boss, stop covering for you) is still in force "
            "and has not been withdrawn, stay in that high bin even if the latest line names a specific task."
        ),
        "criteria": [
            "Light chat or joking; no complaint, no test, no deadline.",
            "Mild tease or a small reminder that is easy to laugh off; a clumsy reply would only feel slightly awkward.",
            "A mild complaint or 'please remember next time' said without heat; they still send warm or practical follow-ups.",
            "Noticeable unhappiness; they mention being forgotten, ignored, or kept waiting, but still give you a chance to make it right.",
            "Sarcasm, cold short replies, or 'you better'; they are testing you, and a sloppy or fake-confident reply will escalate.",
            "Openly upset; they accuse you of not listening or not caring; they expect a real response, not a joke.",
            "Clearly angry and blaming you; a wrong reply will turn this into a fight.",
            "Last-chance warning. They will not cover for you, do not want to keep talking unless this changes, "
            "or tell you to finish a named checklist yourself because trust is almost gone.",
            "An ultimatum is already on the table even if they also give a practical next step: "
            "break up if you forget again, report you tonight, or stop working together if you miss this.",
            "Active rupture: they said it is over, told you not to reply, deleted you, or are exploding.",
        ],
    },
    "should_reply_now": {
        "type": "noul",
        "instructions": (
            "Should your next message contain substantive content? "
            "Substantive means: admitting a specific known fault, giving a concrete time/plan/deliverable, "
            "explaining facts you actually know, or reciting the recalled content they asked you to say. "
            "This is NOT 'should you send any message'. Timing is irrelevant. "
            "Answer FALSE if the thing they want you to recite or prove is not present in this snippet "
            "(you would be guessing). 'Then say it' / 'you better' while you are stalling is FALSE. "
            "Answer FALSE if they already accepted and closed the topic. "
            "Answer true only if the needed fact, plan, or named fault is already in this snippet."
        ),
        "criteria": {
            "true": (
                "The needed fact, named fault, or named time/place is already in this snippet, "
                "and they are waiting for that substance now."
            ),
            "false": (
                "Do not put substance in the next message: the recalled content is not in this snippet, "
                "they are testing whether you remember, a holding line is enough, "
                "saying less is safer, or they already closed the topic."
            ),
        },
    },
    "best_action": {
        "type": "choice",
        "instructions": (
            "What type of next action is best? Do not decide whether to send a message immediately. "
            "Ignore timing. Choose only the action type. "
            "If they asked you to recall a specific past message or event and you have not shown that you actually remember it, "
            "choose check_history — do not apologize or invent a plan instead."
        ),
        "criteria": {
            "check_history": (
                "Look up prior chat or facts before taking a position. "
                "Use when they ask you to repeat, recall, or prove you remember something specific."
            ),
            "apologize": (
                "Lead with a sincere apology for a real mistake or hurt already identified. "
                "Not for an unnamed forgotten thing when you should first find out what it was."
            ),
            "give_commitment": (
                "Give a concrete promise, deadline, or arrangement they asked for "
                "in a conflict or work-pressure setting."
            ),
            "explain": (
                "Explain what happened or why, without leading with apology or a new plan."
            ),
            "acknowledge": (
                "Show you heard them and care, without new facts, an apology, or a plan. "
                "Use for light chat or when they mainly need to feel seen."
            ),
            "say_less": (
                "Keep it short or add nothing. Extra words would over-explain, reopen a closed topic, "
                "or pour fuel on an ultimatum that told you not to talk."
            ),
            "make_plan": (
                "Propose or confirm logistics (time, place, task) for a non-conflict request "
                "such as a meal or a meeting."
            ),
        },
    },
    "she_needs": {
        "type": "choice",
        "instructions": (
            "What does the other person need from you right now? Judge the LATEST message first. "
            "If they genuinely accepted (thanks / got it / 没事了 / 那就这样 / 收到了 / 过去了), "
            "you MUST choose nothing, even if earlier they wanted action or an apology. "
            "Sarcastic 'I'm used to it', 'whatever', 'I don't want to hear it', 'don't bother coming' "
            "is NOT genuine satisfaction — do not choose nothing. "
            "If they asked you to recap a named time/place/date, choose action. "
            "If they are testing whether you remember or still care, and the content is unnamed, choose care."
        ),
        "criteria": {
            "apology": (
                "They need a sincere apology for hurt or a mistake, and they have not accepted one yet."
            ),
            "action": (
                "They need a concrete action, time, commitment, recap of a named fact, or follow-through, "
                "and they have not yet accepted one."
            ),
            "explanation": (
                "They need a clear explanation of what happened or why, and have not received it."
            ),
            "care": (
                "They need proof you remember, listen, or care — a loyalty or attention test — "
                "not yet a plan or an apology. Sarcastic 'I am used to it' belongs here, not nothing."
            ),
            "nothing": (
                "They need nothing further. Genuine acceptance, a peaceful closed topic, "
                "warm casual chat with no ask, or a rupture where they told you not to reply. "
                "Not sarcasm pretending to be fine."
            ),
        },
    },
    "tension_resolved": {
        "type": "noul",
        "instructions": (
            "Has interpersonal tension already been resolved? "
            "Answer true only if there was never tension, or the other person has clearly accepted, "
            "cooled down, joked again, or said it is fine. "
            "A sarcastic 'you better', an unanswered test, leftover blame, or an open ultimatum means false."
        ),
        "criteria": {
            "true": (
                "No remaining tension: they accepted, joked again, said it's fine, "
                "confirmed a happy plan, or the chat was never tense."
            ),
            "false": (
                "Tension is still present: they are waiting, testing, angry, sarcastic, "
                "issuing an ultimatum, or the issue is open."
            ),
        },
    },
}


def build_state(messages: list, relationship: str, keep: int = 10,
                reply_to: str | None = None, background: str = "",
                history: list | None = None) -> dict:
    """messages: (from, text) / (from, text, name) / dict（name 可选）。from 只认 her/me。

    name = 群里的发言人；有 name 就当群聊（chat.is_group）。reply_to = 群里指定的回复对象。

    background / history 是知识库那两个可选字段（见 knowledge_parts）：
    **两者都为空时一个字段都不加**，state 跟加这个功能之前逐字节一致——这正是上游的做法，
    也是「没建知识库的用户不受影响」的保证。
    """
    cleaned = []
    for item in messages:
        if isinstance(item, dict):
            who, text, name = item.get("from"), item.get("text"), item.get("name")
        else:
            who, text = item[0], item[1]
            name = item[2] if len(item) > 2 else None
        if who not in ("her", "me"):
            raise ValueError(f"message from must be 'her' or 'me', got {who!r}")
        message = {"from": who, "text": str(text)}
        if name:
            message["name"] = str(name)
        cleaned.append(message)
    cleaned = cleaned[-keep:]
    latest_from = cleaned[-1]["from"] if cleaned else "her"
    chat = {
        "relationship": relationship,
        "messages": cleaned,
        "latest_from": latest_from,
        "is_group": any("name" in m for m in cleaned),
    }
    if reply_to:
        chat["reply_to"] = str(reply_to)
    state = {"chat": chat}
    bg = str(background or "").strip()
    if bg:
        state["background"] = bg
    hist = history or []
    if hist:
        state["history"] = hist
    return state


def build_rank_question(candidates: list[str], with_background: bool = False) -> dict:
    """Build the best_reply choice question. criteria values stay in original Chinese."""
    if not 2 <= len(candidates) <= 3:
        raise ValueError("build_rank_question expects 2 or 3 candidate replies")
    keys = ("reply_a", "reply_b", "reply_c")[:len(candidates)]
    instructions = (
        "Which candidate reply is the most appropriate next message, "
        "given the conversation and the other person's true need? "
        "Prefer a reply that matches the best action type. "
        "Penalize dismissive, over-promising, or off-topic replies. "
        "If the facts are not yet confirmed, prefer the candidate that looks them up "
        "instead of faking memory or a vague apology."
    )
    if with_background:   # 跟其余判断题同一个口径，见 BACKGROUND_NOTE
        instructions += BACKGROUND_NOTE
    return {
        "best_reply": {
            "type": "choice",
            "instructions": instructions,
            "criteria": {key: text for key, text in zip(keys, candidates)},
        }
    }


if __name__ == "__main__":
    # 只测不联网的部分。这里唯一会坏的非平凡逻辑就是「知识库字段该不该出现」。
    import json

    _MSGS = [("her", "在吗"), ("me", "在")]

    # 1) **最关键的一条**：没有知识库时，state 与加这个功能之前逐字节一致。
    #    一个多余的空字段就会让所有老用户的请求体变样，也会污染那套校准过的题干。
    plain = build_state(_MSGS, "friends", keep=6)
    assert set(plain) == {"chat"}, plain
    assert set(plain["chat"]) == {"relationship", "messages", "latest_from", "is_group"}, plain["chat"]
    assert plain == {"chat": {"relationship": "friends",
                              "messages": [{"from": "her", "text": "在吗"},
                                           {"from": "me", "text": "在"}],
                              "latest_from": "me", "is_group": False}}, plain
    # 空串 / 纯空白 / None / 空列表，全都不该产生字段
    for bg in ("", "   ", None):
        assert set(build_state(_MSGS, "friends", background=bg)) == {"chat"}, bg
    for hist in ([], None, ()):
        assert set(build_state(_MSGS, "friends", history=hist)) == {"chat"}, hist

    # 2) 带了就出现，且 background 去掉首尾空白
    st = build_state(_MSGS, "friends", background="  关系：恋人  ")
    assert st["background"] == "关系：恋人", st
    st = build_state(_MSGS, "friends", history=[{"from": "her", "text": "上次说的事"}])
    assert st["history"] == [{"from": "her", "text": "上次说的事"}], st

    # 3) 判断题：默认一字不动，带 background 时每道题都追加 BACKGROUND_NOTE
    base = judge_questions()
    assert base == JUDGE_QUESTIONS, "必须返回副本，不能动模块级那份校准过的原文"
    assert not any(BACKGROUND_NOTE in str(s.get("instructions")) for s in base.values())
    with_bg = judge_questions(True)
    assert len(with_bg) == len(JUDGE_QUESTIONS)
    for name, spec in with_bg.items():
        assert spec["instructions"] == JUDGE_QUESTIONS[name]["instructions"] + BACKGROUND_NOTE, name
        # 其余字段原样
        assert spec["type"] == JUDGE_QUESTIONS[name]["type"]
        assert spec["criteria"] == JUDGE_QUESTIONS[name]["criteria"]
    # 而且不能污染原文（返回的是浅拷贝，instructions 是新拼的字符串）
    assert not any(BACKGROUND_NOTE in str(s.get("instructions")) for s in JUDGE_QUESTIONS.values())

    # 4) 排序题同样处理；两条候选时只给 reply_a / reply_b
    rq = build_rank_question(["甲", "乙"])
    assert set(rq["best_reply"]["criteria"]) == {"reply_a", "reply_b"}, rq
    assert BACKGROUND_NOTE not in rq["best_reply"]["instructions"]
    assert build_rank_question(["甲", "乙"], with_background=True)["best_reply"]["instructions"] \
        .endswith(BACKGROUND_NOTE)
    for bad in ([], ["甲"], ["甲", "乙", "丙", "丁"]):
        try:
            build_rank_question(bad)
            raise AssertionError(f"应当抛错: {bad!r}")
        except ValueError:
            pass

    # 5) knowledge_parts：脏值一律当「没有知识库」，绝不抛
    assert knowledge_parts(None) == ("", [])
    assert knowledge_parts("随便一个字符串") == ("", [])
    assert knowledge_parts([]) == ("", [])
    assert knowledge_parts({}) == ("", [])
    assert knowledge_parts({"background": "  ", "history": None}) == ("", [])
    assert knowledge_parts({"background": 123}) == ("123", [])
    assert knowledge_parts({"history": "不是列表"}) == ("", [])
    # 历史里认不出的条目丢掉，不猜
    bg, hist = knowledge_parts({"background": "B", "history": [
        {"from": "her", "text": "一"}, ("me", "二"), {"from": "her"}, "坏条目", {"text": "没有from"}]})
    assert bg == "B", bg
    assert hist == [{"from": "her", "text": "一"}, {"from": "me", "text": "二"},
                    {"from": "her", "text": "没有from"}], hist
    # 非 me 一律算对方（上游是 me/other，本项目是 me/her），保证过得了 build_state 的校验
    assert knowledge_parts({"history": [{"from": "other", "text": "x"}]})[1] == \
        [{"from": "her", "text": "x"}]
    assert knowledge_parts({"history": [{"from": "me", "text": None}]})[1] == []

    # 6) 注入的历史一定过得了 build_state 的校验（这正是第 5 条那个归一化的意义）
    _, hist = knowledge_parts({"history": [{"from": "other", "text": "x"},
                                           {"from": "me", "text": "y"}]})
    assert build_state(_MSGS, "friends", history=hist)["history"] == \
        [{"from": "her", "text": "x"}, {"from": "me", "text": "y"}]

    # 7) JSON 序列化出来的形状：字段名跟上游一致
    st = build_state(_MSGS, "friends", background="B", history=[{"from": "her", "text": "旧"}])
    dumped = json.loads(json.dumps(st, ensure_ascii=False))
    assert dumped["background"] == "B"
    assert dumped["history"][0] == {"from": "her", "text": "旧"}
    assert dumped["chat"]["messages"][0] == {"from": "her", "text": "在吗"}

    print("questions 自测通过（无知识库时 state 逐字节不变 + 两个字段的条件注入 + 脏值兜底）")
