"""Громкость ролика: замер и выравнивание одним усилением.

Замер — фильтр ffmpeg loudnorm (интегральная громкость по ITU-R BS.1770 в
LUFS, истинный пик в dBTP, диапазон громкости в LU). Выравнивание — одно
общее усиление для всего звука монтажа: характер голоса и разница между
тихими и громкими местами не меняются, компрессии и лимитера нет.

Если до цели не дотянуть, не подняв истинный пик выше предела, усиление
ограничивается пределом пика, и это видно в отчёте. Цель — настройка
проекта (стартовое значение −16 LUFS), а не требование какой-либо площадки.
"""

import json
import math
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from longform.ffmpeg import CancelCheck, Cancelled, run_stderr

TARGET_DEFAULT = -16.0  # LUFS, стартовое значение для проекта
TRUE_PEAK_LIMIT = -1.5  # dBTP: запас на сжатие в AAC, которое поднимает пики
QUIET_LUFS = -70.0  # тише этого — тишина, выравнивать нечего
MIN_GAIN_DB = 0.05
CHUNK = 1 << 20


@dataclass(frozen=True)
class Loudness:
    integrated: float  # LUFS
    true_peak: float  # dBTP
    lra: float  # LU


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -math.inf


def measure(path: Path, cancel: CancelCheck | None = None) -> Loudness:
    """Громкость звука файла (WAV или звуковой дорожки ролика)."""
    text = run_stderr(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
                       "-af", "loudnorm=print_format=json", "-f", "null", "-"], cancel=cancel)
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", text)
    if not blocks:
        raise ValueError("ffmpeg не выдал замер громкости")
    data = json.loads(blocks[-1])
    return Loudness(_number(data["input_i"]), _number(data["input_tp"]), max(0.0, _number(data["input_lra"])))


def gain_for(m: Loudness, target: float, tp_limit: float = TRUE_PEAK_LIMIT) -> tuple[float, bool]:
    """Усиление в дБ и ограничено ли оно пиками."""
    if not math.isfinite(m.integrated) or m.integrated < QUIET_LUFS:
        return 0.0, False
    wanted = target - m.integrated
    allowed = tp_limit - m.true_peak if math.isfinite(m.true_peak) else wanted
    return (allowed, True) if allowed < wanted else (wanted, False)


def apply_gain(src: Path, dst: Path, gain_db: float, cancel: CancelCheck | None = None) -> None:
    """Тот же WAV 16 бит с общим усилением; длина и формат не меняются."""
    factor = 10 ** (gain_db / 20)
    with wave.open(str(src), "rb") as r, wave.open(str(dst), "wb") as w:
        if r.getsampwidth() != 2:
            raise ValueError(f"{src.name}: ожидался WAV 16 бит")
        w.setnchannels(r.getnchannels())
        w.setsampwidth(2)
        w.setframerate(r.getframerate())
        left = r.getnframes()
        while left:
            if cancel and (reason := cancel()):
                raise Cancelled(reason)
            n = min(CHUNK, left)
            x = np.frombuffer(r.readframes(n), np.int16).astype(np.float32) * factor
            w.writeframes(np.clip(np.round(x), -32768, 32767).astype(np.int16).tobytes())
            left -= n


def normalize(wav: Path, dst: Path, target: float, cancel: CancelCheck | None = None) -> dict:
    """Замер, усиление, и что сделано — для отчёта качества."""
    before = measure(wav, cancel)
    gain, capped = gain_for(before, target)
    applied = abs(gain) >= MIN_GAIN_DB
    if applied:
        apply_gain(wav, dst, gain, cancel)
    return {
        "target_lufs": target,
        "true_peak_limit": TRUE_PEAK_LIMIT,
        "before_lufs": round(before.integrated, 1) if math.isfinite(before.integrated) else None,
        "before_true_peak": round(before.true_peak, 1) if math.isfinite(before.true_peak) else None,
        "lra": round(before.lra, 1),
        "gain_db": round(gain, 2) if applied else 0.0,
        "capped_by_peak": capped,
        "applied": applied,
    }
