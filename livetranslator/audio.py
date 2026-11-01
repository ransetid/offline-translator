"""音源：麦克风（sounddevice）+ 系统/媒体声音（ScreenCaptureKit，回退 BlackHole 等回环设备）。

回调只做最轻的事：把原始单声道块交给 sink(label, block, rate)。重活全在 engine 的工作线程。
sounddevice / pyobjc 都是懒导入——缺库时 app 仍能打开（只是该音源不可用）。
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

import numpy as np

from .config import CFG

BLOCK_SECONDS = 0.25
Sink = Callable[[str, np.ndarray, int], None]


def _sd():
    import sounddevice as sd  # noqa: WPS433（PortAudio 缺失时只影响麦克风）
    return sd


def list_input_devices() -> list[dict]:
    try:
        sd = _sd()
        return [{"index": i, "name": d["name"]} for i, d in enumerate(sd.query_devices())
                if d.get("max_input_channels", 0) > 0]
    except Exception:
        return []


def find_device(keyword: str) -> int | None:
    kw = (keyword or "").lower()
    if not kw:
        return None
    for d in list_input_devices():
        if kw in d["name"].lower():
            return d["index"]
    return None


class MicSource:
    """麦克风或任意输入设备。"""

    def __init__(self, label: str, device_index: int | None, sink: Sink):
        self.label = label
        self.device_index = device_index
        self.sink = sink
        self.stream = None
        self.rate = 16000
        self.available = False
        self.error = ""
        self.device_name = ""

    def start(self) -> bool:
        try:
            sd = _sd()
        except Exception as e:
            self.error = f"音频库不可用: {e}"
            return False
        for rate in (16000, None):
            try:
                info = sd.query_devices(self.device_index, "input")
                r = rate or int(info["default_samplerate"])
                self.stream = sd.InputStream(
                    device=self.device_index, samplerate=r, channels=1,
                    blocksize=int(r * BLOCK_SECONDS), callback=self._callback)
                self.stream.start()
                self.rate = r
                self.available = True
                self.device_name = info.get("name", "")
                self.error = ""
                return True
            except Exception as e:
                self.error = str(e)
                continue
        print(f"⚠️  {self.label} 启动失败: {self.error}", file=sys.stderr)
        return False

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.available = False

    def _callback(self, indata, frames, time_info, status):
        self.sink(self.label, indata[:, 0].copy(), self.rate)


# ---------------------------- ScreenCaptureKit ----------------------------

_sck_delegate_cls = None


def _build_sck_delegate():
    global _sck_delegate_cls
    if _sck_delegate_cls is not None:
        return _sck_delegate_cls
    import ScreenCaptureKit as sck
    import CoreMedia as cm
    import objc
    from Foundation import NSObject
    objc.setVerbose(0)
    import warnings
    try:
        warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
    except AttributeError:
        pass

    class _SCKAudioDelegate(NSObject):
        owner = None   # SystemAudioSource（alloc().init() 后赋值，避开自定义 init 的坑）

        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, stype):
            try:
                if stype != sck.SCStreamOutputTypeAudio or self.owner is None:
                    return
                n = cm.CMSampleBufferGetNumSamples(sbuf)
                if not n:
                    return
                bb = cm.CMSampleBufferGetDataBuffer(sbuf)
                if bb is None:
                    return
                total = cm.CMBlockBufferGetDataLength(bb)
                buf = bytearray(total)
                status, data = cm.CMBlockBufferCopyDataBytes(bb, 0, total, buf)
                if status != 0:
                    return
                arr = np.frombuffer(bytes(data), dtype=np.float32)
                if arr.size == 0:
                    return
                ch = max(1, round(arr.size / n))
                if ch >= 2:
                    mono = arr[:n] if CFG.get("system_audio_planar") else arr.reshape(-1, ch).mean(axis=1)
                else:
                    mono = arr
                self.owner.sink(self.owner.label,
                                np.ascontiguousarray(mono, dtype=np.float32), 48000)
            except Exception as e:
                print("SCK cb err:", e, file=sys.stderr)

        def stream_didStopWithError_(self, stream, err):
            if err and self.owner is not None:
                self.owner.available = False
                self.owner.error = f"系统音频中断: {err}"

    _sck_delegate_cls = _SCKAudioDelegate
    return _SCKAudioDelegate


class SystemAudioSource:
    """ScreenCaptureKit 抓本机播放的声音（需「屏幕录制」权限）。可只抓某个应用。"""

    kind = "sck"

    def __init__(self, label: str, sink: Sink):
        self.label = label
        self.sink = sink
        self.stream = None
        self.delegate = None
        self.queue = None
        self.needs_permission = False
        self.available = False
        self.error = ""
        self.active_app: str | None = None
        self.apps: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @staticmethod
    def supported() -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import ScreenCaptureKit  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def has_permission() -> bool:
        try:
            import Quartz
            return bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            return False

    @staticmethod
    def request_permission():
        try:
            import Quartz
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass

    def _wait(self, done: threading.Event, timeout: float):
        """主线程上要泵 runloop（完成回调可能投递回主线程）；其他线程直接等。"""
        if threading.current_thread() is threading.main_thread():
            from Foundation import NSDate, NSRunLoop
            deadline = time.time() + timeout
            rl = NSRunLoop.currentRunLoop()
            while not done.is_set() and time.time() < deadline:
                rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        else:
            done.wait(timeout)

    def start(self) -> bool:
        with self._lock:
            return self._start()

    def _start(self) -> bool:
        if not self.has_permission():
            self.needs_permission = True
            self.error = "需要屏幕录制权限"
            return False
        try:
            import ScreenCaptureKit as sck
            import libdispatch
        except Exception as e:
            self.needs_permission = True
            self.error = f"ScreenCaptureKit 不可用: {e}"
            return False

        Delegate = _build_sck_delegate()
        done = threading.Event()
        cancelled = threading.Event()     # 超时后晚到的回调不许再起一路「孤儿」stream
        started = threading.Event()
        state: dict = {}
        target = (CFG.get("system_capture_app") or "").strip()

        def on_content(content, err):
            try:
                if cancelled.is_set():
                    return
                if err or content is None or not content.displays():
                    state["err"] = str(err) if err else "没有可用显示器"
                    return
                apps = []
                match = None
                for a in content.applications():
                    try:
                        name, bid = a.applicationName(), a.bundleIdentifier()
                    except Exception:
                        continue
                    if name and bid:
                        apps.append((str(name), str(bid)))
                        if target and bid == target:
                            match = a
                self.apps = sorted(set(apps), key=lambda x: x[0].lower())
                display = content.displays()[0]
                if match is not None:
                    flt = sck.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                        display, [match], [])
                    self.active_app = str(match.applicationName())
                else:
                    flt = sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
                    self.active_app = None
                    if target:
                        state["note"] = "指定应用未在运行，已改抓全部系统声音"
                cfg = sck.SCStreamConfiguration.alloc().init()
                cfg.setCapturesAudio_(True)
                cfg.setSampleRate_(48000)
                cfg.setChannelCount_(2)
                cfg.setWidth_(128)    # 尺寸过小（如 2x2）stream 会不交付音频
                cfg.setHeight_(128)
                delegate = Delegate.alloc().init()
                delegate.owner = self
                stream = sck.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, delegate)
                q = libdispatch.dispatch_queue_create(b"lt.sck.audio", None)
                ok, e2 = stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    delegate, sck.SCStreamOutputTypeAudio, q, None)
                if not ok:
                    state["err"] = f"addStreamOutput 失败: {e2}"
                    return
                def on_started(e):
                    state["start_err"] = e
                    started.set()
                stream.startCaptureWithCompletionHandler_(on_started)
                state.update(stream=stream, delegate=delegate, queue=q)
            finally:
                done.set()

        sck.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
        self._wait(done, 6)
        if not done.is_set():
            cancelled.set()
        if "stream" in state:
            self._wait(started, 4)
            if state.get("start_err") is not None or not started.is_set():
                err = state.get("start_err") or "启动超时"
                try:
                    state["delegate"].owner = None
                    state["stream"].stopCaptureWithCompletionHandler_(None)
                except Exception:
                    pass
                state.pop("stream", None)
                state["err"] = f"startCapture 失败: {err}"
        if "stream" in state:
            self.stream, self.delegate, self.queue = state["stream"], state["delegate"], state["queue"]
            self.available = True
            self.needs_permission = False
            self.error = state.get("note", "")
            return True
        self.error = f"系统音频启动失败: {state.get('err', '超时')}"
        print(f"⚠️  {self.error}", file=sys.stderr)
        self.needs_permission = not self.has_permission()
        return False

    def stop(self):
        with self._lock:
            self._stop()

    def _stop(self):
        if not self.stream:
            return
        # 等 SCK 真正停完：完成回调若拖到解释器退出期才跑会崩（见旧版注释）。
        done = threading.Event()
        try:
            self.stream.stopCaptureWithCompletionHandler_(lambda e: done.set())
        except Exception:
            done.set()
        self._wait(done, 3)
        if self.delegate is not None:
            self.delegate.owner = None
        self.stream = self.delegate = None
        self.available = False

    def restart(self) -> bool:
        with self._lock:
            self._stop()
            return self._start()

    def refresh_apps(self) -> list[tuple[str, str]]:
        """只刷新可捕获应用列表（不动正在跑的 stream）。"""
        try:
            import ScreenCaptureKit as sck
        except Exception:
            return self.apps
        done = threading.Event()

        def on_content(content, err):
            try:
                if content is not None:
                    apps = []
                    for a in content.applications():
                        try:
                            n, b = a.applicationName(), a.bundleIdentifier()
                            if n and b:
                                apps.append((str(n), str(b)))
                        except Exception:
                            pass
                    self.apps = sorted(set(apps), key=lambda x: x[0].lower())
            finally:
                done.set()
        sck.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
        self._wait(done, 3)
        return self.apps
