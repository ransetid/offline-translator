"""界面演示 / 截图用：假后端 + 合成音频实时喂入 + 预置几条历史记录。

    python3 tests/run_demo.py [port]      # 然后浏览器打开打印出来的地址
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ.setdefault("LT_HOME", tempfile.mkdtemp(prefix="lt-demo-"))

import fake_omlx  # noqa: E402
import synth  # noqa: E402
from livetranslator import config  # noqa: E402
from livetranslator.api import Api, Shell  # noqa: E402
from livetranslator.engine import Engine  # noqa: E402
from livetranslator.server import AppServer  # noqa: E402

RATE = 48000


def speak(engine, i, realtime=True):
    text = fake_omlx.SENTENCES[i % len(fake_omlx.SENTENCES)]
    label = config.MIC if i % 2 == 0 else config.MEDIA
    dur = min(4.5, max(1.8, len(text) * (0.17 if i % 2 == 0 else 0.055)))
    fake_omlx.CURRENT.update(i=i, dur=dur)
    for b in synth.blocks(synth.utterance(dur, RATE, seed=i), RATE):
        engine.sink(label, b, RATE)
        time.sleep(0.25 if realtime else 0.004)
    for b in synth.blocks(synth.silence(1.6, RATE), RATE):
        engine.sink(label, b, RATE)
        engine.sink(config.MIC if label == config.MEDIA else config.MEDIA, b, RATE)
        time.sleep(0.25 if realtime else 0.004)


def seed_records(engine, n=3):
    for k in range(n):
        engine.start_session(force=True)
        for i in range(k, k + 4 + k * 2):
            speak(engine, i, realtime=False)
            time.sleep(0.6)
        time.sleep(1.5)
        engine.end_session()
        engine.wait_jobs(60)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    fake = fake_omlx.serve()
    config.load_config()
    config.CFG.update(omlx_url=f"http://127.0.0.1:{fake.server_address[1]}", self_host_llm=False,
                      diarize_enabled=True, onboarded=os.environ.get("LT_ONBOARDED", "1") == "1")
    engine = Engine()
    engine.start(with_audio=False)
    engine.src[config.MIC]["threshold"] = 0.01
    if os.environ.get("LT_SEED", "1") == "1":
        seed_records(engine)
    fake_omlx.CURRENT["i"] = None
    api = Api(engine, Shell())
    srv = AppServer(api, port)
    srv.start()
    # 演示：媒体源当作 SCK 可用
    from livetranslator import audio as A

    class FakeSCK:
        available, needs_permission, error = True, False, ""
        active_app = None
        apps = [("Zoom", "us.zoom.xos"), ("飞书", "com.bytedance.lark"), ("Safari", "com.apple.Safari")]
        device_name = ""
        def stop(self): pass
        def refresh_apps(self): return self.apps
        def restart(self): return True

    class FakeMic:
        available, error, device_name = True, "", "MacBook Pro 麦克风"
        def stop(self): pass

    engine.mic = FakeMic()
    engine.media, engine.media_kind = FakeSCK(), "sck"
    A.SystemAudioSource.supported = staticmethod(lambda: True)
    engine.sources_state  # noqa
    _orig = engine.sources_state

    def patched():
        st = _orig()
        m = st["media"]
        m["apps"] = [{"name": n, "bundle": b} for n, b in FakeSCK.apps]
        m["needs_permission"] = False
        return st
    engine.sources_state = patched
    engine.emit_sources()
    print(f"DEMO_URL {srv.url}", flush=True)

    def feeder():
        i = 0
        while True:
            if engine.session is not None:
                speak(engine, i)
                i += 1
            else:
                for b in synth.blocks(synth.silence(0.5, RATE), RATE):
                    engine.sink(config.MIC, b * 6, RATE)
                time.sleep(0.5)
    threading.Thread(target=feeder, daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
