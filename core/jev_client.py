"""Jev decisions API client. Standard library only. Never log the API key."""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://openrouter.ai/api/alpha/decisions"
DECISIONS_PATH = "/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"

# 失败后默认最多再试几次（0 = 不重试）。用户在设置页能改，范围见 app/settings.retries()。
# 默认给 2 而不是更大：单次请求本身可能要 2~20s，再试 2 次最坏也就多等十几秒；
# 再往上加用户会以为程序卡死了。
DEFAULT_RETRIES = 2
# 退避上限（秒）。1s / 2s / 4s / 4s...，别让它指数涨到几十秒。
MAX_BACKOFF = 4

# 所有出网请求都带这个 UA。**必须显式设**：不设的话 urllib 会填 `Python-urllib/3.x`，
# 而 Cloudflare 默认规则里就有一条拦它（实测某中转站返回 403 + `error code: 1010`，
# 同一个请求换成任何别的 UA 都是 200）。这里刻意用诚实的自家标识，不伪装浏览器——
# 实测非 Python-urllib 的 UA 一律放行，没必要说谎，而且以后对端要封时假 UA 更难查。
# 不带版本号：core/ 是平台无关层，不能 import app/version.py（依赖方向是 app → core）。
USER_AGENT = "jev-chat-windows"

# HTTP 状态码 → 一句用户能照做的话（进状态栏）。不在表里的码原样报数字，至少让人知道是几。
# 别把这里写成技术解释：用户看到这句时只知道「失败了」，要给他下一步动作。
_HTTP_HINTS = {
    400: "请求被该地址拒绝了",
    401: "密钥被拒，检查这个地址对应的 key",
    403: "被该地址的防护拦下了（不是密钥问题）",
    404: "地址路径不对，确认要不要带 /v1",
    408: "对端响应太慢",
    429: "被限流了，稍后重试",
    500: "对端服务出错",
    502: "对端网关出错",
    503: "对端服务暂时不可用",
    504: "对端网关超时",
}


def http_hint(status: int | None) -> str:
    """状态码 → 短提示；认不出（None / 不在表里）返回空串，调用方自己兜底文案。"""
    return _HTTP_HINTS.get(status, "") if isinstance(status, int) else ""


def retryable_status(status: int) -> bool:
    """这个 HTTP 状态码值不值得重试。

    判据是「重试有没有可能变好」：
      - 5xx：服务端或上游的问题。实测某中转站对没有可用渠道的模型一直回 503
        「Service temporarily unavailable」，这类几十秒内就可能恢复。
      - 408（请求超时）、429（限流）：也是临时的。
    其余 4xx 是「我们的请求有问题」——密钥错、地址错、body 不合法，重试一百次也一样，
    只会让用户多等，所以一次都不重试。
    """
    return status >= 500 or status in (408, 429)


def request_headers(key: str, accept: bool = False) -> dict:
    """所有出网请求共用的请求头。起草和判断都从这里取，免得两边漂移。

    关键就是那个 User-Agent：**不显式设的话 urllib 会填 `Python-urllib/3.x`，会被
    Cloudflare 默认规则拦掉**（见 USER_AGENT 的注释）。这条 bug 已经真实发生过一次，
    所以把头的构造收成一个函数，任何新加的请求都绕不过它。
    """
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    if accept:
        headers["Accept"] = "application/json"
    return headers


def normalize_endpoint(base: str, path: str) -> str:
    """用户填的基础地址 → 完整端点：已含该 path 就原样用，否则去尾斜杠再补上。

    空值、缺 scheme、缺 host 一律返回 ""，调用方退回官方默认——绝不因为一个手填值
    把整条链打死。只校验格式，不联网探测（探测要真发一次请求，等于白花钱）。
    """
    url = (base or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.endswith(path):
        url += path
    parts = urllib.parse.urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else ""


class JevError(Exception):
    """一次出网调用的最终失败。

    status:  HTTP 状态码；连接层失败（超时、连接被掐）没有码，为 None。
    hint:    给状态栏用的一句短话（中文、可操作）；空串 = 调用方自己兜底文案。
             有它状态栏才能说「503 对端服务暂时不可用」而不是「请检查网络和服务设置」。
    retries: 抛出前已经重试过几次（0 = 第一次就失败，没重试）。
    """

    def __init__(self, message: str, status: int | None = None,
                 hint: str = "", retries: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.hint = hint
        self.retries = retries


def parse_json(raw: str, what: str) -> dict:
    """把响应体解析成 JSON 对象。形状不对一律转成带原因的 JevError。

    **绝不让 JSONDecodeError 逃出去**：逃出去就只剩状态栏一句笼统文案，用户完全不知道
    为什么失败。中转站抽风时最常见的表现就是网关回一个 HTML 错误页（200 或 502 都有），
    那种情况下 `json.loads` 会炸，而这一炸原本是没有原因可看的。

    hint 刻意说「不是 JSON」而不是「服务不可用」——用户能据此判断是对方网关的问题，
    不是自己的密钥或地址填错了。
    """
    snippet = redact_secrets(raw[:200].replace("\n", " "))
    try:
        data = json.loads(raw)
    except ValueError:
        raise JevError(f"{what}：对端返回的不是 JSON（可能是网关错误页）: {snippet!r}",
                       None, "对端返回的不是 JSON（可能是网关错误页）") from None
    if not isinstance(data, dict):
        raise JevError(f"{what}：对端返回的不是 JSON 对象: {snippet!r}",
                       None, "对端响应格式不对") from None
    return data


def _conn_hint(exc: BaseException) -> str:
    """连接层失败的短提示。给状态栏用，得说清「是网络这边断的，不是你填错了」。"""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "请求超时"
    if isinstance(exc, http.client.RemoteDisconnected):
        return "连接被对端中断"
    return "连不上该地址"


def post_json(url: str, key: str, payload: bytes, timeout: float, retries: int,
              what: str, parse) -> dict:
    """POST + 重试，最后交给 parse(raw) 解析。起草和判断共用这一个循环。

    重试条件（判据：重试有没有可能变好）：
      - 5xx / 408 / 429 —— 服务端或上游的临时问题（见 retryable_status）
      - 连接被掐（RemoteDisconnected）、连不上（URLError）、TLS 错
      - 响应体不是预期格式 —— 网关错误页换一次可能就好了，所以 parse 抛的 JevError 也重试
    不重试：其余 4xx（密钥错、地址错、body 不合法，重试一百次也一样）。
    **超时也不重试**（见下面那个 except 分支的注释）：对端已经慢到超限，再试一次几乎必然再超时，
    而用户要多等一整个 timeout + 退避。这是本项目最大的一段无谓等待。

    注意 except 的顺序：HTTPError 是 URLError 的子类，必须排在前面；
    RemoteDisconnected 同时是 OSError 和 http.client.HTTPException 的子类。
    """
    last: JevError | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(2 ** (attempt - 1), MAX_BACKOFF))
        try:
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers=request_headers(key, accept=True))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = redact_secrets(exc.read().decode("utf-8", errors="replace"))[:800]
            last = JevError(f"{what} HTTP {exc.code}: {detail}", exc.code,
                            http_hint(exc.code), attempt)
            if retryable_status(exc.code) and attempt < retries:
                continue
            raise last from None
        except (TimeoutError, socket.timeout) as exc:
            # **超时不重试**，这是一条刻意的策略，不是漏写。
            # 超时的含义是「对端已经慢到超过我们愿意等的上限」，立刻再打一次大概率还是超时——
            # 中间那 1s/2s 退避等的是空气，用户却要实打实地再等一个完整 timeout。
            # 实测代价：判断 timeout=15、retries=2 时，超时重试链是 15+1+15+2+15 = 48 秒，
            # 而且第二、三次几乎注定失败（中转站慢到爆 15s 就不会在第 16 秒变快）。
            # 不重试之后这种情况 15 秒就报错，用户早知道、少等 33 秒。
            # 注意跟 408 的区别：408 是**对端自己**声明「我处理超时了」，那是个 HTTP 状态码，
            # 走上面的 HTTPError 分支，仍然重试（对端明确表态了，值得再试一次）。
            # 这里捕获的是**我们这边**等不下去了，性质不同。
            raise JevError(f"{what}请求超时 {timeout}s", None, _conn_hint(exc), attempt) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = redact_secrets(str(getattr(exc, "reason", exc)))
            last = JevError(f"{what}请求失败: {reason}", None, _conn_hint(exc), attempt)
            if attempt < retries:
                continue
            raise last from None

        try:
            return parse(raw)
        except JevError as exc:  # 响应体形状不对——也重试，网关换一次可能就正常了
            if attempt < retries:
                last = exc
                continue
            raise JevError(str(exc), exc.status, exc.hint, attempt) from None

    raise last or JevError(f"{what}：重试用尽", None, "", retries)


def _api_key(env: str = "OPENROUTER_API_KEY") -> str:
    key = (os.environ.get(env) or "").strip()
    if not key:
        raise JevError(
            f"{env} is not set. Export it in the environment; "
            "do not put the key in a file."
        )
    return key


def ask(state: dict, questions: dict, timeout: float = 20,
        base_url: str = "", model: str = "",
        retries: int = DEFAULT_RETRIES) -> dict:
    """POST state+questions to Jev. Returns the parsed JSON body.

    base_url: 用户填的 OpenRouter 兼容代理地址（设置页「判断服务地址」）。
              空值/非法值一律退回官方端点，不会因为填错就整条链失败。
    model: 覆盖默认模型名；空值用 typesafe/jev-1.13。
    retries: 失败后最多再试几次；重试条件见 post_json。
    Never prints or writes the API key.
    """
    key = _api_key()
    url = normalize_endpoint(base_url, DECISIONS_PATH) or API_URL
    payload = json.dumps(
        {"model": (model or "").strip() or MODEL, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    return post_json(url, key, payload, timeout, retries, "Jev",
                     lambda raw: parse_json(raw, "Jev"))


def redact_secrets(text: str) -> str:
    """Strip every live key from any string before print or disk write.

    遍历 os.environ 里**所有**以 _API_KEY 结尾的变量，不硬编码名字——硬编码的列表
    必然漏掉下一个新加的 key（CUSTOM_API_KEY 就是这么漏的），而漏一个就等于把密钥
    原样写进界面和日志。

    为什么遍历环境变量是完备的：key 只有进了 os.environ 才可能被 `_api_key()` 取到、
    才可能发出去；没进环境变量的 key 压根不在任何请求里，也就不可能出现在响应体里。
    （settings 从注册表读 key 时会把值写进 os.environ，所以注册表里的 key 也覆盖到了。）
    """
    if not isinstance(text, str):
        text = str(text)
    for name, key in os.environ.items():
        if name.endswith("_API_KEY") and key:
            text = text.replace(key, "[REDACTED]")
    return text


if __name__ == "__main__":
    # ponytail: 只测不联网的部分。这几处都出过真事，别随手删。

    # 1) UA 必须显式带。漏了它 = urllib 填 Python-urllib/3.x = Cloudflare 默认规则 403/1010，
    #    实测某中转站就是这样，同一个请求换任何别的 UA 都是 200。
    for accept in (False, True):
        h = request_headers("sk-x", accept=accept)
        assert h["User-Agent"] == USER_AGENT == "jev-chat-windows", h
        assert h["Authorization"] == "Bearer sk-x"
        assert "json" in h["Content-Type"]
        assert ("Accept" in h) is accept
    assert "Python-urllib" not in USER_AGENT  # 别手滑改成 urllib 的默认值

    # 2) 脱敏要覆盖**所有** *_API_KEY，不能硬编码名单——CUSTOM_API_KEY 就是硬编码漏掉的
    os.environ["OPENROUTER_API_KEY"] = "sk-or-abc"
    os.environ["CUSTOM_API_KEY"] = "sk-custom-def"
    os.environ["DEEPSEEK_API_KEY"] = ""
    blob = "err: sk-or-abc / sk-custom-def / 不相关的 sk-other"
    red = redact_secrets(blob)
    assert "sk-or-abc" not in red and "sk-custom-def" not in red, red
    assert red.count("[REDACTED]") == 2, red
    assert "sk-other" in red  # 不是环境变量里的 key，不动它（我们无从知道那是不是密钥）
    assert redact_secrets(None) == "None"  # 非字符串也别炸

    # 3) 状态码 → 短提示；认不出给空串，由调用方兜底
    assert "密钥" in http_hint(401) and "防护" in http_hint(403) and "/v1" in http_hint(404)
    assert http_hint(418) == "" and http_hint(None) == "" and http_hint("403") == ""

    # 4) 重试策略：判据是「重试有没有可能变好」。5xx / 408 / 429 重试，其余 4xx 不重试。
    #    这条边界是真金白银换来的：某中转站对没有可用渠道的模型一直回 503。
    for code in (500, 502, 503, 504, 529, 408, 429):
        assert retryable_status(code), code
    for code in (400, 401, 403, 404, 405, 409, 413, 415, 422):
        assert not retryable_status(code), code

    # 5) 响应体不是 JSON 时必须转成带 hint 的 JevError，**不能漏 JSONDecodeError**。
    #    网关回 HTML 错误页是中转站抽风时最常见的表现，漏出去就只剩笼统文案。
    for bad in ("<html>502 Bad Gateway</html>", "", "  ", "not json at all"):
        try:
            parse_json(bad, "起草")
            raise AssertionError(f"应当抛错: {bad!r}")
        except JevError as e:
            assert e.status is None and "不是 JSON" in e.hint, (bad, e.hint)
    assert parse_json('{"a": 1}', "起草") == {"a": 1}
    try:  # JSON 但不是对象：hint 说格式不对，message 里带原文供排查
        parse_json("[1, 2]", "起草")
        raise AssertionError("应当抛错")
    except JevError as e:
        assert e.hint == "对端响应格式不对" and "不是 JSON 对象" in str(e), (e.hint, str(e))
    # 网关错误页里若夹带了密钥，日志里不能原样出现
    leaked = None
    try:
        parse_json("<html>key=sk-or-abc</html>", "起草")
    except JevError as e:
        leaked = str(e)
    assert leaked is not None and "sk-or-abc" not in leaked, leaked

    # 6) JevError 的三个字段是状态栏/日志的唯一来源，别退化成只有 message
    e = JevError("boom", 503, http_hint(503), 2)
    assert (e.status, e.hint, e.retries) == (503, "对端服务暂时不可用", 2)
    assert JevError("boom").retries == 0 and JevError("boom").hint == ""

    print("jev_client 自测通过")
