"""UI 无关的运行时：音源 → 分句 → 识别 → 翻译 → 落盘，会话生命周期，后台任务，事件总线。

数据流（每个箭头一个独立线程/队列，实时回调永不阻塞）：

  音源回调 ──raw──▶ segmenter ──asr──▶ asr_worker ──translate──▶ translate_worker
      │                (VAD/电平)           (过滤/说话人/落盘)          (流式译文)
      └──rec──▶ audio_writer（逐块写 wav）
  所有状态变化 → EventHub.emit() → 订阅者（主窗口 / 字幕条，经 SSE）

关键保证：
  * 结束会议时先 flush 半截句子、再等识别/翻译队列清空，最后一句不会丢。
  * 队列里的每个任务都带着所属 Session，跨会议不串台。
  * 转录逐条落盘（崩溃安全）；下次启动可从记录库「恢复」未完成的记录。
"""
from __future__ import annotations

import collections
import datetime
import itertools
import queue
import sys
import threading
import time
import traceback
import uuid
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import backend, pipeline, records, summary
from .audio import MicSource, SystemAudioSource, find_device
from .backend import BackendError
from .config import (CFG, CONFIG_DIR, MEDIA, MIC, OLD_OUTPUT_DIR, SOURCE_KEYS,
                     apply_terms, lang_pair, save_config, target_lang)

SAMPLE_RATE = pipeline.SAMPLE_RATE
LABEL_KEY = {v: k for k, v in SOURCE_KEYS.items()}


# ============================ 事件总线 ============================

class Subscriber:
    def __init__(self, name: str):
        self.name = name
        self.q: collections.deque = collections.deque(maxlen=5000)
        self.ev = threading.Event()

    def drain(self, timeout: float) -> list:
        if not self.q:
            self.ev.wait(timeout)
        self.ev.clear()
        out = []
        while self.q:
            out.append(self.q.popleft())
        return out


class EventHub:
    def __init__(self):
        self._subs: set[Subscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self, name: str) -> Subscriber:
        s = Subscriber(name)
        with self._lock:
            self._subs.add(s)
        return s

    def unsubscribe(self, s: Subscriber):
        with self._lock:
            self._subs.discard(s)

    def emit(self, type_: str, data=None):
        msg = {"type": type_, "data": data if data is not None else {}}
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s.q.append(msg)
            s.ev.set()


# ============================ 会话 ============================

class Session:
    def __init__(self, record: records.Record):
        self.record = record
        self.id = record.id
        self.started = time.time()
        self.entries: list[dict] = []
        self.by_id: dict[int, dict] = {}
        self.pending = 0                      # 已入队、尚未完成识别+翻译的片段
        self.lock = threading.Lock()
        self.garbage = pipeline.GarbageFilter()
        self.diarizers: dict[str, pipeline.SpeakerClusterer] = {}
        self.closing = False
        self.final_queued: set[int] = set()
        self.dropped: set[int] = set()
        self.interim_seq: dict[int, int] = {}

    def inc(self):
        with self.lock:
            self.pending += 1

    def dec(self):
        with self.lock:
            self.pending = max(0, self.pending - 1)

    def diarizer(self, label: str) -> pipeline.SpeakerClusterer:
        if label not in self.diarizers:
            self.diarizers[label] = pipeline.SpeakerClusterer(
                float(CFG.get("diarize_threshold", 0.55)), int(CFG.get("diarize_max_speakers", 8)))
        return self.diarizers[label]

    def elapsed(self) -> float:
        return time.time() - self.started


def public_entry(e: dict) -> dict:
    keys = ("id", "time", "t_start", "t_end", "speaker", "src", "lang", "target", "source",
            "translation", "translating", "error", "starred", "edited", "latency")
    return {k: e.get(k) for k in keys}


# ============================ 引擎 ============================

class Engine:
    def __init__(self, hub: EventHub | None = None):
        self.hub = hub or EventHub()
        self.stop_flag = threading.Event()
        self.raw_q: queue.Queue = queue.Queue(maxsize=2000)
        self.rec_q: queue.Queue = queue.Queue(maxsize=4000)
        self.asr_q: queue.Queue = queue.Queue()
        self.tr_q: queue.Queue = queue.Queue()
        self.session: Session | None = None
        self._ids = itertools.count(1)
        self._ids_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._flush_req = threading.Event()
        self._flush_done = threading.Event()
        self._calib: dict[str, list] = {}
        self.src: dict[str, dict] = {
            MIC: {"enabled": bool(CFG["mic_enabled"]), "gain": float(CFG["mic_gain"]),
                  "level": 0.0, "peak": 0.0, "threshold": float(CFG["silence_threshold_mic"])},
            MEDIA: {"enabled": bool(CFG["system_enabled"]), "gain": float(CFG["system_gain"]),
                    "level": 0.0, "peak": 0.0, "threshold": float(CFG["silence_threshold_sys"])},
        }
        self.mic: MicSource | None = None
        self.media = None                 # SystemAudioSource | MicSource(回环设备) | None
        self.media_kind = "none"          # sck / device / none
        self.jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
        self.backend_state: dict = {"ok": False, "checking": True, "error": "", "models": [],
                                    "asr_ok": False, "chat_ok": False}
        self._llm_server: backend.ManagedLLMServer | None = None
        self._last_err_toast = 0.0
        self._search_cache: dict[str, tuple[float, str]] = {}
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ 生命周期
    def start(self, with_audio: bool = True):
        try:
            moved = records.migrate_legacy_records(OLD_OUTPUT_DIR, CFG["output_dir"])
            if moved:
                print(f"ℹ️  已把 {moved} 条旧记录迁入记录库")
        except Exception:
            pass
        for name, fn in (("segmenter", self._segmenter), ("asr", self._asr_worker),
                         ("translate", self._translate_worker), ("writer", self._writer),
                         ("monitor", self._monitor)):
            t = threading.Thread(target=fn, name=f"lt-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        if with_audio:
            self.start_audio()
        if CFG.get("diarize_enabled"):
            threading.Thread(target=self._prepare_speaker_model, daemon=True).start()

    def _prepare_speaker_model(self):
        if pipeline.ensure_speaker_model():
            pipeline.get_extractor()

    def start_audio(self):
        self.mic = MicSource(MIC, None, self.sink)
        self.mic.start()
        self._start_media()
        self.emit_sources()

    def _start_media(self):
        self.media, self.media_kind = None, "none"
        if SystemAudioSource.supported():
            s = SystemAudioSource(MEDIA, self.sink)
            try:
                s.start()
            except Exception as e:
                s.error = f"系统音频初始化异常: {e}"
            self.media, self.media_kind = s, "sck"
            if s.available:
                return
        dev = find_device(CFG.get("system_fallback_device_keyword", ""))
        if dev is not None:
            fb = MicSource(MEDIA, dev, self.sink)
            if fb.start():
                if self.media is not None:
                    self._sck_hint = self.media     # 保留权限状态，UI 仍可提示去授权
                self.media, self.media_kind = fb, "device"

    def shutdown(self):
        """退出：进行中的会议走完整收尾（同步），停音源和子进程。"""
        if self.session is not None:
            self.end_session()
        with self._jobs_lock:
            jobs = list(self.jobs.values())
        for j in jobs:                       # 长导入/重识别不阻塞退出（取消后记录会被清理）
            if j["status"] == "running" and j.get("cancellable"):
                j["_cancel"].set()
        self.wait_jobs(timeout=600)          # 但纪要一定要等它写完
        self.stop_flag.set()
        for s in (self.mic, self.media):
            try:
                if s is not None:
                    s.stop()
            except Exception:
                pass
        if self._llm_server:
            self._llm_server.stop()

    def emit(self, t, d=None):
        self.hub.emit(t, d)

    def toast(self, text: str, level: str = "info"):
        self.emit("toast", {"text": text, "level": level})

    def _next_id(self) -> int:
        with self._ids_lock:
            return next(self._ids)

    # ------------------------------------------------------------------ 音源
    def sink(self, label: str, block: np.ndarray, rate: int):
        """实时回调线程调用：只入队。满了就丢（宁丢块也不阻塞 PortAudio/SCK）。"""
        try:
            self.raw_q.put_nowait((label, block, rate))
        except queue.Full:
            pass
        s = self.session
        if s is not None and not s.closing:
            # 静音的音源写入零：音轨时间轴保持连续（回放与转录对齐），且关掉的麦克风不会被录下
            if not self.src[label]["enabled"]:
                block = np.zeros_like(block)
            try:
                self.rec_q.put_nowait((s.record, label, block, rate))
            except queue.Full:
                pass

    def sources_state(self) -> dict:
        out = {}
        for key, label in SOURCE_KEYS.items():
            st = self.src[label]
            obj = self.mic if key == "mic" else self.media
            d = {"key": key, "label": label, "enabled": st["enabled"],
                 "gain": round(st["gain"], 2), "threshold": st["threshold"],
                 "available": bool(obj is not None and getattr(obj, "available", False)),
                 "error": getattr(obj, "error", "") if obj is not None else "",
                 "kind": "mic" if key == "mic" else self.media_kind}
            if key == "mic" and obj is not None:
                d["device"] = obj.device_name
            if key == "media":
                sck = obj if isinstance(obj, SystemAudioSource) else getattr(self, "_sck_hint", None)
                d["needs_permission"] = bool(sck is not None and sck.needs_permission)
                d["supported_sck"] = SystemAudioSource.supported()
                d["capture_app"] = CFG.get("system_capture_app", "")
                d["active_app"] = getattr(obj, "active_app", None)
                d["apps"] = [{"name": n, "bundle": b} for n, b in getattr(sck, "apps", [])] if sck else []
                d["device"] = getattr(obj, "device_name", "") if self.media_kind == "device" else ""
                d["diarize"] = bool(CFG.get("diarize_enabled"))
                if self.media_kind == "none" and not d["error"]:
                    d["error"] = "未检测到系统音频来源"
            out[key] = d
        return out

    def emit_sources(self):
        self.emit("sources", self.sources_state())

    def set_source(self, key: str, enabled=None, gain=None) -> dict:
        label = SOURCE_KEYS[key]
        cfg_en, cfg_gain = ("mic_enabled", "mic_gain") if key == "mic" else ("system_enabled", "system_gain")
        if enabled is not None:
            self.src[label]["enabled"] = bool(enabled)
            CFG[cfg_en] = bool(enabled)
        if gain is not None:
            g = max(0.0, min(3.0, float(gain)))
            self.src[label]["gain"] = g
            CFG[cfg_gain] = g
        save_config()
        self.emit_sources()
        return self.sources_state()[key]

    def set_capture_app(self, bundle: str) -> dict:
        CFG["system_capture_app"] = bundle or ""
        save_config()
        if isinstance(self.media, SystemAudioSource):
            self.media.restart()
            if self.media.error:
                self.toast(self.media.error, "warn")
        self.emit_sources()
        return self.sources_state()["media"]

    def refresh_apps(self) -> list:
        sck = self.media if isinstance(self.media, SystemAudioSource) else getattr(self, "_sck_hint", None)
        if sck is not None:
            sck.refresh_apps()
        return self.sources_state()["media"]["apps"]

    def retry_media(self) -> dict:
        """授权后重试系统音频（无需重启 app；部分 macOS 版本仍需重启才生效）。"""
        if self.media is not None:
            try:
                self.media.stop()
            except Exception:
                pass
        self._start_media()
        self.emit_sources()
        return self.sources_state()["media"]

    def apply_thresholds(self):
        self.src[MIC]["threshold"] = float(CFG["silence_threshold_mic"])
        self.src[MEDIA]["threshold"] = float(CFG["silence_threshold_sys"])

    def levels(self) -> dict:
        return {LABEL_KEY[l]: round(min(1.0, s["level"] / 0.15), 3) if s["enabled"] else 0.0
                for l, s in self.src.items()}

    def calibrate(self, key: str, seconds: float = 2.5) -> dict:
        """采集一段环境噪声，给出建议静音阈值（不自动保存，由用户确认）。"""
        label = SOURCE_KEYS[key]
        self._calib[label] = []
        time.sleep(seconds)
        vals = self._calib.pop(label, [])
        if len(vals) < 3:
            return {"ok": False, "error": "没有采到音频，该音源可能未连接"}
        thr = pipeline.estimate_noise_threshold(vals)
        return {"ok": True, "threshold": round(thr, 4),
                "noise": round(float(np.median(vals)), 4), "samples": len(vals)}

    # ------------------------------------------------------------------ 线程：分句
    def _segmenter(self):
        segs: dict[str, pipeline.VadSegmenter] = {}
        rates: dict[str, int] = {}
        while not self.stop_flag.is_set():
            try:
                self._segment_step(segs, rates)
            except Exception:
                traceback.print_exc()          # 单块出错不能让分句线程死掉

    def _segment_step(self, segs: dict, rates: dict):
        if self._flush_req.is_set() and self.raw_q.empty():   # 先把已到的音频处理完再收尾
            s = self.session
            for label, sg in segs.items():
                r0 = rates.get(label, SAMPLE_RATE)
                for ev in sg.flush(r0):
                    self._dispatch_seg(s, label, ev, r0)
            self._flush_req.clear()
            self._flush_done.set()
        try:
            label, block, rate = self.raw_q.get(timeout=0.2)
        except queue.Empty:
            return
        st = self.src[label]
        rates[label] = rate
        sg = segs.get(label)
        if sg is None:
            sg = segs[label] = pipeline.VadSegmenter(st["threshold"], self._next_id)
        sg.threshold = st["threshold"]
        sg.interim = "sense" in (CFG.get("asr_model", "") or "").lower()
        s = self.session
        if not st["enabled"]:
            for ev in sg.abort():
                self._dispatch_seg(s, label, ev, rate)
            st["level"] = 0.0
            return
        if st["gain"] != 1.0:
            block = np.clip(block * st["gain"], -1.0, 1.0)
        r = pipeline.rms(block)
        st["level"] = max(r, st["level"] * 0.6)
        cal = self._calib.get(label)
        if cal is not None:
            cal.append(r)
        if s is None or s.closing:          # 未在会议中：只刷电平，丢半截状态
            for ev in sg.abort():
                self._dispatch_seg(s, label, ev, rate)
            return
        for ev in sg.feed(block, rate):
            self._dispatch_seg(s, label, ev, rate)

    def _dispatch_seg(self, s: Session | None, label: str, ev: tuple, rate: int):
        kind, uid, audio = ev
        if s is None:
            if kind == "drop":
                self.emit("interim_clear", {"id": uid})
            return
        if kind == "start":
            return
        if kind == "drop":
            s.dropped.add(uid)
            self.emit("interim_clear", {"id": uid})
            return
        if rate != SAMPLE_RATE:
            audio = pipeline.resample(audio, rate, SAMPLE_RATE)
        if kind == "interim":
            seq = s.interim_seq.get(uid, 0) + 1
            s.interim_seq[uid] = seq
            self.asr_q.put(("interim", s, label, audio, uid, seq, None, None))
            return
        t_end = s.record.offset()
        t_start = max(0.0, t_end - len(audio) / SAMPLE_RATE)
        s.final_queued.add(uid)
        s.inc()
        self.asr_q.put(("final", s, label, audio, uid, 0, t_start, t_end))

    # ------------------------------------------------------------------ 线程：识别
    def _asr_error(self, e: Exception):
        now = time.time()
        if now - self._last_err_toast > 12:
            self._last_err_toast = now
            self.toast(f"识别失败：{e}", "error")
        self._check_backend_async()

    def _asr_worker(self):
        while not self.stop_flag.is_set():
            try:
                kind, s, label, audio, uid, seq, t_start, t_end = self.asr_q.get(timeout=0.3)
            except queue.Empty:
                continue
            if kind == "interim":
                # 过期的临时稿直接跳过：已有更新的临时稿或定稿在排队 / 已作废
                if (uid in s.final_queued or uid in s.dropped or seq < s.interim_seq.get(uid, 0)
                        or s.closing):
                    continue
                try:
                    res = backend.asr_transcribe(audio)
                    text = apply_terms((res.get("text") or "").strip())
                except BackendError as e:
                    self._asr_error(e)
                    continue
                except Exception:
                    traceback.print_exc()
                    continue
                if text and uid not in s.final_queued and uid not in s.dropped:
                    self.emit("interim", {"id": uid, "speaker": label, "src": LABEL_KEY.get(label),
                                          "lang": pipeline.detect_lang(text), "source": text})
                continue
            try:
                self._finalize_utterance(s, label, audio, uid, t_start, t_end)
            except Exception:
                traceback.print_exc()
                s.dec()

    def _finalize_utterance(self, s: Session, label, audio, uid, t_start, t_end):
        try:
            res = backend.asr_transcribe(audio)
        except BackendError as e:
            self._asr_error(e)
            self.emit("interim_clear", {"id": uid})
            s.dec()
            return
        text = apply_terms((res.get("text") or "").strip())
        res["text"] = text
        if s.garbage.is_garbage(res, label):
            self.emit("interim_clear", {"id": uid})
            s.dec()
            return
        speaker = label
        emb = None
        if label == MEDIA and CFG.get("diarize_enabled"):
            emb = pipeline.compute_embedding(audio)
            if emb is not None:
                speaker = f"{label}·说话人{s.diarizer(label).assign(emb)}"
        lang = pipeline.detect_lang(text)
        entry = {
            "id": uid, "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "t_start": round(t_start, 2), "t_end": round(t_end, 2),
            "speaker": speaker, "src": LABEL_KEY.get(label), "lang": lang,
            "target": target_lang(lang), "source": text, "translation": None,
            "translating": True, "starred": False, "embedding": emb, "t0": time.time(),
        }
        with s.lock:
            s.entries.append(entry)
            s.by_id[uid] = entry
        try:
            s.record.append_entry(entry)
        except Exception as e:
            print(f"转录落盘失败: {e}", file=sys.stderr)
        self.emit("entry", public_entry(entry))
        self.tr_q.put((s, entry, True))

    # ------------------------------------------------------------------ 线程：翻译
    def _translate_worker(self):
        while not self.stop_flag.is_set():
            try:
                s, entry, counted = self.tr_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._translate_entry(s, entry)
            except Exception:
                traceback.print_exc()
            finally:
                if counted:
                    s.dec()

    def _translate_entry(self, s: Session | None, entry: dict):
        ctx = ""
        if CFG.get("translate_context") and s is not None:
            with s.lock:
                idx = s.entries.index(entry) if entry in s.entries else -1
                if idx > 0:
                    ctx = s.entries[idx - 1].get("source", "")
        system, user = pipeline.translate_prompt(entry["source"], entry["lang"], ctx)
        entry["target"] = target_lang(entry["lang"])
        last = [0.0]

        def on_delta(acc):
            entry["translation"] = acc
            now = time.time()
            if now - last[0] >= 0.05:
                last[0] = now
                self.emit("translation", {"id": entry["id"], "translation": acc, "done": False})

        entry["translating"] = True
        entry["error"] = None
        try:
            full = backend.chat_stream(user, on_delta, system=system)
            if not full:
                full = backend.chat(user, system=system)
            entry["translation"] = full
        except BackendError as e:
            entry["translation"] = ""
            entry["error"] = str(e)
            self._asr_error(e)
        entry["translating"] = False
        entry["latency"] = round(time.time() - entry.get("t0", time.time()), 2)
        self.emit("translation", {"id": entry["id"], "translation": entry["translation"],
                                  "done": True, "error": entry.get("error"),
                                  "latency": entry["latency"], "target": entry["target"]})

    # ------------------------------------------------------------------ 线程：写盘
    def _writer(self):
        while not self.stop_flag.is_set():
            try:
                rec, label, block, rate = self.rec_q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                rec.write_audio(label, block, rate)
            except Exception as e:
                print(f"音频写盘失败: {e}", file=sys.stderr)

    # ------------------------------------------------------------------ 线程：后端体检
    def _monitor(self):
        self.check_backend()
        try:
            self._setup_llm()
        except Exception:
            traceback.print_exc()
        while not self.stop_flag.is_set():
            self.check_backend()
            wait = 30 if self.backend_state.get("ok") else 6
            self.stop_flag.wait(wait)

    def _setup_llm(self):
        """外部后端没加载对话模型、且允许自托管时，后台起 mlx_lm.server 只接管翻译/纪要。"""
        if not CFG.get("self_host_llm"):
            return
        h = backend.health()
        if h["ok"] and h["chat_ok"]:
            return
        try:
            import importlib.util
            if importlib.util.find_spec("mlx_lm") is None:
                return
        except Exception:
            return
        self.backend_state.update(checking=True, note="正在启动内置翻译模型（首次会下载）…")
        self.emit("backend", self.backend_state)
        srv = backend.ManagedLLMServer(CFG["llm_model_repo"], int(CFG["llm_port"]))
        if srv.start():
            self._llm_server = srv
            backend.RUNTIME.update(chat_url=srv.url, chat_model=CFG["llm_model_repo"])
        self.backend_state.pop("note", None)

    def check_backend(self) -> dict:
        h = backend.health()
        h["checking"] = False
        h["url"] = CFG["omlx_url"]
        h["asr_model"] = CFG["asr_model"]
        h["chat_model"] = backend.RUNTIME["chat_model"] or CFG["chat_model"]
        changed = any(self.backend_state.get(k) != h.get(k)
                      for k in ("ok", "asr_ok", "chat_ok", "error", "asr_model", "chat_model", "checking"))
        self.backend_state = h
        if changed:
            self.emit("backend", h)
        return h

    def _check_backend_async(self):
        threading.Thread(target=self.check_backend, daemon=True).start()

    # ------------------------------------------------------------------ 会话
    def start_session(self, force: bool = False) -> dict:
        with self._session_lock:
            return self._start_session(force)

    def _start_session(self, force: bool) -> dict:
        if self.session is not None:
            return {"ok": True, "session": self.session_state()}
        if not force:
            b = self.backend_state
            if not b.get("ok") or not b.get("asr_ok"):
                return {"ok": False, "reason": "backend",
                        "message": b.get("error") or "识别模型未加载"}
        a, b = lang_pair()
        rec = records.Record(CFG["output_dir"], CFG["translate_mode"],
                             app=CFG.get("system_capture_app", ""))
        rec.set_meta(lang_pair=[a, b])
        # 丢掉空闲期积压的音频块（避免混入开始前的声音）
        try:
            while True:
                self.raw_q.get_nowait()
        except queue.Empty:
            pass
        self.session = Session(rec)
        self.emit("session", self.session_state())
        return {"ok": True, "session": self.session_state()}

    def session_state(self) -> dict:
        s = self.session
        if s is None:
            return {"active": False}
        lat = [e["latency"] for e in s.entries if e.get("latency")]
        return {"active": True, "id": s.id, "started": s.started, "elapsed": s.elapsed(),
                "count": len(s.entries), "avg_latency": round(sum(lat) / len(lat), 2) if lat else None,
                "pending": s.pending}

    def live_entries(self) -> list:
        s = self.session
        if s is None:
            return []
        with s.lock:
            return [public_entry(e) for e in s.entries]

    def end_session(self, template: str | None = None) -> str | None:
        with self._session_lock:             # 双击「结束」只收尾一次
            s = self.session
            if s is None or getattr(s, "ending", False):
                return None
            s.ending = True
        self._flush_done.clear()
        self._flush_req.set()
        self._flush_done.wait(3.0)            # 先把正在说的半句话收进来，再关门
        s.closing = True
        self.session = None
        self.emit("session", {"active": False, "ended_id": s.id})
        return self._job("summarize", "生成会议纪要", lambda job: self._close_session(s, job, template),
                         record_id=s.id)

    def _close_session(self, s: Session, job: dict, template):
        deadline = time.time() + 120
        while s.pending > 0 and time.time() < deadline:   # 等最后几句识别 + 翻译完
            self._progress(job, 0.05, f"处理最后 {s.pending} 句…")
            time.sleep(0.25)
        entries = list(s.entries)
        if not entries:
            s.record.discard()
            self.emit("records_changed", {})
            return {"message": "没有录到内容，未生成记录", "discarded": True}
        s.record.finalize(entries=entries, summary_status="summarizing")
        self.emit("records_changed", {"id": s.id})
        return self._summarize_into(s.record.dir, entries, job, template, title_from_model=True)

    def _summarize_into(self, rec_dir, entries, job, template, title_from_model=False):
        meta = records.read_meta(rec_dir)
        title = meta.get("title", "")
        if title_from_model and not title:
            self._progress(job, 0.1, "起标题")
            try:
                title = summary.auto_title(entries)
            except BackendError:
                title = ""
        tpl = template or CFG.get("summary_template")
        cur = records.read_meta(rec_dir).get("title", "")   # 用户在生成期间改过名：以用户为准
        if cur:
            title = cur
        records.update_meta(rec_dir, summary_status="summarizing", summary_template=tpl,
                            **({"title": title} if title else {}))
        try:
            text = summary.summarize(entries, tpl, lambda p, m: self._progress(job, 0.15 + p * 0.85, m))
        except BackendError as e:
            records.update_meta(rec_dir, summary_status="failed", summary_error=str(e))
            self.emit("records_changed", {"id": Path(rec_dir).name})
            raise
        records.write_summary(rec_dir, f"# {title or '会议纪要'}\n\n{text.strip()}\n")
        records.update_meta(rec_dir, summary_status="done", summary_error="")
        self.emit("records_changed", {"id": Path(rec_dir).name})
        return {"message": f"纪要已生成：{title or Path(rec_dir).name}", "record_id": Path(rec_dir).name}

    # 会中操作（标星 / 纠错 / 重译）
    def live_update(self, entry_id: int, **fields) -> dict | None:
        s = self.session
        if s is None:
            return None
        e = s.by_id.get(int(entry_id))
        if e is None:
            return None
        if "starred" in fields:
            e["starred"] = bool(fields["starred"])
        if "source" in fields and fields["source"] is not None:
            e["source"] = str(fields["source"]).strip()
            e["edited"] = True
        if "translation" in fields and fields["translation"] is not None:
            e["translation"] = str(fields["translation"]).strip()
            e["edited"] = True
        self.emit("entry_update", public_entry(e))
        return public_entry(e)

    def retranslate(self, entry_id: int) -> bool:
        s = self.session
        e = s.by_id.get(int(entry_id)) if s else None
        if e is None:
            return False
        e["translation"], e["translating"] = "", True
        self.emit("entry_update", public_entry(e))
        s.inc()                               # 计入 pending：结束会议会等它翻完
        self.tr_q.put((s, e, True))
        return True

    # ------------------------------------------------------------------ 后台任务
    def _job(self, kind: str, title: str, fn: Callable[[dict], dict], record_id: str | None = None) -> str:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "title": title, "status": "running", "progress": 0.0,
               "message": "排队中", "record_id": record_id, "started": time.time(),
               "cancellable": kind in ("import", "reprocess")}
        job["_cancel"] = threading.Event()
        with self._jobs_lock:
            self.jobs[jid] = job

        def run():
            try:
                res = fn(job) or {}
                job.update(status="done", progress=1.0, message=res.get("message", "完成"),
                           record_id=res.get("record_id", job.get("record_id")))
            except JobCancelled:
                job.update(status="cancelled", message="已取消")
            except BackendError as e:
                job.update(status="failed", message=str(e))
            except Exception as e:
                traceback.print_exc()
                job.update(status="failed", message=f"出错：{e}")
            job["finished"] = time.time()
            self.emit("job", self.job_public(job))
            self.emit("records_changed", {"id": job.get("record_id")})

        self.emit("job", self.job_public(job))
        threading.Thread(target=run, name=f"lt-job-{kind}", daemon=True).start()
        return jid

    @staticmethod
    def job_public(job: dict) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def _progress(self, job: dict, p: float, msg: str):
        if job["_cancel"].is_set():
            raise JobCancelled()
        job["progress"] = round(max(job.get("progress", 0), min(0.99, p)), 3)
        job["message"] = msg
        self.emit("job", self.job_public(job))

    def cancel_job(self, jid: str) -> bool:
        j = self.jobs.get(jid)
        if j and j["status"] == "running" and j.get("cancellable"):
            j["_cancel"].set()
            return True
        return False

    def list_jobs(self) -> list:
        now = time.time()
        with self._jobs_lock:
            for k in [k for k, j in self.jobs.items() if j.get("finished") and now - j["finished"] > 900]:
                self.jobs.pop(k, None)
            return [self.job_public(j) for j in self.jobs.values()]

    def _jobs_snapshot(self) -> list:
        with self._jobs_lock:
            return list(self.jobs.values())

    def busy(self) -> bool:
        return any(j["status"] == "running" for j in self._jobs_snapshot())

    def wait_jobs(self, timeout: float = 600):
        deadline = time.time() + timeout
        while self.busy() and time.time() < deadline:
            time.sleep(0.2)

    # ------------------------------------------------------------------ 记录库
    def _dir(self, rec_id: str) -> Path:
        return records.record_dir(CFG["output_dir"], rec_id)

    def list_records(self, query: str = "") -> list:
        q = (query or "").strip().lower()
        out = []
        busy = {j.get("record_id") for j in self._jobs_snapshot() if j["status"] == "running"}
        live = self.session.id if self.session else None
        for m in records.list_records(CFG["output_dir"]):
            if m["id"] == live:
                continue
            status = m.get("summary_status", "")
            if status in ("recording", "summarizing") and m["id"] not in busy:
                status = "interrupted"          # 上次异常退出 / 生成到一半被关
            item = {"id": m["id"], "title": m.get("title") or "", "created_at": m.get("created_at", ""),
                    "duration_s": m.get("duration_s", 0), "status": status,
                    "entry_count": m.get("entry_count"), "starred_count": m.get("starred_count", 0),
                    "speakers": len(m.get("speaker_names") or m.get("speakers") or []),
                    "langs": m.get("langs", []), "app": m.get("app", ""),
                    "translate_mode": m.get("translate_mode", "")}
            if q:
                hay = (item["title"] + " " + item["created_at"] + " " + item["id"]).lower()
                if q not in hay and q not in self._body_text(m["_dir"]):
                    continue
                item["match"] = self._snippet(m["_dir"], q)
            out.append(item)
        return out

    def _body_text(self, d: str) -> str:
        p = Path(d) / "transcript.jsonl"
        try:
            mt = p.stat().st_mtime + (Path(d) / "summary.md").stat().st_mtime \
                if (Path(d) / "summary.md").exists() else p.stat().st_mtime
        except OSError:
            return ""
        c = self._search_cache.get(d)
        if c and c[0] == mt:
            return c[1]
        txt = "\n".join((e.get("source", "") + " " + (e.get("translation") or ""))
                        for e in records.load_transcript(d)).lower()
        txt += "\n" + records.read_summary(d).lower()
        self._search_cache[d] = (mt, txt)
        return txt

    def _snippet(self, d: str, q: str) -> str:
        for e in records.load_transcript(d):
            for f in ("source", "translation"):
                t = e.get(f) or ""
                i = t.lower().find(q)
                if i >= 0:
                    a = max(0, i - 18)
                    return ("…" if a else "") + t[a:i + len(q) + 30]
        return ""

    def get_record(self, rec_id: str) -> dict:
        d = self._dir(rec_id)
        meta = records.read_meta(d)
        tr = records.load_transcript(d)
        sources = []
        for s in meta.get("sources", []):
            f = d / s["wav"]
            if f.exists():
                sources.append({"label": s["label"], "key": LABEL_KEY.get(s["label"], s["label"]),
                                "url": f"/media/{rec_id}/{s['wav']}", "duration": records.wav_duration(f),
                                "offset": float(s.get("offset", 0) or 0)})
        meta.pop("_dir", None)
        return {"meta": meta, "transcript": tr, "summary": records.read_summary(d),
                "sources": sources, "dir": str(d)}

    def rename_record(self, rec_id: str, title: str) -> dict:
        m = records.update_meta(self._dir(rec_id), title=title.strip()[:80])
        self.emit("records_changed", {"id": rec_id})
        return {"ok": True, "title": m["title"]}

    def delete_record(self, rec_id: str) -> dict:
        records.delete_record(self._dir(rec_id))
        self.emit("records_changed", {"id": rec_id, "deleted": True})
        return {"ok": True}

    def update_record_entry(self, rec_id: str, entry_id, **fields) -> dict | None:
        clean = {}
        if "starred" in fields:
            clean["starred"] = bool(fields["starred"])
        for k in ("source", "translation", "speaker"):
            if fields.get(k) is not None:
                clean[k] = str(fields[k]).strip()
                if k != "speaker":
                    clean["edited"] = True
        return records.update_entry(self._dir(rec_id), entry_id, **clean)

    def resummarize(self, rec_id: str, template: str | None = None) -> str:
        d = self._dir(rec_id)

        def run(job):
            tr = records.load_transcript(d)
            if not tr:
                raise RuntimeError("这条记录没有转录内容")
            return self._summarize_into(d, tr, job, template,
                                        title_from_model=not records.read_meta(d).get("title"))
        return self._job("summarize", "重新生成纪要", run, record_id=rec_id)

    def recover(self, rec_id: str) -> str:
        """异常中断的记录：补齐元数据，缺译文的补翻，再生成纪要。"""
        d = self._dir(rec_id)

        def run(job):
            records.close_orphan(d)
            tr = records.load_transcript(d)
            if not tr:
                return self._reprocess(d, job)
            missing = [e for e in tr if not e.get("translation")]
            for i, e in enumerate(missing):
                self._progress(job, 0.4 * i / max(1, len(missing)), f"补翻译 {i + 1}/{len(missing)}")
                e["translation"] = self._translate_text(e.get("source", ""), e.get("lang", "zh"))
            if missing:
                records.write_transcript(d, tr)
            return self._summarize_into(d, tr, job, None, title_from_model=True)
        return self._job("recover", "恢复记录", run, record_id=rec_id)

    def reprocess(self, rec_id: str) -> str:
        """从录音重新识别 + 翻译 + 纪要（后端离线时录下的会，事后补救）。"""
        d = self._dir(rec_id)
        return self._job("reprocess", "从录音重新识别", lambda job: self._reprocess(d, job), record_id=rec_id)

    def _translate_text(self, text: str, lang: str) -> str:
        if not text:
            return ""
        system, user = pipeline.translate_prompt(text, lang)
        try:
            return backend.chat(user, system=system)
        except BackendError:
            return ""

    def _transcribe_audio(self, audio: np.ndarray, label: str, job, p0: float, p1: float,
                          diarize: bool) -> list[dict]:
        garbage = pipeline.GarbageFilter()
        diar = pipeline.SpeakerClusterer(float(CFG.get("diarize_threshold", 0.55)),
                                         int(CFG.get("diarize_max_speakers", 8)))
        thr = float(CFG.get("silence_threshold_mic", 0.01))
        total = len(audio) / SAMPLE_RATE or 1
        out = []
        for t_start, t_end, seg in pipeline.offline_segments(audio, SAMPLE_RATE, thr):
            self._progress(job, p0 + (p1 - p0) * (t_end / total),
                           f"识别中 {summary.fmt_ts(t_end)} / {summary.fmt_ts(total)}")
            res = backend.asr_transcribe(seg)
            text = apply_terms((res.get("text") or "").strip())
            res["text"] = text
            if garbage.is_garbage(res, label):
                continue
            emb = pipeline.compute_embedding(seg) if diarize else None
            speaker = label
            if emb is not None:
                speaker = f"{label}·说话人{diar.assign(emb)}" if label in (MEDIA,) else f"说话人{diar.assign(emb)}"
            lang = pipeline.detect_lang(text)
            out.append({"t_start": round(t_start, 2), "t_end": round(t_end, 2), "speaker": speaker,
                        "lang": lang, "source": text, "translation": None, "embedding": emb})
        return out

    def _finish_offline(self, d: Path, rec: records.Record | None, entries: list, job) -> dict:
        self._store_offline(d, rec, entries, job)
        return self._summarize_into(d, entries, job, None, title_from_model=True)

    def _store_offline(self, d: Path, rec: records.Record | None, entries: list, job):
        entries.sort(key=lambda e: e["t_start"])
        for i, e in enumerate(entries, 1):
            e["id"] = i
        for i, e in enumerate(entries):
            self._progress(job, 0.6 + 0.2 * i / max(1, len(entries)), f"翻译 {i + 1}/{len(entries)}")
            e["translation"] = self._translate_text(e["source"], e["lang"])
        if rec is not None:
            for e in entries:
                rec.append_entry(e)
            rec.finalize(entries=entries, summary_status="summarizing")
        else:
            records.write_transcript(d, entries)
            embs = {str(e["id"]): np.asarray(e["embedding"], dtype=np.float32)
                    for e in entries if e.get("embedding") is not None}
            if embs:
                records.save_embeddings(d, embs)
            elif (d / "embeddings.npz").exists():
                (d / "embeddings.npz").unlink()   # 旧向量的 id 已对不上，删掉免得误配
            records.update_meta(d, **records._entry_stats(entries))

    def _reprocess(self, d: Path, job) -> dict:
        meta = records.read_meta(d)
        srcs = [s for s in meta.get("sources", []) if (d / s["wav"]).exists()]
        if not srcs:
            raise RuntimeError("这条记录没有录音文件")
        entries: list[dict] = []
        for i, s in enumerate(srcs):
            audio = read_wav_mono(d / s["wav"])
            entries += self._transcribe_audio(
                audio, s["label"], job, 0.6 * i / len(srcs), 0.6 * (i + 1) / len(srcs),
                diarize=bool(CFG.get("diarize_enabled")) and s["label"] == MEDIA)
        if not entries:
            records.update_meta(d, summary_status="failed", summary_error="没识别到有效语音")
            raise RuntimeError("没识别到有效语音")
        return self._finish_offline(d, None, entries, job)

    def import_file(self, path: str) -> str:
        name = Path(path).name

        def run(job):
            self._progress(job, 0.01, "解码音频")
            audio = records.decode_audio_file(path, SAMPLE_RATE)
            if len(audio) < SAMPLE_RATE * 0.5:
                raise RuntimeError("音频太短或无法解码")
            rec = records.Record(CFG["output_dir"], CFG["translate_mode"], app=f"import:{name}")
            job["record_id"] = rec.id
            rec.set_meta(title="", source_file=name)
            try:                               # 落盘完成前任何失败/取消：整条删掉，不留孤儿
                rec.write_audio("导入", audio, SAMPLE_RATE)
                entries = self._transcribe_audio(audio, "导入", job, 0.03, 0.6,
                                                 diarize=bool(CFG.get("diarize_enabled")))
                if not entries:
                    raise RuntimeError("没识别到有效语音")
                self._store_offline(rec.dir, rec, entries, job)
            except BaseException:
                rec.discard()
                job["record_id"] = None
                raise
            res = self._summarize_into(rec.dir, entries, job, None, title_from_model=True)
            res["message"] = f"已导入：{name}"
            return res
        return self._job("import", f"导入 {name}", run)

    def export_record(self, rec_id: str, fmt: str, dest_dir: str | None = None) -> str:
        d = self._dir(rec_id)
        meta = records.read_meta(d)
        tr = records.load_transcript(d)
        summ = records.read_summary(d)
        safe = "".join(c for c in (meta.get("title") or rec_id) if c not in '/\\:*?"<>|').strip() or rec_id
        out_dir = Path(dest_dir) if dest_dir else (d / "exports")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{safe}.{fmt}"
        if fmt == "md":
            out.write_text(records.to_markdown(meta, tr, summ), encoding="utf-8")
        elif fmt == "srt":
            out.write_text(records.to_srt(tr), encoding="utf-8")
        elif fmt == "vtt":
            out.write_text(records.to_vtt(tr), encoding="utf-8")
        elif fmt == "txt":
            out.write_text(records.to_txt(meta, tr, summ), encoding="utf-8")
        elif fmt == "docx":
            records.to_docx(meta, tr, summ, out)
        else:
            raise ValueError(f"不支持的格式: {fmt}")
        return str(out)

    def speakers(self, rec_id: str) -> list:
        d = self._dir(rec_id)
        store = records.SpeakerStore(CFG["speakers_store"])
        thr = float(CFG.get("diarize_global_threshold", 0.70))
        groups = records.speaker_embeddings(d)
        tr = records.load_transcript(d)
        counts = collections.Counter(e.get("speaker") for e in tr)
        out = []
        names = list(dict.fromkeys(e.get("speaker") for e in tr if e.get("speaker")))
        for name in names:
            vecs = groups.get(name, [])
            sug = None
            if len(vecs) >= 2:
                rep = np.mean(np.asarray(vecs, dtype=np.float32), axis=0)
                m = store.match(rep, thr)
                if m:
                    sug = {"name": m["name"], "sim": round(m["sim"], 2)}
            sample = next((e.get("source", "") for e in tr if e.get("speaker") == name), "")
            out.append({"id": name, "count": counts.get(name, 0), "has_voice": bool(vecs),
                        "suggest": sug, "sample": sample[:60]})
        return out

    def name_speakers(self, rec_id: str, mapping: dict) -> dict:
        d = self._dir(rec_id)
        store = records.SpeakerStore(CFG["speakers_store"])
        groups = records.speaker_embeddings(d)
        n = 0
        for local_id, name in (mapping or {}).items():
            name = (name or "").strip()
            if not name or name == local_id:
                continue
            if groups.get(local_id):
                store.assign(name, groups[local_id])
            records.rename_speaker(d, local_id, name)
            n += 1
        tr = records.load_transcript(d)
        records.update_meta(d, **records._entry_stats(tr))
        self.emit("records_changed", {"id": rec_id})
        return {"ok": True, "renamed": n}


class JobCancelled(Exception):
    pass


def read_wav_mono(path) -> np.ndarray:
    """读本 app 写的 16-bit 单声道 wav，重采样到 16k。"""
    with wave.open(str(path), "rb") as w:
        rate, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return pipeline.resample(a, rate, SAMPLE_RATE)


__all__ = ["Engine", "EventHub", "Session", "CONFIG_DIR"]
