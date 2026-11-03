"""Audio resampling to 16 kHz without SciPy (keeps the installer ~70 MB smaller).

Same result as scipy.signal.resample_poly(x, up, down): a Kaiser-windowed (beta 5) low-pass FIR applied
polyphase, so only the taps that touch real samples are computed.
"""

from math import gcd

import numpy as np

BLOCK = 65536  # output samples computed at once (bounds memory for hour-long files)


def _filter(up: int, down: int) -> np.ndarray:
    max_rate = max(up, down)
    half_len = 10 * max_rate
    n = np.arange(2 * half_len + 1) - half_len
    cutoff = 1.0 / max_rate
    h = cutoff * np.sinc(cutoff * n) * np.kaiser(2 * half_len + 1, 5.0)
    return (h / h.sum() * up).astype(np.float32)


def resample_poly(x: np.ndarray, up: int, down: int) -> np.ndarray:
    g = gcd(up, down)
    up, down = up // g, down // g
    x = np.asarray(x, dtype=np.float32)
    if up == down == 1 or not len(x):
        return x.astype(np.float32)
    h = _filter(up, down)
    half_len = (len(h) - 1) // 2
    taps = -(-len(h) // up)  # input samples per output sample
    # phase p uses h[p], h[p + up], h[p + 2up], ...; pad h so every phase has `taps` taps
    h = np.concatenate([h, np.zeros(taps * up - len(h), dtype=np.float32)]).reshape(taps, up).T
    xp = np.concatenate([np.zeros(taps, dtype=np.float32), x, np.zeros(taps, dtype=np.float32)])  # input index i lives at xp[i + taps]
    n_out = -(-len(x) * up // down)
    out = np.empty(n_out, dtype=np.float32)
    j = np.arange(taps)
    for start in range(0, n_out, BLOCK):
        m = np.arange(start, min(start + BLOCK, n_out)) * down + half_len  # position in the upsampled signal
        phase, i0 = m % up, m // up
        # y[n] = sum_j h[phase + j*up] * x[i0 - j]
        out[start:start + len(m)] = np.einsum("nj,nj->n", h[phase], xp[(i0 + taps)[:, None] - j])
    return out.astype(np.float32)


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    return audio.astype(np.float32) if rate == 16000 else resample_poly(audio, 16000, rate)
