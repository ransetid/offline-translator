"""UI ↔ 引擎 的 API 层（JSON 进 / JSON 出）。

所有方法都可从 HTTP（/api/<name>）调用；参数为关键字参数。原生能力（文件对话框、
字幕窗口、置顶、剪贴板…）由 Shell 提供：pywebview 外壳一套，浏览器模式一套（退化实现）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import __version__, backend, config
from .audio import SystemAudioSource, list_input_devices
from .config import (CFG, CONFIG_DIR, CONFIG_PATH, LANG_PAIRS, LANGS, SUMMARY_TEMPLATES,
                     lang_pair, load_terms, save_config, save_terms, update_config)
from .engine import Engine

AUDIO_EXTS = ("mp3", "m4a", "wav", "aac", "mp4", "mov", "m4v", "flac", "ogg", "webm", "mkv")


class Shell:
    """浏览器模式下的原生能力兜底实现。pywebview 外壳会覆盖其中大部分。"""
    native = False

    def _open(self, *args):
        cmd = ["open", *args] if sys.platform == "darwin" else ["xdg-open", *args[-1:]]
        try:
            subprocess.Popen(cmd)
        except OSError:
            pass

    def pick_file(self, title: str, exts: tuple) -> str | None:
        if sys.platform == "darwin":
            types = ", ".join(f'"{e}"' for e in exts)
            script = f'POSIX path of (choose file with prompt "{title}" of type {{{types}}})'
            return _osascript(script)
        return None

    def pick_folder(self, title: str, start: str = "") -> str | None:
        if sys.platform == "darwin":
            return _osascript(f'POSIX path of (choose folder with prompt "{title}")')
        return None

    def reveal(self, path: str):
        p = Path(path)
        if sys.platform == "darwin":
            self._open("-R", str(p)) if p.is_file() else self._open(str(p))
        else:
            self._open(str(p if p.is_dir() else p.parent))

    def open_url(self, url: str):
        self._open(url)

    def copy(self, text: str) -> bool:
        try:
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
                return True
        except (OSError, subprocess.CalledProcessError):
            pass
        return False

    def open_overlay(self):
        return False

    def close_overlay(self):
        return False

    def show_main(self):
        return False

    def set_on_top(self, on: bool):
        return False

    def quit(self):
        return False


def _osascript(script: str) -> str | None:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
        out = r.stdout.strip()
        return out or None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Api:
    def __init__(self, engine: Engine, shell: Shell | None = None):
        self.engine = engine
        self.shell = shell or Shell()

    # ---------------------------------------------------------------- 启动快照
    def bootstrap(self) -> dict:
        e = self.engine
        return {
            "version": __version__,
            "platform": sys.platform,
            "native": self.shell.native,
            "config": self._public_config(),
            "langs": LANGS,
            "lang_pairs": LANG_PAIRS,
            "templates": list(SUMMARY_TEMPLATES),
            "session": e.session_state(),
            "entries": e.live_entries(),
            "sources": e.sources_state(),
            "backend": e.backend_state,
            "jobs": e.list_jobs(),
            "paths": {"config": str(CONFIG_PATH), "data": str(CONFIG_DIR),
                      "records": str(Path(CFG["output_dir"]) / "records")},
            "terms_count": len(load_terms()),
        }

    @staticmethod
    def _public_config() -> dict:
        c = {k: v for k, v in CFG.items() if not k.startswith("_")}
        c["lang_pair"] = list(lang_pair())
        return c

    # ---------------------------------------------------------------- 会话
    def start_session(self, force: bool = False) -> dict:
        return self.engine.start_session(force=force)

    def end_session(self, template: str | None = None) -> dict:
        jid = self.engine.end_session(template)
        return {"ok": True, "job": jid}

    def session_state(self) -> dict:
        return self.engine.session_state()

    def live_entries(self) -> list:
        return self.engine.live_entries()

    def update_entry(self, id: int, record_id: str | None = None, **fields) -> dict | None:
        if record_id:
            return self.engine.update_record_entry(record_id, id, **fields)
        return self.engine.live_update(id, **fields)

    def retranslate(self, id: int) -> bool:
        return self.engine.retranslate(id)

    # ---------------------------------------------------------------- 音源
    def set_source(self, key: str, enabled=None, gain=None) -> dict:
        return self.engine.set_source(key, enabled, gain)

    def set_capture_app(self, bundle: str = "") -> dict:
        return self.engine.set_capture_app(bundle)

    def refresh_apps(self) -> list:
        return self.engine.refresh_apps()

    def set_diarize(self, on: bool) -> dict:
        CFG["diarize_enabled"] = bool(on)
        save_config()
        self.engine.emit_sources()
        return {"ok": True}

    def retry_media(self) -> dict:
        return self.engine.retry_media()

    def request_permission(self) -> dict:
        SystemAudioSource.request_permission()
        self.shell.open_url("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
        return {"ok": True}

    def calibrate(self, key: str) -> dict:
        return self.engine.calibrate(key)

    def input_devices(self) -> list:
        return list_input_devices()

    # ---------------------------------------------------------------- 后端 / 设置
    def check_backend(self) -> dict:
        return self.engine.check_backend()

    def test_connection(self, url: str, api_key: str = "", asr_model: str = "", chat_model: str = "") -> dict:
        url = (url or "").rstrip("/")
        try:
            models = backend.list_models(url, api_key)
        except backend.BackendError as e:
            return {"ok": False, "error": str(e), "models": []}
        return {"ok": True, "models": models,
                "asr_ok": backend.model_loaded(asr_model, models),
                "chat_ok": backend.model_loaded(chat_model, models)}

    def save_settings(self, values: dict) -> dict:
        before = dict(CFG)
        errors = update_config(values or {})
        if errors:
            return {"ok": False, "errors": errors}
        e = self.engine
        e.apply_thresholds()
        if before.get("system_capture_app") != CFG.get("system_capture_app"):
            e.set_capture_app(CFG.get("system_capture_app", ""))
        if any(before.get(k) != CFG.get(k) for k in ("omlx_url", "api_key", "asr_model", "chat_model")):
            if before.get("chat_model") != CFG.get("chat_model"):
                backend.RUNTIME.update(chat_url=None, chat_model=None)   # 换模型 = 放弃自托管兜底
            e._check_backend_async()
        if before.get("ui_on_top") != CFG.get("ui_on_top"):
            self.shell.set_on_top(bool(CFG["ui_on_top"]))
        e.emit("config", self._public_config())
        return {"ok": True, "config": self._public_config()}

    def set_pref(self, key: str, value) -> dict:
        """界面偏好的轻量写入（视图、主题、字号…）。"""
        if not key.startswith(("ui_", "subtitle_")) and key not in ("onboarded", "translate_mode",
                                                                    "summary_template"):
            return {"ok": False}
        if key == "onboarded":
            CFG["onboarded"] = bool(value)
            save_config()
            return {"ok": True}
        res = self.save_settings({key: value})
        return res

    def get_terms(self) -> list:
        return load_terms()

    def save_terms(self, terms: list) -> dict:
        clean = save_terms(terms)
        return {"ok": True, "terms": clean}

    def onboarding(self) -> dict:
        b = self.engine.check_backend()
        src = self.engine.sources_state()
        return {
            "backend": b,
            "mic": src["mic"]["available"],
            "media": src["media"]["available"],
            "media_needs_permission": src["media"].get("needs_permission", False),
            "media_supported": src["media"].get("supported_sck", False),
            "speaker_model": Path(CFG["speaker_model"]).exists(),
        }

    def open_omlx(self) -> dict:
        if sys.platform == "darwin":
            self.shell._open("-a", "oMLX")
        return {"ok": sys.platform == "darwin"}

    # ---------------------------------------------------------------- 记录库
    def list_records(self, query: str = "") -> list:
        return self.engine.list_records(query)

    def get_record(self, id: str) -> dict:
        return self.engine.get_record(id)

    def rename_record(self, id: str, title: str) -> dict:
        return self.engine.rename_record(id, title)

    def delete_record(self, id: str) -> dict:
        return self.engine.delete_record(id)

    def resummarize(self, id: str, template: str | None = None) -> dict:
        return {"job": self.engine.resummarize(id, template)}

    def recover(self, id: str) -> dict:
        return {"job": self.engine.recover(id)}

    def reprocess(self, id: str) -> dict:
        return {"job": self.engine.reprocess(id)}

    def export_record(self, id: str, fmt: str, reveal: bool = True) -> dict:
        path = self.engine.export_record(id, fmt)
        if reveal:
            self.shell.reveal(path)
        return {"ok": True, "path": path}

    def reveal_record(self, id: str) -> dict:
        self.shell.reveal(str(self.engine._dir(id)))
        return {"ok": True}

    def speakers(self, id: str) -> list:
        return self.engine.speakers(id)

    def name_speakers(self, id: str, mapping: dict) -> dict:
        return self.engine.name_speakers(id, mapping)

    # ---------------------------------------------------------------- 任务 / 导入
    def jobs(self) -> list:
        return self.engine.list_jobs()

    def cancel_job(self, id: str) -> dict:
        return {"ok": self.engine.cancel_job(id)}

    def pick_and_import(self) -> dict:
        path = self.shell.pick_file("选择要导入的音频或视频", AUDIO_EXTS)
        if not path:
            return {"ok": False, "cancelled": True, "native": self.shell.native}
        return {"ok": True, "job": self.engine.import_file(path), "name": Path(path).name}

    def import_path(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.is_file():
            return {"ok": False, "error": "文件不存在"}
        return {"ok": True, "job": self.engine.import_file(str(p)), "name": p.name}

    # ---------------------------------------------------------------- 原生能力
    def pick_folder(self, start: str = "") -> dict:
        p = self.shell.pick_folder("选择记录保存目录", start)
        return {"ok": bool(p), "path": p}

    def reveal(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.exists() and not p.suffix:      # 还没建过的目录也能直接打开
            p.mkdir(parents=True, exist_ok=True)
        self.shell.reveal(str(p))
        return {"ok": True}

    def copy(self, text: str) -> dict:
        return {"ok": self.shell.copy(text or "")}

    def open_overlay(self) -> dict:
        return {"ok": bool(self.shell.open_overlay())}

    def close_overlay(self) -> dict:
        return {"ok": bool(self.shell.close_overlay())}

    def show_main(self) -> dict:
        return {"ok": bool(self.shell.show_main())}

    def set_on_top(self, on: bool) -> dict:
        CFG["ui_on_top"] = bool(on)
        save_config()
        return {"ok": bool(self.shell.set_on_top(bool(on)))}

    def quit(self) -> dict:
        return {"ok": bool(self.shell.quit())}

    # 白名单：只有这些名字能被 HTTP 调用
    EXPOSED = {
        "bootstrap", "start_session", "end_session", "session_state", "live_entries", "update_entry",
        "retranslate", "set_source", "set_capture_app", "refresh_apps", "set_diarize", "retry_media",
        "request_permission", "calibrate", "input_devices", "check_backend", "test_connection",
        "save_settings", "set_pref", "get_terms", "save_terms", "onboarding", "open_omlx",
        "list_records", "get_record", "rename_record", "delete_record", "resummarize", "recover",
        "reprocess", "export_record", "reveal_record", "speakers", "name_speakers", "jobs",
        "cancel_job", "pick_and_import", "import_path", "pick_folder", "reveal", "copy",
        "open_overlay", "close_overlay", "show_main", "set_on_top", "quit",
    }


_ = config  # 保持导入（测试里会 monkeypatch config）
