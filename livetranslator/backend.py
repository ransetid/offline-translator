"""OpenAI 兼容后端（oMLX / mlx_lm.server）的 HTTP 客户端 + 自托管 LLM 子进程。"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import wave

import numpy as np
import requests

from .config import CFG, auth_headers

SAMPLE_RATE = 16000

# 复用连接：每句识别/翻译都是一次请求，keep-alive 能省掉握手延迟
_session = requests.Session()

# 运行期覆盖（不落盘）：自托管 mlx_lm.server 只接管「对话」，识别仍走 oMLX
RUNTIME = {"chat_url": None, "chat_model": None}


def _chat_url() -> str:
    return RUNTIME["chat_url"] or CFG["omlx_url"]


def _chat_model() -> str:
    return RUNTIME["chat_model"] or CFG["chat_model"]


class BackendError(Exception):
    """对 UI 友好的后端错误（message 已是中文说明）。"""


def _friendly(e: Exception) -> BackendError:
    if isinstance(e, requests.ConnectionError):
        return BackendError(f"连不上模型后端（{CFG.get('omlx_url')}），请确认 oMLX 已打开")
    if isinstance(e, requests.Timeout):
        return BackendError("模型后端响应超时")
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        if code == 404:
            return BackendError("接口 404：模型未加载或后端不支持该接口")
        if code in (401, 403):
            return BackendError("API Key 不对（401/403）")
        return BackendError(f"后端返回 {code}")
    return BackendError(str(e))


def to_wav_bytes(audio: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def asr_transcribe(audio: np.ndarray) -> dict:
    try:
        resp = _session.post(
            f"{CFG['omlx_url']}/v1/audio/transcriptions",
            files={"file": ("audio.wav", to_wav_bytes(audio), "audio/wav")},
            data={"model": CFG["asr_model"], "response_format": "verbose_json"},
            headers=auth_headers(), timeout=60)
        resp.raise_for_status()
        res = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise _friendly(e) from e
    if not isinstance(res, dict):
        raise BackendError("识别接口返回格式不对")
    res.setdefault("segments", [])
    return res


def chat(prompt: str, timeout: int = 120, system: str | None = None) -> str:
    msgs = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    try:
        resp = _session.post(
            f"{_chat_url()}/v1/chat/completions",
            json={"model": _chat_model(), "messages": msgs, "temperature": 0.2},
            headers=auth_headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        raise _friendly(e) from e


def chat_stream(prompt: str, on_delta, timeout: int = 120, system: str | None = None) -> str:
    """流式补全：每收到一段就用累计文本回调 on_delta(acc)。后端不支持流式时返回空串。"""
    msgs = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    parts: list[str] = []
    try:
        resp = _session.post(
            f"{_chat_url()}/v1/chat/completions",
            json={"model": _chat_model(), "messages": msgs,
                  "temperature": 0.2, "stream": True},
            headers=auth_headers(), timeout=timeout, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "event-stream" not in ctype and "ndjson" not in ctype:
            # 忽略了 stream 参数、直接回了完整 JSON
            try:
                return resp.json()["choices"][0]["message"]["content"].strip()
            except (ValueError, KeyError, IndexError):
                return ""
        # 自己按 UTF-8 解码：SSE 响应常不带 charset，requests 会按 ISO-8859-1 解，中文全成乱码
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                choice = json.loads(data)["choices"][0]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                parts.append(delta)
                on_delta("".join(parts))
    except requests.RequestException as e:
        raise _friendly(e) from e
    return "".join(parts).strip()


def list_models(url: str | None = None, api_key: str | None = None, timeout: float = 4) -> list[str]:
    url = (url or CFG["omlx_url"]).rstrip("/")
    try:
        resp = _session.get(f"{url}/v1/models", headers=auth_headers(api_key), timeout=timeout)
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", []) if "id" in m]
    except (requests.RequestException, ValueError) as e:
        raise _friendly(e) from e


def model_loaded(name: str, models: list[str]) -> bool:
    name = (name or "").strip()
    return bool(name) and any(name == m or name in m for m in models)


def health(url: str | None = None, api_key: str | None = None) -> dict:
    """后端体检：在线？识别/对话模型是否已加载？给顶栏状态和向导用。"""
    try:
        models = list_models(url, api_key)
    except BackendError as e:
        return {"ok": False, "error": str(e), "models": [],
                "asr_ok": False, "chat_ok": False}
    chat_ok = model_loaded(CFG.get("chat_model"), models)
    if not chat_ok and RUNTIME["chat_url"]:
        chat_ok = True          # 对话由自托管 mlx_lm.server 承担（懒加载，未必出现在列表里）
    return {"ok": True, "error": "", "models": models,
            "asr_ok": model_loaded(CFG.get("asr_model"), models), "chat_ok": chat_ok,
            "self_hosted_chat": bool(RUNTIME["chat_url"])}


class ManagedLLMServer:
    """自托管 LLM：子进程跑 mlx_lm.server。HTTP 端口起来即视为就绪，模型首个请求时懒加载。"""

    def __init__(self, model: str, port: int):
        self.model = model
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 60) -> bool:
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "mlx_lm.server", "--model", self.model,
                 "--host", "127.0.0.1", "--port", str(self.port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"⚠️  启动自托管 LLM 失败: {e}", file=sys.stderr)
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                requests.get(f"{self.url}/v1/models", timeout=2)
                return True
            except requests.RequestException:
                time.sleep(0.5)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
