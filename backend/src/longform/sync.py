"""Синхронизация камеры с записью экрана по звуку.

Автор пишет экран с микрофоном и лицо камерой одновременно, на разных
устройствах. Звук камеры (её встроенный микрофон) — то же, что слышал
микрофон, только хуже и со сдвигом. По нему и сводим.

1. Грубо — по огибающей громкости целиком (шаг 10 мс): находим, где камера
   начинается относительно экрана, даже если сдвиг в минутах.
2. Точно — в нескольких окнах вдоль записи взаимной корреляцией с
   выравниванием спектра (PHAT): она смотрит на совпадение фаз, а не на
   тембр, поэтому разные микрофоны ей не мешают. Шаг — 1/8000 с.
3. По точкам — прямая: смещение и уплывание часов (миллионные доли).
   Устройства идут на разных часах, и за час сдвиг уплывает.

Время камеры: cam_us = offset_us + main_us * (1 + drift_ppm / 1e6), оба —
время исходника (от первого кадра своего видео).
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from longform.ffmpeg import QUIET, CancelCheck, run

SR = 8000
HOP = SR // 100  # огибающая — 10 мс
FINE_WINDOW_S = 20
FINE_SEARCH_S = 0.5  # точный поиск вокруг грубого сдвига, ±
POINTS = 7
MIN_PEAK = 6.0  # острота пика: во сколько раз выше фона корреляции
MAX_RESIDUAL_US = 2_000  # точки не дальше 2 мс от прямой — синхронизация уверенная
OUTLIER_US = 5_000


@dataclass(frozen=True)
class SyncResult:
    state: str  # ok | weak | failed
    offset_us: int | None
    drift_ppm: float
    points: list[dict]
    residual_us: int | None
    coarse_peak: float

    def camera_us(self, main_us: int) -> int:
        return round(self.offset_us + main_us * (1 + self.drift_ppm / 1e6))


def decode(path: Path, stream_index: int, cancel: CancelCheck | None = None) -> np.ndarray:
    """Звук файла: моно 8 кГц с первого раскодированного отсчёта."""
    raw = run(["ffmpeg", *QUIET, "-i", str(path), "-map", f"0:{stream_index}", "-ac", "1", "-ar", str(SR),
               "-f", "s16le", "pipe:1"], cancel=cancel)
    x = np.frombuffer(raw, np.int16).astype(np.float32)
    return x - x.mean() if len(x) else x


ENV_DETREND = 50  # убрать перепады медленнее 0,5 с: «говорю / молчу минутами»


def envelope(x: np.ndarray) -> np.ndarray:
    """Огибающая громкости по 10 мс, без медленных перепадов: остаётся ритм
    слогов и пауз, по которому и совпадают два микрофона."""
    n = len(x) // HOP
    if n == 0:
        return np.zeros(0, np.float32)
    rms = np.sqrt((x[: n * HOP].reshape(n, HOP) ** 2).mean(axis=1))
    e = np.log(rms + 1.0)
    e = e - np.convolve(e, np.ones(ENV_DETREND) / ENV_DETREND, mode="same")
    e = e - e.mean()
    return e / (e.std() + 1e-9)


def _xcorr(a: np.ndarray, b: np.ndarray, phat: bool) -> np.ndarray:
    """c[k] — совпадение b, сдвинутого на k отсчётов позже a (k < 0 — раньше)."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    fa, fb = np.fft.rfft(a, n), np.fft.rfft(b, n)
    spec = np.conj(fa) * fb
    if phat:
        spec /= np.abs(spec) + 1e-12
    c = np.fft.irfft(spec, n)
    return np.concatenate([c[n - len(a) + 1:], c[: len(b)]])  # лаги от −(len(a)−1) до len(b)−1


def _peak(c: np.ndarray, guard: int) -> tuple[int, float]:
    k = int(np.argmax(c))
    rest = np.concatenate([c[: max(0, k - guard)], c[k + guard + 1:]])
    noise = np.sort(np.abs(rest))[-50:].mean() if len(rest) >= 50 else (np.abs(rest).max() if len(rest) else 1e-12)
    return k, float(c[k] / (noise + 1e-12))


def coarse_lag(main: np.ndarray, cam: np.ndarray) -> tuple[int, float]:
    """Где звук камеры совпадает со звуком экрана: лаг в отсчётах SR
    (cam[n + lag] ≈ main[n]) и острота пика."""
    em, ec = envelope(main), envelope(cam)
    if len(em) < 100 or len(ec) < 100:
        return 0, 0.0
    # PHAT и здесь: у настоящей записи без него пик широкий (острота 1,7 на
    # записи автора), с ним — 190
    c = _xcorr(em, ec, phat=True)
    k, peak = _peak(c, guard=50)
    return (k - (len(em) - 1)) * HOP, peak


def fine_points(main: np.ndarray, cam: np.ndarray, lag: int) -> list[dict]:
    """Точный сдвиг в POINTS окнах вдоль общей части записи."""
    w, s = FINE_WINDOW_S * SR, int(FINE_SEARCH_S * SR)
    start = max(0, -lag) + s
    end = min(len(main), len(cam) - lag) - w - s
    if end <= start:
        return []
    out = []
    for i in range(1, POINTS + 1):
        t = start + (end - start) * i // (POINTS + 1)
        a = main[t: t + w]
        b = cam[t + lag - s: t + lag + w + s]
        if len(b) < w + 2 * s or a.std() < 1e-3:
            continue
        c = _xcorr(a, b, phat=True)
        # Ищем только около грубого сдвига: ±s отсчётов
        zero = len(a) - 1 + s
        window = c[zero - s: zero + s + 1]
        k, peak = _peak(window, guard=8)
        # Окно меряет средний сдвиг — то есть сдвиг в его середине: при
        # уплывании часов начало и середина 20-секундного окна расходятся
        out.append({"main_sample": t + w // 2, "lag": lag + (k - s), "peak": round(peak, 1)})
    return out


def fit(points: list[dict], main_offset_us: float, cam_offset_us: float) -> tuple[float, float, int]:
    """Прямая offset(t) = a + b·t по точкам: смещение (мкс), уплывание (ppm),
    наибольшее отклонение точки от прямой (мкс). t и offset — время исходников."""
    t = np.array([main_offset_us + p["main_sample"] * 1e6 / SR for p in points])
    off = np.array([(p["main_sample"] + p["lag"]) * 1e6 / SR + cam_offset_us for p in points]) - t
    if len(points) >= 3 and np.ptp(t) > 0:
        b, a = np.polyfit(t, off, 1)
    else:
        a, b = float(np.median(off)), 0.0
    residual = int(np.abs(off - (a + b * t)).max()) if len(points) else 0
    return float(a), float(b * 1e6), residual


def synchronize(main: np.ndarray, cam: np.ndarray, main_audio_offset: Fraction, cam_audio_offset: Fraction) -> SyncResult:
    """main/cam — звук 8 кГц; audio_offset — где начинается звук относительно
    первого кадра своего видео (SourceInfo.audio_offset), в секундах."""
    lag, coarse = coarse_lag(main, cam)
    if coarse < MIN_PEAK:
        return SyncResult("failed", None, 0.0, [], None, round(coarse, 1))
    pts = fine_points(main, cam, lag)
    good = [p for p in pts if p["peak"] >= MIN_PEAK]
    m_off, c_off = float(main_audio_offset) * 1e6, float(cam_audio_offset) * 1e6
    if not good:
        a = (lag * 1e6 / SR + c_off) - m_off
        return SyncResult("weak", round(a), 0.0, pts, None, round(coarse, 1))
    a, ppm, residual = fit(good, m_off, c_off)
    if len(good) >= 4 and residual > OUTLIER_US:  # одна случайная точка не должна портить прямую
        t = np.array([m_off + p["main_sample"] * 1e6 / SR for p in good])
        off = np.array([(p["main_sample"] + p["lag"]) * 1e6 / SR + c_off for p in good]) - t
        keep = np.abs(off - (a + ppm / 1e6 * t)) <= OUTLIER_US
        good = [p for p, k in zip(good, keep) if k] or good
        a, ppm, residual = fit(good, m_off, c_off)
    state = "ok" if len(good) >= 3 and residual <= MAX_RESIDUAL_US else "weak"
    points = [{"main_us": round(m_off + p["main_sample"] * 1e6 / SR),
               "offset_us": round((p["main_sample"] + p["lag"]) * 1e6 / SR + c_off - m_off - p["main_sample"] * 1e6 / SR),
               "peak": p["peak"], "used": p in good} for p in pts]
    return SyncResult(state, round(a), round(ppm, 2), points, residual, round(coarse, 1))
