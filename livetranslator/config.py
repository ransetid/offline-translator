"""配置（外置 JSON）+ 术语表 + 语言常量。

数据根默认 ~/Library/Application Support/LiveTranslator；可用环境变量 LT_HOME 覆盖（测试/便携）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("LT_HOME") or
                  (Path.home() / "Library" / "Application Support" / "LiveTranslator"))
CONFIG_PATH = CONFIG_DIR / "config.json"
TERMS_PATH = CONFIG_DIR / "terms.json"
OLD_OUTPUT_DIR = str(Path.home() / "live_translator_records")

MIC = "麦克风"          # 源标签（会写进转录的 speaker 字段，保持与旧版一致）
MEDIA = "媒体"
SOURCE_KEYS = {"mic": MIC, "media": MEDIA}

LANGS = {
    "zh": {"name": "中文", "short": "中"},
    "en": {"name": "英文", "short": "英"},
    "ja": {"name": "日文", "short": "日"},
    "ko": {"name": "韩文", "short": "韩"},
}
LANG_PAIRS = ["zh-en", "zh-ja", "zh-ko", "en-ja"]

SUMMARY_TEMPLATES = {
    "通用会议": ("一、核心内容概要\n二、各方主要观点\n三、达成的共识或结论\n"
             "四、待办事项（注明责任方与时间，如有）\n五、章节速览（按时间顺序列出各议题）"),
    "客户沟通": ("一、客户背景与诉求\n二、我方回应与方案\n三、客户顾虑 / 异议\n"
             "四、下一步行动（责任方 + 时间）\n五、商机判断与风险"),
    "访谈调研": ("一、受访者概况\n二、关键发现（附原话要点）\n三、痛点与需求\n"
             "四、意外洞察\n五、后续追问建议"),
    "课程讲座": ("一、主题与结构\n二、核心概念（逐条解释）\n三、例子与案例\n"
             "四、重点 / 易错点\n五、复习问题（3-5 个）"),
}

DEFAULTS: dict = {
    "omlx_url": "http://127.0.0.1:16578",
    "api_key": "1234",
    "chat_model": "gemma-4-e2b-it-4bit",
    "asr_model": "SenseVoiceSmall",
    "translate_mode": "zh-en",              # 语言对 a-b：检测到 a 译成 b，检测到其他一律译成 a
    "translate_context": False,             # 把上一句作为参考上文（大模型再开，小模型易串句）

    "self_host_llm": True,                  # 无外部后端时，子进程起 mlx_lm.server 兜底
    "llm_model_repo": "mlx-community/gemma-4-e2b-it-4bit",
    "llm_port": 8080,
    "onboarded": False,

    "mic_enabled": True,
    "system_enabled": True,
    "mic_gain": 1.0,
    "system_gain": 1.0,
    "silence_threshold_mic": 0.010,
    "silence_threshold_sys": 0.006,
    "system_capture_app": "",
    "system_fallback_device_keyword": "BlackHole",
    "system_audio_planar": False,

    "diarize_enabled": True,
    "speaker_model": str(CONFIG_DIR / "models" / "campplus_zh.onnx"),
    "diarize_threshold": 0.55,
    "diarize_max_speakers": 8,
    "diarize_global_threshold": 0.70,
    "speakers_store": str(CONFIG_DIR / "speakers.json"),

    "summary_template": "通用会议",
    "summary_language": "zh",               # zh / en / auto（跟随会议主语言）

    "output_dir": str(CONFIG_DIR),

    # 界面偏好（只影响外观，不影响流水线）
    "ui_theme": "dark",                     # dark / light / system
    "ui_view": "timeline",                  # timeline / columns
    "ui_on_top": True,
    "ui_font_scale": 1.0,
    "subtitle_font_size": 26,
    "subtitle_opacity": 0.86,
    "subtitle_show_source": True,
}

# 设置页可编辑字段 → 类型（save_settings 用来校验/转换）
FIELD_TYPES: dict[str, type] = {
    k: type(v) for k, v in DEFAULTS.items() if k not in ("onboarded",)
}

CFG: dict = {}
_lock = threading.RLock()


def load_config() -> dict:
    with _lock:
        CFG.clear()
        CFG.update(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                CFG.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                print(f"⚠️  配置文件损坏，使用默认配置: {e}", file=sys.stderr)
            changed = False
            if CFG.get("output_dir") in (OLD_OUTPUT_DIR, str(CONFIG_DIR / "records")):
                CFG["output_dir"] = DEFAULTS["output_dir"]
                changed = True
            if CFG.get("asr_model") == "whisper-large-v3-turbo-q4":
                CFG["asr_model"] = DEFAULTS["asr_model"]
                changed = True
            if CFG.get("translate_mode") not in LANG_PAIRS:
                CFG["translate_mode"] = DEFAULTS["translate_mode"]
                changed = True
            if CFG.get("summary_template") not in SUMMARY_TEMPLATES:
                CFG["summary_template"] = DEFAULTS["summary_template"]
                changed = True
            if changed:
                save_config()
        else:
            save_config()
        return CFG


def save_config():
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)            # 原子替换：写一半崩溃也不会把配置写坏


def update_config(values: dict) -> dict:
    """校验并写入一批设置。返回 {key: 错误信息}（空 = 全部成功）。"""
    errors: dict[str, str] = {}
    clean: dict = {}
    for k, v in values.items():
        t = FIELD_TYPES.get(k)
        if t is None:
            continue
        try:
            if t is bool:
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif t is float:
                v = float(v)
            elif t is int:
                v = int(float(v))
            else:
                v = str(v).strip()
        except (TypeError, ValueError):
            errors[k] = "格式不对"
            continue
        if k == "omlx_url":
            v = v.rstrip("/")
            if not v.startswith(("http://", "https://")):
                errors[k] = "需以 http:// 或 https:// 开头"
                continue
        if k == "translate_mode" and v not in LANG_PAIRS:
            errors[k] = "不支持的语言对"
            continue
        if k == "summary_template" and v not in SUMMARY_TEMPLATES:
            errors[k] = "未知模板"
            continue
        if k.startswith("silence_threshold") and not (0.0005 <= v <= 0.2):
            errors[k] = "建议在 0.0005 ~ 0.2 之间"
            continue
        if k in ("diarize_threshold", "diarize_global_threshold") and not (0.1 <= v <= 0.99):
            errors[k] = "应在 0.1 ~ 0.99 之间"
            continue
        if k == "output_dir" and not v:
            errors[k] = "不能为空"
            continue
        clean[k] = v
    if not errors:
        with _lock:
            CFG.update(clean)
            save_config()
    return errors


def lang_pair() -> tuple[str, str]:
    a, b = (CFG.get("translate_mode") or "zh-en").split("-", 1)
    return a, b


def target_lang(lang: str) -> str:
    a, b = lang_pair()
    return b if lang == a else a


def auth_headers(api_key: str | None = None) -> dict:
    key = CFG.get("api_key", "") if api_key is None else api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


# ---------------------------- 术语表 ----------------------------
# terms.json = [{"term": 规范写法, "aliases": [常见错写/别名...], "translation": 指定译文}]
_terms_cache: list[dict] | None = None


def load_terms() -> list[dict]:
    global _terms_cache
    if _terms_cache is None:
        _terms_cache = []
        if TERMS_PATH.exists():
            try:
                data = json.loads(TERMS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    _terms_cache = [t for t in data if isinstance(t, dict)]
            except (json.JSONDecodeError, OSError):
                pass
    return _terms_cache


def save_terms(terms: list[dict]) -> list[dict]:
    global _terms_cache
    clean = []
    seen = set()
    for t in terms or []:
        term = str(t.get("term", "")).strip()
        if not term or term in seen:
            continue
        seen.add(term)
        aliases = t.get("aliases", [])
        if isinstance(aliases, str):
            aliases = aliases.replace("，", ",").split(",")
        aliases = [a.strip() for a in aliases if a and a.strip() and a.strip() != term]
        clean.append({"term": term, "aliases": aliases,
                      "translation": str(t.get("translation", "") or "").strip()})
    _terms_cache = clean
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TERMS_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def apply_terms(text: str) -> str:
    """精确替换已知别名/错写 → 规范术语（长别名优先，避免短别名先替换掉长别名的一部分）。"""
    if not text:
        return text
    pairs = []
    for t in load_terms():
        term = (t.get("term") or "").strip()
        for alias in t.get("aliases", []):
            alias = (alias or "").strip()
            if term and alias and alias != term:
                pairs.append((alias, term))
    for alias, term in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(alias, term)
    return text


def terms_glossary() -> str:
    pairs = [f"{t['term']}→{t['translation']}" for t in load_terms()
             if t.get("term") and t.get("translation")]
    return "；".join(pairs)
