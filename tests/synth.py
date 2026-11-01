"""合成「语音」：带包络的多谐波音（能量高于 VAD 阈值），中间夹静音。"""
import numpy as np


def utterance(seconds: float, rate: int, seed: int = 0) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    rng = np.random.default_rng(seed)
    f0 = 140 + 60 * rng.random()
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 5))
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 3.5 * t) ** 2
    return (0.12 * x * env).astype(np.float32)


def silence(seconds: float, rate: int) -> np.ndarray:
    return (np.random.default_rng(1).standard_normal(int(seconds * rate)) * 0.0008).astype(np.float32)


def blocks(audio: np.ndarray, rate: int, block_s: float = 0.25):
    n = int(rate * block_s)
    for i in range(0, len(audio), n):
        yield audio[i:i + n]
