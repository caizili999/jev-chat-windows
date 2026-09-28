# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；relationship 不是密钥，落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方），
**存进去的是 DPAPI 密文**——见下面 _protect_key。进程环境里放的仍是明文，因为 core/ 那层
只认 os.environ，redact_secrets() 也靠它脱敏。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。

config.json 走**原子写**（_write_config）：直接覆盖写会在中途崩溃时留下半截 JSON，
而坏 JSON 会让所有设置静默退回默认值——那正是「用户刚改完设置」这个最不该丢的时刻。
坏文件也不会被无声覆盖，保存前会先备份成 config.json.bad-<时间戳>。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住
from datetime import datetime

from core.draft import CHAT_PATH
from core.jev_client import normalize_endpoint

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_HISTORY_DIR = "聊天记录"  # 跟 config.json 同级，整个文件夹拷走记录也跟着走
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
_DEFAULT_RETRIES = 2  # 失败后最多再试几次。跟 core.jev_client.DEFAULT_RETRIES 保持一致
_MAX_RETRIES = 5  # 上限：单次请求可能要十几秒，再多用户会以为程序卡死了
_ENV = "OPENROUTER_API_KEY"
_DEEPSEEK_ENV = "DEEPSEEK_API_KEY"
_CUSTOM_ENV = "CUSTOM_API_KEY"  # 自定义 OpenAI 兼容地址专用的密钥，同样只进注册表
_JUDGE_ENV = "JUDGE_API_KEY"  # 判断专用密钥（选填）；留空就用起草那家的
_PROVIDERS = ("openrouter", "deepseek", "custom")
# 判断引擎：openrouter（decisions 协议，有校准概率）/ self（用起草模型，只给排序）/ none（不判断）
_JUDGE_ENGINES = ("openrouter", "self", "none")
# 发送键：要跟微信「设置 → 通用 → 快捷键 → 按 Enter 发送消息」保持一致，
# 否则要么发不出去、要么在输入框里插一个换行。
_SEND_KEYS = ("enter", "ctrl_enter")
_DEFAULT_AUTO_DELAY = 5   # 生成完到真正发送之间的秒数，给用户一个能后悔的窗口
_MAX_AUTO_DELAY = 30
# 「群里不@我也回」的安静窗口：群里最后一条消息之后静默这么久才动手生成。
# 单聊不需要它——那边对方说完就在等你；群里连着聊的时候立刻回就是插话，会变成刷屏机器人。
# 这里只是**默认值**，实际取用户在设置页填的那个数（见 auto_send_any_wait）。
AUTO_QUIET_SECONDS = 8
# 单次请求超时。起草和判断分开配：判断要读的上下文短得多、模型也常常更小，给同样的 30s 是浪费。
# 20/15 对 DeepSeek 直连（实测 2~5s）仍然很宽松，对跨境线路也算把最坏情况收住了一截。
_DEFAULT_DRAFT_TIMEOUT = 20
_DEFAULT_JUDGE_TIMEOUT = 15
_MIN_TIMEOUT = 5
_MAX_TIMEOUT = 120
# 起草要几条候选。1 条时模型输出短、不用在多个版本之间权衡 → 生成更快，这是「想更快」最直接的口子。
_DEFAULT_CANDIDATES = 3
_MIN_CANDIDATES = 1
_MAX_CANDIDATES = 3

def _cfg() -> dict:
    """每次都重新读文件，改设置不用重启进程。读不到/坏了/不是对象一律当空配置，退回默认值。

    **但坏了必须说出来。** 以前这里对坏文件静默返回 {}，于是所有设置悄悄变回默认值——
    关系背景、中转地址、模型名、自动发送三个开关、群昵称，一次性全丢，而用户看到的只是
    「我的配置怎么没了」，没有任何解释；更糟的是他接着点一次「保存」，就会把那份
    **可能还能手工救回来**的文件覆盖掉。
    现在坏掉时记一条告警（见 take_config_problem，由 main.tick() 报给用户），
    并且在下次保存前把坏文件备份成 config.json.bad-<时间戳>，不再无声销毁。

    注意 FileNotFoundError 要单独放行：那是「还没建过配置」的正常首次运行路径，
    不是故障，不能给新用户弹一条吓人的告警。
    """
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        _note_config_problem(
            f"config.json 读不出来（{type(e).__name__}），这次先按默认值运行；"
            "原文件会保留，下次保存前自动备份成 config.json.bad-*")
        return {}
    if not isinstance(data, dict):
        _note_config_problem("config.json 里不是 JSON 对象，这次先按默认值运行")
        return {}
    return data


# ── 配置读取告警：坏文件不能再静默 ────────────────────────────────────────────
# 跟 app/recorder.py 的 problems 一个套路：攒着，由上层（main.tick）取走报给用户。
# 同一条只说一次——_cfg() 一次生成要调十几次，不封口就会刷屏。
_config_problems: list = []
_config_noted: set = set()


def _note_config_problem(msg: str) -> None:
    if msg in _config_noted:
        return
    _config_noted.add(msg)
    _config_problems.append(msg)


def take_config_problem() -> list:
    """取走读配置时攒下的告警（取完清空）。main.tick() 拿去写日志和状态栏。"""
    out, _config_problems[:] = list(_config_problems), []
    return out


def _backup_if_broken(folder: str) -> None:
    """旧 config.json 存在但解析不了 → 改名成 config.json.bad-<时间戳> 留着。

    只在保存前做这一次。坏文件里可能是用户唯一一份配置（手工修一下就能救回来），
    不该被我们一次覆盖就永久消失。备份失败也不能拦着保存——那是两件事。
    """
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            json.load(f)
        return  # 能正常解析，不需要备份
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 —— 读不动就是「坏」，往下走备份
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(folder, f"{os.path.basename(_CONFIG)}.bad-{stamp}")
    try:
        os.replace(_CONFIG, target)
    except OSError:
        return
    _note_config_problem(f"之前那个损坏的 config.json 已备份为 {os.path.basename(target)}")


def _write_config(data: dict) -> None:
    """原子写 config.json：写临时文件 → fsync → os.replace。

    原来直接 `open(_CONFIG, "w")` 覆盖写。那样写到一半崩溃、断电、磁盘满，就会留下
    半截 JSON；而 _cfg() 见到坏 JSON 就退回默认值——等于用户所有设置一次性全丢，
    而且触发它的往往正是「用户刚改完设置」这个最不该丢的时刻。
    os.replace 在同一分区上是原子的：外界看到的要么是完整旧文件、要么是完整新文件，
    不存在中间态。fsync 是为了让数据真的落到盘上，而不只是进了系统缓存。

    fsync 是**尽力而为**：个别文件系统（网络盘、某些容器卷）上它会直接报错，而那时
    「设置保存失败」比「少一层落盘保障」严重得多——原子性来自 os.replace，不来自 fsync。
    """
    folder = os.path.dirname(_CONFIG) or "."
    _backup_if_broken(folder)
    tmp = os.path.join(folder, os.path.basename(_CONFIG) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, _CONFIG)

def _text(name: str, default: str = "") -> str:
    """config.json 里的字符串项。"""
    return str(_cfg().get(name) or default)

def relationship() -> str:
    return _text("relationship", _DEFAULT_RELATIONSHIP)

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_cfg().get("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return _text("style")

def retries() -> int:
    """失败后最多再试几次（0 = 不重试）。0~5，缺失/脏数据一律退默认值。

    这是「失败后再试几次」而不是「总共打几次」——界面上的文案必须跟这里一致，
    否则用户填 3 会以为总共只打 3 次，实际是 4 次。
    起草和判断共用这一个值：两边都是「对端偶发不可用」，没有分开配的理由。
    """
    try:
        n = int(_cfg().get("retries", _DEFAULT_RETRIES))
    except (TypeError, ValueError):
        return _DEFAULT_RETRIES
    return max(0, min(_MAX_RETRIES, n))

def _timeout(name: str, default: int) -> int:
    try:
        n = int(_cfg().get(name, default))
    except (TypeError, ValueError):
        return default
    return max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, n))

def draft_timeout() -> int:
    """起草那一次的单次请求超时（秒）。跟判断分开配——起草要读完整上下文、输出三条候选，天生比判断重。"""
    return _timeout("draft_timeout", _DEFAULT_DRAFT_TIMEOUT)

def judge_timeout() -> int:
    """判断那一次的单次请求超时（秒）。判断的上下文和输出都短得多，给起草那么长的超时是白等。"""
    return _timeout("judge_timeout", _DEFAULT_JUDGE_TIMEOUT)

def candidate_count() -> int:
    """起草要几条候选，1~3，默认 3。

    这是「我想更快」最直接的一个口子，不只是界面上少几张卡：要 1 条时模型输出短一截、
    也不需要在三个版本之间权衡取舍，生成时间跟着短。缺失/脏数据一律退默认 3——
    绝不能因为设置里一个坏值让起草少给东西，用户会以为模型坏了。
    """
    try:
        n = int(_cfg().get("candidate_count", _DEFAULT_CANDIDATES))
    except (TypeError, ValueError):
        return _DEFAULT_CANDIDATES
    return max(_MIN_CANDIDATES, min(_MAX_CANDIDATES, n))

def draft_provider() -> str:
    """起草走哪家：openrouter（默认）/ deepseek 直连 / custom（自定义 OpenAI 兼容地址）。
    脏值退回默认，别让一个手改的 config.json 把起草打死。"""
    v = _text("draft_provider")
    return v if v in _PROVIDERS else _PROVIDERS[0]

def draft_base_url() -> str:
    """provider="custom" 时用的基础地址（OpenAI 兼容）。空/非法由 core 退回官方默认。"""
    return _text("draft_base_url")

def draft_model() -> str:
    """起草用的模型名覆盖；空 = 用该来源的默认模型。"""
    return _text("draft_model")

def judge_base_url() -> str:
    """判断/排序的 OpenRouter 兼容代理地址；空 = 官方端点。"""
    return _text("judge_base_url")

def judge_model() -> str:
    """判断/排序的模型名覆盖；空 = 跟起草同一个（self）/ typesafe/jev-1.13（openrouter）。"""
    return _text("judge_model")


def stored_judge_engine() -> str:
    """config.json 里存的那个值；没存过或存了脏值就返回空串。**不做任何降级。**

    降级是运行时的事（见 judge_engine）。存下来的必须是**用户的选择**——否则「选了 OpenRouter
    但暂时没填密钥」会被存成 self，等他哪天补上密钥也回不到完整模式了。
    """
    value = _text("judge_engine")
    return value if value in _JUDGE_ENGINES else ""


def judge_engine() -> str:
    """判断走哪条路，返回三值之一：openrouter / self / none。

    - openrouter：OpenRouter 私有的 decisions 协议，**有校准过的胜出概率**（卡片上的百分比）。
    - self：用任意 OpenAI 兼容模型（留空就是起草那家）做判断，**只给排序不给概率**——
      模型自评的百分比是编的，给出来就是假信息。见 core/judge.py。
    - none：不判断，只给候选（原来的「起草模式」）。

    config.json 里没这一项时（老用户升级上来）按「有 OpenRouter 密钥就用它，没有就用起草模型」
    推断——符合直觉，且用户显式保存过一次之后就固定下来。
    **选了 openrouter 却没密钥会降级到 self**（跟别处一样：运行时降级，不把整条链打死）；
    连起草都跑不起来就只能 none 了。界面按这个返回值显示文案，所以它必须是「实际会发生什么」，
    而不是「用户选了什么」。
    """
    raw = stored_judge_engine() or ("openrouter" if has_key() else "self")
    if raw == "openrouter" and not has_key():
        raw = "self"
    if raw == "self" and not draft_ready():
        raw = "none"
    return raw


def judge_problem() -> str:
    """判断这一半跑不起来的原因；空串 = 能跑。只给设置页做提示用。

    判断是可选的（缺了就是「不判断」，照样有候选），所以这里不像 draft_problem() 那样
    决定「能不能开始」，只是把降级原因说清楚，别让用户以为自己选了 OpenRouter 就在用 OpenRouter。
    """
    raw = stored_judge_engine()
    if raw == "openrouter" and not has_key():
        return "选了 OpenRouter 判断但没填 OpenRouter 密钥，已退回用起草模型判断"
    if raw == "self" and not draft_ready():
        return "起草服务都还没配好，判断先跳过"
    return ""


def judge_key() -> str:
    """判断专用密钥。刻意跟起草分开：判断可能指向另一家地址，拿起草那把发给陌生 host 是发错凭据。"""
    return _get_key(_JUDGE_ENV)


def has_judge_key() -> bool:
    return bool(judge_key())

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_cfg().get("reply_target", False))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。三个来源（OpenRouter/DeepSeek/自定义）都吃这个开关。"""
    return bool(_cfg().get("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：默认开，只出这一次网，设置里能关。"""
    return bool(_cfg().get("check_update", True))


def save_history() -> bool:
    """要不要把对话记录持续写到 history_dir()。**默认关。**

    默认关的理由跟自动发送一样：它要长期往磁盘写文件，不该替用户默认打开。
    """
    return bool(_cfg().get("save_history", False))


def history_dir() -> str:
    """聊天记录的根目录。放在程序目录下（跟 config.json 同级），所以开发态是项目根、
    打包后是 exe 所在目录——不用 os.getcwd()，双击 exe 时那个可能是桌面。"""
    return os.path.join(_ROOT, _HISTORY_DIR)


def auto_send_dm() -> bool:
    """单聊里对方直接找我就自动发送。**默认关。**

    自动发送是这个工具里唯一不可逆的动作（发出去就是发出去了），所以默认值必须是
    「跟以前完全一样」——升级上来的人不该在聊天记录里突然看到自己没看过的消息。
    """
    return bool(_cfg().get("auto_send_dm", False))

def auto_send_group() -> bool:
    """群里 @我 才自动发送。跟单聊拆成两个开关：群里的 @ 是靠**文本匹配**认出来的，
    昵称 OCR 抖一下就会漏，误判面比单聊大，值得让人单独决定。"""
    return bool(_cfg().get("auto_send_group", False))

def auto_send_on() -> bool:
    """三个开关的并集。只要有一个开着，采集子进程就要盯着输入框有没有内容。"""
    return auto_send_dm() or auto_send_group() or auto_send_group_any()

def auto_send_group_any() -> bool:
    """群里不@我也自动回复。**默认关。**

    跟 auto_send_group 是**包含**关系，不是并列：这个开着就等于「群里都回」——@我的消息
    当然也在里面，不必另外再开 auto_send_group。反过来「只开这个、不开 @我」也不会漏掉
    @我的消息，否则就成「有人点名找你反而不回」了。

    它也不依赖「我在群里的昵称」：这条路不匹配 @，昵称空着照样跑（@我 那条路仍然要求填，
    留空 = 那条路一律不发，见 auto_pick）。

    单独成一个开关，是因为它的误判面比 @我 大得多——群里大部分消息本来就不是说给你听的。
    """
    return bool(_cfg().get("auto_send_group_any", False))

def auto_send_any_wait() -> int:
    """「群里不@我也回」那条路的**安静窗口**（0~30，默认 AUTO_QUIET_SECONDS）。

    群里来了没@我的消息后先静默这么久才生成——这是那条路唯一的节奏控制：连着聊的时候它一直
    往后等，只在话题停顿处动手，不然就成「群里每有人说话都插一句」的刷屏机器人。

    **不是倒计时**。倒计时三条路共用一个值（auto_send_delay）。这两个概念以前被混在
    「群里不@我时等几秒」这一个框里：它写的是等待、实际配的是倒计时，而真正的安静窗口被
    硬编码成 8 秒，用户填的数根本不生效——所以这里连配置键一起换成 auto_send_any_wait。

    老配置里的 auto_send_any_delay 仍然认：那时它的语义是倒计时，但用户填它的时候想的是
    「群里没@我时先等几秒」，值照搬过来最贴近本意，不该退回默认值。
    """
    cfg = _cfg()
    if "auto_send_any_wait" in cfg:
        raw = cfg["auto_send_any_wait"]
    else:
        raw = cfg.get("auto_send_any_delay", AUTO_QUIET_SECONDS)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return AUTO_QUIET_SECONDS
    return max(0, min(_MAX_AUTO_DELAY, n))

def auto_send_delay() -> int:
    """生成完到真正发送之间等几秒（0~30，默认 5）。这段是留给用户的后悔时间：
    看一眼不对就点「取消」，不用跑去关总开关。"""
    try:
        n = int(_cfg().get("auto_send_delay", _DEFAULT_AUTO_DELAY))
    except (TypeError, ValueError):
        return _DEFAULT_AUTO_DELAY
    return max(0, min(_MAX_AUTO_DELAY, n))

def send_key() -> str:
    """按哪个键发送：enter / ctrl_enter。**必须跟微信那个设置一致**
    （微信「设置 → 通用 → 快捷键 → 按 Enter 发送消息」），否则要么发不出去、
    要么在输入框里插一个换行。脏值退回 enter（微信的默认值）。"""
    v = _text("send_key")
    return v if v in _SEND_KEYS else _SEND_KEYS[0]

def my_name() -> str:
    """我在群里的昵称，用来认「@我」。**要填群昵称**——它常常跟微信昵称不一样，
    而且每个群可以不同（本版只能填一个，填最常用的那个）。留空 = 群里不自动发送。"""
    return _text("my_name").strip()

# ── 密钥的存储：注册表里存 DPAPI 密文，不是明文 ────────────────────────────────
# 原来的做法是把 key 以明文 REG_SZ 写进 HKCU\Environment。那跟「把 key 写在文件里」没有实质
# 区别——任何以本用户身份运行的程序（随便一个脚本、随便一个 IDE 插件）都能直接读走，
# 而 docs/KICKOFF.md #6 立的规矩正是「key 不落明文」。现在改成 DPAPI 加密后再存：
# 密文只能被**同一个 Windows 用户**解开，注册表快照、备份、截图、别的账户都读不出明文。
#
# 诚实说清它的边界：DPAPI 挡不住「已经以你的身份在跑的恶意程序」——那种情况下它本来也能
# 读注册表。它挡的是「密钥以明文形式躺在那里被顺手拿走」这一类，这是收益最大的一档。
_KEY_PREFIX = "dpapi:"  # 带这个前缀 = 我们加密存的；不带 = 历史遗留的明文，照读


class _Blob(ctypes.Structure):
    """Win32 的 DATA_BLOB。CryptProtectData / CryptUnprotectData 都吃它，
    字段顺序和宽度必须跟 Windows 头文件一致，否则是内存踩踏而不是报错。"""
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _dpapi_fn(name: str):
    """取 crypt32 里的一个函数并声明签名；取不到（非 Windows）返回 None。

    这两个函数是同一套签名（7 个参数、返回 BOOL），所以共用一份声明。
    """
    try:
        fn = getattr(ctypes.windll.crypt32, name)
    except (AttributeError, OSError):
        return None
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    fn.restype = ctypes.c_int
    return fn


def _crypt(data: bytes, decrypt: bool) -> bytes | None:
    """DPAPI 加密/解密一趟；任何一步不成就返回 None（调用方负责退回明文行为）。"""
    fn = _dpapi_fn("CryptUnprotectData" if decrypt else "CryptProtectData")
    if fn is None or not data:
        return None
    src_buf = ctypes.create_string_buffer(data, len(data))  # 必须活到调用结束
    src = _Blob(len(data), ctypes.cast(src_buf, ctypes.c_void_p))
    out = _Blob()
    try:
        ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    except Exception:  # noqa: BLE001 —— DPAPI 不可用只该降级，不该让保存/读取炸掉
        return None
    if not ok or not out.pbData:
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(out.pbData)  # DPAPI 分配的内存要还
        except Exception:  # noqa: BLE001
            pass


def _protect_key(plain: str) -> str:
    """明文 key → 写进注册表的字符串。

    加密不可用时**原样返回明文**，退回历史行为：存储方式出问题绝不能让用户填的密钥直接消失，
    那比明文存着严重得多（用户会以为程序坏了，还得重新去申请一个 key）。
    """
    if not plain:
        return ""
    blob = _crypt(plain.encode("utf-8"), decrypt=False)
    if blob is None:
        return plain
    return _KEY_PREFIX + base64.b64encode(blob).decode("ascii")


def _unprotect_key(raw: str) -> str:
    """注册表/环境变量里的值 → 明文 key。

    解不开时返回**空串**，而不是把密文原样当密钥发出去——那只会换回一个 401，比
    「没填密钥」更难查。同时记一条告警说清原因（换过 Windows 账户、把配置搬到了别的机器）。
    不带前缀的一律当历史遗留明文，老用户升级上来密钥还在，下次保存才转成加密。
    """
    if not raw:
        return ""
    if not raw.startswith(_KEY_PREFIX):
        return raw
    try:
        blob = base64.b64decode(raw[len(_KEY_PREFIX):], validate=True)
    except Exception:  # noqa: BLE001 —— 值被手改坏了
        blob = None
    plain = _crypt(blob, decrypt=True) if blob else None
    if plain is None:
        _note_config_problem(
            "已保存的密钥解不开（换过 Windows 账户，或把配置搬到了别的机器），"
            "请在设置页重新填一次")
        return ""
    return plain.decode("utf-8", errors="replace")


def _get_key(env_name: str) -> str:
    """进程环境优先；没有就读注册表并带进进程环境，之后 core/ 里按 os.environ 读就有了。

    两个来源都可能存着密文（注册表那份一定，进程环境那份在被广播过之后也是），
    所以统一过一遍 _unprotect_key；解出来的明文再回填进 os.environ，
    core/ 那层和 redact_secrets() 都只认明文。
    """
    v = os.environ.get(env_name, "").strip()
    if not v:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, env_name)[0]).strip()
        except Exception:  # 非 Windows / 没这个值
            v = ""
    v = _unprotect_key(v)
    if v:
        os.environ[env_name] = v
    return v

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。

    进程环境里放明文（core/ 只认 os.environ，redact_secrets 也靠它脱敏）；
    注册表里放 DPAPI 密文（见 _protect_key）。
    """
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, _protect_key(value))
        # 广播一下，之后新开的终端/进程就能看到；已经开着的 IDE 看不到也无所谓，启动时会读注册表。
        # 广播出去的是密文，所以这一步不再扩大明文暴露面。
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略

def key() -> str:
    return _get_key(_ENV)

def has_key() -> bool:
    return bool(key())

def deepseek_key() -> str:
    return _get_key(_DEEPSEEK_ENV)

def has_deepseek_key() -> bool:
    return bool(deepseek_key())

def custom_key() -> str:
    """自定义地址专用密钥。刻意不复用 OPENROUTER_API_KEY：那是把凭据发给一个陌生 host。"""
    return _get_key(_CUSTOM_ENV)

def has_custom_key() -> bool:
    return bool(custom_key())

def draft_problem() -> str:
    """起草跑不起来的原因；空串 = 能跑。

    **判断那一半缺 OpenRouter 密钥不算问题**——那是「起草模式」（没有摘要/概率/排序），
    是正式支持的一档，不是故障。所以这里只看选中的起草来源缺不缺东西。
    返回的是可以直接拼进状态栏的文案，main.py 和界面共用同一份判断，避免两处口径打架。
    """
    provider = draft_provider()
    if provider == "deepseek":
        return "" if has_deepseek_key() else "选了 DeepSeek 直连但没填 DeepSeek 密钥"
    if provider == "custom":
        if not normalize_endpoint(draft_base_url(), CHAT_PATH):
            return "选了自定义地址但基础地址不可用（需 http:// 或 https:// 开头）"
        return "" if has_custom_key() else "选了自定义地址但没填自定义密钥"
    return "" if has_key() else "起草选了 OpenRouter 但没填 OpenRouter 密钥"

def draft_ready() -> bool:
    """起草这一半能不能跑。界面用它决定「等待新消息」还是「先设置，再开始」。"""
    return not draft_problem()

def save_judge_engine(engine: str) -> None:
    """只改「判断用哪一档」，别的设置一个字都不动。给标题栏那个一键开关用。

    为什么不让界面直接调 save()：save() 要收二十来个参数，从标题栏那一个开关去拼一整套
    当前值是极容易漏项的（漏了 = 把用户设置清成默认）。这里直接读文件、只换一个键、写回去，
    是最不会出事的形式。

    传进来的值必须是 _JUDGE_ENGINES 里的一员，脏值一律忽略（宁可不动，也别写坏）。
    """
    if engine not in _JUDGE_ENGINES:
        return
    data = _cfg()
    data["judge_engine"] = engine
    try:
        _write_config(data)
    except OSError:
        pass  # 写不进去（文件只读之类）不该崩：开关看起来没生效，但程序照常能用


def save(key_text: str | None, relationship_text: str, context_n: int | None = None,
         deepseek_key_text: str | None = None, provider_text: str | None = None,
         reply_target_on: bool | None = None, style_text: str | None = None,
         thinking_on: bool | None = None, check_update_on: bool | None = None,
         custom_key_text: str | None = None, draft_base_url_text: str | None = None,
         draft_model_text: str | None = None, judge_base_url_text: str | None = None,
         judge_model_text: str | None = None, retries_n: int | None = None,
         judge_engine_text: str | None = None, judge_key_text: str | None = None,
         auto_send_dm_on: bool | None = None, auto_send_group_on: bool | None = None,
         auto_send_delay_n: int | None = None, send_key_text: str | None = None,
         my_name_text: str | None = None, draft_timeout_n: int | None = None,
         judge_timeout_n: int | None = None, candidate_count_n: int | None = None,
         auto_send_group_any_on: bool | None = None,
         auto_send_any_wait_n: int | None = None,
         save_history_on: bool | None = None) -> None:
    """每个参数为 None = 保留当前值；字符串项传 "" 表示清掉。

    四个 key（OpenRouter / DeepSeek / 自定义 / 判断）都只写进程环境 + HKCU\\Environment，
    不写任何文件；地址、模型名、判断引擎、自动发送开关和「我的群昵称」都不是密钥，
    落 config.json，整个文件夹拷走设置也跟着走。
    """
    if key_text:
        _set_key(_ENV, key_text)
    if deepseek_key_text:
        _set_key(_DEEPSEEK_ENV, deepseek_key_text)
    if custom_key_text:
        _set_key(_CUSTOM_ENV, custom_key_text)
    if judge_key_text:
        _set_key(_JUDGE_ENV, judge_key_text)
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    tries = retries() if retries_n is None else max(0, min(_MAX_RETRIES, int(retries_n)))
    provider = provider_text if provider_text in _PROVIDERS else draft_provider()  # None 或脏值 = 保留原来的
    target = reply_target() if reply_target_on is None else bool(reply_target_on)
    style_v = style() if style_text is None else str(style_text).strip()  # 空串 = 清掉
    think = thinking() if thinking_on is None else bool(thinking_on)
    check = check_update() if check_update_on is None else bool(check_update_on)
    draft_url = draft_base_url() if draft_base_url_text is None else str(draft_base_url_text).strip()
    draft_m = draft_model() if draft_model_text is None else str(draft_model_text).strip()
    judge_url = judge_base_url() if judge_base_url_text is None else str(judge_base_url_text).strip()
    judge_m = judge_model() if judge_model_text is None else str(judge_model_text).strip()
    # 存的是**用户的选择**，不是 judge_engine() 那个降级后的结果（见 stored_judge_engine）。
    # 引擎传了脏值 = 保留原来的（跟 provider 一个口径），不写一个认不出的值进去。
    engine = judge_engine_text if judge_engine_text in _JUDGE_ENGINES else (
        stored_judge_engine() or ("openrouter" if has_key() else "self"))
    dm = auto_send_dm() if auto_send_dm_on is None else bool(auto_send_dm_on)
    grp = auto_send_group() if auto_send_group_on is None else bool(auto_send_group_on)
    delay = auto_send_delay() if auto_send_delay_n is None else max(0, min(_MAX_AUTO_DELAY, int(auto_send_delay_n)))
    any_on = auto_send_group_any() if auto_send_group_any_on is None else bool(auto_send_group_any_on)
    any_wait = (auto_send_any_wait() if auto_send_any_wait_n is None
                else max(0, min(_MAX_AUTO_DELAY, int(auto_send_any_wait_n))))
    hist = save_history() if save_history_on is None else bool(save_history_on)
    skey = send_key_text if send_key_text in _SEND_KEYS else send_key()  # 脏值 = 保留原来的
    myname = my_name() if my_name_text is None else str(my_name_text).strip()
    dto = draft_timeout() if draft_timeout_n is None else max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, int(draft_timeout_n)))
    jto = judge_timeout() if judge_timeout_n is None else max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, int(judge_timeout_n)))
    cnt = (candidate_count() if candidate_count_n is None
           else max(_MIN_CANDIDATES, min(_MAX_CANDIDATES, int(candidate_count_n))))
    # 原子写：见 _write_config 的 docstring。用户改完设置点「保存」的这一刻，
    # 正是最不该因为半截文件而把全部配置丢掉的时刻。
    _write_config({"relationship": relationship_text, "context": n, "draft_provider": provider,
                   "reply_target": target, "style": style_v, "thinking": think,
                   "check_update": check, "save_history": hist,
                   "draft_base_url": draft_url, "draft_model": draft_m,
                   "judge_base_url": judge_url, "judge_model": judge_m, "retries": tries,
                   "judge_engine": engine, "auto_send_dm": dm, "auto_send_group": grp,
                   "auto_send_group_any": any_on, "auto_send_any_wait": any_wait,
                   "auto_send_delay": delay, "send_key": skey, "my_name": myname,
                   "draft_timeout": dto, "judge_timeout": jto, "candidate_count": cnt})
