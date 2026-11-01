"""程序入口：pywebview 原生窗口外壳（主窗口 + 字幕条）。没有 pywebview 时退化为浏览器模式。

    python -m livetranslator            # 原生窗口
    python -m livetranslator --browser  # 在默认浏览器里打开（调试 / 无 pywebview 时）
    python -m livetranslator --no-audio # 不开音源（只看记录库 / 演示）
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from .api import AUDIO_EXTS, Api, Shell
from .config import CFG, load_config, save_config
from .engine import Engine
from .server import AppServer


class NativeShell(Shell):
    native = True

    def __init__(self, webview, server: AppServer, engine: Engine):
        self.wv = webview
        self.server = server
        self.engine = engine
        self.main = None
        self.overlay = None
        self._quitting = False
        self._ov_lock = threading.Lock()

    # -------------------------------------------------- 窗口
    def create_main(self):
        self.main = self.wv.create_window(
            "实时翻译", self.server.url, width=1240, height=780, min_size=(900, 580),
            on_top=bool(CFG.get("ui_on_top")), background_color="#0f1016", text_select=True)
        self.main.events.closing += self._on_closing
        return self.main

    def _on_closing(self):
        if self._quitting:
            return False        # 收尾中再点关闭 / ⌘Q：继续等；destroy() 走的路径不经过这里
        if self.engine.session is not None or self.engine.busy():
            # 进行中的会议 / 正在生成的纪要：后台收尾，完成后再真正关窗
            self._quitting = True
            self.engine.emit("quitting", {"text": "正在保存记录并生成纪要，完成后自动退出…"})
            threading.Thread(target=self._finish_and_close, daemon=True).start()
            return False
        return True

    def _finish_and_close(self):
        try:
            self.engine.shutdown()
        finally:
            for w in (self.overlay, self.main):
                try:
                    if w is not None:
                        w.destroy()
                except Exception:
                    pass

    def quit(self):
        if self.main is not None:
            if self._on_closing():
                self._quitting = True
                self.main.destroy()
        return True

    def open_overlay(self):
        with self._ov_lock:
            return self._open_overlay()

    def _open_overlay(self):
        if self.overlay is None:
            try:
                scr = self.wv.screens[0]
                sw, sh = int(scr.width), int(scr.height)
            except Exception:
                sw, sh = 1440, 900
            w = min(1040, sw - 200)
            h = 150
            self.overlay = self.wv.create_window(
                "字幕", self.server.overlay_url, width=w, height=h,
                x=(sw - w) // 2, y=sh - h - 64, frameless=True, on_top=True,
                transparent=True, easy_drag=True, resizable=True, min_size=(420, 96),
                background_color="#000000")
            ov = self.overlay
            ov.events.closed += lambda: self._overlay_closed(ov)
        else:
            self.overlay.show()
        if self.main is not None:
            self.main.hide()
        return True

    def _overlay_closed(self, ov):
        with self._ov_lock:
            if self.overlay is not ov:
                return
            self.overlay = None
        if self.main is not None and not self._quitting:
            self.main.show()

    def close_overlay(self):
        with self._ov_lock:
            ov, self.overlay = self.overlay, None
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass
        if self.main is not None:
            self.main.show()
        return True

    def show_main(self):
        if self.main is not None:
            self.main.show()
        return True

    def set_on_top(self, on: bool):
        try:
            self.main.on_top = bool(on)
            return True
        except Exception:
            return False

    # -------------------------------------------------- 对话框
    def pick_file(self, title, exts):
        pat = ";".join(f"*.{e}" for e in exts)
        try:
            res = self.main.create_file_dialog(
                self.wv.FileDialog.OPEN, file_types=(f"音频视频 ({pat})", "全部文件 (*.*)"))
        except Exception:
            return super().pick_file(title, exts)
        return res[0] if res else None

    def pick_folder(self, title, start=""):
        try:
            res = self.main.create_file_dialog(self.wv.FileDialog.FOLDER, directory=start or "")
        except Exception:
            return super().pick_folder(title, start)
        return res[0] if res else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="livetranslator")
    ap.add_argument("--browser", action="store_true", help="在浏览器里打开界面")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-audio", action="store_true", help="不启动音源（演示 / 只看记录库）")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    load_config()
    engine = Engine()
    api = Api(engine)
    server = AppServer(api, args.port)
    server.start()
    engine.start(with_audio=False)

    def start_audio():
        if not args.no_audio:
            engine.start_audio()

    webview = None
    if not args.browser:
        try:
            import webview  # type: ignore
        except Exception as e:
            print(f"ℹ️  未安装 pywebview（{e}），改用浏览器打开界面。安装：pip install pywebview")

    if webview is None:
        api.shell = Shell()
        start_audio()
        url = f"{server.url}"
        print(f"✓ 界面已就绪：{url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在保存…")
        finally:
            engine.shutdown()
            server.stop()
        return

    shell = NativeShell(webview, server, engine)
    api.shell = shell
    shell.create_main()
    try:
        webview.start(start_audio, debug=args.debug, private_mode=True)
    finally:
        if engine.session is not None or engine.busy():
            engine.shutdown()
        else:
            engine.stop_flag.set()
            for s in (engine.mic, engine.media):
                try:
                    if s is not None:
                        s.stop()
                except Exception:
                    pass
            if engine._llm_server:
                engine._llm_server.stop()
        save_config()
        server.stop()


if __name__ == "__main__":
    main(sys.argv[1:])

_ = AUDIO_EXTS
