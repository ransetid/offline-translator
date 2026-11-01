"""假的 oMLX（OpenAI 兼容）后端：给端到端测试 / 界面演示用。不依赖任何模型。

识别：按请求次数轮流返回中/英例句；对话：标题 / 纪要 / 翻译 三种分支，支持 SSE 流式。
"""
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SENTENCES = [
    "我们先过一下这个季度的 GEO 投放数据，整体曝光比上月涨了百分之三十。",
    "That sounds great. Could you share the breakdown by channel before Friday?",
    "可以，我会把渠道拆分表在周四之前发到群里。",
    "One concern is the latency of the translation model on older MacBooks.",
    "这个我们测过，M1 上平均延迟一点二秒，基本可以接受。",
    "Let's also decide who owns the onboarding guide for new customers.",
    "新客户的上手指南由 Evan 负责，下周三之前出初稿。",
    "Perfect. I think that covers everything for today, thanks everyone.",
]
TRANS = {
    0: "Let's first go over this quarter's GEO campaign data; overall impressions are up 30% from last month.",
    1: "听起来不错。你能在周五前分享一下按渠道的细分数据吗？",
    2: "Sure, I'll send the channel breakdown to the group before Thursday.",
    3: "一个顾虑是翻译模型在旧款 MacBook 上的延迟。",
    4: "We've tested this — average latency on M1 is 1.2 seconds, which is acceptable.",
    5: "我们也要定一下，新客户上手指南由谁负责。",
    6: "Evan will own the onboarding guide for new customers, with a first draft by next Wednesday.",
    7: "太好了。我想今天的内容都覆盖到了，谢谢大家。",
}
SUMMARY = """## 一、核心内容概要
本次会议回顾了本季度 GEO 投放数据，确认翻译延迟可接受，并明确了新客户上手指南的负责人。

## 二、各方主要观点
- **麦克风**：本季度曝光环比上涨 **30%**；M1 上平均翻译延迟 1.2 秒。
- **媒体·说话人1**：关注旧款 MacBook 上的延迟，希望周五前拿到渠道细分数据。

## 三、达成的共识或结论
1. 当前延迟水平可接受，暂不更换模型。
2. 上手指南作为下周重点交付。

## 四、待办事项
- [ ] 渠道拆分表 —— 麦克风方，周四前
- [ ] 新客户上手指南初稿 —— Evan，下周三前

## 五、章节速览
| 时间 | 议题 |
|---|---|
| 00:00 | 季度投放数据 |
| 00:40 | 翻译延迟 |
| 01:10 | 上手指南分工 |
"""

_counter = itertools.count()
_lock = threading.Lock()
LAST = {"i": 0}
# 演示模式：喂音频的线程把「当前在说第几句」写进来，识别就按它返回（临时稿按时长截断）
CURRENT = {"i": None, "dur": 3.0}
DELAY = {"asr": 0.25, "token": 0.03}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json({"data": [{"id": "SenseVoiceSmall"}, {"id": "gemma-4-e2b-it-4bit"},
                                        {"id": "whisper-large-v3-turbo"}]})
        self._json({"error": "nf"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path.startswith("/v1/audio/transcriptions"):
            time.sleep(DELAY["asr"])
            if CURRENT["i"] is not None:
                text = SENTENCES[CURRENT["i"] % len(SENTENCES)]
                dur = max(0.1, (n - 44) / 2 / 16000)
                frac = dur / CURRENT["dur"]
                if frac < 0.95:
                    text = text[:max(4, int(len(text) * frac))]
                return self._json({"text": text, "segments": [{"no_speech_prob": 0.01, "avg_logprob": -0.2}]})
            with _lock:
                i = next(_counter) % len(SENTENCES)
            return self._json({"text": SENTENCES[i], "segments": [{"no_speech_prob": 0.01, "avg_logprob": -0.2}]})
        if self.path.startswith("/v1/chat/completions"):
            req = json.loads(body)
            msgs = req["messages"]
            user = msgs[-1]["content"]
            sysm = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
            if "起一个简洁标题" in user:
                out = "Q3 GEO 投放复盘会"
            elif "纪要" in user or "要点" in user:
                out = SUMMARY
            else:
                key = user.split("【需要翻译的内容】")[-1].strip()
                idx = next((k for k, s in enumerate(SENTENCES) if s == key), None)
                out = TRANS.get(idx, f"(译) {key}")
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                step = 3
                for k in range(0, len(out), step):
                    d = json.dumps({"choices": [{"delta": {"content": out[k:k + step]}}]}, ensure_ascii=False)
                    chunk = f"data: {d}\n\n".encode()
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    time.sleep(DELAY["token"])
                end = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(end):x}\r\n".encode() + end + b"\r\n0\r\n\r\n")
                return
            time.sleep(0.2)
            return self._json({"choices": [{"message": {"content": out}}]})
        self._json({"error": "nf"}, 404)


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = serve(16578)
    print("fake oMLX on", s.server_address)
    while True:
        time.sleep(3600)
