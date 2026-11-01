"""流水线纯逻辑：VAD 分句、语种检测、幻听过滤、说话人聚类、翻译 prompt。

这里不碰 UI、不碰全局队列——实时会话（engine）和离线导入共用同一套逻辑，也便于单测。
"""
from __future__ import annotations

import collections
import re
import string
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .config import CFG, LANGS, lang_pair, target_lang, terms_glossary

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.25
SILENCE_BLOCKS_TO_CUT = 4      # 连续 1.0s 静音 → 切句
PREROLL_BLOCKS = 3             # 句首预滚 0.75s，防吞字
MIN_UTTERANCE_SECONDS = 0.6
MAX_UTTERANCE_SECONDS = 14
INTERIM_SECONDS = 1.2          # 说话中每 1.2s 跑一次临时识别（仅快模型）

NO_SPEECH_MAX = 0.45
AVG_LOGPROB_MIN = -1.1
HALLUCINATION_BLACKLIST = {
    "you", "thank you", "thanks", "thanks for watching", "bye", "okay", "oh",
    "嗯", "啊", "哦", "谢谢", "谢谢观看", "好", "好的", "字幕由amara org社区提供",
    "ご視聴ありがとうございました", "감사합니다",
}
DEDUP_WINDOW_SECONDS = 6


# ---------------------------- 重采样 ----------------------------

def resample(audio: np.ndarray, src_rate: int, dst_rate: int = SAMPLE_RATE) -> np.ndarray:
    """优先 scipy 多相滤波（带抗混叠）；没有 scipy 退化为线性插值。"""
    if src_rate == dst_rate or len(audio) == 0:
        return audio.astype(np.float32)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(src_rate), int(dst_rate))
        return resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)
    except Exception:
        n_dst = int(len(audio) * dst_rate / src_rate)
        x_src = np.linspace(0, 1, len(audio), endpoint=False)
        x_dst = np.linspace(0, 1, n_dst, endpoint=False)
        return np.interp(x_dst, x_src, audio).astype(np.float32)


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0


# ---------------------------- VAD 分句 ----------------------------

class VadSegmenter:
    """能量 VAD 状态机。feed() 一块音频，返回事件列表：
        ("start",  uid, None)    语音开始（分配稳定 id，临时稿与定稿共用）
        ("interim", uid, audio)  说话中的累计音频（供临时识别）
        ("final",  uid, audio)   定稿片段（已含预滚）
        ("drop",   uid, None)    太短，作废
    audio 为输入采样率；调用方自行重采样。"""

    def __init__(self, threshold: float, next_id: Callable[[], int], interim: bool = False):
        self.threshold = threshold
        self.next_id = next_id
        self.interim = interim
        self.reset()

    def reset(self):
        self.buffer: list[np.ndarray] = []
        self.preroll: collections.deque = collections.deque(maxlen=PREROLL_BLOCKS)
        self.silence = 0
        self.speaking = False
        self.uid = 0
        self.interim_mark = 0
        self.total = 0

    def abort(self) -> list[tuple]:
        ev = [("drop", self.uid, None)] if self.speaking else []
        self.reset()
        return ev

    def feed(self, block: np.ndarray, rate: int) -> list[tuple]:
        is_sil = rms(block) < self.threshold
        out: list[tuple] = []
        if self.speaking:
            self.buffer.append(block)
            self.total += len(block)
            self.silence = self.silence + 1 if is_sil else 0
            if self.silence >= SILENCE_BLOCKS_TO_CUT or self.total / rate >= MAX_UTTERANCE_SECONDS:
                out.extend(self._cut(rate))
            elif (self.interim and not is_sil
                  and self.total - self.interim_mark >= INTERIM_SECONDS * rate):
                self.interim_mark = self.total
                out.append(("interim", self.uid, np.concatenate(self.buffer)))
        elif not is_sil:
            self.speaking = True
            self.uid = self.next_id()
            self.buffer = list(self.preroll) + [block]
            self.total = sum(len(b) for b in self.buffer)
            self.silence = 0
            self.interim_mark = 0
            out.append(("start", self.uid, None))
        else:
            self.preroll.append(block)
        return out

    def flush(self, rate: int) -> list[tuple]:
        return self._cut(rate) if self.speaking and self.buffer else []

    def _cut(self, rate: int) -> list[tuple]:
        audio = np.concatenate(self.buffer)
        uid = self.uid
        self.buffer, self.speaking, self.silence = [], False, 0
        self.interim_mark = self.total = 0
        self.preroll.clear()
        if len(audio) / rate >= MIN_UTTERANCE_SECONDS:
            return [("final", uid, audio)]
        return [("drop", uid, None)]


def offline_segments(audio: np.ndarray, rate: int, threshold: float):
    """整段音频离线分句（与实时同一套 VAD）。yield (t_start, t_end, seg)。"""
    blk = int(rate * BLOCK_SECONDS)
    ids = iter(range(1, 1 << 30))
    seg = VadSegmenter(threshold, lambda: next(ids))
    pos = 0
    for i in range(0, len(audio), blk):
        b = audio[i:i + blk]
        pos = i + len(b)
        for kind, _uid, a in seg.feed(b, rate):
            if kind == "final":
                t_end = pos / rate
                yield (max(0.0, t_end - len(a) / rate), t_end, a)
    for kind, _uid, a in seg.flush(rate):
        if kind == "final":
            t_end = pos / rate
            yield (max(0.0, t_end - len(a) / rate), t_end, a)


def estimate_noise_threshold(levels: list[float]) -> float:
    """自动校准：取安静时段 RMS 的 90 分位 × 2.5，夹在合理区间。"""
    if not levels:
        return 0.01
    floor = float(np.percentile(np.asarray(levels), 90))
    return float(min(0.08, max(0.002, floor * 2.5)))


# ---------------------------- 语种检测 / 幻听过滤 ----------------------------

def detect_lang(text: str) -> str:
    """按主导文字判语种。CJK 字信息密度高，按 2 倍权重比拉丁字母。"""
    ja = len(re.findall(r"[぀-ヿ]", text))
    ko = len(re.findall(r"[가-힯]", text))
    zh = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if ja:
        return "ja"
    if ko and ko * 2 >= latin:
        return "ko"
    if zh and zh * 2 >= latin:
        return "zh"
    if latin:
        return "en"
    return "zh" if zh else lang_pair()[0]


_PUNCT = str.maketrans("", "", string.punctuation + "。，！？、…－—·「」『』（）")


def normalize(text: str) -> str:
    return text.lower().translate(_PUNCT).strip()


class GarbageFilter:
    """幻听过滤 + 短时间同源重复去重。每个会话 / 导入各用一个实例。"""

    def __init__(self):
        self._last: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def is_garbage(self, result: dict, label: str) -> bool:
        text = (result.get("text") or "").strip()
        norm = normalize(text)
        if not norm or norm in HALLUCINATION_BLACKLIST:
            return True
        segs = result.get("segments") or []
        if segs:
            if max(s.get("no_speech_prob", 0) or 0 for s in segs) > NO_SPEECH_MAX:
                return True
            avg = sum(s.get("avg_logprob", 0) or 0 for s in segs) / len(segs)
            if avg < AVG_LOGPROB_MIN:
                return True
        now = time.time()
        with self._lock:
            prev = self._last.get(label)
            self._last[label] = (norm, now)
        return bool(prev and prev[0] == norm and now - prev[1] < DEDUP_WINDOW_SECONDS)


# ---------------------------- 翻译 prompt ----------------------------

def translate_prompt(text: str, lang: str, context: str = "") -> tuple[str, str]:
    """返回 (system, user)。目标语言由语言对决定。"""
    target = LANGS[target_lang(lang)]["name"]
    g = terms_glossary()
    system = (f"你是专业同声传译员。把用户给的内容忠实翻译成{target}。"
              + (f"以下术语严格按对照翻译：{g}。" if g else "")
              + "只输出译文本身，不要解释、注释、引号或前缀。口语语气词可省略，保持自然流畅。")
    user = text
    if context:
        user = (f"【上文，仅供理解，不要翻译】{context}\n"
                f"【需要翻译的内容】{text}")
    return system, user


# ---------------------------- 说话人 ----------------------------

class SpeakerClusterer:
    """在线聚类：与已有质心比余弦，够近归入，否则新建；满员归入溢出桶。"""

    def __init__(self, threshold: float, max_speakers: int):
        self.threshold = threshold
        self.max_speakers = max_speakers
        self.centroids: list[np.ndarray] = []
        self.counts: list[int] = []

    def assign(self, emb) -> int:
        e = np.asarray(emb, dtype=np.float32)
        n = float(np.linalg.norm(e))
        if n < 1e-6:
            return 1
        e = e / n
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
        return self.max_speakers + 1


_extractor = None
_extractor_lock = threading.Lock()


def get_extractor():
    """懒加载 sherpa-onnx CAM++ 提取器（onnxruntime 推理释放 GIL，worker 线程算不卡 UI）。"""
    global _extractor
    if _extractor is None:
        with _extractor_lock:
            if _extractor is None:
                p = CFG.get("speaker_model", "")
                _extractor = False
                if p and Path(p).exists():
                    try:
                        import sherpa_onnx
                        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                            model=p, num_threads=1, provider="cpu")
                        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
                    except Exception as e:
                        print(f"说话人模型加载失败（不区分）: {e}", file=sys.stderr)
    return _extractor or None


def compute_embedding(audio: np.ndarray):
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


def ensure_speaker_model() -> bool:
    """缺 CAM++ 模型就下载（28MB）。失败不致命。"""
    dest = Path(CFG["speaker_model"])
    if dest.exists():
        return True
    try:
        import urllib.request
        url = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, str(tmp))
        tmp.replace(dest)
        return True
    except Exception as e:
        print(f"⚠️  说话人模型下载失败（将不区分说话人）: {e}", file=sys.stderr)
        return False
