#!/usr/bin/env python3
"""
本地实时翻译 v4 —— 多音源开关 + 系统音频捕获版
==================================================
产品形态（承接 v3.5）：
  - 置顶窗口，分语种栏展示历史 + 译文为主的双语显示
  - 「字幕模式」：屏幕底部点击穿透的半透明字幕条
  - 配置外置到 ~/Library/Application Support/LiveTranslator/config.json
  - 先出原文再补译文；幻听过滤；结束一键总结

v4 重点改进：
  1. 修卡顿：实时音频回调只把原始块丢进队列，VAD 分句 / 拼接 / 重采样
     全部移到独立的 segmenter 线程，不再阻塞 PortAudio / SCStream 实时线程。
  2. 系统/媒体声音捕获：用 ScreenCaptureKit 直接抓本机播放的声音（视频、
     会议软件、音乐），无需安装 BlackHole（首次需授权"屏幕录制"权限）。
     若无权限但存在 BlackHole 等回环输入设备，自动退化用该设备。
  3. 音源控制台：每路音源独立「开关 / 增益滑杆 / 实时电平表」，
     开会中可随时静音麦克风、调高媒体音量，所见即所得。

v5 重点改进（继续治卡顿 + 提体验）：
  1. ASR 出进程：SenseVoice 模型加载/推理移到独立子进程（asr_server.py），
     MLX 的 GIL 与 UI 彻底解耦——不再有"界面卡死/转圈"，音频线程也不被饿死。
  2. 流式译文：翻译走 SSE 流式输出，译文逐字流出，不再憋很久一次性蹦字。
  3. 防吞字：VAD 起始处增加 0.75s 预滚缓冲，补回被切掉的句首。

v6 重点改进（按需收音 + 出字更快）：
  1. 按应用挑声音：媒体源可在控制台「来源」下拉框里只抓某个应用（如 Zoom/飞书）
     的音频，避开音乐/系统通知；底层用 SCContentFilter 的 includingApplications。
  2. 边说边出字：说话中每累计 1.2s 跑一次临时识别（仅 SenseVoice），原文先流到
     顶部实时行，停顿定稿后再落到分栏并翻译，大幅降低"出字慢"的体感。
  3. 说话人区分：媒体源用 sherpa-onnx 的 CAM++ embedding（子进程算）+ 主进程在线
     聚类，把不同的人标成「媒体·说话人N」；控制台可勾选开关。依赖 sherpa-onnx。

依赖：sounddevice / numpy / requests / pyobjc(ScreenCaptureKit 等)。
"""

import collections
import datetime
import io
import itertools
import json
import queue
import re
import string
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd

import records

# ---------------------------- 配置（外置 JSON） ----------------------------

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "LiveTranslator"
CONFIG_PATH = CONFIG_DIR / "config.json"
TERMS_PATH = CONFIG_DIR / "terms.json"   # 术语表：人名/黑话，提识别准确率 + 统一译法

MIC_LABEL = "麦克风"
SYS_LABEL = "媒体"

DEFAULTS = {
    "omlx_url": "http://127.0.0.1:16578",
    "api_key": "1234",
    "chat_model": "gemma-4-e2b-it-4bit",
    "translate_mode": "zh-en",            # zh-en / zh-ja

    # 自托管 LLM：app 自己用子进程跑 mlx_lm.server（OpenAI 兼容），不再手动起 oMLX。
    # True = 自包含一键；False = 用上面的外部 omlx_url。
    "self_host_llm": True,
    "llm_model_repo": "mlx-community/gemma-4-e2b-it-4bit",   # mlx_lm.server 加载的模型
    "llm_port": 8080,
    "onboarded": False,                   # 首次运行显示使用向导；「知道了」后置 True

    # 识别模型：全走 oMLX 的 /v1/audio/transcriptions。填 oMLX 里加载的 ASR 模型名，
    # 在「设置」里从 oMLX 已加载列表下拉选择。SenseVoiceSmall 快、Whisper 准（需先在 oMLX 里加载）。
    "asr_model": "SenseVoiceSmall",

    # 音源开关与增益（开会中可在控制台实时改）
    "mic_enabled": True,
    "system_enabled": True,
    "mic_gain": 1.0,                       # 1.0 = 100%
    "system_gain": 1.0,
    "silence_threshold_mic": 0.010,
    "silence_threshold_sys": 0.006,

    # 媒体声音只抓指定应用：空 = 全部系统声音；否则填应用 bundle id（如 us.zoom.xos）
    # 只抓某个会议软件，可避开音乐/系统通知等无关声音。
    "system_capture_app": "",

    # 说话人区分（仅对「媒体」源、仅 SenseVoice 后端生效）：用 sherpa-onnx 的 CAM++
    # embedding + 在线聚类，把媒体里不同的人标成「媒体·说话人N」。
    "diarize_enabled": True,
    "speaker_model": str(CONFIG_DIR / "models" / "campplus_zh.onnx"),
    "diarize_threshold": 0.55,        # 会内聚类：余弦 ≥ 此值判为同一人（CAM++ 经验值）
    "diarize_max_speakers": 8,
    "diarize_global_threshold": 0.70, # 跨会议匹配：更高阈值，宁缺毋滥（只「建议」）
    "speakers_store": str(CONFIG_DIR / "speakers.json"),
    # 系统音频回退：无屏幕录制权限时，若存在该关键词的输入设备就用它
    "system_fallback_device_keyword": "BlackHole",
    # 极少数情况下 SCK 给的是平面(planar)而非交错(interleaved)立体声，听感不对时改 true
    "system_audio_planar": False,

    # 记录根：默认就是 app 数据根 CONFIG_DIR，记录实际落在 <output_dir>/records/{id}（隐藏、
    # 统一管理，卸载删 CONFIG_DIR 即净）。用户经「记录库」管理、经「导出」取文件，不在 ~ 下堆可见文件夹。
    "output_dir": str(CONFIG_DIR),
}

# 旧默认路径（一次性迁移用）
_OLD_OUTPUT_DIR = str(Path.home() / "live_translator_records")

CFG: dict = {}


def load_config():
    CFG.clear()
    CFG.update(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            CFG.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  配置文件损坏，使用默认配置: {e}", file=sys.stderr)
        # 一次性迁移：旧默认路径（含一版中途写错的 CONFIG_DIR/records）→ app 数据根
        if CFG.get("output_dir") in (_OLD_OUTPUT_DIR, str(CONFIG_DIR / "records")):
            CFG["output_dir"] = DEFAULTS["output_dir"]
            save_config()
        # 一次性迁移：旧的 whisper 默认（oMLX 没加载）→ oMLX 实际在跑的 SenseVoiceSmall
        if CFG.get("asr_model") == "whisper-large-v3-turbo-q4":
            CFG["asr_model"] = DEFAULTS["asr_model"]
            save_config()
    else:
        save_config()


def save_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {CFG['api_key']}"} if CFG["api_key"] else {}


# ---------------------------- 术语表 ----------------------------
# terms.json = [{"term": 规范写法, "aliases": [常见错写/别名...], "translation": 指定译文?}]
_terms_cache: "list[dict] | None" = None


def load_terms() -> list[dict]:
    global _terms_cache
    if _terms_cache is None:
        _terms_cache = []
        if TERMS_PATH.exists():
            try:
                data = json.loads(TERMS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    _terms_cache = data
            except (json.JSONDecodeError, OSError):
                pass
    return _terms_cache


def save_terms(terms: list[dict]):
    global _terms_cache
    _terms_cache = terms
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TERMS_PATH.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_terms(text: str) -> str:
    """精确替换已知别名/错写 → 规范术语。低风险（不做同音猜测）。"""
    if not text:
        return text
    for t in load_terms():
        term = (t.get("term") or "").strip()
        if not term:
            continue
        for alias in t.get("aliases", []):
            alias = (alias or "").strip()
            if alias and alias != term:
                text = text.replace(alias, term)
    return text


def terms_glossary() -> str:
    """给翻译 LLM 的术语对照串（仅含指定了译文的词）。"""
    pairs = [f"{t['term']}→{t['translation']}" for t in load_terms()
             if (t.get("term") and t.get("translation"))]
    return "；".join(pairs)


# 音频底层参数
# Whisper 每次识别有 ~2s 固定开销（30s 窗口编码），与片段长短几乎无关。
# 因此策略是「少切、切长块」：让每段语音只触发一次识别，避免碎块堆叠 2s 开销。
SAMPLE_RATE = 16000          # 送 ASR 的统一采样率
BLOCK_SECONDS = 0.25
SILENCE_BLOCKS_TO_CUT = 4    # 连续静音块数 → 切句（4×0.25=1.0s，按自然句末停顿切）
PREROLL_BLOCKS = 3           # 语音起始前预滚的块数（3×0.25=0.75s），防止吞掉句首字
MIN_UTTERANCE_SECONDS = 0.6
MAX_UTTERANCE_SECONDS = 14   # 长发言才强制切块；切得越碎总开销越大，故放宽
INTERIM_SECONDS = 1.2        # 说话中每累计这么久就跑一次临时识别，边说边出字（仅 SenseVoice）

# 幻听过滤
NO_SPEECH_MAX = 0.45
AVG_LOGPROB_MIN = -1.1
HALLUCINATION_BLACKLIST = {
    "you", "thank you", "thanks", "thanks for watching", "bye", "okay", "oh",
    "嗯", "啊", "哦", "谢谢", "谢谢观看", "好", "好的", "字幕由amara org社区提供",
    "ご視聴ありがとうございました",
}
DEDUP_WINDOW_SECONDS = 6

LANG_NAMES = {"zh": "中文", "en": "英文", "ja": "日文"}

# 全局运行态
raw_queue: "queue.Queue[tuple[str, np.ndarray, int]]" = queue.Queue()  # (label, mono块, 采样率)
record_queue: "queue.Queue[tuple[str, np.ndarray, int]]" = queue.Queue()  # 音频写盘专用，独立于 raw_queue
# (kind, label, audio, utt_id, t_start, t_end)
asr_queue: "queue.Queue[tuple]" = queue.Queue()
translate_queue: "queue.Queue[dict]" = queue.Queue()
ui_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
transcript: list[dict] = []
stop_flag = threading.Event()         # 整个 app 退出
session_active = threading.Event()    # 一场会议进行中（开始/结束控制）
entry_ids = itertools.count(1)
current_record: "records.Record | None" = None   # 本场会议的可写记录（点「开始」时建）
_llm_server: "ManagedLLMServer | None" = None     # 自托管 LLM 子进程（无外部后端时才起）

# 每路音源的实时共享状态（UI 与 segmenter 共读写；Python 赋值原子，无需锁）
SRC: dict[str, dict] = {
    MIC_LABEL: {"enabled": True, "gain": 1.0, "level": 0.0, "threshold": 0.010, "available": False},
    SYS_LABEL: {"enabled": True, "gain": 1.0, "level": 0.0, "threshold": 0.006, "available": False},
}

ALL_APPS_LABEL = "全部系统声音"
# SCK 发现的可捕获应用 [(显示名, bundle id)]，由 SystemAudioSource 填充给 UI 下拉框用
AVAILABLE_APPS: list[tuple[str, str]] = []


# ---------------------------- 音源：麦克风 ----------------------------

class MicSource:
    """麦克风（或任意输入设备）。回调只做最轻的事：拷贝原始块入队。"""

    def __init__(self, label: str, device_index: int | None):
        self.label = label
        self.device_index = device_index
        self.stream: sd.InputStream | None = None
        self.rate = SAMPLE_RATE

    def start(self):
        for rate in (SAMPLE_RATE, None):   # 先试 16k，不行用设备默认
            try:
                r = rate or int(sd.query_devices(self.device_index, "input")["default_samplerate"])
                self.stream = sd.InputStream(
                    device=self.device_index, samplerate=r, channels=1,
                    blocksize=int(r * BLOCK_SECONDS), callback=self._callback,
                )
                self.stream.start()
                self.rate = r
                SRC[self.label]["available"] = True
                return
            except Exception:
                continue
        print(f"⚠️  {self.label} 启动失败", file=sys.stderr)

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def _callback(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        raw_queue.put((self.label, block, self.rate))        # → 分句
        record_queue.put((self.label, block, self.rate))     # → 写盘（独立消费者）


# ---------------------------- 音源：系统/媒体声音（ScreenCaptureKit） ----------------------------

_sck_delegate_cls = None


def _build_sck_delegate():
    """惰性构建 SCStream 音频回调委托类（避免无 pyobjc 时导入即崩）。"""
    global _sck_delegate_cls
    if _sck_delegate_cls is not None:
        return _sck_delegate_cls
    import ScreenCaptureKit as sck
    import CoreMedia as cm
    import objc
    from Foundation import NSObject
    objc.setVerbose(0)
    # CMSampleBuffer 指针每帧都会触发 ObjCPointerWarning，刷屏且无害，关掉
    import warnings
    try:
        warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
    except AttributeError:
        pass

    class _SCKAudioDelegate(NSObject):
        # 用 alloc().init() 后直接赋 .label，避免自定义 init 的 objc.super 坑
        label = SYS_LABEL

        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, stype):
            try:
                if stype != sck.SCStreamOutputTypeAudio:
                    return
                n = cm.CMSampleBufferGetNumSamples(sbuf)
                if not n:
                    return
                bb = cm.CMSampleBufferGetDataBuffer(sbuf)
                if bb is None:
                    return
                total = cm.CMBlockBufferGetDataLength(bb)
                buf = bytearray(total)
                # PyObjC 返回 (status, 填充后的缓冲) 元组，而非裸 status
                status, data = cm.CMBlockBufferCopyDataBytes(bb, 0, total, buf)
                if status != 0:
                    return
                arr = np.frombuffer(bytes(data), dtype=np.float32)
                if arr.size == 0:
                    return
                ch = max(1, round(arr.size / n))
                if ch >= 2:
                    if CFG.get("system_audio_planar"):
                        mono = arr[:n]                       # 平面：取左声道
                    else:
                        mono = arr.reshape(-1, ch).mean(axis=1)  # 交错：下混
                else:
                    mono = arr
                mono = np.ascontiguousarray(mono, dtype=np.float32)
                raw_queue.put((self.label, mono, 48000))        # → 分句
                record_queue.put((self.label, mono, 48000))     # → 写盘（独立消费者）
            except Exception as e:
                print("SCK cb err:", e, file=sys.stderr)

        def stream_didStopWithError_(self, stream, err):
            if err:
                print("SCK stopped:", err, file=sys.stderr)

    _sck_delegate_cls = _SCKAudioDelegate
    return _SCKAudioDelegate


class SystemAudioSource:
    """用 ScreenCaptureKit 抓本机播放的声音。需"屏幕录制"权限。
    在主线程启动前同步完成设置（短暂泵 NSRunLoop），音频块经独立 dispatch 队列回调。"""

    def __init__(self, label: str):
        self.label = label
        self.stream = None
        self.delegate = None
        self.needs_permission = False
        self.active_app: str | None = None   # 当前实际抓取的应用显示名（None=全部）

    @staticmethod
    def has_permission() -> bool:
        try:
            import Quartz
            return bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            return False

    def start(self):
        if not self.has_permission():
            self.needs_permission = True
            return
        try:
            import ScreenCaptureKit as sck
            import libdispatch
            from Foundation import NSRunLoop, NSDate
        except Exception as e:
            print(f"⚠️  ScreenCaptureKit 不可用: {e}", file=sys.stderr)
            self.needs_permission = True
            return

        Delegate = _build_sck_delegate()
        done = threading.Event()
        state = {}
        target_bundle = (CFG.get("system_capture_app") or "").strip()

        def on_content(content, err):
            try:
                if err or not content.displays():
                    state["err"] = str(err) or "no display"
                    return
                # 刷新可捕获应用列表（给 UI 下拉框）
                apps = []
                for a in content.applications():
                    try:
                        name = a.applicationName()
                        bid = a.bundleIdentifier()
                        if name and bid:
                            apps.append((name, bid))
                    except Exception:
                        pass
                AVAILABLE_APPS[:] = sorted(set(apps), key=lambda x: x[0].lower())

                display = content.displays()[0]
                match = None
                if target_bundle:
                    for a in content.applications():
                        try:
                            if a.bundleIdentifier() == target_bundle:
                                match = a
                                break
                        except Exception:
                            pass
                if match is not None:
                    # 只抓指定应用的音频
                    flt = sck.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                        display, [match], [])
                    self.active_app = match.applicationName()
                else:
                    if target_bundle:
                        print(f"ℹ️  指定应用 {target_bundle} 未在运行，改抓全部系统声音", file=sys.stderr)
                    flt = sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
                        display, [])
                    self.active_app = None
                cfg = sck.SCStreamConfiguration.alloc().init()
                cfg.setCapturesAudio_(True)
                cfg.setSampleRate_(48000)
                cfg.setChannelCount_(2)
                cfg.setWidth_(128)   # 视频尺寸过小(如2x2)会导致 stream 不交付音频
                cfg.setHeight_(128)
                delegate = Delegate.alloc().init()
                delegate.label = self.label
                stream = sck.SCStream.alloc().initWithFilter_configuration_delegate_(
                    flt, cfg, delegate)
                q = libdispatch.dispatch_queue_create(b"lt.sck.audio", None)
                ok, e2 = stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    delegate, sck.SCStreamOutputTypeAudio, q, None)
                if not ok:
                    state["err"] = f"addStreamOutput 失败: {e2}"
                    return
                stream.startCaptureWithCompletionHandler_(lambda e: state.update(start_err=e))
                state["stream"] = stream
                state["delegate"] = delegate
                state["queue"] = q   # 必须持有 dispatch 队列，否则被回收后停止交付
            finally:
                done.set()

        sck.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
        # 主线程泵 runloop 等待异步设置完成（含权限弹窗）
        deadline = time.time() + 6
        rl = NSRunLoop.currentRunLoop()
        while not done.is_set() and time.time() < deadline:
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))

        if "stream" in state:
            self.stream = state["stream"]
            self.delegate = state["delegate"]
            self.queue = state["queue"]
            SRC[self.label]["available"] = True
        else:
            print(f"⚠️  系统音频启动失败: {state.get('err', '超时')}", file=sys.stderr)
            self.needs_permission = True

    def stop(self):
        if not self.stream:
            return
        # 必须等 SCK 真正停完再返回：完成回调在 SCK 自己的 dispatch 队列上跑。若拖到
        # Python 解释器退出期(Py_FinalizeEx)才跑，take_gil 会对这个非 pthread 线程调
        # pthread_exit → 崩溃（com.screenCaptureKit.streamQueue）。这里 wait 让回调在
        # 解释器还活着时跑完；Event.wait 会释放 GIL，SCK 线程才能拿到 GIL 执行回调。
        done = threading.Event()
        try:
            self.stream.stopCaptureWithCompletionHandler_(lambda e: done.set())
        except Exception:
            done.set()
        done.wait(timeout=3)
        self.stream = None

    def restart(self):
        """切换捕获应用后重建 SCStream（须在主线程调用，内部会泵 runloop）。"""
        self.stop()
        self.stream = None
        self.delegate = None
        self.needs_permission = False
        SRC[self.label]["available"] = False
        self.start()


# ---------------------------- VAD 分句（独立线程，重活在这） ----------------------------

class _SegState:
    __slots__ = ("buffer", "preroll", "silence", "speaking", "rate", "utt_id", "interim_mark")

    def __init__(self):
        self.buffer: list[np.ndarray] = []
        # 静音期滚动保留最近几块，语音一起就把它们拼到句首，避免吞字
        self.preroll: "collections.deque[np.ndarray]" = collections.deque(maxlen=PREROLL_BLOCKS)
        self.silence = 0
        self.speaking = False
        self.rate = SAMPLE_RATE
        self.utt_id = 0          # 本句的稳定 id（起始时分配，临时稿与定稿共用）
        self.interim_mark = 0    # 上次发临时识别时已累计的样本数


def _rec_offset() -> float:
    return current_record.offset() if current_record else 0.0


def segmenter_worker():
    """从 raw_queue 取原始块：应用开关/增益 → 更新电平 → VAD 分句 → 重采样 → asr_queue。"""
    states: dict[str, _SegState] = {}
    while not stop_flag.is_set():
        try:
            label, block, rate = raw_queue.get(timeout=0.3)
        except queue.Empty:
            continue
        st = states.setdefault(label, _SegState())
        st.rate = rate
        src = SRC[label]

        if not src["enabled"]:
            if st.speaking:                # 关闭时丢弃半截句，重置状态
                st.buffer, st.speaking, st.silence = [], False, 0
            st.preroll.clear()
            src["level"] = 0.0
            continue

        gain = float(src["gain"])
        if gain != 1.0:
            block = np.clip(block * gain, -1.0, 1.0)

        rms = float(np.sqrt(np.mean(block**2)))
        src["level"] = max(rms, src["level"] * 0.6)   # 平滑衰减，给电平表用（空闲时也显示）
        is_silence = rms < float(src["threshold"])

        if not session_active.is_set():     # 未开始：只刷电平，不分句，丢半截状态
            if st.speaking:
                st.buffer, st.speaking, st.silence = [], False, 0
            st.preroll.clear()
            continue

        # 边说边出字只对快模型（SenseVoice）开；Whisper 太慢，关掉临时识别
        interim_on = "sense" in (CFG.get("asr_model", "") or "").lower()
        if st.speaking:
            st.buffer.append(block)
            st.silence = st.silence + 1 if is_silence else 0
            total = sum(len(b) for b in st.buffer)
            if st.silence >= SILENCE_BLOCKS_TO_CUT or total / st.rate >= MAX_UTTERANCE_SECONDS:
                audio = np.concatenate(st.buffer)
                uid = st.utt_id
                st.buffer, st.speaking, st.silence, st.interim_mark = [], False, 0, 0
                dur = len(audio) / st.rate
                if dur >= MIN_UTTERANCE_SECONDS:
                    t_end = _rec_offset()
                    t_start = max(0.0, t_end - dur)
                    if st.rate != SAMPLE_RATE:
                        audio = resample(audio, st.rate, SAMPLE_RATE)
                    asr_queue.put(("final", label, audio, uid, t_start, t_end))
                else:
                    ui_queue.put(("interim_clear", {"id": uid}))   # 太短，撤掉临时稿
            elif interim_on and not is_silence and total - st.interim_mark >= INTERIM_SECONDS * st.rate:
                st.interim_mark = total                            # 说话中跑一次临时识别
                audio = np.concatenate(st.buffer)
                if st.rate != SAMPLE_RATE:
                    audio = resample(audio, st.rate, SAMPLE_RATE)
                asr_queue.put(("interim", label, audio, st.utt_id, None, None))
        elif not is_silence:
            st.speaking = True
            st.utt_id = next(entry_ids)              # 本句 id：临时稿与定稿共用
            st.interim_mark = 0
            st.buffer = list(st.preroll) + [block]   # 拼上预滚块，补回句首
            st.silence = 0
        else:
            st.preroll.append(block)                 # 静音期持续滚动预滚缓冲


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """重采样到 dst_rate。优先用 scipy 的多相滤波（带抗混叠），显著优于线性插值——
    48k→16k 下采样若不抗混叠，8kHz 以上内容会混叠进语音频段，直接拉低识别率。"""
    if src_rate == dst_rate or len(audio) == 0:
        return audio.astype(np.float32)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(src_rate), int(dst_rate))
        return resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)
    except Exception:
        n_dst = int(len(audio) * dst_rate / src_rate)   # 退化：线性插值（无抗混叠）
        x_src = np.linspace(0, 1, len(audio), endpoint=False)
        x_dst = np.linspace(0, 1, n_dst, endpoint=False)
        return np.interp(x_dst, x_src, audio).astype(np.float32)


def find_device(keyword: str) -> int | None:
    for idx, dev in enumerate(sd.query_devices()):
        if keyword.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            return idx
    return None


# ---------------------------- 后端接口（OpenAI 兼容） ----------------------------

def to_wav_bytes(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def asr_transcribe(audio: np.ndarray) -> dict:
    """统一 ASR 入口：全部走 oMLX 的 /v1/audio/transcriptions（SenseVoice 或 Whisper 都在
    oMLX 里，OpenAI 兼容）。返回 {text, segments}；is_garbage 用 .get 兜底缺失的置信度字段。"""
    resp = requests.post(
        f"{CFG['omlx_url']}/v1/audio/transcriptions",
        files={"file": ("audio.wav", to_wav_bytes(audio), "audio/wav")},
        data={"model": CFG["asr_model"], "response_format": "verbose_json"},
        headers=auth_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    res = resp.json()
    res.setdefault("segments", [])
    return res


# ---------------------------- 说话人 embedding（进程内 CAM++，onnxruntime 不卡 GIL） ----------------------------
_extractor = None
_extractor_lock = threading.Lock()


def get_extractor():
    """懒加载 sherpa-onnx CAM++ 提取器。onnxruntime 推理会释放 GIL，放 worker 线程算不冻 UI。"""
    global _extractor
    if _extractor is None:
        with _extractor_lock:
            if _extractor is None:
                p = CFG.get("speaker_model", "")
                if p and Path(p).exists():
                    try:
                        import sherpa_onnx
                        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                            model=p, num_threads=1, provider="cpu")
                        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
                    except Exception as e:
                        print(f"说话人模型加载失败（不区分）: {e}", file=sys.stderr)
                        _extractor = False
                else:
                    _extractor = False
    return _extractor or None


def compute_embedding(audio: np.ndarray) -> "list | None":
    ex = get_extractor()
    if ex is None:
        return None
    try:
        s = ex.create_stream()
        s.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(audio, dtype=np.float32))
        s.input_finished()
        if not ex.is_ready(s):
            return None
        return list(ex.compute(s))
    except Exception as e:
        print(f"说话人 embedding 计算失败: {e}", file=sys.stderr)
        return None


def chat(prompt: str, timeout: int = 120) -> str:
    resp = requests.post(
        f"{CFG['omlx_url']}/v1/chat/completions",
        json={
            "model": CFG["chat_model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        headers=auth_headers(),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def chat_stream(prompt: str, on_delta, timeout: int = 120) -> str:
    """流式聊天补全。每收到一段就用累计文本回调 on_delta(acc)，返回完整结果。
    让译文逐字流出，而不是憋很久一次性蹦出来。"""
    resp = requests.post(
        f"{CFG['omlx_url']}/v1/chat/completions",
        json={
            "model": CFG["chat_model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "stream": True,
        },
        headers=auth_headers(),
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()
    parts: list[str] = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
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
    return "".join(parts).strip()


def list_models(url: str, api_key: str) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(f"{url}/v1/models", headers=headers, timeout=5)
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", [])]


class ManagedLLMServer:
    """自托管 LLM：子进程跑 mlx_lm.server（OpenAI 兼容），免去手动起 oMLX。
    模型懒加载——HTTP 端口起来即视为就绪，首个翻译请求才真正加载（在本子进程，不冻 UI）。"""

    def __init__(self, model: str, port: int):
        self.model = model
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc = None

    def start(self, timeout: float = 60) -> bool:
        import subprocess
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
            if self.proc.poll() is not None:    # 进程退了 = 启动失败
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


# ---------------------------- 语种检测 / 幻听过滤 / 翻译 ----------------------------

def detect_lang(text: str) -> str:
    """按主导文字判语种，而不是「有一个 CJK 字就算中文」——否则英文里混一个汉字就被
    误判成中文、塞进中文栏，英文栏永远不出内容。CJK 字信息密度高，按 2 倍权重比拉丁字母。"""
    ja = len(re.findall(r"[぀-ヿ]", text))          # 平/片假名
    zh = len(re.findall(r"[一-鿿]", text))          # 汉字
    latin = len(re.findall(r"[A-Za-z]", text))
    if ja:
        return "ja"
    if zh and zh * 2 >= latin:      # 汉字占主导才算中文
        return "zh"
    if latin:
        return "en"
    return "zh" if zh else "en"


_PUNCT = str.maketrans("", "", string.punctuation + "。，！？、…－—·「」『』（）")
_last_seen: dict[str, tuple[str, float]] = {}


def is_garbage(result: dict, label: str) -> bool:
    text = result.get("text", "").strip()
    if not text:
        return True
    norm = text.lower().translate(_PUNCT).strip()
    if not norm or norm in HALLUCINATION_BLACKLIST:
        return True
    segs = result.get("segments") or []
    if segs:
        if max(s.get("no_speech_prob", 0) for s in segs) > NO_SPEECH_MAX:
            return True
        avg = sum(s.get("avg_logprob", 0) for s in segs) / len(segs)
        if avg < AVG_LOGPROB_MIN:
            return True
    prev = _last_seen.get(label)
    now = time.time()
    _last_seen[label] = (norm, now)
    if prev and prev[0] == norm and now - prev[1] < DEDUP_WINDOW_SECONDS:
        return True
    return False


def _translate_prompt(text: str, lang: str) -> str:
    if lang == "zh":
        target = LANG_NAMES["en"] if CFG["translate_mode"] == "zh-en" else LANG_NAMES["ja"]
    else:
        target = LANG_NAMES["zh"]
    g = terms_glossary()
    gloss = f"以下术语请严格按对照翻译：{g}。" if g else ""
    return (
        f"你是专业同声传译员。把下面的内容忠实翻译成{target}，{gloss}"
        f"只输出译文本身，不要任何解释、注释或引号。"
        f"口语语气词可适当省略，保持译文自然流畅。\n\n{text}"
    )


def translate(text: str, lang: str) -> str:
    return chat(_translate_prompt(text, lang))


# ---------------------------- 识别/翻译流水线 ----------------------------

class SpeakerClusterer:
    """在线说话人聚类：每来一个 192 维 embedding，与已有质心比余弦，够近归入、
    否则新建。纯 numpy，主进程内跑，开销可忽略。"""

    def __init__(self, threshold: float, max_speakers: int):
        self.threshold = threshold
        self.max_speakers = max_speakers
        self.centroids: list[np.ndarray] = []
        self.counts: list[int] = []

    def assign(self, emb) -> int:
        e = np.asarray(emb, dtype=np.float32)
        norm = float(np.linalg.norm(e))
        if norm < 1e-6:
            return 1
        e = e / norm
        if self.centroids:
            sims = [float(c @ e) for c in self.centroids]
            best = int(np.argmax(sims))
            if sims[best] >= self.threshold:
                c = self.centroids[best] * self.counts[best] + e
                self.centroids[best] = c / (float(np.linalg.norm(c)) + 1e-9)
                self.counts[best] += 1
                return best + 1
        if len(self.centroids) < self.max_speakers:
            self.centroids.append(e)
            self.counts.append(1)
            return len(self.centroids)
        # 满员且都不够近：归入溢出桶，不强行并入最近质心（避免污染将来要持久化的声纹）
        return self.max_speakers + 1


_diarizers: dict[str, SpeakerClusterer] = {}


def _get_diarizer(label: str) -> SpeakerClusterer:
    if label not in _diarizers:
        _diarizers[label] = SpeakerClusterer(
            float(CFG.get("diarize_threshold", 0.55)),
            int(CFG.get("diarize_max_speakers", 8)))
    return _diarizers[label]


def asr_worker():
    diarize_label = SYS_LABEL   # 只对「媒体」源区分说话人；麦克风默认本地用户
    while not stop_flag.is_set():
        try:
            kind, label, audio, uid, t_start, t_end = asr_queue.get(timeout=0.3)
        except queue.Empty:
            continue
        want_emb = (kind == "final" and label == diarize_label
                    and bool(CFG.get("diarize_enabled")))
        try:
            result = asr_transcribe(audio)     # 全走 oMLX HTTP
        except requests.RequestException as e:
            ui_queue.put(("status", {"text": f"识别失败: {e}"}))
            continue
        text = apply_terms((result.get("text") or "").strip())   # 术语精确纠正

        if kind == "interim":
            # 临时稿只滤空白，不做去重/黑名单（它会随说话不断变化）
            if text:
                ui_queue.put(("interim", {
                    "id": uid, "speaker": label,
                    "lang": detect_lang(text), "source": text}))
            continue

        # 定稿：完整幻听过滤
        if is_garbage(result, label):
            ui_queue.put(("interim_clear", {"id": uid}))
            continue
        speaker = label
        emb = compute_embedding(audio) if want_emb else None   # 进程内算 CAM++ 声纹
        if emb is not None:
            speaker = f"{label}·说话人{_get_diarizer(label).assign(emb)}"
        entry = {
            "id": uid,
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "t_start": t_start,
            "t_end": t_end,
            "speaker": speaker,
            "lang": detect_lang(text),
            "source": text,
            "translation": None,
            "embedding": emb,          # 仅 final+媒体 有；存 records 的 embeddings.npz
            "t0": time.time(),
        }
        transcript.append(entry)
        if current_record is not None:
            current_record.append_entry(entry)   # 逐条落盘，崩溃安全
        ui_queue.put(("source", entry))
        translate_queue.put(entry)


def translate_worker():
    while not stop_flag.is_set():
        try:
            entry = translate_queue.get(timeout=0.3)
        except queue.Empty:
            continue
        prompt = _translate_prompt(entry["source"], entry["lang"])
        last_push = [0.0]

        def on_delta(acc, e=entry, lp=last_push):
            e["translation"] = acc
            now = time.time()
            if now - lp[0] >= 0.06:           # 节流，避免每个 token 都刷 UI
                lp[0] = now
                ui_queue.put(("translation_partial", e))

        try:
            full = chat_stream(prompt, on_delta)
            if not full:                      # 后端忽略了 stream 参数 → 退回非流式
                full = chat(prompt)
            entry["translation"] = full or "[空]"
        except requests.RequestException as e:
            entry["translation"] = f"[翻译失败: {e}]"
        entry["latency"] = time.time() - entry["t0"]
        ui_queue.put(("translation", entry))


# ---------------------------- 记录与总结 ----------------------------

def auto_title(full_text: str) -> str:
    """让 LLM 给会议起个简洁标题（≤15字，跟随会议主语种）。失败由调用方兜。"""
    snippet = full_text[:2000]
    t = chat(
        "给下面这场会议起一个简洁标题，不超过15个字，用会议的主要语言，"
        f"只输出标题本身，不要引号、不要句末标点：\n\n{snippet}",
        timeout=60,
    ).strip().strip('"\'《》 ')
    return (t.splitlines()[0] if t else "")[:30]


SUMMARY_CHUNK_ENTRIES = 80   # 转录超过这么多条就 map-reduce，避免爆小模型上下文

_SUMMARY_SECTIONS = (
    "一、核心内容概要\n二、各方主要观点\n三、达成的共识或结论\n"
    "四、待办事项（如有，注明责任方）\n五、章节速览（按时间顺序列出各议题）"
)


def _fmt_ts(sec) -> str:
    s = int(sec or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _entries_text(entries) -> str:
    return "\n".join(f"[{e['speaker']}] {e['source']}" for e in entries)


def summarize_transcript(entries: list) -> str:
    """结构化总结。长会走 map-reduce（分块各自摘要 → 合并），短会单次。"""
    head = (f"[{MIC_LABEL}]为本地麦克风发言，[{SYS_LABEL}]为本机播放的媒体/会议软件声音。")
    if len(entries) <= SUMMARY_CHUNK_ENTRIES:
        return chat(
            f"你是专业的会议记录员。{head}以下是完整转录：\n\n{_entries_text(entries)}\n\n"
            f"请用中文输出会议总结，Markdown 格式，包含：\n{_SUMMARY_SECTIONS}",
            timeout=300,
        )
    # map：逐段要点
    notes = []
    for i in range(0, len(entries), SUMMARY_CHUNK_ENTRIES):
        chunk = entries[i:i + SUMMARY_CHUNK_ENTRIES]
        ts = _fmt_ts(chunk[0].get("t_start"))
        note = chat(
            f"这是一场会议的第 {i // SUMMARY_CHUNK_ENTRIES + 1} 段转录（{head}），"
            f"请用中文 5-8 条要点概括，保留关键结论与待办：\n\n{_entries_text(chunk)}",
            timeout=200,
        )
        notes.append(f"【约 {ts} 起】\n{note}")
    # reduce：合并成完整总结
    return chat(
        "以下是一场会议各段的要点笔记（按时间顺序），请合并成完整的中文会议总结，"
        f"Markdown 格式，包含：\n{_SUMMARY_SECTIONS}\n\n" + "\n\n".join(notes),
        timeout=300,
    )


def save_and_summarize(rec: "records.Record | None", entries: list) -> str:
    if not entries:
        if rec is not None:
            rec.discard()                       # 空会议不留孤儿目录
        return "没有录到内容，未生成记录。"

    # 收尾记录：用最终 entries（含译文）重写转录、存 embeddings、补时长
    if rec is not None:
        rec.finalize(entries=entries, summary_status="summarizing")

    full_text = "\n".join(f"[{it['speaker']}] {it['source']}" for it in entries)

    title = ""
    try:                                        # 自动起名失败不致命
        title = auto_title(full_text)
    except requests.RequestException:
        pass

    try:
        summary = summarize_transcript(entries)
    except requests.RequestException as e:
        if rec is not None:
            rec.finalize(entries=entries, title=title, summary_status="failed")
        return (f"记录已保存: {rec.dir if rec else CFG['output_dir']}\n"
                f"总结生成失败: {e}")

    if rec is not None:
        rec.write_summary(f"# {title or '会议总结'}\n\n{summary}\n")
        rec.finalize(entries=entries, title=title, summary_status="done")
        return f"已保存记录: {rec.dir}（{title or '未命名'}）"
    return "已生成总结。"


def _backend_ok() -> bool:
    try:
        list_models(CFG["omlx_url"], CFG["api_key"])
        return True
    except requests.RequestException:
        return False


def _offline_segments(audio: np.ndarray, rate: int):
    """对一整段音频做与实时一致的 VAD 分句，逐段 yield (t_start, t_end, seg)。"""
    blk = int(rate * BLOCK_SECONDS)
    thr = float(CFG.get("silence_threshold_mic", 0.01))
    buf: list[np.ndarray] = []
    preroll: "collections.deque[np.ndarray]" = collections.deque(maxlen=PREROLL_BLOCKS)
    silence, speaking = 0, False
    blocks = [audio[i:i + blk] for i in range(0, len(audio), blk)]
    for bi, b in enumerate(blocks):
        rms = float(np.sqrt(np.mean(b ** 2))) if len(b) else 0.0
        is_sil = rms < thr
        if speaking:
            buf.append(b)
            silence = silence + 1 if is_sil else 0
            total = sum(len(x) for x in buf)
            if silence >= SILENCE_BLOCKS_TO_CUT or total / rate >= MAX_UTTERANCE_SECONDS:
                seg = np.concatenate(buf)
                t_end = (bi + 1) * blk / rate
                dur = len(seg) / rate
                if dur >= MIN_UTTERANCE_SECONDS:
                    yield (max(0.0, t_end - dur), t_end, seg)
                buf, speaking, silence = [], False, 0
        elif not is_sil:
            speaking = True
            buf = list(preroll) + [b]
            silence = 0
        else:
            preroll.append(b)
    if speaking and buf:
        seg = np.concatenate(buf)
        if len(seg) / rate >= MIN_UTTERANCE_SECONDS:
            t_end = len(audio) / rate
            yield (max(0.0, t_end - len(seg) / rate), t_end, seg)


def import_audio_file(path: str) -> str:
    """导入音/视频文件 → 解码 16k → 离线分句 → 识别/区分/翻译/摘要 → 一条记录。"""
    try:
        audio = records.decode_audio_file(path, SAMPLE_RATE)
    except Exception as e:
        return f"解码失败: {e}"
    if len(audio) < SAMPLE_RATE * 0.5:
        return "音频太短或无法解码"

    rec = records.Record(CFG["output_dir"], CFG["translate_mode"], app=f"import:{Path(path).name}")
    rec.write_audio("导入", audio, SAMPLE_RATE)
    diar = SpeakerClusterer(float(CFG.get("diarize_threshold", 0.55)),
                            int(CFG.get("diarize_max_speakers", 8)))
    want_emb = bool(CFG.get("diarize_enabled"))
    ids = itertools.count(1)
    entries: list[dict] = []
    for t_start, t_end, seg in _offline_segments(audio, SAMPLE_RATE):
        try:
            res = asr_transcribe(seg)
        except requests.RequestException:
            continue
        text = apply_terms((res.get("text") or "").strip())
        if not text or is_garbage({"text": text}, "导入"):
            continue
        emb = compute_embedding(seg) if want_emb else None
        speaker = "导入"
        if emb is not None:
            speaker = f"说话人{diar.assign(emb)}"
        e = {"id": next(ids), "t_start": round(t_start, 2), "t_end": round(t_end, 2),
             "speaker": speaker, "lang": detect_lang(text), "source": text,
             "translation": None, "embedding": emb}
        try:
            e["translation"] = translate(text, e["lang"])
        except requests.RequestException:
            e["translation"] = ""
        entries.append(e)
        rec.append_entry(e)

    if not entries:
        rec.discard()
        return "没识别到有效内容"
    full = "\n".join(f"[{x['speaker']}] {x['source']}" for x in entries)
    title = ""
    try:
        title = auto_title(full)
    except requests.RequestException:
        pass
    status = "done"
    try:
        rec.write_summary(f"# {title or '导入'}\n\n{summarize_transcript(entries)}\n")
    except requests.RequestException:
        status = "failed"
    rec.finalize(entries=entries, title=title, summary_status=status)
    return f"已导入: {rec.dir}（{title or Path(path).name}）"


# ---------------------------- GUI ----------------------------
# 配色取自 DESIGN.md（暗色广播字幕间：近黑蓝灰三级底 + 单一薄荷强调色）
BG = "#14151c"        # bg 应用底
PANEL = "#10121a"     # panel 控制台（最深）
COL_BG = "#1b1d26"    # surface 卡片（最浅，靠明度浮起）
FG = "#e7e9f2"        # text 主文字（译文）
DIM = "#a6abbd"       # text-dim 次要（原文/meta）
SRC_FG = "#a6abbd"    # 原文用次要色
MUTED = "#6b7085"     # text-muted 小标签
HAIRLINE = "#2b2e3c"  # 细线/分隔
ACCENT = "#6fd6a8"    # 唯一强调色：录制信号 / 开始键 / 电平
ACCENT_PRESS = "#59c295"
LANG_COLOR = {"zh": "#93e6b0", "en": "#8fb8ff", "ja": "#f2d38a"}
SUB_DST = "#ffffff"
SUB_SRC = "#b9bfd4"
METER_FG = ACCENT
METER_HI = "#f2849c"  # peak 削波警示


def setup_theme(root):
    """ttk 'clam' 暗色主题：把按钮/下拉/列表/滚动条统一成扁平深色，摆脱 macOS 原生浅色控件。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground=COL_BG,
                    bordercolor=HAIRLINE, focuscolor=ACCENT, insertcolor=FG)
    style.configure("TButton", background=COL_BG, foreground=FG, borderwidth=0,
                    relief="flat", padding=(11, 5), font=("PingFang SC", 11))
    style.map("TButton", background=[("active", HAIRLINE), ("pressed", HAIRLINE)],
              foreground=[("disabled", MUTED)])
    style.configure("Accent.TButton", background=ACCENT, foreground=BG, borderwidth=0,
                    relief="flat", padding=(13, 5), font=("PingFang SC", 11, "bold"))
    style.map("Accent.TButton", background=[("active", ACCENT_PRESS), ("pressed", ACCENT_PRESS)])
    style.configure("TCombobox", fieldbackground=COL_BG, background=COL_BG, foreground=FG,
                    arrowcolor=DIM, bordercolor=HAIRLINE, borderwidth=0, padding=3)
    style.map("TCombobox", fieldbackground=[("readonly", COL_BG)],
              foreground=[("readonly", FG)], arrowcolor=[("active", ACCENT)])
    style.configure("Treeview", background=COL_BG, fieldbackground=COL_BG, foreground=FG,
                    borderwidth=0, rowheight=26, font=("PingFang SC", 11))
    style.configure("Treeview.Heading", background=PANEL, foreground=DIM, relief="flat",
                    borderwidth=0, font=("PingFang SC", 10, "bold"))
    style.map("Treeview", background=[("selected", HAIRLINE)], foreground=[("selected", FG)])
    for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(sb, background=PANEL, troughcolor=BG, bordercolor=BG,
                        arrowcolor=DIM, borderwidth=0)
    style.configure("TEntry", fieldbackground=COL_BG, foreground=FG,
                    bordercolor=HAIRLINE, borderwidth=1, padding=4, insertcolor=FG)
    # 经典 tk 控件的默认色（下拉弹出列表 / Entry）
    root.option_add("*TCombobox*Listbox.background", COL_BG)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", HAIRLINE)
    root.option_add("*Entry.background", COL_BG)
    root.option_add("*Entry.foreground", FG)
    root.option_add("*Entry.insertBackground", FG)
    root.option_add("*Entry.relief", "flat")


def _btn(parent, **kw):
    """统一按钮：ttk.Button（扁平深色，见 setup_theme），吞掉 tk 专有 kwargs。"""
    for k in ("highlightbackground", "bg", "fg", "activebackground",
              "activeforeground", "bd", "relief"):
        kw.pop(k, None)
    return ttk.Button(parent, **kw)


def set_click_through(width: int, height: int):
    try:
        from AppKit import NSApplication
        for w in NSApplication.sharedApplication().windows():
            f = w.frame()
            if abs(f.size.width - width) < 2 and abs(f.size.height - height) < 2:
                w.setIgnoresMouseEvents_(True)
    except Exception:
        pass


class TermsWindow(tk.Toplevel):
    """术语表编辑器：每行 `规范术语 | 别名1,别名2 | 指定译文(可空)`。"""
    HELP = ("每行一个术语，竖线分隔：\n规范术语 | 别名1,别名2 | 指定译文(可空)\n"
            "例：比特币 | BTC,比特弊 | Bitcoin")

    def __init__(self, master):
        super().__init__(master)
        self.title("术语表")
        self.configure(bg=BG, padx=12, pady=10)
        self.attributes("-topmost", True)
        tk.Label(self, text=self.HELP, bg=BG, fg=DIM, font=("PingFang SC", 10),
                 justify="left", anchor="w").pack(fill="x", pady=(0, 6))
        self.txt = tk.Text(self, width=52, height=14, bg=COL_BG, fg=FG,
                           insertbackground=FG, font=("PingFang SC", 12))
        self.txt.pack(fill="both", expand=True)
        self.txt.insert("1.0", self._dump())
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        _btn(row, text="保存", command=self.save,
                  highlightbackground=BG).pack(side="right", padx=4)
        _btn(row, text="取消", command=self.destroy,
                  highlightbackground=BG).pack(side="right", padx=4)

    @staticmethod
    def _dump() -> str:
        lines = []
        for t in load_terms():
            lines.append(" | ".join([
                t.get("term", ""),
                ",".join(t.get("aliases", [])),
                t.get("translation", ""),
            ]))
        return "\n".join(lines)

    def save(self):
        terms = []
        for line in self.txt.get("1.0", "end").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            term = parts[0] if parts else ""
            if not term:
                continue
            aliases = ([a.strip() for a in parts[1].split(",")]
                       if len(parts) > 1 and parts[1] else [])
            translation = parts[2] if len(parts) > 2 else ""
            terms.append({"term": term,
                          "aliases": [a for a in aliases if a],
                          "translation": translation})
        save_terms(terms)
        self.destroy()


class OnboardingWindow(tk.Toplevel):
    """首次运行的使用向导：环境检查清单（带一键修复）+ 快速上手。也可从「帮助」重开。"""

    HOWTO = (
        "1. 点左上角「● 开始」开始录音。\n"
        "2. 说话或播放会议/视频声音——中文进左栏、英文进右栏，自动双语，边说边出字。\n"
        "3. 说完点「■ 结束并总结」，自动起标题 + 生成会议纪要（后台跑，不卡）。\n"
        "4. 在「记录库」看历史、播放、导出(md/docx/srt)、给说话人命名。\n"
        "5. 「设置 → 术语表」加人名/黑话，识别更准、译法统一。\n"
        "6. 想更准：在 oMLX 里加载 whisper-large-v3-turbo，再到「设置 → 识别模型」选它。"
    )

    def __init__(self, master):
        super().__init__(master)
        self.title("欢迎 / 使用向导")
        self.configure(bg=BG, padx=22, pady=18)
        self.attributes("-topmost", True)
        tk.Label(self, text="本地实时翻译 · 会议纪要", bg=BG, fg=FG,
                 font=("PingFang SC", 18, "bold"), anchor="w").pack(fill="x")
        tk.Label(self, text="完全离线，音频不出本机 · 中英/中日实时双语 + 自动纪要",
                 bg=BG, fg=DIM, font=("PingFang SC", 12), anchor="w").pack(fill="x", pady=(2, 14))

        tk.Label(self, text="环境检查", bg=BG, fg=MUTED,
                 font=("PingFang SC", 11), anchor="w").pack(fill="x")
        self.checks = tk.Frame(self, bg=BG)
        self.checks.pack(fill="x", pady=(4, 12))

        tk.Label(self, text="怎么用", bg=BG, fg=MUTED,
                 font=("PingFang SC", 11), anchor="w").pack(fill="x", pady=(6, 2))
        tk.Label(self, text=self.HOWTO, bg=COL_BG, fg=FG, font=("PingFang SC", 12),
                 justify="left", anchor="w", padx=14, pady=12, wraplength=520).pack(fill="x")

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(16, 0))
        _btn(row, text="重新检查", command=self.refresh).pack(side="left")
        _btn(row, text="打开设置",
             command=lambda: SettingsWindow(self, on_saved=self.refresh)).pack(side="left", padx=6)
        _btn(row, text="知道了，开始使用", command=self._done,
             style="Accent.TButton").pack(side="right")
        self.refresh()

    def refresh(self):
        for w in self.checks.winfo_children():
            w.destroy()
        try:
            names = list_models(CFG["omlx_url"], CFG["api_key"])
            backend = True
        except requests.RequestException:
            names, backend = [], False
        self._row("oMLX 模型后端在线", backend, "打开 oMLX", self._open_omlx)
        self._row(f"识别模型已加载（{CFG['asr_model']}）",
                  any(CFG["asr_model"] in n for n in names), "去设置", self._settings)
        self._row(f"翻译模型已加载（{CFG['chat_model']}）",
                  any(CFG["chat_model"] in n for n in names), "去设置", self._settings)
        perm = SystemAudioSource.has_permission()
        self._row("屏幕录制权限（抓系统/会议声音）", perm, "去授权", self._request_perm)

    def _row(self, text, ok, fix_label, fix_cmd):
        r = tk.Frame(self.checks, bg=BG)
        r.pack(fill="x", pady=3)
        tk.Label(r, text=("✓" if ok else "✗"), bg=BG, fg=(ACCENT if ok else METER_HI),
                 font=("PingFang SC", 13, "bold"), width=2).pack(side="left")
        tk.Label(r, text=text, bg=BG, fg=FG, font=("PingFang SC", 12),
                 anchor="w").pack(side="left")
        if not ok:
            _btn(r, text=fix_label, command=fix_cmd).pack(side="right")

    def _open_omlx(self):
        import subprocess
        subprocess.run(["open", "-a", "oMLX"])

    def _settings(self):
        SettingsWindow(self, on_saved=self.refresh)

    def _request_perm(self):
        try:
            import Quartz
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass
        import subprocess
        subprocess.run(["open",
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])

    def _done(self):
        CFG["onboarded"] = True
        save_config()
        self.destroy()


class SettingsWindow(tk.Toplevel):
    # choices: 元组=只读下拉；"MODELS"=从 oMLX 已加载模型里选（可编辑）；None=文本框
    FIELDS = [
        ("translate_mode", "翻译方向", ("zh-en", "zh-ja")),
        ("asr_model", "识别模型（oMLX）", "MODELS"),
        ("chat_model", "翻译模型（oMLX）", "MODELS"),
        ("output_dir", "记录保存目录", None),
        ("omlx_url", "后端地址（OpenAI 兼容）", None),
        ("api_key", "API Key", None),
        ("system_fallback_device_keyword", "系统音回退设备关键词", None),
        ("silence_threshold_mic", "麦克风静音阈值", None),
        ("silence_threshold_sys", "媒体静音阈值", None),
    ]

    def __init__(self, master, on_saved):
        super().__init__(master)
        self.on_saved = on_saved
        self.title("设置")
        self.configure(bg=BG, padx=14, pady=10)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.vars: dict[str, tk.StringVar] = {}
        try:                                    # oMLX 已加载的模型，填进识别/翻译下拉
            self.models = list_models(CFG["omlx_url"], CFG["api_key"])
        except requests.RequestException:
            self.models = []

        for row, (key, label, choices) in enumerate(self.FIELDS):
            tk.Label(self, text=label, bg=BG, fg=FG, anchor="w").grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 10))
            var = tk.StringVar(value=str(CFG.get(key, "")))
            self.vars[key] = var
            if choices == "MODELS":              # oMLX 模型下拉（可编辑，离线也能手填）
                ttk.Combobox(self, textvariable=var, values=self.models,
                             state="normal", width=33).grid(row=row, column=1, pady=3)
            elif choices:
                ttk.Combobox(self, textvariable=var, values=choices,
                             state="readonly", width=33).grid(row=row, column=1, pady=3)
            elif key == "output_dir":            # 目录：文本框 + 选择器 + 一键在访达打开（方便找/删录音）
                cell = tk.Frame(self, bg=BG)
                cell.grid(row=row, column=1, pady=3, sticky="w")
                tk.Entry(cell, textvariable=var, width=24).pack(side="left")
                _btn(cell, text="选择…", command=self._pick_dir).pack(side="left", padx=(4, 0))
                _btn(cell, text="打开", command=self._open_dir).pack(side="left", padx=(4, 0))
            else:
                show = "•" if key == "api_key" else ""
                tk.Entry(self, textvariable=var, width=36, show=show).grid(
                    row=row, column=1, pady=3)

        btns = tk.Frame(self, bg=BG)
        btns.grid(row=len(self.FIELDS), column=0, columnspan=2, pady=(10, 0))
        _btn(btns, text="测试连接", command=self.test_conn,
                  highlightbackground=BG).pack(side="left", padx=4)
        _btn(btns, text="术语表", command=lambda: TermsWindow(self),
                  highlightbackground=BG).pack(side="left", padx=4)
        _btn(btns, text="保存", command=self.save,
                  highlightbackground=BG).pack(side="left", padx=4)
        _btn(btns, text="取消", command=self.destroy,
                  highlightbackground=BG).pack(side="left", padx=4)
        tk.Label(self, text=f"配置文件: {CONFIG_PATH}", bg=BG, fg=DIM,
                 font=("PingFang SC", 9)).grid(
            row=len(self.FIELDS) + 1, column=0, columnspan=2, pady=(8, 0))

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self, title="选择记录保存目录",
                                    initialdir=self.vars["output_dir"].get() or None)
        if d:
            self.vars["output_dir"].set(d)

    def _open_dir(self):
        import subprocess
        d = self.vars["output_dir"].get().strip()
        if not d:
            return
        Path(d).mkdir(parents=True, exist_ok=True)   # 还没建过也能直接打开
        subprocess.run(["open", d])

    def test_conn(self):
        url = self.vars["omlx_url"].get().rstrip("/")
        try:
            names = list_models(url, self.vars["api_key"].get())
        except requests.RequestException as e:
            messagebox.showerror("连接失败", str(e), parent=self)
            return
        missing = [self.vars[k].get() for k in ("asr_model", "chat_model")
                   if not any(self.vars[k].get() in n for n in names)]
        msg = "连接成功，oMLX 已加载:\n" + "\n".join(f"  • {n}" for n in names)
        if missing:
            msg += f"\n\n⚠️ oMLX 未加载: {', '.join(missing)}\n（在 oMLX.app 里加载它，或选一个上面已有的）"
        messagebox.showinfo("测试连接", msg, parent=self)

    def save(self):
        for key, var in self.vars.items():
            val: str | float = var.get().strip()
            if key.startswith("silence_threshold"):
                try:
                    val = float(val)
                except ValueError:
                    messagebox.showerror("无效值", f"阈值必须是数字: {val}", parent=self)
                    return
            elif key == "omlx_url":
                val = val.rstrip("/")
            CFG[key] = val
        # 阈值同步到运行态
        SRC[MIC_LABEL]["threshold"] = float(CFG["silence_threshold_mic"])
        SRC[SYS_LABEL]["threshold"] = float(CFG["silence_threshold_sys"])
        save_config()
        self.on_saved()
        self.destroy()


class SpeakerDialog(tk.Toplevel):
    """给一条记录里的本地说话人「建议 + 确认」真名；确认后并入全局声纹并改写记录。"""

    def __init__(self, master, rec_meta: dict, on_done=None):
        super().__init__(master)
        self.rec = rec_meta
        self.on_done = on_done
        self.title("说话人命名")
        self.configure(bg=BG, padx=12, pady=10)
        self.attributes("-topmost", True)
        self.store = records.SpeakerStore(CFG["speakers_store"])
        thr = float(CFG.get("diarize_global_threshold", 0.70))
        groups = records.speaker_embeddings(rec_meta["_dir"])
        self.rows = []
        if not groups:
            tk.Label(self, text="这条记录没有说话人向量\n（仅媒体源 + 开启区分时才有）。",
                     bg=BG, fg=DIM, justify="left").pack()
        for local_id, vecs in groups.items():
            rep = np.mean(np.asarray(vecs, dtype=np.float32), axis=0)
            sug = self.store.match(rep, thr) if len(vecs) >= 2 else None
            row = tk.Frame(self, bg=BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=local_id, bg=BG, fg=FG, width=15, anchor="w").pack(side="left")
            hint = (f"建议:{sug['name']}({sug['sim']:.2f})" if sug else f"{len(vecs)}句")
            tk.Label(row, text=hint, bg=BG, fg=DIM, width=16, anchor="w").pack(side="left")
            var = tk.StringVar(value=sug["name"] if sug else "")
            tk.Entry(row, textvariable=var, width=12).pack(side="left")
            self.rows.append((local_id, vecs, var))
        b = tk.Frame(self, bg=BG)
        b.pack(fill="x", pady=(8, 0))
        _btn(b, text="应用", command=self.apply,
                  highlightbackground=BG).pack(side="right", padx=4)
        _btn(b, text="取消", command=self.destroy,
                  highlightbackground=BG).pack(side="right", padx=4)

    def apply(self):
        for local_id, vecs, var in self.rows:
            name = var.get().strip()
            if not name or name == local_id:
                continue
            self.store.assign(name, vecs)                          # 并入全局声纹
            records.rename_speaker(self.rec["_dir"], local_id, name)  # 改写本记录
        if self.on_done:
            self.on_done()
        self.destroy()


class LibraryWindow(tk.Toplevel):
    """记录库：列表 + 查看 + 播放 / 重命名 / 重生成 / 导出 / 说话人 / 删除。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("记录库")
        self.configure(bg=BG)
        self.geometry("1040x640")
        self.attributes("-topmost", True)
        self.records: list[dict] = []
        self.current: dict | None = None
        self._rows: list = []

        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="搜索", bg=BG, fg=DIM).pack(side="left")
        self.q = tk.StringVar()
        ent = tk.Entry(top, textvariable=self.q, width=26)
        ent.pack(side="left", padx=6)
        ent.bind("<KeyRelease>", lambda e: self.refresh())
        _btn(top, text="刷新", command=self.reload,
                  highlightbackground=BG).pack(side="left")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y")
        self.tree = ttk.Treeview(left, columns=("date", "dur", "spk"),
                                 show="tree headings", height=28)
        self.tree.heading("#0", text="标题")
        self.tree.column("#0", width=230)
        self.tree.heading("date", text="时间")
        self.tree.column("date", width=130)
        self.tree.heading("dur", text="时长")
        self.tree.column("dur", width=56, anchor="e")
        self.tree.heading("spk", text="人")
        self.tree.column("spk", width=34, anchor="e")
        self.tree.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        btns = tk.Frame(right, bg=BG)
        btns.pack(fill="x")
        for txt, cmd in (("播放", self.play), ("重命名", self.rename),
                         ("重新生成摘要", self.resummarize), ("说话人", self.speakers),
                         ("导出md", lambda: self.export("md")),
                         ("导出docx", lambda: self.export("docx")),
                         ("导出srt", lambda: self.export("srt")), ("删除", self.delete)):
            _btn(btns, text=txt, command=cmd,
                      highlightbackground=BG).pack(side="left", padx=2)
        self.detail = tk.Text(right, bg=COL_BG, fg=FG, wrap="word",
                              font=("PingFang SC", 12), state="disabled")
        self.detail.pack(fill="both", expand=True, pady=(6, 0))
        self.reload()

    def reload(self):
        self.records = records.list_records(CFG["output_dir"])
        self.refresh()

    def refresh(self):
        q = self.q.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._rows = []
        for m in self.records:
            title = m.get("title") or m.get("id")
            if q and q not in (title + m.get("created_at", "")).lower():
                if not self._match_body(m, q):
                    continue
            iid = self.tree.insert(
                "", "end", text=title,
                values=(m.get("created_at", "")[:16].replace("T", " "),
                        f"{int(m.get('duration_s', 0))}s",
                        str(len(m.get("speakers", [])) or "")))
            self._rows.append((iid, m))

    @staticmethod
    def _match_body(m, q) -> bool:
        for e in records.load_transcript(m["_dir"]):
            if q in (e.get("source", "") + (e.get("translation") or "")).lower():
                return True
        return False

    def _on_select(self):
        sel = self.tree.selection()
        self.current = next((m for i, m in self._rows if i == sel[0]), None) if sel else None
        self._render()

    def _render(self):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if self.current:
            d = Path(self.current["_dir"])
            sm = d / "summary.md"
            if sm.exists():
                self.detail.insert("end", sm.read_text(encoding="utf-8") + "\n\n———— 转录 ————\n\n")
            for e in records.load_transcript(d):
                self.detail.insert("end", f"[{e.get('speaker', '')}] {e.get('source', '')}\n")
                if e.get("translation"):
                    self.detail.insert("end", f"  → {e['translation']}\n")
        self.detail.configure(state="disabled")

    def play(self):
        if not self.current or not self.current.get("sources"):
            return
        import subprocess
        subprocess.Popen(["afplay", str(Path(self.current["_dir"]) / self.current["sources"][0]["wav"])])

    def rename(self):
        if not self.current:
            return
        new = simpledialog.askstring("重命名", "新标题：",
                                     initialvalue=self.current.get("title", ""), parent=self)
        if new:
            mp = Path(self.current["_dir"]) / "meta.json"
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
                meta["title"] = new
                mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                return
            self.reload()

    def delete(self):
        if not self.current:
            return
        if messagebox.askyesno("删除", "确认删除这条记录（含音频）？不可恢复。", parent=self):
            import shutil
            shutil.rmtree(self.current["_dir"], ignore_errors=True)
            self.current = None
            self.reload()
            self._render()

    def resummarize(self):
        if not self.current:
            return
        if not _backend_ok():
            messagebox.showwarning("后端未启动", "重新生成摘要需要 oMLX 后端在线。", parent=self)
            return
        d = self.current["_dir"]
        tr = records.load_transcript(d)
        try:
            s = summarize_transcript(tr)
        except requests.RequestException as e:
            messagebox.showerror("失败", str(e), parent=self)
            return
        title = self.current.get("title") or self.current["id"]
        (Path(d) / "summary.md").write_text(f"# {title}\n\n{s}\n", encoding="utf-8")
        self._render()

    def export(self, fmt: str):
        if not self.current:
            return
        d = Path(self.current["_dir"])
        tr = records.load_transcript(d)
        summ = (d / "summary.md").read_text(encoding="utf-8") if (d / "summary.md").exists() else ""
        out = d / f"export.{fmt}"
        try:
            if fmt == "md":
                out.write_text(records.to_markdown(self.current, tr, summ), encoding="utf-8")
            elif fmt == "srt":
                out.write_text(records.to_srt(tr), encoding="utf-8")
            elif fmt == "docx":
                records.to_docx(self.current, tr, summ, out)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self)
            return
        import subprocess
        subprocess.Popen(["open", "-R", str(out)])

    def speakers(self):
        if self.current:
            SpeakerDialog(self, self.current, on_done=self._render)


class App:
    def __init__(self, root: tk.Tk, sys_source: "SystemAudioSource | None"):
        self.root = root
        self.sys_source = sys_source
        self.columns: dict[str, tk.Text] = {}
        self.col_frames: dict[str, tk.Frame] = {}   # 语种半栏外框（全屏切换用）
        self._fullscreen: str | None = None          # None=并排；否则=当前全屏的语种
        self._col_filled: set[str] = set()   # 已有真实内容的语言栏（用于清占位提示）
        self.expanded_geometry = "1120x600+160+70"
        self.overlay: tk.Toplevel | None = None
        self.ctrl: tk.Toplevel | None = None
        self.ov_dst = self.ov_src = None
        self.latest_source = ""
        self.meters: dict[str, tk.Canvas] = {}

        root.title("实时翻译")
        root.configure(bg=BG)
        setup_theme(root)
        root.attributes("-topmost", True)
        root.geometry(self.expanded_geometry)
        root.protocol("WM_DELETE_WINDOW", self.finish)

        # 字号阶梯（见 DESIGN.md）：译文最亮加粗，原文次要，meta/caption 更小更暗
        self.f_title = tkfont.Font(family="PingFang SC", size=13, weight="bold")
        self.f_src = tkfont.Font(family="PingFang SC", size=15)
        self.f_dst = tkfont.Font(family="PingFang SC", size=15, weight="bold")
        self.f_meta = tkfont.Font(family="PingFang SC", size=11)
        self.f_small = tkfont.Font(family="PingFang SC", size=11)
        self.f_cap = tkfont.Font(family="PingFang SC", size=10)   # caption：控件小标签

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=14, pady=(10, 8))
        self.session_btn = _btn(top, text="● 开始", command=self.toggle_session,
                                style="Accent.TButton")
        self.session_btn.pack(side="left", padx=(0, 10))
        # live-dot（DESIGN.md §Components）：录制中显示一个呼吸的 accent 圆点，空闲隐藏
        self.live_dot = tk.Canvas(top, width=14, height=14, bg=BG, highlightthickness=0)
        self.live_dot.pack(side="left", padx=(0, 8))
        self._dot_tick = 0
        self.status = tk.Label(top, text="", bg=BG, fg=DIM, font=self.f_meta, anchor="w")
        self.status.pack(side="left")
        self.refresh_status()
        for txt, cmd in (("帮助", self.open_help), ("字幕模式", self.enter_compact),
                         ("设置", self.open_settings), ("记录库", self.open_library),
                         ("导入文件", self.import_file)):
            _btn(top, text=txt, command=cmd).pack(side="right", padx=3)
        tk.Frame(root, bg=HAIRLINE, height=1).pack(fill="x")   # 顶栏细分隔线

        main = tk.Frame(root, bg=BG)
        main.pack(fill="both", expand=True, padx=14, pady=12)

        self.panel = tk.Frame(main, bg=PANEL, width=216)
        self.panel.pack(side="left", fill="y", padx=(0, 12))
        self.panel.pack_propagate(False)
        self._build_control_panel()

        self.body = tk.Frame(main, bg=BG)
        self.body.pack(side="left", fill="both", expand=True)
        # 实时临时稿：说话中先把识别到的原文流出来，定稿后清空
        self.live_uid = None
        self.live = tk.Label(self.body, text="", bg=COL_BG, fg=DIM, font=self.f_small,
                             anchor="w", justify="left", wraplength=820, padx=14, pady=8)
        self.live.pack(fill="x", pady=(0, 8))
        self._set_live_idle()
        self.cols = tk.Frame(self.body, bg=BG)
        self.cols.pack(fill="both", expand=True)
        langs = ["zh", "en"] if CFG["translate_mode"] == "zh-en" else ["zh", "ja"]
        for lang in langs:
            self._make_column(lang)

        root.after(100, self.poll)
        if not CFG.get("onboarded"):            # 首次运行弹使用向导
            root.after(500, lambda: OnboardingWindow(self.root))

    # ---- 控制台：每路音源 开关/增益/电平 ----

    def _build_control_panel(self):
        # 侧栏区块标题：安静的 muted caption，不与内容抢注意（DESIGN：chrome 不抢眼）
        tk.Label(self.panel, text="音源控制台", bg=PANEL, fg=MUTED,
                 font=self.f_cap, anchor="w").pack(fill="x", padx=12, pady=(14, 6))

        for label, cfg_en, cfg_gain in (
            (MIC_LABEL, "mic_enabled", "mic_gain"),
            (SYS_LABEL, "system_enabled", "system_gain"),
        ):
            box = tk.Frame(self.panel, bg=PANEL)
            box.pack(fill="x", padx=12, pady=8)

            en_var = tk.BooleanVar(value=bool(CFG[cfg_en]))
            SRC[label]["enabled"] = en_var.get()

            def on_toggle(lbl=label, var=en_var, key=cfg_en):
                SRC[lbl]["enabled"] = var.get()
                CFG[key] = var.get()
                save_config()

            row = tk.Frame(box, bg=PANEL)
            row.pack(fill="x")
            cb = tk.Checkbutton(row, text=label, variable=en_var, command=on_toggle,
                                bg=PANEL, fg=FG, selectcolor=PANEL,
                                activebackground=PANEL, activeforeground=FG,
                                font=self.f_small, highlightthickness=0, bd=0)
            cb.pack(side="left")
            avail = tk.Label(row, text="", bg=PANEL, fg=MUTED, font=self.f_cap)
            avail.pack(side="right")
            SRC[label]["_avail_label"] = avail

            meter = tk.Canvas(box, height=6, bg=HAIRLINE, highlightthickness=0)
            meter.pack(fill="x", pady=(8, 4))
            self.meters[label] = meter

            grow = tk.Frame(box, bg=PANEL)
            grow.pack(fill="x")
            tk.Label(grow, text="音量", bg=PANEL, fg=MUTED, font=self.f_cap).pack(side="left")
            gain_var = tk.DoubleVar(value=float(CFG[cfg_gain]) * 100)
            SRC[label]["gain"] = float(CFG[cfg_gain])

            def on_gain(val, lbl=label, key=cfg_gain):
                g = float(val) / 100
                SRC[lbl]["gain"] = g
                CFG[key] = g
            sc = tk.Scale(grow, from_=0, to=300, orient="horizontal", variable=gain_var,
                          command=on_gain, bg=PANEL, fg=DIM, troughcolor=HAIRLINE,
                          highlightthickness=0, bd=0, showvalue=True, length=120,
                          font=("PingFang SC", 8))
            sc.pack(side="right", fill="x", expand=True)
            sc.bind("<ButtonRelease-1>", lambda e: save_config())

            # 媒体源支持「按应用挑声音」（仅 ScreenCaptureKit 模式）
            if label == SYS_LABEL and self.sys_source is not None:
                self._build_app_picker(box)

        # 系统音频权限提示按钮（无权限时显示）
        self.perm_frame = tk.Frame(self.panel, bg=PANEL)
        if self.sys_source and self.sys_source.needs_permission:
            self.perm_frame.pack(fill="x", padx=12, pady=(8, 4))
            tk.Label(self.perm_frame, text="⚠️ 媒体声音需要\n屏幕录制权限", bg=PANEL,
                     fg=FG, font=self.f_cap, justify="left").pack(fill="x")
            _btn(self.perm_frame, text="去授权",
                 command=self.request_permission).pack(fill="x", pady=(6, 0))

    def _app_choices(self) -> list[str]:
        return [ALL_APPS_LABEL] + [n for n, _ in AVAILABLE_APPS]

    def _build_app_picker(self, box):
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="来源", bg=PANEL, fg=MUTED,
                 font=self.f_cap).pack(side="left")
        cur_bid = (CFG.get("system_capture_app") or "").strip()
        cur_name = ALL_APPS_LABEL
        for n, b in AVAILABLE_APPS:
            if cur_bid and b == cur_bid:
                cur_name = n
                break
        self.app_var = tk.StringVar(value=cur_name)
        cmb = ttk.Combobox(row, textvariable=self.app_var, values=self._app_choices(),
                           state="readonly", width=15, font=self.f_cap)
        cmb.pack(side="right", fill="x", expand=True)
        # 下拉时刷新应用列表（应用可能中途启动/退出）
        cmb.configure(postcommand=lambda: cmb.configure(values=self._app_choices()))
        cmb.bind("<<ComboboxSelected>>", self._on_pick_app)
        self.app_combo = cmb

        # 区分说话人开关（仅对媒体源、SenseVoice 后端生效）
        dia_var = tk.BooleanVar(value=bool(CFG.get("diarize_enabled")))

        def on_dia(var=dia_var):
            CFG["diarize_enabled"] = var.get()
            save_config()
        tk.Checkbutton(box, text="区分说话人", variable=dia_var, command=on_dia,
                       bg=PANEL, fg=MUTED, selectcolor=PANEL, activebackground=PANEL,
                       activeforeground=FG, font=self.f_cap,
                       highlightthickness=0, bd=0).pack(fill="x", pady=(8, 0))

    def _on_pick_app(self, _evt=None):
        name = self.app_var.get()
        bid = ""
        if name != ALL_APPS_LABEL:
            for n, b in AVAILABLE_APPS:
                if n == name:
                    bid = b
                    break
        CFG["system_capture_app"] = bid
        save_config()
        self.status.configure(text=f"⏳ 切换媒体来源到「{name}」…")
        self.root.update_idletasks()
        if self.sys_source is not None:
            self.sys_source.restart()   # 主线程重建 SCStream
        self.refresh_status()

    def request_permission(self):
        try:
            import Quartz
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass
        import subprocess
        subprocess.run(["open",
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
        messagebox.showinfo(
            "授权屏幕录制",
            "请在系统设置里勾选你的终端/本程序，然后【完全退出并重新运行】本程序，"
            "即可捕获媒体声音。", parent=self.root)

    def refresh_status(self):
        mode_desc = "中⇄英" if CFG["translate_mode"] == "zh-en" else "中⇄日"
        self.status.configure(
            text=f"🎙️ {mode_desc}　识别:{CFG['asr_model']}　翻译:{CFG['chat_model']}", fg=DIM)

    def open_settings(self):
        SettingsWindow(self.root, on_saved=self.refresh_status)

    def open_help(self):
        OnboardingWindow(self.root)

    def open_library(self):
        LibraryWindow(self.root)

    def import_file(self):
        path = filedialog.askopenfilename(
            title="导入音/视频文件",
            filetypes=[("音视频", "*.mp3 *.m4a *.wav *.aac *.mp4 *.mov *.m4v *.flac *.ogg"),
                       ("全部", "*.*")])
        if not path:
            return
        self.status.configure(text=f"⏳ 正在导入 {Path(path).name} …（识别+翻译，耐心等）")

        def work():
            ui_queue.put(("status", {"text": import_audio_file(path)}))
        threading.Thread(target=work, daemon=True).start()

    def _make_column(self, lang: str):
        frame = tk.Frame(self.cols, bg=COL_BG, bd=0)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.col_frames[lang] = frame
        header = tk.Frame(frame, bg=COL_BG)
        header.pack(fill="x", padx=12, pady=(10, 6))
        # 语言胶囊：语言色文字 + 更暗底（DESIGN: 标题做成小胶囊）
        tk.Label(header, text=LANG_NAMES[lang], bg=HAIRLINE, fg=LANG_COLOR[lang],
                 font=self.f_title).pack(side="left", ipadx=10, ipady=3)
        # 全屏切换：只看这一半 / 恢复并排
        _btn(header, text="⤢", width=3,
             command=lambda l=lang: self.toggle_fullscreen(l)).pack(side="right")
        tk.Frame(frame, bg=HAIRLINE, height=1).pack(fill="x", padx=12)
        # width=1：别让 Text 默认 80 字符的请求宽度撑爆布局，否则两栏会争空间、把「英文」栏挤没
        txt = tk.Text(frame, bg=COL_BG, fg=FG, bd=0, highlightthickness=0,
                      wrap="word", font=self.f_src, padx=14, pady=8, cursor="arrow",
                      width=1, spacing1=2, spacing3=8)
        sb = ttk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.tag_configure("meta", foreground=MUTED, font=self.f_meta)
        txt.tag_configure("src", foreground=SRC_FG, font=self.f_src)
        # 译文按「目标语言」着色，而不是本栏语言：中文栏里的英译显蓝、英文栏里的中译显绿，两种语言始终区分得开
        txt.tag_configure("dst", foreground=LANG_COLOR[self._target_lang(lang)], font=self.f_dst)
        txt.tag_configure("pending", foreground=MUTED, font=self.f_meta)
        txt.tag_configure("ph", foreground=MUTED, font=self.f_small, justify="center")
        self.columns[lang] = txt
        self._set_placeholder(lang)   # 空栏显示「XX发言会出现在这里，自动翻成YY」

    def toggle_fullscreen(self, lang: str):
        """点半栏的 ⤢：只看这一半（另一半收起）；再点恢复并排。"""
        if self._fullscreen == lang:                 # 恢复并排
            self._fullscreen = None
            for fr in self.col_frames.values():
                fr.pack_forget()
            for fr in self.col_frames.values():      # dict 有序：按创建顺序重排
                fr.pack(side="left", fill="both", expand=True, padx=(0, 8))
        else:                                        # 只留本半栏
            self._fullscreen = lang
            for l, fr in self.col_frames.items():
                if l != lang:
                    fr.pack_forget()

    def _target_lang(self, lang: str) -> str:
        """本栏原文语言 → 译文语言（zh→en/ja，其余→zh）。"""
        if lang == "zh":
            return "en" if CFG["translate_mode"] == "zh-en" else "ja"
        return "zh"

    def _target_name(self, lang: str) -> str:
        return LANG_NAMES[self._target_lang(lang)]

    def _set_placeholder(self, lang: str):
        txt = self.columns.get(lang)
        if txt is None:
            return
        self._col_filled.discard(lang)
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("1.0",
                   f"\n\n{LANG_NAMES[lang]}发言会出现在这里，\n自动翻成{self._target_name(lang)}。\n",
                   "ph")
        txt.configure(state="disabled")

    def poll(self):
        try:
            while True:
                event, data = ui_queue.get_nowait()
                if event == "source":
                    self.on_source(data)
                elif event == "interim":
                    self.on_interim(data)
                elif event == "interim_clear":
                    self.on_interim_clear(data)
                elif event == "translation_partial":
                    self.on_translation(data, partial=True)
                elif event == "translation":
                    self.on_translation(data)
                elif event == "status":
                    self.status.configure(text=data["text"])
        except queue.Empty:
            pass
        self._update_meters()
        self._pulse_dot()
        self.root.after(100, self.poll)

    def _pulse_dot(self):
        """录制中：accent 圆点缓慢呼吸（半径+明度双相，每 ~700ms 一拍）；空闲清空。"""
        c = self.live_dot
        c.delete("all")
        if not session_active.is_set():
            self._dot_tick = 0
            return
        self._dot_tick += 1
        bright = (self._dot_tick // 7) % 2 == 0      # 100ms/tick → 每 7 拍换相
        color, r = (ACCENT, 5) if bright else (ACCENT_PRESS, 4)
        c.create_oval(7 - r, 7 - r, 7 + r, 7 + r, fill=color, width=0)

    def _update_meters(self):
        for label, canvas in self.meters.items():
            w = canvas.winfo_width() or 1
            h = canvas.winfo_height() or 6
            lvl = min(1.0, SRC[label]["level"] / 0.15)   # 0.15 RMS 视作满格
            canvas.delete("all")
            if not SRC[label]["enabled"]:
                continue
            fill = METER_HI if lvl > 0.9 else METER_FG
            canvas.create_rectangle(0, 0, int(w * lvl), h, fill=fill, width=0)
            av = SRC[label].get("_avail_label")
            if av is not None:
                av.configure(text="" if SRC[label]["available"] else "未连接")

    def _column_for(self, lang: str) -> tk.Text:
        if lang not in self.columns:
            self._make_column(lang)
        return self.columns[lang]

    def _set_live_idle(self):
        """无人说话时，实时行显示一句安静的占位，让这条 surface 带读成「待命的直播条」而非空框。"""
        self.live.configure(text="待识别的语音会先显示在这里…", fg=MUTED)

    def on_interim(self, e: dict):
        """说话中的临时识别稿：流在实时行，定稿后被清掉。"""
        self.live_uid = e["id"]
        self.live.configure(text=f"✏️ [{e['speaker']}] {e['source']}", fg=DIM)
        if self.overlay:
            self.ov_src.configure(text=f"[{e['speaker']}] {e['source']}")
            self.ov_dst.configure(text="⏳")

    def on_interim_clear(self, e: dict):
        if self.live_uid == e["id"]:
            self._set_live_idle()
            self.live_uid = None

    def on_source(self, e: dict):
        if self.live_uid == e["id"]:        # 定稿到了，清掉实时临时稿
            self._set_live_idle()
            self.live_uid = None
        txt = self._column_for(e["lang"])
        if e["lang"] not in self._col_filled:      # 首条真实内容：清掉占位提示
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.configure(state="disabled")
            self._col_filled.add(e["lang"])
        txt.configure(state="normal")
        txt.insert("end", f"{e['time'][:5]}  {e['speaker']}\n", "meta")
        txt.insert("end", f"{e['source']}\n", "src")
        txt.insert("end", "翻译中…\n\n", ("pending", f"tr{e['id']}"))
        txt.configure(state="disabled")
        txt.see("end")
        self.latest_source = e["source"]
        if self.overlay:
            self.ov_src.configure(text=f"[{e['speaker']}] {e['source']}")
            self.ov_dst.configure(text="⏳")

    def on_translation(self, e: dict, partial: bool = False):
        txt = self._column_for(e["lang"])
        tag = f"tr{e['id']}"
        rng = txt.tag_ranges(tag)
        if rng:
            txt.configure(state="normal")
            txt.delete(rng[0], rng[1])
            txt.insert(rng[0], f"{e['translation']}\n\n", ("dst", tag))
            txt.configure(state="disabled")
            txt.see("end")
        if self.overlay and self.latest_source == e["source"]:
            self.ov_dst.configure(text=e["translation"])

    # ---- 字幕模式 ----

    def enter_compact(self):
        if self.overlay:
            return
        self.expanded_geometry = self.root.geometry()
        self.root.withdraw()

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        ow, oh = min(960, sw - 240), 104
        ox, oy = (sw - ow) // 2, sh - oh - 70

        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.attributes("-alpha", 0.82)
        ov.configure(bg=BG)
        ov.geometry(f"{ow}x{oh}+{ox}+{oy}")
        f_big = tkfont.Font(family="PingFang SC", size=22, weight="bold")
        f_small = tkfont.Font(family="PingFang SC", size=12)
        self.ov_dst = tk.Label(ov, text="等待语音…", bg=BG, fg=SUB_DST, font=f_big,
                               anchor="w", justify="left", wraplength=ow - 24)
        self.ov_dst.pack(fill="x", padx=12, pady=(10, 0))
        self.ov_src = tk.Label(ov, text="", bg=BG, fg=SUB_SRC, font=f_small,
                               anchor="w", justify="left", wraplength=ow - 24)
        self.ov_src.pack(fill="x", padx=12, pady=(0, 8))
        self.overlay = ov

        ct = tk.Toplevel(self.root)
        ct.overrideredirect(True)
        ct.attributes("-topmost", True)
        ct.configure(bg=BG)
        ct.geometry(f"64x{oh}+{ox + ow + 8}+{oy}")
        _btn(ct, text="展开", command=self.exit_compact,
                  highlightbackground=BG).pack(fill="x", padx=4, pady=(8, 2))
        _btn(ct, text="结束", command=self.finish,
                  highlightbackground=BG).pack(fill="x", padx=4)
        self.ctrl = ct

        ov.update_idletasks()
        ov.update()
        set_click_through(ow, oh)

    def exit_compact(self):
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None
        if self.ctrl:
            self.ctrl.destroy()
            self.ctrl = None
        self.root.deiconify()
        self.root.geometry(self.expanded_geometry)

    # ---- 会议生命周期：开始 / 结束 ----

    def toggle_session(self):
        self.end_session() if session_active.is_set() else self.start_session()

    def start_session(self):
        global current_record
        try:                                    # 丢掉空闲期堆积的音频，避免混入开始前的声音
            while True:
                record_queue.get_nowait()
        except queue.Empty:
            pass
        transcript.clear()
        _diarizers.clear()
        self.live_uid = None
        self._set_live_idle()
        for lang in list(self.columns):
            self._set_placeholder(lang)          # 回到「XX发言会出现在这里」占位
        current_record = records.Record(CFG["output_dir"], CFG["translate_mode"],
                                        app=CFG.get("system_capture_app", ""))
        session_active.set()
        self.session_btn.configure(text="■ 结束并总结")
        self.status.configure(text="录制中", fg=FG)   # 「live」信号交给呼吸圆点，文字不再重复绿点

    def end_session(self):
        global current_record
        if not session_active.is_set():
            return
        session_active.clear()
        rec, entries = current_record, transcript[:]   # 快照后清空，下一场可立即开始
        current_record = None
        transcript.clear()
        self.session_btn.configure(text="● 开始")
        self.status.configure(text="⏳ 生成纪要中…（完成后可在记录库查看；现在就能开下一场）", fg=DIM)
        threading.Thread(target=self._finalize_session,
                         args=(rec, entries), daemon=True).start()

    def _finalize_session(self, rec, entries):
        ui_queue.put(("status", {"text": save_and_summarize(rec, entries)}))

    def finish(self):
        # 关窗退出：进行中的会议先同步收尾，确保存盘
        if session_active.is_set():
            session_active.clear()
            self.status.configure(text="⏳ 正在保存并生成纪要…")
            self.root.update()
            print(save_and_summarize(current_record, transcript[:]))
        stop_flag.set()
        self.root.destroy()


# ---------------------------- 启动 ----------------------------

def ensure_models():
    """缺模型就下载；已存在则跳过。ASR 模型（SenseVoice/Whisper）由 oMLX 管理，gemma 由
    mlx_lm.server 按需下；这里只管进程内用的 CAM++ 说话人模型。"""
    if CFG.get("diarize_enabled") and not Path(CFG["speaker_model"]).exists():
        try:
            import urllib.request
            url = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                   "speaker-recongition-models/"
                   "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")
            dest = Path(CFG["speaker_model"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            print("⬇️  首次运行：下载说话人模型（28MB）…")
            urllib.request.urlretrieve(url, str(dest))
            print("✓ 说话人模型就绪")
        except Exception as e:
            print(f"⚠️  说话人模型下载失败（将不区分说话人）: {e}", file=sys.stderr)


def setup_llm_backend():
    """自包含 LLM：优先用已就绪的外部后端（如开发机在跑的 oMLX，避免重复加载抢显存）；
    否则用子进程起 mlx_lm.server。返回后才有可用的翻译/摘要后端。"""
    global _llm_server
    if not CFG.get("self_host_llm"):
        return
    try:                                   # 外部后端已就绪？直接用
        names = list_models(CFG["omlx_url"], CFG["api_key"])
        if any(CFG["chat_model"] in n for n in names):
            return
    except requests.RequestException:
        pass
    print("ℹ️  未发现可用外部 LLM 后端，启动自托管 mlx_lm.server（首次会下载模型）…")
    _llm_server = ManagedLLMServer(CFG["llm_model_repo"], CFG["llm_port"])
    if _llm_server.start():
        CFG["omlx_url"] = _llm_server.url
        CFG["chat_model"] = CFG["llm_model_repo"]
        print(f"✓ 自托管 LLM 已就绪: {_llm_server.url}")
    else:
        _llm_server = None
        print("⚠️  自托管 LLM 启动失败，翻译/摘要将不可用。")


def check_backend():
    try:
        names = list_models(CFG["omlx_url"], CFG["api_key"])
    except requests.RequestException:
        if _llm_server is not None:        # 自托管刚起、模型可能还在懒加载，不致命
            print("ℹ️  LLM 后端启动中，首次翻译会稍慢。")
            return
        print(f"❌ 无法连接后端（{CFG['omlx_url']}）。把配置 self_host_llm 设为 true 可自托管，"
              f"或先启动 oMLX。配置: {CONFIG_PATH}")
        sys.exit(1)
    # 自托管时模型懒加载、可能暂不出现在列表里，不强校验模型名
    if _llm_server is None and not any(CFG["chat_model"] in n for n in names):
        print(f"⚠️  未找到翻译模型 \"{CFG['chat_model']}\"，后端当前加载: {names}\n"
              f"    可在应用内「设置」或配置文件中修改: {CONFIG_PATH}")
        sys.exit(1)

    # ASR 模型检查：全走 oMLX，识别模型必须在 oMLX 已加载列表里
    if not any(CFG["asr_model"] in n for n in names):
        print(f"⚠️  未找到识别模型 \"{CFG['asr_model']}\"，oMLX 当前加载: {names}\n"
              f"    在 oMLX.app 里加载该模型，或在应用「设置」里选一个已加载的。")
        sys.exit(1)


def main():
    load_config()
    ensure_models()
    setup_llm_backend()
    check_backend()

    SRC[MIC_LABEL]["threshold"] = float(CFG["silence_threshold_mic"])
    SRC[SYS_LABEL]["threshold"] = float(CFG["silence_threshold_sys"])

    # 先建 Tk root，确立 TKApplication 为共享 NSApplication。
    # 必须在任何 PyObjC（Quartz/ScreenCaptureKit）访问 NSApplication 之前，
    # 否则 Tk 解析颜色时调 [NSApplication macOSVersion] 会崩溃。
    root = tk.Tk()
    root.withdraw()

    # 启动即空闲（不自动录音），点「开始」才建记录。音频写盘线程常驻，靠 current_record 是否为 None 决定是否落盘。
    try:
        moved = records.migrate_legacy_records(_OLD_OUTPUT_DIR, CFG["output_dir"])
        if moved:
            print(f"ℹ️  已把 {moved} 条旧记录迁入新的记录库位置")
        orphans = records.find_orphans(CFG["output_dir"])
        if orphans:
            print(f"ℹ️  发现 {len(orphans)} 个上次未正常结束的记录（可在记录库里恢复/重生成）")
    except Exception:
        pass
    threading.Thread(target=records.audio_writer_worker,
                     args=(record_queue, stop_flag, lambda: current_record),
                     daemon=True).start()

    sources: list = []

    # 麦克风
    mic = MicSource(MIC_LABEL, None)
    mic.start()
    sources.append(mic)

    # 媒体/系统声音：优先 ScreenCaptureKit；无权限则尝试回环输入设备
    sys_source = SystemAudioSource(SYS_LABEL)
    try:
        sys_source.start()
    except Exception as e:
        print(f"⚠️  系统音频初始化异常: {e}", file=sys.stderr)
        sys_source.needs_permission = True
    if sys_source.needs_permission:
        kw = CFG["system_fallback_device_keyword"]
        dev = find_device(kw) if kw else None
        if dev is not None:
            print(f"ℹ️  无屏幕录制权限，改用输入设备「{kw}」捕获媒体声音")
            fallback = MicSource(SYS_LABEL, dev)
            fallback.start()
            sources.append(fallback)
            sys_source = None
        else:
            print("ℹ️  媒体声音捕获需要屏幕录制权限（应用内「去授权」），或安装 BlackHole")
    else:
        sources.append(sys_source)

    # 说话人模型预热（进程内 CAM++，懒加载；提前起一个线程避免第一句时才加载）
    if CFG.get("diarize_enabled"):
        threading.Thread(target=get_extractor, daemon=True).start()

    threading.Thread(target=segmenter_worker, daemon=True).start()
    threading.Thread(target=asr_worker, daemon=True).start()
    threading.Thread(target=translate_worker, daemon=True).start()

    App(root, sys_source)
    root.deiconify()
    try:
        root.mainloop()
    finally:
        stop_flag.set()
        for s in sources:
            s.stop()
        if _llm_server:
            _llm_server.stop()


if __name__ == "__main__":
    main()
