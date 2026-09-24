# -*- coding: utf-8 -*-
"""端到端验证重试语义：起一个真的会失败的本地 HTTP 服务，数它到底被打了多少次。

为什么必须端到端：重试这件事的 bug 全是「条件写反」或「参数没透传」——
  - 该重试的（5xx/408/429/连接层/响应体形状不对）没重试；
  - 不该重试的（401/403/404 这类 4xx）却在傻等；
  - 设置里配的次数根本没传到出网层（改了等于没改）。
单测 `retryable_status()` 只能证明判据本身对，证明不了调用链有没有把它用上。

退避 sleep 被换成一个只记账、不真等的桩：既省时间，又能断言退避序列是不是 1/2/4/4。

跑法：python tools/check_retry.py   （通过 exit 0，失败抛 AssertionError 并 exit 1）

**不碰真实配置、不发真实请求**：服务只在 127.0.0.1 的随机端口上跑；密钥用假串。
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 必须先把全局 opener 换成「不走代理」：urllib 默认认 HTTP_PROXY/HTTPS_PROXY，
# 本机若有代理，连 127.0.0.1 的请求也会被它接管——代理会把「连接被拒」翻译成 502，
# 于是「连接层失败」这条路径就永远测不到了（本机实测：直连 ECONNREFUSED → 代理 502）。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

from core import draft, jev_client  # noqa: E402
from core.jev_client import JevError  # noqa: E402

FAKE_KEY = "sk-not-a-real-key-0000"  # 只为满足「非空」检查，不会被发到任何真实服务


class _FakeTime:
    """替掉 jev_client.time：sleep 只记账。退避序列因此可断言，且测试不用真等。"""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, sec: float) -> None:
        self.slept.append(sec)


HANGUP = 0  # 状态码 0 = 收到请求但不回任何东西、直接掐线（模拟 RemoteDisconnected）
SLOW = -1  # 状态码 -1 = 收到请求但拖很久才回（让客户端自己超时）


class _Handler(BaseHTTPRequestHandler):
    script: list[tuple[int, str]] = []  # [(状态码, body)]，按请求顺序取；用光后重复最后一条
    hits = 0

    def do_POST(self) -> None:  # noqa: N802（BaseHTTPRequestHandler 的命名约定）
        cls = type(self)
        idx = min(cls.hits, len(cls.script) - 1)
        status, body = cls.script[idx]
        cls.hits += 1
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if status == HANGUP:
            self.close_connection = True  # 一个字都不回就断，客户端会拿到 RemoteDisconnected
            return
        if status == SLOW:
            time.sleep(cls.slow_secs)  # 拖到客户端自己超时，模拟「对端慢到超过我们愿意等的上限」
            return
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    slow_secs = 2.0

    def log_message(self, *args) -> None:  # 别把每个请求都打到 stderr
        pass


def _start_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def _serve(script: list[tuple[int, str]]) -> str:
    _Handler.script = script
    _Handler.hits = 0
    return BASE


def _post(url: str, retries: int, parse=None) -> object:
    return jev_client.post_json(url + "/chat/completions", FAKE_KEY, b"{}", 5.0, retries,
                                "测试", parse or (lambda raw: jev_client.parse_json(raw, "测试")))


OK_BODY = json.dumps({"choices": [{"message": {"content": '["甲","乙","丙"]'}}]},
                     ensure_ascii=False)


def check_retry_then_succeed() -> None:
    """5xx → 5xx → 200：该重试就重试，好了就停，别多打。"""
    _serve([(503, "upstream busy"), (503, "upstream busy"), (200, OK_BODY)])
    got = _post(BASE, retries=2)
    assert _Handler.hits == 3, f"应打 3 次（2 次重试后成功），实际 {_Handler.hits}"
    assert got["choices"][0]["message"]["content"] == '["甲","乙","丙"]'


def check_no_retry_on_4xx() -> None:
    """401 一次都不该重试——密钥错重试一百次还是错，只会让用户白等。"""
    for status in (400, 401, 403, 404, 422):
        _serve([(status, '{"error":"nope"}')])
        try:
            _post(BASE, retries=3)
            raise AssertionError(f"{status} 应当抛错")
        except JevError as e:
            assert e.status == status, (e.status, status)
            assert _Handler.hits == 1, f"{status} 不该重试，实际打了 {_Handler.hits} 次"
            assert e.retries == 0, f"{status} 的 retries 应为 0，实际 {e.retries}"


def check_retry_on_5xx_and_429() -> None:
    """该重试的状态码，重试次数要正好等于 retries。"""
    for status in (500, 502, 503, 504, 408, 429):
        _serve([(status, "boom")])
        try:
            _post(BASE, retries=2)
            raise AssertionError(f"{status} 应当抛错")
        except JevError as e:
            assert e.status == status, (e.status, status)
            assert _Handler.hits == 3, f"{status} 应打 3 次，实际 {_Handler.hits}"
            assert e.retries == 2, f"{status} 的 retries 应为 2，实际 {e.retries}"


def check_retries_zero_means_one_shot() -> None:
    """retries=0 = 一次都不重试。界面上填 0 必须真的是一次。"""
    _serve([(503, "boom")])
    try:
        _post(BASE, retries=0)
        raise AssertionError("应当抛错")
    except JevError as e:
        assert _Handler.hits == 1, f"retries=0 应只打 1 次，实际 {_Handler.hits}"
        assert e.retries == 0


def check_bad_shape_retried() -> None:
    """200 但响应体不是预期格式（网关错误页）——也得重试，换一次可能就好了。"""
    _serve([(200, "<html>502 Bad Gateway</html>"), (200, "<html>502 Bad Gateway</html>"),
            (200, OK_BODY)])
    got = _post(BASE, retries=2)
    assert _Handler.hits == 3, f"形状不对也该重试，应打 3 次，实际 {_Handler.hits}"
    assert got["choices"], got
    # 一直坏：重试用尽后 hint 必须非空，否则状态栏又只剩笼统文案
    _serve([(200, "<html>502</html>")])
    try:
        _post(BASE, retries=1)
        raise AssertionError("应当抛错")
    except JevError as e:
        assert e.hint, "形状错重试用尽后必须带 hint"
        assert _Handler.hits == 2, f"应打 2 次，实际 {_Handler.hits}"


def check_backoff_sequence() -> None:
    """退避 1/2/4/4...，且封顶在 MAX_BACKOFF，别指数涨到几十秒。"""
    fake = _FakeTime()
    real_time, jev_client.time = jev_client.time, fake
    try:
        _serve([(503, "boom")])
        try:
            _post(BASE, retries=5)
        except JevError:
            pass
        assert _Handler.hits == 6, f"retries=5 应打 6 次，实际 {_Handler.hits}"
        assert fake.slept == [1, 2, 4, 4, 4], f"退避序列不对：{fake.slept}"
        assert max(fake.slept) <= jev_client.MAX_BACKOFF
    finally:
        jev_client.time = real_time


def check_remote_disconnect_retried() -> None:
    """连接被对端掐断（RemoteDisconnected）——这曾经是完全没被捕获的 7 条路径之一。"""
    _serve([(HANGUP, ""), (HANGUP, ""), (200, OK_BODY)])
    got = _post(BASE, retries=2)
    assert _Handler.hits == 3, f"掐线也该重试，应打 3 次，实际 {_Handler.hits}"
    assert got["choices"], got


def check_conn_error_hint() -> None:
    """连不上（端口没人听）要带 hint、要按 retries 重试，不能漏出裸 OSError。"""
    fake = _FakeTime()
    real_time, jev_client.time = jev_client.time, fake
    try:
        dead = "http://127.0.0.1:9"  # 9 是 discard 端口，本机基本没人听
        try:
            _post(dead, retries=1)
            raise AssertionError("应当抛错")
        except JevError as e:
            assert e.status is None, f"连接层失败不该有状态码，实际 {e.status}"
            assert e.hint, "连不上必须带 hint"
            assert e.retries == 1, f"应重试 1 次，实际 {e.retries}"
            assert fake.slept == [1], fake.slept
    finally:
        jev_client.time = real_time


def check_timeout_not_retried() -> None:
    """**超时不该重试**——这是本项目最大的一段无谓等待，改错了用户会一直觉得程序卡死。

    超时的含义是「对端已经慢到超过我们愿意等的上限」。立刻再打一次几乎必然还是超时，
    中间那 1s/2s 退避等的是空气，用户却要实打实地再等一整个 timeout。
    实测代价：判断 timeout=15、retries=2 时，重试链是 15+1+15+2+15 = 48 秒；
    不重试之后同样的情况 15 秒就报错。

    注意跟 408 的区别：408 是**对端自己**声明处理超时，那是个 HTTP 状态码，仍然重试
    （见 check_retry_on_5xx_and_429）。这里测的是「我们这边等不下去了」。
    """
    _Handler.slow_secs = 1.2
    _serve([(SLOW, "")])
    fake = _FakeTime()
    real_time, jev_client.time = jev_client.time, fake
    try:
        try:
            # timeout=0.4 远小于服务端的 1.2s，必定客户端超时
            jev_client.post_json(BASE + "/chat/completions", FAKE_KEY, b"{}", 0.4, 2,
                                 "测试", lambda raw: jev_client.parse_json(raw, "测试"))
            raise AssertionError("应当超时抛错")
        except JevError as e:
            assert e.hint == "请求超时", f"应报超时，实际 {e.hint!r} / {e}"
            assert _Handler.hits == 1, \
                f"超时只该打 1 次（不重试），实际 {_Handler.hits} 次——重试链会让用户白等几十秒"
            assert fake.slept == [], f"超时不该有退避等待，实际等了 {fake.slept}"
            assert e.retries == 0, f"超时的 retries 应为 0，实际 {e.retries}"
    finally:
        jev_client.time = real_time
        _Handler.slow_secs = 2.0


def check_draft_retries_plumbed() -> None:
    """起草那条路真的把设置里的次数传下去了——这是最容易漏的一环（改了等于没改）。"""
    os.environ["CUSTOM_API_KEY"] = FAKE_KEY
    msgs = [("her", "在吗"), ("me", "在")]
    for retries, want_hits in ((0, 1), (2, 3)):
        _serve([(503, "boom")])
        try:
            draft.draft_candidates(msgs, "friends", provider="custom",
                                   base_url=BASE, model="m", retries=retries)
            raise AssertionError("应当抛错")
        except JevError as e:
            assert e.status == 503, e.status
            assert _Handler.hits == want_hits, \
                f"retries={retries} 时起草应打 {want_hits} 次，实际 {_Handler.hits}（说明没透传）"


if __name__ == "__main__":
    BASE = _start_server()
    check_retry_then_succeed()
    check_no_retry_on_4xx()
    check_retry_on_5xx_and_429()
    check_retries_zero_means_one_shot()
    check_bad_shape_retried()
    check_backoff_sequence()
    check_remote_disconnect_retried()
    check_conn_error_hint()
    check_timeout_not_retried()
    check_draft_retries_plumbed()
    print("重试语义端到端检查通过")
