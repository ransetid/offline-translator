#!/usr/bin/env python3
"""
本地实时翻译 v2 —— 双音源版
================================
流水线：
  [麦克风]  ──┐
              ├─→ 分句(VAD) → oMLX 语音识别 → 语种检测 → oMLX(Gemma) 翻译 → 终端字幕
  [BlackHole]─┘                                                    ↓
                                              Ctrl+C 结束 → 保存转录 + 生成会议总结

特性：
  - 双路音频：麦克风=[本地]，BlackHole 虚拟声卡(系统/会议软件声音)=[远端]
  - 中英日三语自动互译（识别语种后自动决定翻译方向）
  - 识别与翻译统一走 oMLX 的 OpenAI 兼容接口（需 oMLX >= 0.3.0 且安装 omlx[audio]）
  - 纯离线运行
"""

import datetime
import io
import json
import queue
import re
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

# ========================== 配置区（按需修改） ==========================

OMLX_URL = "http://127.0.0.1:16578"   # oMLX 服务地址（默认端口 16578）
OMLX_API_KEY = "1234"                # oMLX API key（见 ~/.omlx/settings.json 的 auth.api_key）
AUTH_HEADERS = {"Authorization": f"Bearer {OMLX_API_KEY}"}
CHAT_MODEL = "gemma-4-e2b-it-4bit"   # 翻译模型名，须与 oMLX 中加载的模型名一致
                                     # 查看已加载模型: curl http://127.0.0.1:16578/v1/models
ASR_MODEL = "whisper-large-v3-turbo-q4" # 识别模型名，同样以 /v1/models 列出的为准

TRANSLATE_MODE = "zh-en"             # 翻译方向："zh-en" 中英互译 / "zh-ja" 中日互译
                                     # 英文/日文 → 始终翻译成中文；中文 → 翻成本模式的另一种语言

REMOTE_DEVICE_KEYWORD = "BlackHole"  # 远端音源设备名关键词（虚拟声卡）
                                     # 没装 BlackHole 时自动退化为仅麦克风模式

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.5                  # 音频块长度
SILENCE_THRESHOLD_MIC = 0.010        # 麦克风静音阈值（环境吵调高）
SILENCE_THRESHOLD_SYS = 0.005        # 系统声音静音阈值（数字信号更干净，可更低）
SILENCE_BLOCKS_TO_CUT = 2            # 连续静音块数 → 切句（2 块 = 1 秒）
MIN_UTTERANCE_SECONDS = 0.6
MAX_UTTERANCE_SECONDS = 12

OUTPUT_DIR = Path.home() / "live_translator_records"

# =======================================================================

LANG_NAMES = {"zh": "中文", "en": "英文", "ja": "日文"}
utterance_queue: "queue.Queue[tuple[str, np.ndarray]]" = queue.Queue()
transcript: list[dict] = []
stop_flag = threading.Event()
print_lock = threading.Lock()


# ---------------------------- 音频采集与分句 ----------------------------

class AudioSource:
    """一路音频输入：采集 → 能量 VAD 分句 → 整句丢进全局处理队列。"""

    def __init__(self, label: str, device_index: int | None, silence_threshold: float):
        self.label = label
        self.device_index = device_index   # None = 系统默认输入设备
        self.silence_threshold = silence_threshold
        self.buffer: list[np.ndarray] = []
        self.silence_count = 0
        self.speaking = False
        self.stream: sd.InputStream | None = None
        self.actual_rate = SAMPLE_RATE

    def start(self):
        block = int(SAMPLE_RATE * BLOCK_SECONDS)
        try:
            self.stream = sd.InputStream(
                device=self.device_index, samplerate=SAMPLE_RATE, channels=1,
                blocksize=block, callback=self._callback,
            )
            self.stream.start()
        except Exception:
            # 设备不支持 16k 时退回设备默认采样率，识别前再重采样
            info = sd.query_devices(self.device_index, "input")
            self.actual_rate = int(info["default_samplerate"])
            block = int(self.actual_rate * BLOCK_SECONDS)
            self.stream = sd.InputStream(
                device=self.device_index, samplerate=self.actual_rate, channels=1,
                blocksize=block, callback=self._callback,
            )
            self.stream.start()
        print(f"  ✓ {self.label} 音源已启动（采样率 {self.actual_rate}Hz）")

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def _callback(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block**2)))
        is_silence = rms < self.silence_threshold

        if self.speaking:
            self.buffer.append(block)
            self.silence_count = self.silence_count + 1 if is_silence else 0
            total_sec = len(self.buffer) * BLOCK_SECONDS
            if self.silence_count >= SILENCE_BLOCKS_TO_CUT or total_sec >= MAX_UTTERANCE_SECONDS:
                audio = np.concatenate(self.buffer)
                self.buffer, self.speaking, self.silence_count = [], False, 0
                if len(audio) / self.actual_rate >= MIN_UTTERANCE_SECONDS:
                    if self.actual_rate != SAMPLE_RATE:
                        audio = resample(audio, self.actual_rate, SAMPLE_RATE)
                    utterance_queue.put((self.label, audio))
        elif not is_silence:
            self.speaking = True
            self.buffer = [block]
            self.silence_count = 0


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    n_dst = int(len(audio) * dst_rate / src_rate)
    x_src = np.linspace(0, 1, len(audio), endpoint=False)
    x_dst = np.linspace(0, 1, n_dst, endpoint=False)
    return np.interp(x_dst, x_src, audio).astype(np.float32)


def find_device(keyword: str) -> int | None:
    for idx, dev in enumerate(sd.query_devices()):
        if keyword.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            return idx
    return None


# ---------------------------- oMLX 接口 ----------------------------

def to_wav_bytes(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def asr(audio: np.ndarray) -> str:
    """调用 oMLX 的 OpenAI 兼容语音识别接口。"""
    resp = requests.post(
        f"{OMLX_URL}/v1/audio/transcriptions",
        files={"file": ("audio.wav", to_wav_bytes(audio), "audio/wav")},
        data={"model": ASR_MODEL},
        headers=AUTH_HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def chat(prompt: str) -> str:
    """调用 oMLX 的 OpenAI 兼容对话接口。
    注：指令并入 user 消息，兼容 Gemma 等不支持 system 角色的模型。"""
    resp = requests.post(
        f"{OMLX_URL}/v1/chat/completions",
        json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        headers=AUTH_HEADERS,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ---------------------------- 语种检测与翻译 ----------------------------

def detect_lang(text: str) -> str:
    """启发式语种检测：假名→日文；汉字→中文；否则→英文。"""
    if re.search(r"[\u3040-\u30ff]", text):       # 平假名/片假名
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):       # 汉字
        return "zh"
    return "en"


def translate(text: str, lang: str) -> str:
    if lang == "zh":
        target = LANG_NAMES["en"] if TRANSLATE_MODE == "zh-en" else LANG_NAMES["ja"]
    else:
        target = LANG_NAMES["zh"]
    prompt = (
        f"你是专业同声传译员。把下面的内容忠实翻译成{target}，"
        f"只输出译文本身，不要任何解释、注释或引号。"
        f"口语语气词可适当省略，保持译文自然流畅。\n\n{text}"
    )
    try:
        return chat(prompt)
    except requests.RequestException as e:
        return f"[翻译失败: {e}]"


# ---------------------------- 处理流水线 ----------------------------

def process_worker():
    """串行处理识别+翻译（16G 内存下避免并发推理）。"""
    while not stop_flag.is_set():
        try:
            label, audio = utterance_queue.get(timeout=0.3)
        except queue.Empty:
            continue
        t0 = time.time()
        try:
            text = asr(audio)
        except requests.RequestException as e:
            with print_lock:
                print(f"[识别失败: {e}]", file=sys.stderr)
            continue
        if not text:
            continue

        lang = detect_lang(text)
        translation = translate(text, lang)
        latency = time.time() - t0
        now = datetime.datetime.now().strftime("%H:%M:%S")

        with print_lock:
            print(f"\n[{now}] [{label}] ({LANG_NAMES[lang]})")
            print(f"  原文: {text}")
            print(f"  译文: {translation}   ({latency:.1f}s)")

        transcript.append({
            "time": now, "speaker": label, "lang": lang,
            "source": text, "translation": translation,
        })


# ---------------------------- 记录与总结 ----------------------------

def save_and_summarize():
    if not transcript:
        print("\n没有录到内容，未生成记录。")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    transcript_path = OUTPUT_DIR / f"transcript_{stamp}.md"
    lines = ["# 会议转录\n"]
    for it in transcript:
        lines.append(f"**[{it['time']}] [{it['speaker']}]** {it['source']}")
        lines.append(f"> {it['translation']}\n")
    transcript_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ 转录已保存: {transcript_path}")

    print("⏳ 正在生成会议总结...")
    full_text = "\n".join(f"[{it['speaker']}] {it['source']}" for it in transcript)
    try:
        summary = chat(
            "你是专业的会议记录员。以下是一场会议的完整转录（[本地]为我方发言，[远端]为对方/线上发言）：\n\n"
            f"{full_text}\n\n"
            "请用中文输出会议总结，Markdown 格式，包含：\n"
            "一、核心内容概要\n二、各方主要观点\n三、达成的共识或结论\n四、待办事项（如有，注明责任方）"
        )
        summary_path = OUTPUT_DIR / f"summary_{stamp}.md"
        summary_path.write_text(f"# 会议总结\n\n{summary}\n", encoding="utf-8")
        print(f"✅ 总结已保存: {summary_path}")
    except requests.RequestException as e:
        print(f"❌ 总结生成失败: {e}")


# ---------------------------- 启动检查与主程序 ----------------------------

def check_omlx():
    try:
        resp = requests.get(f"{OMLX_URL}/v1/models", headers=AUTH_HEADERS, timeout=5)
        resp.raise_for_status()
        names = [m["id"] for m in resp.json().get("data", [])]
    except requests.RequestException:
        print(f"❌ 无法连接 oMLX（{OMLX_URL}），请先启动: omlx serve（或菜单栏启动）")
        sys.exit(1)

    print(f"oMLX 已加载的模型: {names}")
    for need, kind in [(CHAT_MODEL, "翻译"), (ASR_MODEL, "识别")]:
        if not any(need in n for n in names):
            print(f"⚠️  未找到{kind}模型 \"{need}\"，请在 oMLX 管理面板下载/加载，"
                  f"或把配置区的模型名改成上面列表中的实际名称")
            sys.exit(1)


def main():
    check_omlx()

    sources = [AudioSource("本地", None, SILENCE_THRESHOLD_MIC)]
    remote_idx = find_device(REMOTE_DEVICE_KEYWORD)
    if remote_idx is not None:
        sources.append(AudioSource("远端", remote_idx, SILENCE_THRESHOLD_SYS))
    else:
        print(f"⚠️  未检测到 {REMOTE_DEVICE_KEYWORD} 虚拟声卡，本次仅监听麦克风。\n"
              "    线上会议场景请按 README 安装并配置 BlackHole。")

    print("\n启动音源:")
    for s in sources:
        s.start()

    worker = threading.Thread(target=process_worker, daemon=True)
    worker.start()

    mode_desc = "中⇄英" if TRANSLATE_MODE == "zh-en" else "中⇄日"
    print(f"\n🎙️  实时翻译已启动（{mode_desc}，英/日→中自动识别）")
    print("    按 Ctrl+C 结束并生成会议记录\n" + "=" * 50)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\n🛑 停止录音...")
        stop_flag.set()
        for s in sources:
            s.stop()
        worker.join(timeout=10)
        save_and_summarize()


if __name__ == "__main__":
    main()
