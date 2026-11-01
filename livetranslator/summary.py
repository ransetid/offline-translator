"""自动标题 + 结构化纪要（模板化；长会 map-reduce，避免撑爆小模型上下文）。"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from . import backend
from .config import CFG, LANGS, MEDIA, MIC, SUMMARY_TEMPLATES

CHUNK_ENTRIES = 80


def fmt_ts(sec) -> str:
    s = int(sec or 0)
    h, r = divmod(s, 3600)
    return f"{h}:{r // 60:02d}:{r % 60:02d}" if h else f"{r // 60:02d}:{r % 60:02d}"


def entries_text(entries) -> str:
    lines = []
    for e in entries:
        star = "★" if e.get("starred") else ""
        lines.append(f"[{fmt_ts(e.get('t_start'))}][{e.get('speaker', '')}]{star} {e.get('source', '')}")
    return "\n".join(lines)


def _out_lang(entries) -> str:
    want = CFG.get("summary_language", "zh")
    if want == "auto":
        c = Counter(e.get("lang") for e in entries if e.get("lang"))
        want = c.most_common(1)[0][0] if c else "zh"
    return LANGS.get(want, LANGS["zh"])["name"]


def auto_title(entries) -> str:
    snippet = entries_text(entries)[:2000]
    t = backend.chat(
        "给下面这场会议起一个简洁标题，不超过15个字，用会议的主要语言，"
        f"只输出标题本身，不要引号、不要句末标点：\n\n{snippet}", timeout=60)
    t = (t.splitlines()[0] if t else "").strip().strip('"\'《》「」# ')
    return t[:30]


def summarize(entries: list, template: str | None = None,
              progress: Callable[[float, str], None] | None = None) -> str:
    template = template if template in SUMMARY_TEMPLATES else CFG.get("summary_template", "通用会议")
    sections = SUMMARY_TEMPLATES.get(template, SUMMARY_TEMPLATES["通用会议"])
    lang = _out_lang(entries)
    head = (f"[{MIC}] 为本地麦克风发言，[{MEDIA}] 为本机播放的会议软件/媒体声音；"
            "带 ★ 的句子是用户手动标记的重点，请优先体现。")
    p = progress or (lambda *_: None)
    if len(entries) <= CHUNK_ENTRIES:
        p(0.2, "生成纪要")
        out = backend.chat(
            f"你是专业的会议记录员。{head}以下是完整转录（含时间戳）：\n\n{entries_text(entries)}\n\n"
            f"请用{lang}输出纪要，Markdown 格式，按以下结构组织（无内容的小节写“无”）：\n{sections}",
            timeout=300)
        p(1.0, "完成")
        return out
    chunks = [entries[i:i + CHUNK_ENTRIES] for i in range(0, len(entries), CHUNK_ENTRIES)]
    notes = []
    for i, chunk in enumerate(chunks):
        p(0.1 + 0.75 * i / len(chunks), f"分段整理 {i + 1}/{len(chunks)}")
        note = backend.chat(
            f"这是一场会议的第 {i + 1}/{len(chunks)} 段转录。{head}\n"
            f"请用{lang} 5-8 条要点概括，保留关键结论、数字与待办：\n\n{entries_text(chunk)}",
            timeout=200)
        notes.append(f"【约 {fmt_ts(chunk[0].get('t_start'))} 起】\n{note}")
    p(0.9, "合并纪要")
    out = backend.chat(
        "以下是一场会议各段的要点笔记（按时间顺序），请合并成完整纪要，去重、保留关键数字。"
        f"用{lang}输出 Markdown，结构如下：\n{sections}\n\n" + "\n\n".join(notes),
        timeout=300)
    p(1.0, "完成")
    return out
