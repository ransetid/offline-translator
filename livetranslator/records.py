#!/usr/bin/env python3
"""
会议记录持久化 + 记录库
=======================
一场会 = records/{id}/，是后续「项目管理 / 搜索 / 重生成 / 说话人命名 / 导出」的地基。

目录结构：
  records/{id}/                       id = 20260630-143022（连字符）
    meta.json        起点即写(summary_status=recording)，结束补 duration/status/title
    audio/{src}.wav  各源原生采样率增量落盘（mic 16k / media 48k）；独立 record_queue 喂，
                     绝不从 segmenter 的 raw_queue 取块，避免饿死分句
    transcript.jsonl 每条定稿即追加一行（崩溃安全；JSON 数组追加要整写、易写坏，故用 JSONL）
    embeddings.npz   稀疏：仅 final+媒体+开了区分的句子有 192d 向量
    summary.md       结束时生成

崩溃恢复：起点就写 meta(status=recording)，转录逐条落盘；下次启动扫到 recording 的孤儿目录
可提示恢复。空会议结束时整目录删除，不留孤儿。
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import re
import shutil
import tempfile
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

# 源标签 → 文件名 slug（避免中文文件名带来的跨平台坑）
_SRC_SLUG = {"麦克风": "mic", "媒体": "media", "导入": "import"}

# transcript / 重写时持久化的字段
_ROW_KEYS = ("id", "time", "t_start", "t_end", "speaker", "src", "lang", "source", "translation",
             "starred", "edited")
_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


def _slug(label: str) -> str:
    return _SRC_SLUG.get(label) or ("".join(c for c in label if c.isascii() and c.isalnum()) or "src")


class _WavWriter:
    """增量写 16-bit PCM 单声道 WAV。"""

    # WAV 的 RIFF/data 长度字段是 32 位无符号，超过 wave 模块回填头就崩（'L' format）。
    # 留 36 字节头余量。48kHz 单声道 16-bit 约 12.4 小时封顶。
    _MAX_DATA_BYTES = 0xFFFFFFFF - 36

    def __init__(self, path: Path, rate: int):
        self.path = path
        self.rate = rate
        self.frames = 0
        self._capped = False
        self._w = wave.open(str(path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(rate)

    def write(self, mono_f32: np.ndarray):
        pcm = (np.clip(mono_f32, -1.0, 1.0) * 32767).astype("<i2")
        b = pcm.tobytes()
        # ponytail: 到顶就停写，别崩。要保留 4GB 以上录音才需要分段成多文件。
        if self.frames * 2 + len(b) > self._MAX_DATA_BYTES:
            if not self._capped:
                self._capped = True
                print(f"音频已达 WAV 4GB 上限，后续块丢弃: {self.path.name}", file=sys.stderr)
            return
        self._w.writeframes(b)
        self.frames += len(mono_f32)

    def close(self):
        try:
            self._w.close()
        except Exception:
            pass


class Record:
    """一场会议的可写记录。线程安全：音频写盘线程与识别线程并发访问。"""

    def __init__(self, root, translate_mode: str, app: str = ""):
        base = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.root = Path(root)
        (self.root / "records").mkdir(parents=True, exist_ok=True)
        self.id, n = base, 1
        while True:                       # 同一秒内连开两场（或会议+导入）：原子建目录，撞了加后缀
            try:
                (self.root / "records" / self.id).mkdir(exist_ok=False)
                break
            except FileExistsError:
                n += 1
                self.id = f"{base}-{n}"
        self.dir = self.root / "records" / self.id
        (self.dir / "audio").mkdir(parents=True, exist_ok=True)
        self.start_perf = time.time()
        self._lock = threading.Lock()
        self._closed = False    # finalize/discard 后置 True：拦住写盘线程写已关闭的 wav
        self._writers: dict[str, _WavWriter] = {}
        self._emb: dict[str, list] = {}
        self._tr_path = self.dir / "transcript.jsonl"
        self._n_entries = 0
        self.meta = {
            "id": self.id,
            "title": "",
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_s": 0,
            "sources": [],          # [{label, wav, sample_rate}]
            "app": app,
            "translate_mode": translate_mode,
            "speakers": [],         # [{local_id, name, global_id}]
            "summary_status": "recording",
            "summary_template": "",
            "entry_count": 0,
            "starred_count": 0,
            "langs": [],
        }
        self._write_meta()

    # ---- 时间戳：相对录制起点的秒偏移 ----
    def offset(self) -> float:
        return time.time() - self.start_perf

    # ---- 音频（写盘线程调用） ----
    def write_audio(self, label: str, block: np.ndarray, rate: int):
        with self._lock:
            if self._closed:        # 已收尾：写盘线程晚到的块直接丢，别写关闭的 wav
                return
            w = self._writers.get(label)
            if w is None:
                fn = f"{_slug(label)}.wav"
                w = _WavWriter(self.dir / "audio" / fn, rate)
                self._writers[label] = w
                self.meta["sources"].append(
                    {"label": label, "wav": f"audio/{fn}", "sample_rate": rate,
                     "offset": round(self.offset(), 3)})   # 该音轨相对会议起点的起始偏移
                self._write_meta()
            w.write(block)

    # ---- 转录（识别线程调用，逐条追加，崩溃安全） ----
    def append_entry(self, entry: dict):
        row = {k: entry.get(k) for k in _ROW_KEYS}
        with self._lock:
            with self._tr_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._n_entries += 1
            emb = entry.get("embedding")
            if emb is not None:
                self._emb[str(entry["id"])] = list(emb)

    def is_empty(self) -> bool:
        return self._n_entries == 0

    # ---- 结束 ----
    def finalize(self, entries=None, title: str = "", summary_status: str = "done"):
        """收尾：关音频；用最终 entries（含译文）整体重写 transcript；存 embeddings；补 meta。"""
        with self._lock:
            self._closed = True
            for w in self._writers.values():
                w.close()
            if entries is not None:
                write_transcript(self.dir, entries)       # 原子替换：重写途中崩溃也不丢逐条日志
            if self._emb:
                save_embeddings(self.dir, self._emb)
            if title:
                self.meta["title"] = title
            if entries is not None:
                self.meta.update(_entry_stats(entries))
            self.meta["duration_s"] = round(self.offset(), 1)
            self.meta["summary_status"] = summary_status
            self._write_meta()

    def set_meta(self, **kw):
        with self._lock:
            self.meta.update(kw)
            self._write_meta()

    def write_summary(self, text: str):
        (self.dir / "summary.md").write_text(text, encoding="utf-8")

    def set_speakers(self, speakers: list):
        with self._lock:
            self.meta["speakers"] = speakers
            self._write_meta()

    def discard(self):
        """空会议：关闭并删除整目录。"""
        with self._lock:
            self._closed = True
            for w in self._writers.values():
                w.close()
        try:
            shutil.rmtree(self.dir)
        except OSError:
            pass

    def _write_meta(self):
        _write_json(self.dir / "meta.json", self.meta)


# 记录目录里的读-改-写（meta / transcript）统一串行：会议收尾、纪要任务、界面改名可能同时发生
_io_lock = threading.RLock()


def _atomic_write(path: Path, text: str):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, data):
    with _io_lock:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def save_embeddings(rec_dir, embs: dict):
    d = Path(rec_dir)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".emb.", suffix=".npz")
    os.close(fd)
    np.savez_compressed(tmp, **{str(k): np.asarray(v, dtype=np.float32) for k, v in embs.items()})
    os.replace(tmp, d / "embeddings.npz")


def _entry_stats(entries) -> dict:
    langs = []
    for e in entries:
        lg = e.get("lang")
        if lg and lg not in langs:
            langs.append(lg)
    spk = []
    for e in entries:
        s = e.get("speaker")
        if s and s not in spk:
            spk.append(s)
    return {"entry_count": len(entries),
            "starred_count": sum(1 for e in entries if e.get("starred")),
            "langs": langs, "speaker_names": spk[:20]}


def audio_writer_worker(record_queue: "queue.Queue", stop_flag: "threading.Event", get_record):
    """独立线程：消费 record_queue，把每源原始块写进当前 Record 的 wav。"""
    while not stop_flag.is_set():
        try:
            label, block, rate = record_queue.get(timeout=0.3)
        except queue.Empty:
            continue
        rec = get_record()
        if rec is None:
            continue
        try:
            rec.write_audio(label, block, rate)
        except Exception as e:
            print(f"音频写盘失败: {e}", file=sys.stderr)


# ---------------------------- 记录库（只读查询） ----------------------------

def list_records(root) -> list[dict]:
    """按时间倒序返回所有记录的 meta（附 _dir）。"""
    out: list[dict] = []
    rdir = Path(root) / "records"
    if not rdir.exists():
        return out
    for d in sorted([p for p in rdir.iterdir() if p.is_dir()], reverse=True):
        mp = d / "meta.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
                meta["_dir"] = str(d)
                out.append(meta)
            except (json.JSONDecodeError, OSError):
                pass
    return out


def speaker_embeddings(rec_dir) -> dict:
    """{local_speaker -> [向量...]}：把 embeddings.npz 按 transcript 的 speaker 分组，
    供跨会议「建议」命名匹配。"""
    p = Path(rec_dir) / "embeddings.npz"
    if not p.exists():
        return {}
    out: dict[str, list] = {}
    try:
        z = np.load(p)
    except (OSError, ValueError):
        return {}
    id2spk = {str(e.get("id")): e.get("speaker") for e in load_transcript(rec_dir)}
    for uid in z.files:
        spk = id2spk.get(uid)
        if spk:
            out.setdefault(spk, []).append(z[uid])
    return out


def load_transcript(rec_dir) -> list[dict]:
    out: list[dict] = []
    p = Path(rec_dir) / "transcript.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def record_dir(root, rec_id: str) -> Path:
    """id → 目录（校验格式，防路径穿越）。不存在抛 FileNotFoundError。"""
    if not rec_id or not _ID_RE.match(rec_id):
        raise FileNotFoundError(rec_id)
    d = Path(root) / "records" / rec_id
    if not (d / "meta.json").exists():
        raise FileNotFoundError(rec_id)
    return d


def read_meta(rec_dir) -> dict:
    meta = json.loads((Path(rec_dir) / "meta.json").read_text(encoding="utf-8"))
    meta["_dir"] = str(rec_dir)
    return meta


def update_meta(rec_dir, **kw) -> dict:
    d = Path(rec_dir)
    with _io_lock:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta.update(kw)
        _write_json(d / "meta.json", meta)
    meta["_dir"] = str(d)
    return meta


def write_transcript(rec_dir, entries: list):
    p = Path(rec_dir) / "transcript.jsonl"
    text = "".join(json.dumps({k: e.get(k) for k in _ROW_KEYS}, ensure_ascii=False) + "\n" for e in entries)
    with _io_lock:
        _atomic_write(p, text)


def update_entry(rec_dir, entry_id, **fields) -> dict | None:
    """就地修改一条转录（纠错 / 标星）。返回修改后的条目。"""
    with _io_lock:
        tr = load_transcript(rec_dir)
        hit = None
        for e in tr:
            if str(e.get("id")) == str(entry_id):
                e.update(fields)
                hit = e
                break
        if hit is None:
            return None
        write_transcript(rec_dir, tr)
        update_meta(rec_dir, **_entry_stats(tr))
    return hit


def read_summary(rec_dir) -> str:
    p = Path(rec_dir) / "summary.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_summary(rec_dir, text: str):
    _atomic_write(Path(rec_dir) / "summary.md", text)


def delete_record(rec_dir):
    shutil.rmtree(rec_dir, ignore_errors=True)


def wav_duration(path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (wave.Error, OSError, EOFError):
        return 0.0


def close_orphan(rec_dir) -> dict:
    """上次异常退出的记录：按已落盘的音频/转录补齐时长与统计，状态改为待生成纪要。"""
    d = Path(rec_dir)
    meta = read_meta(d)
    tr = load_transcript(d)
    dur = max([wav_duration(d / s["wav"]) for s in meta.get("sources", [])] +
              [float(e.get("t_end") or 0) for e in tr] + [0.0])
    return update_meta(d, duration_s=round(dur, 1), summary_status="pending",
                       **_entry_stats(tr))


def find_orphans(root) -> list[dict]:
    """上次异常退出留下的、仍处于 recording 状态的记录。"""
    return [m for m in list_records(root) if m.get("summary_status") == "recording"]


def migrate_legacy_records(old_root, new_root) -> int:
    """把旧位置 old_root/records/* 移到 new_root/records/*（移动、不覆盖同名）。返回迁移条数。
    旧的扁平 transcript_*.md/summary_*.md（结构化之前的）不动，留给用户自行处理。"""
    old = Path(old_root) / "records"
    new = Path(new_root) / "records"
    try:
        if not old.exists() or old.resolve() == new.resolve():
            return 0
    except OSError:
        return 0
    new.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in old.iterdir():
        if d.is_dir() and (d / "meta.json").exists() and not (new / d.name).exists():
            try:
                shutil.move(str(d), str(new / d.name))
                n += 1
            except (OSError, shutil.Error):
                pass
    return n


def rename_speaker(rec_dir, local_id: str, new_name: str):
    """把记录里某个 local_id（如「媒体·说话人2」）就地改成真名：重写 transcript + meta。"""
    d = Path(rec_dir)
    with _io_lock:
        tr = load_transcript(d)
        for e in tr:
            if e.get("speaker") == local_id:
                e["speaker"] = new_name
        write_transcript(d, tr)
        mp = d / "meta.json"
        if not mp.exists():
            return
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        spks = meta.get("speakers", [])
        for sp in spks:
            if sp.get("local_id") == local_id:
                sp["name"] = new_name
                break
        else:
            spks.append({"local_id": local_id, "name": new_name})
        meta["speakers"] = spks
        _write_json(mp, meta)


# ---------------------------- 导出 ----------------------------

_LANG_CN = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文"}


def _ts_srt(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s - int(s)) * 1000):03d}"


def to_markdown(meta: dict, transcript: list, summary: str = "") -> str:
    lines = [f"# {meta.get('title') or meta.get('id')}", ""]
    lines.append(f"_{meta.get('created_at','')} · {meta.get('duration_s',0)}s_\n")
    if summary:
        lines += [summary, "\n---\n", "## 转录\n"]
    for e in transcript:
        star = " ★" if e.get("starred") else ""
        lines.append(f"**[{e.get('speaker','')}] ({_LANG_CN.get(e.get('lang'),'')}){star}** {e.get('source','')}")
        if e.get("translation"):
            lines.append(f"> {e['translation']}")
        lines.append("")
    return "\n".join(lines)


def to_srt(transcript: list) -> str:
    """字幕：原文 + 译文。仅含有时间戳的句子。"""
    out, idx = [], 1
    for e in transcript:
        ts, te = e.get("t_start"), e.get("t_end")
        if ts is None or te is None:
            continue
        body = e.get("source", "")
        if e.get("translation"):
            body += "\n" + e["translation"]
        out.append(f"{idx}\n{_ts_srt(ts)} --> {_ts_srt(te)}\n{body}\n")
        idx += 1
    return "\n".join(out)


def to_vtt(transcript: list) -> str:
    out = ["WEBVTT", ""]
    for e in transcript:
        ts, te = e.get("t_start"), e.get("t_end")
        if ts is None or te is None:
            continue
        body = e.get("source", "")
        if e.get("translation"):
            body += "\n" + e["translation"]
        out.append(f"{_ts_srt(ts).replace(',', '.')} --> {_ts_srt(te).replace(',', '.')}\n{body}\n")
    return "\n".join(out)


def to_txt(meta: dict, transcript: list, summary: str = "") -> str:
    lines = [meta.get("title") or meta.get("id", ""), meta.get("created_at", ""), ""]
    if summary:
        lines += [summary.strip(), "", "—— 转录 ——", ""]
    for e in transcript:
        t = e.get("t_start")
        ts = f"{int(t // 60):02d}:{int(t % 60):02d} " if t is not None else ""
        lines.append(f"{ts}[{e.get('speaker', '')}] {e.get('source', '')}")
        if e.get("translation"):
            lines.append(f"    {e['translation']}")
    return "\n".join(lines) + "\n"


def to_docx(meta: dict, transcript: list, summary: str, path):
    from docx import Document
    doc = Document()
    doc.add_heading(meta.get("title") or meta.get("id"), level=0)
    doc.add_paragraph(f"{meta.get('created_at','')} · {meta.get('duration_s',0)}s")
    if summary:
        doc.add_heading("会议纪要", level=1)
        for ln in summary.splitlines():
            st = ln.strip()
            if not st:
                continue
            if st.startswith("#"):
                level = min(3, len(st) - len(st.lstrip("#")) + 1)
                doc.add_heading(st.lstrip("# ").strip(), level=level)
            elif st[:2] in ("- ", "* "):
                doc.add_paragraph(st[2:].replace("**", ""), style="List Bullet")
            else:
                doc.add_paragraph(st.replace("**", ""))
    doc.add_heading("转录", level=1)
    for e in transcript:
        p = doc.add_paragraph()
        p.add_run(f"[{e.get('speaker','')}] ").bold = True
        p.add_run(e.get("source", ""))
        if e.get("translation"):
            doc.add_paragraph(e["translation"], style="Intense Quote")
    doc.save(str(path))


# ---------------------------- 导入（PyAV 解码任意音/视频） ----------------------------

def decode_audio_file(path, target_rate: int = 16000) -> np.ndarray:
    """用 PyAV 把任意音/视频解码成 target_rate 单声道 float32。"""
    import av
    container = av.open(str(path))
    resampler = av.audio.resampler.AudioResampler(
        format="flt", layout="mono", rate=target_rate)
    chunks = []

    def _collect(frames):
        for rf in frames:
            arr = rf.to_ndarray().reshape(-1).astype(np.float32)
            if arr.size:
                chunks.append(arr)

    try:
        for frame in container.decode(audio=0):
            _collect(resampler.resample(frame))
        _collect(resampler.resample(None))   # flush
    finally:
        container.close()
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


# ---------------------------- 全局说话人声纹（跨会议「建议」命名） ----------------------------

class SpeakerStore:
    """speakers.json：每个具名说话人存原始向量数组（非漂移质心），匹配用均值。"""

    def __init__(self, path):
        self.path = Path(path)
        self.speakers: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.speakers = data
            except (json.JSONDecodeError, OSError):
                pass

    @staticmethod
    def _rep(sp) -> np.ndarray:
        embs = np.asarray(sp["embeddings"], dtype=np.float32)
        m = embs.mean(axis=0)
        return m / (float(np.linalg.norm(m)) + 1e-9)

    def match(self, emb, threshold: float):
        """返回 {global_id,name,sim} 或 None。阈值高（~0.7），宁缺毋滥。"""
        e = np.asarray(emb, dtype=np.float32)
        n = float(np.linalg.norm(e))
        if n < 1e-6 or not self.speakers:
            return None
        e = e / n
        best, best_sim = None, -1.0
        for sp in self.speakers:
            sim = float(self._rep(sp) @ e)
            if sim > best_sim:
                best, best_sim = sp, sim
        if best is not None and best_sim >= threshold:
            return {"global_id": best["global_id"], "name": best["name"], "sim": best_sim}
        return None

    def assign(self, name: str, embeddings: list, cap: int = 50) -> str:
        """把若干向量并入具名说话人（已存在则累加，否则新建）。返回 global_id。"""
        vecs = [np.asarray(x, dtype=np.float32).tolist() for x in embeddings if x is not None]
        for sp in self.speakers:
            if sp["name"] == name:
                sp["embeddings"] = (sp["embeddings"] + vecs)[-cap:]
                sp["count"] = sp.get("count", 0) + len(vecs)
                self.save()
                return sp["global_id"]
        nums = [int(s["global_id"].split("-")[-1]) for s in self.speakers
                if s.get("global_id", "").startswith("spk-") and s["global_id"].split("-")[-1].isdigit()]
        gid = f"spk-{(max(nums) + 1) if nums else 1}"
        self.speakers.append({"global_id": gid, "name": name,
                              "embeddings": vecs[-cap:], "count": len(vecs)})
        self.save()
        return gid

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.speakers, ensure_ascii=False, indent=2), encoding="utf-8")
