# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。默认发送永远手动；
显式开了自动发送之后，单聊、群里 @我、以及开了「群里不@我也回」时的群消息，都会由这里按
发送键（发送前有可取消的倒计时）。最后那条路会先等群里安静几秒才动手，见 should_wait_quiet。
静默期零调用。上下文、结果、聊天记录都按会话名（子进程 OCR 头部标题得来）分开存，切会话不串味。
开了「保存聊天记录到本地」之后，同一条新消息还会分一份带聊天时间的给 app/recorder.py 落成 CSV
（见 sync_history）——history 本身的结构一个字没变，分析和自动发送那半边完全不受影响。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
OpenRouter key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import threading
import traceback
from collections import deque

from app import recorder, settings, update, worker
from app.capture import find_wechat_hwnd
from app.fill import fill, send_text
from app.kb import KbStore
from app.kb.context import as_knowledge
from app.kb.context import build as build_context
from app.overlay import AUTO_CONFIG_REASONS, Overlay, at_me, auto_pick
from app.version import VERSION
from core.engine import analyze
from core.jev_client import JevError

# {会话名: {history, result, rev, target, senders, auto}}：每个会话各自的上下文、上次结果和版本号，互不串味
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
# auto：这一轮生成是不是「对方来了新消息」触发的——用户自己要求重生成时置 False，那种不自动发送
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "",
         # 自动发送相关的状态。input_has：子进程报来的「输入框里有没有字」，None = 还不知道；
         # batches：每个会话见过几批消息，第一批可能是攒下的存量，不自动发（见 start_auto）；
         # auto_title：正在倒计时的那个会话名，发送前要用它再确认一次微信没被切走；
         # quiet：{会话名: 令牌}，这个会话正排着一个「等群里安静下来」的定时器（见 arm_quiet）
         "input_has": None, "batches": {}, "auto_title": "", "quiet": {}}
# 聊天记录导出器。开关关着时是 None——不建它，连去重窗口都不占内存。
# 它跟分析、自动发送完全并行：那边一行都不读它，它写盘失败也绝不往外抛。
history_rec = None
# 知识库（本机 `知识库/` 目录）。**加这个功能之前的所有行为都靠它保持原样**：
# 里面没东西时，每次分析注入的知识库上下文就是空的（见 build_knowledge），
# 发出去的请求跟以前一个字节都不差。
kb_store = None
_self_judge_noticed = False  # 自判模式只提示一次，别每轮都刷状态栏
_PHASE_TEXT = {"draft": "正在起草候选回复…", "judge": "正在判断和排序…"}
results = queue.Queue()
phase_q = queue.Queue()  # 生成进度。跟 results 分开：那边是定长的 5 元组，混进来会拆包失败
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision, info) 形状搅在一起


def chat_of(title):
    return chats.setdefault(title, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": [], "auto": False})


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text):
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError("未找到微信窗口，请确认微信已打开")
    if state["area"] is None:
        raise RuntimeError("微信输入区域尚不可用，请确认微信聊天窗口可见（不要最小化）")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，微信不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text)


def spawn_worker():
    """开一个采集子进程，它跟着 capture_on 走：置位=采集，清掉=暂停。

    watch_input 跟着「自动发送有没有开」走：没开的人不该白付这份输入框检测的计算。
    batches 一并清空——新子进程的去重状态是空的，它报上来的第一批全是「它第一次见」，
    不能拿旧计数当成「这个会话我已经熟了」。等安静那批定时器同理，一并作废。
    """
    state["batches"].clear()
    state["quiet"].clear()
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, watch_input), daemon=True)
    p.start()
    return p


def on_toggle_capture(on):
    """标题栏开关。启动时没找到微信就没有子进程，这会儿再找一次，找到了才真开得起来。"""
    global child
    if not on:
        capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"] = find_wechat_hwnd()
        except RuntimeError:
            ov.set_capture(False, "未找到微信窗口，打开微信后再开启采集")
            return
        child = spawn_worker()
    capture_on.set()


def report_phase(name):
    """engine 的进度回调。它跑在分析线程里，**不能碰 Qt**，所以只往队列里丢一个字符串，
    由 tick() 在主线程里翻译成状态栏文案。"""
    phase_q.put(name)


def build_knowledge(title, msgs):
    """这一轮分析要额外带上的知识库内容（联系人关系 / 命中的笔记 / 更早的历史）。

    返回 core 认的那个普通 dict，或 **None**——None 表示「没有知识库」，core 那边走的
    是加这个功能之前一模一样的老路（连字段都不出现在请求里）。

    ⚠️ 有副作用：开着「记录聊天历史」且匹配到联系人时，会把这一屏追加进那个联系人的历史。
    所以它必须**每次分析前调一次、且只调一次**（上游就是记录与注入共用一个入口）。

    知识库出任何问题都不该拦住回复：坏掉的 JSON、写不进去的盘，全都吞掉记一行日志，
    然后按「这一轮没有知识库」继续——回复能不能发出去，跟知识库没关系。
    """
    if kb_store is None:
        return None
    try:
        ctx = build_context(kb_store, title, msgs, app="wechat",
                            history_enabled=settings.kb_history_enabled(),
                            history_count=settings.kb_history_count())
    except Exception as e:  # noqa: BLE001
        ov.log(f"[知识库] 组装上下文失败：{type(e).__name__}: {e}")
        return None
    # 界面上那行「本轮带了什么」——用户开了知识库之后，这是他唯一能当场确认「真的用上了」
    # 的地方。除了两个条数，还要把「有没有带上联系人背景」和「联系人认没认出来」一起给它：
    # 只说「未使用知识库」会把「没东西可补」说成「功能没生效」（见 Overlay.set_context_info）。
    # background 直接复用马上要发出去的那份，不另算一遍。
    knowledge = as_knowledge(ctx)
    ov.set_context_info(len(ctx.notes), len(ctx.history),
                        background=knowledge["background"],
                        contact_matched=ctx.contact is not None)
    return knowledge


def analyze_bg(msgs, title, revision, reply_to=None, knowledge=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。

    错误分两路：JevError 是「对端明确回了什么」（带 HTTP 状态码 + 一句短提示 + 重试次数），
    状态栏要靠它说清发生了什么；其余异常是我们自己这边的 bug，只能报笼统文案。真因都进日志。
    """
    # judge_engine() 每次都要读 config.json + 注册表（has_key/draft_ready 都在里面），
    # 别在一次生成里算两遍。它已经是「实际会发生什么」（含降级），不是用户选了什么都不用再判。
    engine = settings.judge_engine()
    try:
        results.put(("ok", analyze(msgs, settings.relationship(), context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(), reply_to=reply_to,
                                   style=settings.style(), thinking=settings.thinking(),
                                   draft_base_url=settings.draft_base_url(),
                                   judge_base_url=settings.judge_base_url(),
                                   judge_model=settings.judge_model(),
                                   judge=engine != "none", judge_engine=engine,
                                   judge_key=settings.judge_key(),
                                   retries=settings.retries(),
                                   # 起草和判断分开配超时：判断读的上下文短得多，给同样的
                                   # 30 秒是白等；现在超时不重试，到了上限就尽快报出来。
                                   timeout=settings.draft_timeout(),
                                   judge_timeout=settings.judge_timeout(),
                                   candidate_count=settings.candidate_count(),
                                   knowledge=knowledge,
                                   on_phase=report_phase),
                     title, revision, None))
    except JevError as e:
        results.put(("err", str(e), title, revision, (e.status, e.hint, e.retries)))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", title, revision, (None, "", 0)))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs, auto_ok=False):
    # 只拦「起草跑不起来」的情况（缺密钥 / 地址不可用）。判断那半缺 OpenRouter 密钥不算故障——
    # 那是正式支持的「起草模式」，由 analyze_bg 按有没有密钥自动降级。
    problem = settings.draft_problem()
    if problem:
        ov.set_status(problem + "，去设置里补上", "warning")
        return
    # 知识库上下文在主线程这里建：它要读几份 JSON、还可能追加一屏历史（见 build_knowledge）。
    # 放在 draft_problem 之后，是为了「压根不会发起分析」时不留任何磁盘痕迹——跟上游一样，
    # 只有真要调模型了才建。返回值是 None 时整条链跟加这个功能之前完全一致。
    knowledge = build_knowledge(title, msgs)
    state["busy"] = True
    ov.set_busy(True)
    # 这一轮该不该考虑自动发送。记在会话上而不是结果里：结果是从线程回来的，多带一个字段
    # 会让那条定长元组（kind, r, title, revision, info）到处都要改。
    chat_of(title)["auto"] = auto_ok
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg,
                     args=(msgs, title, chat_of(title)["rev"], reply_to, knowledge),
                     daemon=True).start()


def on_kb_change():
    """知识库内容变了（新建/编辑/删除/清空/一键存联系人）。界面自己会刷新，这里只做一件事：
    把 store 攒下的提示取出来写进日志——「写不进去」「文件坏了」这类问题不该无声无息。"""
    if kb_store is None:
        return
    for line in kb_store.take_problems():
        ov.log(f"[知识库] {line}")


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs, False)
        ov.set_busy(True)
    else:
        # 用户自己要求重生成，不是对方来了新消息——这一轮不自动发送。否则他一改回复对象，
        # 新生成的候选就会自己发出去，而他可能只是想换个说法看看。
        start_analyze(title, msgs, auto_ok=False)


def sync_watch_input():
    """自动发送的开关变了就同步子进程的输入框监视。关掉时把已知状态一起清掉，
    免得关了很久之后重新打开，还拿着一个几十分钟前的「输入框是空的」去做判断。"""
    want = settings.auto_send_on()
    if want == watch_input.is_set():
        return
    if want:
        watch_input.set()
    else:
        watch_input.clear()
        state["input_has"] = None


def sync_history():
    """跟着设置里的开关建/拆聊天记录导出器。

    关掉时先 close()（把攒着的那批 flush 下去）再放掉——开关拨到「关」的那一下，用户以为
    已经写完了，这时候丢掉内存里那批是最说不清的。
    """
    global history_rec
    if settings.save_history():
        if history_rec is None:
            history_rec = recorder.Recorder(settings.history_dir())
    elif history_rec is not None:
        history_rec.close()
        # 关掉开关时最后那次 flush 也可能失败，这条提示不能跟着记录器一起丢掉
        for line in history_rec.take_problems():
            ov.log(f"[聊天记录] {line}")
        history_rec = None


def on_settings_change():
    """设置页保存之后叫一声。要跟着变的有两件：子进程盯不盯输入框、聊天记录写不写盘。"""
    sync_watch_input()
    sync_history()


def on_toggle_judge(on):
    """标题栏那个「判断」开关被拨动了。

    打开 = 恢复用户上次存的那一档（openrouter / self）；关掉 = 存 none。
    为什么要「记住原来那一档」：用户存的是 openrouter，关掉再打开不该变成 self——
    那等于把他的选择弄丢了半档，而且他不会知道为什么概率不见了。
    """
    if on:
        # stored_judge_engine() 是「用户存过的选择」，不受运行时降级影响。
        # 从没存过（老配置）就按「有 OpenRouter 密钥就用它，没有就用起草模型」推断，跟别处一致。
        settings.save_judge_engine(settings.stored_judge_engine()
                                   or ("openrouter" if settings.has_key() else "self"))
    else:
        settings.save_judge_engine("none")
    ov.log(f"[判断] 已{'开启' if on else '关闭'}判断（judge_engine = {settings.judge_engine()}）。")
    if on:
        problem = settings.judge_problem()
        if problem:
            ov.log(f"[判断] 注意：{problem}")


def should_wait_quiet(chat):
    """这条新消息要不要先等群里安静下来再生成。

    只在「群聊 + 开了不@我也回 + 这条没@我」时等：
    - 单聊不等。那边对方说完就在等你，等几秒只会显得迟钝，而且单聊本来就没这个问题；
    - @我的不等。那是明确信号，立刻生成（跟升级前一模一样）。

    等多久由用户在设置页决定（settings.auto_send_any_wait）。

    判据必须跟 auto_pick 对齐：那边判群聊也是「这个会话出现过带名字的发言人」。两边不一致
    就会出现「它等安静、它不等」这种谁也说不清的行为。
    """
    if not settings.auto_send_group_any():
        return False
    if not chat["senders"]:
        return False
    last = chat["history"][-1][1] if chat["history"] else ""
    return not at_me(last, settings.my_name())


def arm_quiet(title):
    """给这个会话排一个「等群里安静下来」的定时器，每来一条新消息就重排一次。

    为什么需要它：单聊里「对方发来消息」和「这条是对我说的」是同一件事，群里不是——照抄
    单聊就会变成「群里每有人说话都插一句」。等安静是群里唯一既不花模型钱、又能把节奏拉回
    单聊那种「一来一回」的办法：连着聊的时候它一直往后等，只在话题停顿处动手。

    令牌用会话的版本号（rev）：QTimer.singleShot 没法取消，所以只能让旧回调自己失效——
    回调里对不上就说明这期间又来消息了，群里还在说，不该轮到我们插话。这跟 overlay 里
    _auto_fire 拿 serial 让过期回调失效是同一个套路。
    """
    token = chat_of(title)["rev"]
    state["quiet"][title] = token
    wait = settings.auto_send_any_wait()  # 用户在设置页填的那个数，别再写死
    ov.set_status(f"群里还在说话，等安静 {wait} 秒再生成…", "busy")
    ov.after(wait * 1000, lambda: fire_quiet(title, token))


def fire_quiet(title, token):
    """安静窗口到点了，这一轮才真交给生成。"""
    if state["quiet"].get(title) != token:
        return  # 又来新消息了（或用户自己说话了），这个定时器已经作废
    state["quiet"].pop(title, None)
    chat = chat_of(title)
    if chat["rev"] != token:
        return
    msgs = list(chat["history"])
    if state["busy"]:
        state["rerun"] = (title, msgs, True)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs, auto_ok=True)


def start_auto(title, result):
    """这份结果该不该自动发送；该发就把倒计时摆出来。

    门禁一层层收，任何一层不满足都不发——自动发送是这个工具里唯一不可逆的动作：
    1. 开关没开 → 不算了（也不往聊天记录里写，免得刷屏）。
    2. 这一轮不是「对方来了新消息」触发的 → 不发（用户自己要求重生成的那种）。
    3. **每个会话的第一批消息不发**：那是程序第一次看到这个会话时的存量（启动时屏幕上本来
       就有的、或者刚切过去的会话里的历史），很可能是用户不在的时候攒下的。替他回一条几分钟
       前的消息，比不回复糟得多。等这个会话再来了新消息，才进自动发送的射程。
    4. auto_pick()：单聊 / 群里 @我 / 群里不@我也回 三个开关、群里有没有 @我、群昵称填没填。
       判断关着时它会给第一条候选和一句说明，不算拦截。
    5. 界面上正看着的必须是微信当前会话，否则候选是「只看不填」的，更不该发。

    **每一条拦截都要出现在状态栏**，不能只写进「聊天记录」面板——那个面板默认折叠，用户开了
    自动发送却没发出去时，第一反应就是「怎么没反应」。原因写在日志里等于没写。
    """
    if not settings.auto_send_on():
        return
    chat = chat_of(title)
    if not chat["auto"]:
        return
    if state["batches"].get(title, 0) < 2:
        ov.log(f"[自动发送] 「{title}」的第一批消息不自动发送（可能是你不在时攒下的）。")
        ov.set_status("自动发送：这次是启动后的第一批消息，不自动发；下一条起才生效。", "warning")
        return
    last = chat["history"][-1][1] if chat["history"] else ""
    is_group = bool(chat["senders"])
    any_on = settings.auto_send_group_any()
    index, note = auto_pick(result, is_group, last, settings.my_name(),
                            settings.auto_send_dm(), settings.auto_send_group(), any_on)
    if index is None:
        ov.log(f"[自动发送] 没发：{note}。")
        # 配置问题用警示色（用户得去改设置），按设计跳过用中性色（别把正常过滤演成故障）
        kind = "warning" if note in AUTO_CONFIG_REASONS else "idle"
        ov.set_status(f"自动发送：没发（{note}）。", kind)
        return
    if not ov.can_auto_send():
        ov.log("[自动发送] 没发：界面上正看着的不是微信当前会话。")
        ov.set_status("自动发送：没发（界面上正看着别的会话）。", "warning")
        return
    # 倒计时三条路共用一个值（「生成完等几秒再发」）：节奏已经由安静窗口负责压住了，
    # 再给这条路叠一层更长的倒计时，只会让用户对不上自己在设置页填的那个数。
    # 但**必须让用户一眼看出这次是没被点名触发的**——这条路判断不出有没有人叫你，不标出来
    # 他根本不知道自己刚拦下的是什么。note 会被倒计时条原样显示出来。
    unasked = is_group and any_on and not at_me(last, settings.my_name())
    if unasked:
        note = (note + " " if note else "") + "这条没@你，是「群里不@我也回」触发的。"
    delay = settings.auto_send_delay()
    state["auto_title"] = title
    # note 是「这次为什么发这条」——判断关着时它是「没有判断结果，直接发第一条」，
    # 会显示在倒计时条上。用户以为发的是「判断过的推荐」，不写出来他永远不会知道。
    ov.begin_auto(result["candidates"][index], delay, note)


def auto_send_reply(text):
    """倒计时走完，真的按发送键。**这里是最后一道闸**。

    倒计时那几秒里什么都可能变：微信被切到别的会话、用户自己在输入框里打了字、采集停了。
    任何一个成立都取消这次发送，并把原因说清楚——方向永远是「不发」，因为发出去收不回来。
    """
    title = state["auto_title"]
    reason = ""
    if not title:
        # 理论上到不了这儿（start_auto 一定会先写 auto_title）。真到了就说明有 bug——
        # 这时候「不知道这条属于哪个会话」只能不发：发错会话比不发严重得多。
        reason = "不知道这条回复属于哪个会话"
    elif state["busy"]:
        reason = "又在生成新的回复了"
    elif state["chat"] != title:
        reason = "微信已经切到别的会话"
    elif ov.current_chat() != title:
        reason = "界面已经切到别的会话"
    elif state["hwnd"] is None or state["area"] is None:
        reason = "微信窗口或输入区域不可用"
    elif state["input_has"] is not False:
        # 子进程 0.25 秒查一次，所以这个判断最多滞后 0.25 秒。够用了：用户真在打字的话，
        # 子进程几乎立刻会报上来，而倒计时本身还给了他几秒可以点取消。
        reason = "输入框里已经有内容了"
    if reason:
        ov.set_status(f"已取消自动发送（{reason}），回复还在候选里。", "warning")
        ov.log(f"[自动发送] 取消：{reason}。")
        return
    try:
        pressed = send_text(state["hwnd"], state["area"], text, settings.send_key())
    except Exception as e:
        ov.set_status("自动发送失败，回复还在候选里，请手动确认。", "error")
        ov.log(f"[自动发送失败] {type(e).__name__}: {e}")
        ov.log(f"[自动发送失败堆栈] {' '.join(traceback.format_exc().split())[:300]}")
        return
    ov.log(f"[自动发送] 已按 {pressed} 发送：{text}")
    if history_rec is not None:
        # 告诉记录器「这句是程序发的」。OCR 随后会把这条读成 who='me'，靠这份名单标成「程序」——
        # 否则事后从 CSV 里分不出哪句是你自己打的、哪句是它替你发的。
        history_rec.note_sent(title, text)
    ov.set_status("已自动发送。", "success")
    ov.after(2500, lambda: check_auto_sent(title))


def check_auto_sent(title):
    """发完 2.5 秒后回看一眼输入框。**发送键填错是自动发送唯一能自查的失败模式**：
    微信那个设置跟这里不一致时，按下去不会发送，只是在输入框里插一个换行——消息留在框里，
    看起来就像「自动发送失灵」。这一眼就是为它留的。

    为什么敢等 2.5 秒才判：粘贴到回车之间只有 0.15 秒，子进程按 0.25 秒一查，中间那个
    「框里临时有字」的状态它可能采到、也可能采不到；但只要有后续变化它就会再报一次，
    2.5 秒之后拿到的必然是稳定值。所以不会因为那个瞬时状态误报。
    """
    if state["chat"] != title or state["input_has"] is not True:
        return
    ov.set_status("自动发送可能没成功：输入框里还有内容。请检查微信「设置 → 通用 → 快捷键」"
                  "里的发送键，跟设置页里选的那个保持一致。", "warning")
    ov.log("[自动发送] 发送后输入框里仍有内容——发送键多半跟微信设置不一致。")


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "input":  # 子进程报来「输入框里有没有字」（只在状态变了才报）
            state["input_has"] = msg[1]
            if msg[1] and ov.auto_pending():
                # 用户已经在输入框里打字了，这是「他要自己回」最强的信号。不必等倒计时走完
                # 再拒绝，当场收掉并说清为什么——他正低头打字，状态栏那句话就在他眼前。
                ov.cancel_auto("输入框里已经有内容（你可能正在打字），已取消这次自动发送。")
            continue
        if kind == "dead":  # 采集彻底停了（微信关了之类），这才是真的要清状态
            state["area"] = None
            for c in chats.values():  # 在跑的分析作废，回来的结果不再往界面上贴
                c["rev"] += 1
            state["rerun"] = None
            state["quiet"].clear()  # 采集都停了，等着的那批定时器也没意义了
            ov.invalidate_replies()
            ov.set_busy(False)
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                child.terminate()
                child.join()
                child = None
            continue
        _, title, new, area = msg
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        # 见了几批。第一批可能是攒下的存量（启动时屏幕上就有的、刚切过去的会话里的历史），
        # start_auto 靠这个计数把它排除在自动发送之外。
        state["batches"][title] = state["batches"].get(title, 0) + 1
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text, _ts in new:
            chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        if history_rec is not None:
            # 分叉点：history 照旧（不含时间戳，喂模型和自动发送那半边一个字都没变），
            # 带时间的这一份只给记录器。
            history_rec.add(title, new)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            if should_wait_quiet(chat):
                # 群聊 + 开了「不@我也回」+ 这条没@我：先等群里安静下来（见 arm_quiet）。
                # 这一步必须排在 busy 之前——正在跑的那一轮是上一批消息触发的，rev 已经变了，
                # tick() 会把它的结果丢掉，所以这里直接排新的定时器就行，不必走 rerun。
                arm_quiet(title)
            else:
                msgs = list(chat["history"])
                if state["busy"]:
                    state["rerun"] = (title, msgs, True)
                    ov.set_busy(True)
                else:
                    start_analyze(title, msgs, auto_ok=True)
        else:
            state["quiet"].pop(title, None)  # 自己说话了，没什么可等的了
            state["rerun"] = None
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def error_status(info):
    """失败时状态栏那一行。info = (状态码, 短提示, 已重试次数)。

    hint 由 core 给（见 jev_client._HTTP_HINTS 和 _conn_hint）：有它就说明这是「对端明确的
    失败」，能说清是密钥、是防护、是超时还是连接被掐；没有才退回笼统文案。
    「已重试 N 次」必须写出来——用户看到它才知道这不是偶发一次，而是重试过了还是不行。
    状态栏只有一行二十来字，所以提示必须短，详情进聊天记录面板。
    """
    status, hint, retries = info
    if not hint:
        return "生成失败，请检查网络和服务设置；新消息到来后会重试。"
    head = f"{status} {hint}" if status else hint
    tail = f"，已重试 {retries} 次" if retries else ""
    return f"{head}{tail}；新消息到来后会再试。"


def notice_self_judge(result):
    """自判模式第一次生成时提一句：比只起草多一次模型调用，可以关掉。

    默认开自判是为了让没有 OpenRouter 密钥的人开箱就有摘要和排序，但那确实多花一次调用的钱。
    多花钱必须让用户知道——所以第一次生成时在状态栏说一句，**只说不说第二遍**（每轮都提示是噪音）。
    判断失败时不提示：那一刻状态栏有更要紧的话，别把它盖掉。
    """
    global _self_judge_noticed
    if _self_judge_noticed or result.get("judge_engine") != "self" or result.get("judge_error"):
        return
    _self_judge_noticed = True
    ov.log("自判模式：这次生成调了两次模型——起草一次，判断一次。"
           "不想要判断就在设置页「判断与排序」→「判断引擎」里选「不判断」。")
    ov.set_status("已用你的模型做判断（比只起草多一次调用），可在设置里关掉", "success")


def tick():
    try:
        drain()
        if history_rec is not None:
            history_rec.poll()  # 到点了就把攒着的记录落盘（不是每条都写，也不是只等退出）
            for line in history_rec.take_problems():
                ov.log(f"[聊天记录] {line}")
                if "写不进去" in line:
                    # 写盘失败用户必须知道——不然他会以为记录一直在存，直到想用时才发现是空的。
                    # 其余（名字被改写、时间退回墙钟）只进面板，不抢状态栏。
                    ov.set_status("聊天记录写不进去，详情见聊天记录面板", "warning")
        for line in settings.take_config_problem():
            # config.json 坏掉**必须说破**。以前 settings._cfg() 是静默退回默认值的：用户只会
            # 发现「我的设置全没了」，既不知道原因，也不知道该去救哪个文件——而下次点保存还会
            # 把那份可能手工能救回来的文件覆盖掉。这条提示就是那个缺口。
            ov.log(f"[配置] {line}")
            ov.set_status("config.json 读不出来，这次按默认值运行（详见聊天记录）", "warning")
        if kb_store is not None:
            for line in kb_store.take_problems():
                # 知识库是**可选**的一层，坏掉不该抢状态栏（用户可能压根没用它）——
                # 但必须留在日志里，否则「我明明写了笔记却没带上」永远查不出原因。
                ov.log(f"[知识库] {line}")
        # 生成进度（起草 → 判断）。分阶段报出来，是因为「整理回复慢」有一大半来自第二次模型调用，
        # 而用户只看到一句「正在整理」，根本不知道钱和时间花在哪一步。状态栏那行只有二十来字，
        # 所以就写「正在起草…」/「正在判断…」。
        while not phase_q.empty():
            text = _PHASE_TEXT.get(phase_q.get())
            if text and state["busy"]:  # 结果已经回来了就别再盖状态栏
                ov.set_status(text, "busy")
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not results.empty():
            kind, r, title, revision, info = results.get()
            state["busy"] = False
            if state["rerun"]:  # 分析期间又来了新消息，接着跑最新的
                (t, msgs, auto_ok), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs, auto_ok)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat():
                    ov.show(r)
                    # 判断失败但候选保住了：完整原因进面板，状态栏那句由 show() 给
                    if r.get("judge_error"):
                        ov.log(r["judge_error"].get("message") or "判断失败")
                    notice_self_judge(r)
                    start_auto(title, r)  # 该自动发送就在这里把倒计时摆出来
                else:
                    ov.set_busy(False)
            else:
                # 先写日志再收尾：set_failed 会把聊天记录面板展开，用户看到的第一行就是原因
                ov.log(r)
                ov.set_failed(error_status(info))
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    capture_on = multiprocessing.Event()  # 父子进程共用的开关，置位=采集
    # 输入框监视的开关，跟 capture_on 分开：自动发送默认关着，没开的人不该白付这份计算。
    # 用户在设置页打开自动发送时，sync_watch_input() 会把它置位，子进程下一圈就开始盯。
    watch_input = multiprocessing.Event()
    # 知识库：进程级的唯一一份 store，界面和「每次分析前组装上下文」共用它。
    # 目录不存在也没关系——不写就不建，没建过知识库的用户磁盘上不会多出任何东西。
    kb_store = KbStore(settings.kb_dir())
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change,
                 result_of=lambda t: chats.get(t, {}).get("result"),
                 on_auto_send=auto_send_reply, on_settings_change=on_settings_change,
                 on_toggle_judge=on_toggle_judge,
                 kb=kb_store, on_kb_change=on_kb_change)
    child = None
    sync_watch_input()  # 上次是开着自动发送的话，这次一启动就盯上
    sync_history()  # 上次开着聊天记录的话，这次一启动就接着记
    try:
        state["hwnd"] = find_wechat_hwnd()
    except RuntimeError:
        ov.set_capture(False, "未找到微信窗口，打开微信后再开启采集")
    else:
        capture_on.set()
        child = spawn_worker()
    # 只有「起草都跑不起来」才把设置页顶到用户脸上。缺 OpenRouter 密钥不算——
    # 那是起草模式，能干活，别拿一个可选的东西拦人。
    if settings.draft_problem():
        ov.set_status(settings.draft_problem() + "，去设置里补上", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if history_rec is not None:
            history_rec.close()  # 退出前把攒着的那批写下去，别让它烂在内存里
        if child is not None:
            child.terminate()
