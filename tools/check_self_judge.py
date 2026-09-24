# -*- coding: utf-8 -*-
"""自判模式（judge_engine="self"）的端到端回归：起一个本地桩服务，数它被打了几次。

`tools/check_draft_mode.py` 是把 `self_judge` 桩掉验逻辑，验不到「真的发了什么出去」。
这个脚本补上那一段：桩服务第 1 次调用回候选、第 2 次回判断 JSON，然后检查：

1. **真的调了两次**（起草一次、判断一次），且判断那一次走的是 `/chat/completions`
   —— 不是 OpenRouter 私有的 decisions 端点；
2. 判断那一次的 body 里**真的带了题目、候选和 temperature=0**（而不是把起草的 prompt 又发一遍）；
3. `ranking` 变成展示顺序、`best_index` 取第一条，`scores` **一律 None**（自判不给概率）；
4. **判断失败不丢候选**：起草成功、判断返回坏格式时，3 条候选照常返回，`judge_error` 带 hint。

只在回环地址上起服务，不出网、不需要 Qt、不需要任何密钥（密钥用一个假值塞进环境变量）。

跑法（项目根在 cwd）：python tools/check_self_judge.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 本机环境里可能设着 HTTP_PROXY（实测有），它会接管连 127.0.0.1 的请求，
# 把「连接被拒」翻译成代理的 502 —— 那样测的就不是我们的代码了。必须在第一次 urlopen 之前装。
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

CANDS = ["不是他的", "我看看", "哪来的啊"]
JUDGE = {
    "literal_question": {"noul": True},
    "true_intent": {"choice": "request_action"},
    "danger_level": {"score": 2},
    "should_reply_now": {"noul": True},
    "best_action": {"choice": "check_history"},
    "she_needs": {"choice": "care"},
    "tension_resolved": {"noul": True},
    "ranking": ["reply_b", "reply_a", "reply_c"],
}
MSGS = [("her", "这个是鱼哥的？")]


class Stub(BaseHTTPRequestHandler):
    """第 1 次 POST 回候选（起草），之后回判断 JSON。mode="judge_bad" 时判断一律回坏格式。"""

    hits = 0
    bodies: list = []
    paths: list = []
    mode = "ok"

    def do_POST(self):
        cls = type(self)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        cls.bodies.append(json.loads(raw.decode("utf-8")))
        cls.paths.append(self.path)
        cls.hits += 1
        if cls.mode == "judge_bad":
            # 起草必须成功，坏的是判断那一步
            content = json.dumps(CANDS, ensure_ascii=False) if cls.hits == 1 else "<html>502</html>"
        elif cls.hits == 1:
            content = json.dumps(CANDS, ensure_ascii=False)
        else:
            content = json.dumps(JUDGE, ensure_ascii=False)
        body = json.dumps({"choices": [{"message": {"content": content}}]},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 桩服务不用刷屏
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    os.environ["CUSTOM_API_KEY"] = "sk-not-a-real-key"  # 只为了让 _api_key() 不为空

    from core.engine import analyze

    try:
        # ── 正常路径：起草 + 判断两次调用 ────────────────────────────────────
        r = analyze(MSGS, "friends", provider="custom", draft_base_url=base, model="m",
                    judge_engine="self")
        assert Stub.hits == 2, f"自判模式该调两次（起草 + 判断），实际 {Stub.hits} 次"
        assert r["judged"] is True and r["judge_engine"] == "self" and r["judge_error"] is None
        assert r["candidates"] == CANDS, r["candidates"]
        assert r["ranking"] == [1, 0, 2], r["ranking"]  # reply_b→1, reply_a→0, reply_c→2
        assert r["best_index"] == 1 and r["best_reply"] == CANDS[1]
        assert r["scores"] == [None, None, None], f"自判模式绝不能给概率：{r['scores']}"
        assert r["answers"]["danger_level"] == {"score": 2}, r["answers"]
        assert r["answers"]["true_intent"] == {"choice": "request_action"}, r["answers"]

        # 判断那一次必须走 /chat/completions，不是 OpenRouter 私有的 decisions 端点
        assert all("/chat/completions" in p for p in Stub.paths), Stub.paths
        assert not any("decisions" in p for p in Stub.paths), Stub.paths

        # 第二次的 body 得真的带题目 / 候选 / schema，而不是把起草的 prompt 又发一遍
        judge_body = Stub.bodies[1]
        content = judge_body["messages"][1]["content"]
        for token in ("danger_level", "best_action", "reply_b", "我看看", "ranking"):
            assert token in content, f"判断 prompt 里少了 {token!r}"
        # 判断要可复现（temperature=0），起草要有点变化（温度 1.2）
        assert judge_body["temperature"] == 0, judge_body["temperature"]
        assert Stub.bodies[0]["temperature"] == 1.2, Stub.bodies[0]["temperature"]
        # 判断那次带的模型名跟起草一致（judge_model 留空 = 跟起草同一个）
        assert judge_body["model"] == Stub.bodies[0]["model"] == "m"

        # ── 判断失败：候选一条都不能丢 ───────────────────────────────────────
        Stub.hits, Stub.mode = 0, "judge_bad"
        r2 = analyze(MSGS, "friends", provider="custom", draft_base_url=base, model="m",
                     judge_engine="self", retries=2)
        assert r2["candidates"] == CANDS, f"判断失败把候选丢了：{r2['candidates']}"
        assert r2["judged"] is False and r2["judge_engine"] == "self"
        assert r2["scores"] == [None] * 3 and r2["ranking"] is None and r2["best_index"] is None
        err = r2["judge_error"] or {}
        assert err.get("hint"), f"判断失败也要带 hint（否则状态栏只剩笼统文案）：{err}"
        assert err.get("retries") == 2, f"重试次数要如实带出来：{err}"
        # 「模型输出不是 JSON」属于「响应体不是预期格式」，必须真的重试：
        # 1 次起草 + 3 次判断（retries=2）。这条曾经是坏的——parse_judgement 在 post_json
        # 的回调外面，抛了也不会重试，填了 2 次等于没填。
        assert Stub.hits == 4, f"判断坏格式该重试到用尽（1 起草 + 3 判断），实际 {Stub.hits} 次"
    finally:
        srv.shutdown()
        srv.server_close()

    print("自判模式端到端检查通过（两次调用 + 只走 chat/completions + 不给概率 + 判断失败不丢候选）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
