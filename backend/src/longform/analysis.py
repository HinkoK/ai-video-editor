"""Громкость, тишина и выравнивание границ правок по тишине.

Границы фраз у Whisper приблизительны: на синтетической речи сегмент
начинался на 2,6 с раньше слов и захватывал паузу целиком. Поэтому где
речь, а где тишина, решает громкость, измеренная на этой же записи:

- громкость (дБ) на каждые 10 мс;
- уровень шума — 10-й процентиль, уровень речи — 90-й;
- порог тишины — шум плюс 35 % разницы между речью и шумом. Он свой у
  каждой записи: у шумного микрофона и у чистого он разный.

Детектор тишины не решает, что вырезать. Он только даёт кандидатов и
помогает поставить границу, не задев слово.

Вместе с громкостью работает детектор речи Silero (whisper-vad-speech-
segments из whisper.cpp): по одной громкости стук клавиатуры и щелчки мыши
на записи экрана выглядят как речь. Правило: речь — то, что детектор
считает речью, или громкое рядом с ней (в пределах NEAR_SPEECH_US: край
слова, который детектор мог пропустить). Громкое вдали от речи — пауза.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

import os
import re

from longform.ffmpeg import CancelCheck, Cancelled, run
from longform.render.audio import WavReader

HOP_US = 10_000
THRESHOLD_SHARE = 0.35
MIN_SILENCE_US = 250_000  # короче — это не пауза, а промежуток между словами
DIP_US = 60_000  # провал внутри слова (смычка «п», «т») — ещё речь
PAD_US = 70_000  # сколько тишины оставлять у оставляемой речи (с двух сторон стыка)
# Голос — громче середины между порогом тишины и уровнем речи. Дыхание и шорох
# комнаты чуть выше порога тишины голосом не считаются
VOICE_SHARE = 0.5
# Отступ от вырезаемой речи: на стыке звук сводится по 5 мс с каждой
# стороны, и хвост вырезанного слова не должен попасть в сведение
MARGIN_US = 40_000
WINDOW_US = 250_000  # насколько далеко можно сдвинуть границу, чтобы не резать слово
NEAR_SPEECH_US = 300_000  # громкое ближе этого к речи детектора — тоже речь
VAD_MODEL = Path(os.environ.get("LONGFORM_VAD_MODEL", "~/.cache/whisper/ggml-silero-v6.2.0.bin")).expanduser()


@dataclass
class Loudness:
    db: np.ndarray  # дБ относительно полной шкалы, по одному на шаг
    offset_us: int  # время первого отсчёта звука относительно первого кадра
    floor_db: float
    speech_db: float
    threshold_db: float
    hop_us: int = HOP_US
    # Речь по детектору: [(начало, конец)] в мкс времени исходника; None — нет
    vad: tuple[tuple[int, int], ...] | None = None

    @property
    def voice_db(self) -> float:
        return self.threshold_db + VOICE_SHARE * (self.speech_db - self.threshold_db)

    def __post_init__(self) -> None:
        silent = self.db < self.threshold_db
        if self.vad is not None:
            n = len(silent)
            voiced = np.zeros(n, bool)
            for a, b in self.vad:
                voiced[max(0, self.idx(a)):max(0, min(n, self.idx(b)))] = True
            # «Рядом с речью»: расширяем отрезки детектора на NEAR_SPEECH_US
            near = np.zeros(n, bool)
            pad = NEAR_SPEECH_US // self.hop_us
            for a, b in self.vad:
                near[max(0, self.idx(a) - pad):max(0, min(n, self.idx(b) + pad))] = True
            silent = ~(voiced | (~silent & near))
        # Короткие провалы внутри речи — не тишина
        dip = DIP_US // self.hop_us
        i, n = 0, len(silent)
        while i < n:
            if silent[i]:
                j = i
                while j < n and silent[j]:
                    j += 1
                if j - i < dip and i > 0 and j < n:
                    silent[i:j] = False
                i = j
            else:
                i += 1
        self.silent = silent

    # Перевод между временем исходника (мкс от первого кадра) и шагом
    def idx(self, src_us: int) -> int:
        return (src_us - self.offset_us) // self.hop_us

    def us(self, i: int) -> int:
        return self.offset_us + i * self.hop_us

    def is_silent_at(self, src_us: int) -> bool:
        i = self.idx(src_us)
        return not 0 <= i < len(self.silent) or bool(self.silent[i])

    def speech_near(self, src_us: int, radius_us: int = 10_000) -> bool:
        a = max(0, self.idx(src_us - radius_us))
        b = min(len(self.silent), self.idx(src_us + radius_us) + 1)
        return bool(b > a and not self.silent[a:b].all())

    def _run(self, i: int) -> tuple[int, int]:
        """Границы серии одинаковых значений (тишина или речь) вокруг шага i."""
        value = self.silent[i]
        a = i
        while a > 0 and self.silent[a - 1] == value:
            a -= 1
        b = i + 1
        while b < len(self.silent) and self.silent[b] == value:
            b += 1
        return a, b

    def silences(self, min_us: int = MIN_SILENCE_US) -> list[tuple[int, int, float]]:
        out, i, n = [], 0, len(self.silent)
        while i < n:
            if self.silent[i]:
                a, b = self._run(i)
                if (b - a) * self.hop_us >= min_us:
                    out.append((self.us(a), self.us(b), float(self.db[a:b].min())))
                i = b
            else:
                i += 1
        return out

    def speech_runs(self, start_us: int, end_us: int) -> list[tuple[int, int]]:
        """Отрезки речи (мкс), задевающие [start, end), целиком, без обрезки."""
        a, b = max(0, self.idx(start_us)), min(len(self.silent), self.idx(end_us) + 1)
        runs, i = [], a
        while i < b:
            if not self.silent[i]:
                r0, r1 = self._run(i)
                runs.append((self.us(r0), self.us(r1)))
                i = r1
            else:
                i += 1
        return runs

    def speech_span(self, start_us: int, end_us: int) -> tuple[int, int] | None:
        """Где звучит речь, принадлежащая сегменту [start, end). None — речи нет.

        Границы сегментов Whisper заходят на соседние фразы на десятки мс.
        Поэтому сегменту принадлежит только речь, которая в основном лежит
        внутри него, и берётся она целыми словами, а не обрезанной.

        Сплошная речь без пауз — один отрезок через несколько коротких фраз,
        и ни одна им «не владеет» (найдено на записи автора: 3,7 мин настоящей
        речи помечались «нет речи»). Тогда речь фразы — пересечение с речью,
        если оно занимает хотя бы половину фразы; заход Whisper на соседа в
        десятки мс до половины не дотягивает.
        """
        runs = self.speech_runs(start_us, end_us)
        own = [(r0, r1) for r0, r1 in runs if (min(r1, end_us) - max(r0, start_us)) * 2 >= r1 - r0]
        if own:
            return own[0][0], own[-1][1]
        inside = [(max(r0, start_us), min(r1, end_us)) for r0, r1 in runs if min(r1, end_us) > max(r0, start_us)]
        if inside and sum(b - a for a, b in inside) * 2 >= end_us - start_us:
            return inside[0][0], inside[-1][1]
        return None

    def nearest_quiet(self, src_us: int, window_us: int = 60_000) -> int:
        """Самый тихий момент в пределах ±window — для точных правок по
        словам: между словами внутри фразы паузы может и не быть, и правило
        «уйти в тишину с запасом» унесло бы границу слишком далеко."""
        if self._outside(src_us):
            return src_us
        a = max(0, self.idx(src_us - window_us))
        b = min(len(self.db), self.idx(src_us + window_us) + 1)
        if b <= a:
            return src_us
        i = a + int(np.argmin(self.db[a:b]))
        return self.us(i) + self.hop_us // 2

    def precise_boundary(self, src_us: int) -> int:
        """Граница точной правки: тише голоса (тишина, дыхание, шорох) —
        остаётся на месте, в звуке — уходит в ближайший тихий момент. Границы
        вырезов «кхм» и возвратов тишины считаются заранее и стоят тише
        голоса: подгонка к самому тихому моменту прижимала их к началу
        следующей фразы (2026-10-08: у «кхм» вплотную к фразе вокруг дыхание
        чуть выше порога тишины — граница уезжала к слову на 15–65 мс)."""
        i = self.idx(src_us)
        if self._outside(src_us) or (0 <= i < len(self.db) and self.db[i] < self.voice_db):
            return src_us
        return self.nearest_quiet(src_us)

    # ---------- выравнивание границ ----------

    def _place(self, a: int, b: int, want: int, pad_before: bool, pad_after: bool) -> int:
        """Точка внутри тишины [a, b) как можно ближе к want: с запасом PAD
        от оставляемой речи и MARGIN от вырезаемой. Если тишина короче —
        её середина."""
        lo = self.us(a) + ((PAD_US if pad_before else MARGIN_US) if a > 0 else 0)
        hi = self.us(b) - ((PAD_US if pad_after else MARGIN_US) if b < len(self.silent) else 0)
        if lo > hi:
            return (self.us(a) + self.us(b)) // 2
        return min(max(want, lo), hi)

    def _outside(self, src_us: int) -> bool:
        """Граница вне записанного звука (до его начала, после конца или на
        краю файла): стыка со звуком там нет, двигать нечего."""
        return src_us <= max(self.offset_us, 0) or src_us >= self.us(len(self.silent))

    def keep_before(self, src_us: int) -> tuple[int, str | None]:
        """Граница, ДО которой материал остаётся (начало выреза, конец возврата).

        Если граница попала в слово: сначала пробуем оставить слово целым
        (сдвиг вперёд, за конец слова), иначе убрать его целиком (назад, до
        начала слова). Дальше слова, чем WINDOW_US, не двигаем — помечаем.
        После оставленной речи сохраняется PAD тишины.
        """
        if self._outside(src_us):
            return src_us, None
        n = len(self.silent)
        i = min(max(self.idx(src_us), 0), n - 1)
        if self.silent[i]:
            a, b = self._run(i)
            return self._place(a, b, src_us, pad_before=True, pad_after=False), None
        r0, r1 = self._run(i)
        if r1 < n and self.us(r1) - src_us <= WINDOW_US:
            a, b = self._run(r1)
            return (self._place(a, b, self.us(r1), pad_before=True, pad_after=False),
                    "граница была в слове, сдвинута вперёд, чтобы слово осталось целым")
        if r0 > 0 and src_us - self.us(r0) <= WINDOW_US:
            a, b = self._run(r0 - 1)
            return (self._place(a, b, self.us(r0) - PAD_US, pad_before=True, pad_after=False),
                    "граница была в слове, слово уходит целиком")
        return src_us, "граница в речи, тишины рядом нет"

    def keep_after(self, src_us: int) -> tuple[int, str | None]:
        """Граница, ПОСЛЕ которой материал остаётся (конец выреза, начало возврата).

        Зеркально keep_before: сначала пробуем сохранить начало слова
        (сдвиг назад), иначе убрать слово целиком (вперёд). Перед оставленной
        речью сохраняется PAD тишины.
        """
        if self._outside(src_us):
            return src_us, None
        n = len(self.silent)
        i = min(max(self.idx(src_us), 0), n - 1)
        if self.silent[i]:
            a, b = self._run(i)
            return self._place(a, b, src_us, pad_before=False, pad_after=True), None
        r0, r1 = self._run(i)
        if r0 > 0 and src_us - self.us(r0) <= WINDOW_US:
            a, b = self._run(r0 - 1)
            return (self._place(a, b, self.us(r0), pad_before=False, pad_after=True),
                    "граница была в слове, сдвинута назад, чтобы начало слова осталось")
        if r1 < n and self.us(r1) - src_us <= WINDOW_US:
            a, b = self._run(r1)
            return (self._place(a, b, self.us(r1) + PAD_US, pad_before=False, pad_after=True),
                    "граница была в слове, слово уходит целиком")
        return src_us, "граница в речи, тишины рядом нет"


VAD_LINE = re.compile(r"Speech segment \d+: start = ([\d.]+), end = ([\d.]+)")


def run_vad(wav16: Path, cancel: CancelCheck | None = None) -> list[tuple[int, int]] | None:
    """Отрезки речи по Silero, мс от первого отсчёта звука. None — детектора нет.

    whisper-vad-speech-segments печатает начало и конец в сотых секунды
    (проверено: 602.00 — это 6,02 с)."""
    if not VAD_MODEL.exists():
        return None
    out = run(["whisper-vad-speech-segments", "-vm", str(VAD_MODEL), "-f", str(wav16), "-np"], cancel=cancel)
    return [(round(float(a) * 10), round(float(b) * 10)) for a, b in VAD_LINE.findall(out.decode(errors="replace"))]


def levels(db: np.ndarray) -> tuple[float, float, float]:
    floor = float(np.percentile(db, 10))
    speech = float(np.percentile(db, 90))
    return floor, speech, floor + THRESHOLD_SHARE * (speech - floor)


def measure(wav: Path, cancel: CancelCheck | None = None) -> np.ndarray:
    """Громкость (дБ) на каждые 10 мс по WAV исходника, без загрузки целиком."""
    reader = WavReader(wav)
    try:
        hop = reader.sample_rate * HOP_US // 1_000_000
        out = []
        pos, chunk = 0, hop * 6000  # минута за раз
        while pos < reader.length:
            if cancel and (reason := cancel()):
                raise Cancelled(reason)
            x = reader.read(pos, min(chunk, reader.length - pos)).mean(axis=1)
            full = len(x) // hop * hop
            if full:
                rms = np.sqrt(np.mean(x[:full].reshape(-1, hop) ** 2, axis=1))
                out.append(20 * np.log10(rms + 1e-9))
            pos += chunk
        return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)
    finally:
        reader.close()


def peaks(db: np.ndarray, start: int, end: int, points: int) -> list[float]:
    """Волна для экрана: максимум громкости в каждом из points отрезков."""
    part = db[max(0, start): max(0, end)]
    if len(part) == 0 or points <= 0:
        return []
    edges = np.linspace(0, len(part), points + 1).astype(int)
    return [round(float(part[a:max(b, a + 1)].max()), 1) for a, b in zip(edges, edges[1:])]
