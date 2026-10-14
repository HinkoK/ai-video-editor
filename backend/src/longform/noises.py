"""Кашель и «кхм» — звуки голоса без слов.

Whisper такие звуки не пишет, поэтому на расшифровке их не видно, а детектор
речи считает их речью.

Поиск — по записи экрана, только в том, что осталось в версии: сначала
чистка, потом поиск — 10–20 минут ролика, а не час записи. Claude зовёт его после чистки пауз и дублей; редактор помнит,
какие отрезки уже проверены, и вернутое из вырезанного досматривает отдельно.

1. громкие отрезки 0,1–0,7 с (порог громкости тот же, что у тишины), не тише
   обычной речи на 10 дБ и больше: тихие щелчки и вдохи не трогаем;
2. в них нет середины ни одного слова расшифровки;
3. каждый перечитывается Whisper отдельно, с секундой тишины по краям:
   «Кхм», пусто или известная выдумка на шуме («Продолжение следует»,
   «Субтитры сделал DimaTorzok») — звук; любое настоящее слово («навык»,
   «или») — речь, которую общая расшифровка пропустила, её не трогаем.

«Кхм» вплотную к фразе. Между ним и словами не
тишина, а провал на 20–40 дБ тише речи: дыхание и шорох комнаты держатся у
порога тишины, и по нему звук сливается с фразой в один длинный отрезок.
Такие ищутся вторым проходом — всплески, отделённые провалами тише речи на
GLUED_GAP_DB дольше GLUED_GAP_US. Признаки строже: не тише обычной речи
(чмоканье, вдох и начало слова тише) и звук при двух вариантах границ
(у начала слова Whisper на одном из них слышит слово).

Для каждого звука сразу считается, что вырезать: сам звук и тишину вокруг
него до голоса с запасом у речи (PAD_US), не дальше REACH_US в каждую сторону —
на стыке остаётся одна обычная пауза, а не две. Дыхание и шорох комнаты вокруг
звука вырезаются вместе с тишиной.
"""

import json
import re
import sqlite3
import subprocess
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from longform.analysis import PAD_US, Loudness
from longform.db import write
from longform.ffmpeg import CancelCheck, Cancelled
from longform.store import _now

VERSION = 3  # меняется вместе с правилами поиска: поиск запускается заново
MIN_US, MAX_US = 100_000, 700_000
JOIN_US = 20_000  # провалы короче внутри звука — тот же звук
# Тишину вокруг звука вырезаем до голоса (Loudness.voice_db): дыхание и шорох
# комнаты чуть выше порога тишины не должны останавливать вырез
BELOW_SPEECH_DB = 10.0  # тише речи на столько и больше — щелчок, вдох: не трогаем
REACH_US = 1_500_000  # дальше этого тишину вокруг звука не трогаем
PAD_S = 1.0  # тишина по краям при перечитке
# «Кхм» вплотную к фразе: провал вокруг — тише речи на столько и не короче
GLUED_GAP_DB = 20.0
GLUED_GAP_US = 60_000
GLUED_MAX_US = 500_000
GLUED_PEAK_DB = 3.0  # громче обычной речи: «кхм» — да, вдох и чмоканье — нет
GLUED_WIDER_US = 40_000  # второй вариант перечитки — границы шире на столько
# Ищется оставленное в версии и секунда вокруг: звук на краю куска виден целиком
MARGIN_US = 1_000_000
WHOLE = (0, 2**62)  # вся запись (старый поиск)
# Что Whisper пишет на кашле: «Кхм», «Кх-кх», «Хм», «[кашель]»
NOISE_TEXT = re.compile(r"^[\W_]*((к?х+м*|хм+|кх+|кашел\w*|кашля\w*)[\W_]*)*$", re.IGNORECASE)


@dataclass(frozen=True)
class Noise:
    start_us: int  # сам звук
    end_us: int
    cut_start_us: int  # что вырезать: звук и тишина вокруг до запаса у речи
    cut_end_us: int
    heard: str  # что Whisper услышал в звуке отдельно
    peak_db: float  # громкость относительно уровня речи


def is_noise(heard: str) -> bool:
    from longform.transcribe import suspect

    text = heard.strip()
    return not text or bool(NOISE_TEXT.match(text)) or suspect(text)


def candidates(loud: Loudness, word_mids: list[int], start_us: int, end_us: int) -> list[tuple[int, int, float]]:
    """Громкие отрезки без середины слова: [(начало, конец, громкость к речи)]."""
    db = loud.db
    a, b = max(0, loud.idx(start_us)), min(len(db), loud.idx(end_us))
    loud_ = db[a:b] > loud.threshold_db
    hop = loud.hop_us
    join = JOIN_US // hop
    mids = np.asarray(sorted(word_mids), dtype=np.int64)
    out, i, n = [], 0, len(loud_)
    while i < n:
        if not loud_[i]:
            i += 1
            continue
        j = i + 1
        while j < n and (loud_[j] or loud_[j:j + join + 1].any()):
            j += 1
        while not loud_[j - 1]:
            j -= 1
        s_us = loud.offset_us + (a + i) * hop
        e_us = loud.offset_us + (a + j) * hop
        peak = float(db[a + i:a + j].max() - loud.speech_db)
        inside = mids[(mids >= s_us) & (mids < e_us)].size if mids.size else 0
        if MIN_US <= e_us - s_us <= MAX_US and peak >= -BELOW_SPEECH_DB and not inside:
            out.append((s_us, e_us, round(peak, 1)))
        i = j
    return out


def glued_candidates(loud: Loudness, word_mids: list[int], start_us: int, end_us: int) -> list[tuple[int, int, float]]:
    """Всплески без середины слова, отделённые от речи провалами (тише речи на
    GLUED_GAP_DB дольше GLUED_GAP_US), а не тишиной: [(начало, конец, громкость к речи)]."""
    db = loud.db
    a, b = max(0, loud.idx(start_us)), min(len(db), loud.idx(end_us))
    up = db[a:b] >= loud.speech_db - GLUED_GAP_DB
    hop = loud.hop_us
    gap = max(1, GLUED_GAP_US // hop)
    mids = np.asarray(sorted(word_mids), dtype=np.int64)
    out, i, n = [], 0, len(up)
    while i < n:
        if not up[i]:
            i += 1
            continue
        j = i + 1
        while j < n and (up[j] or up[j:j + gap].any()):
            j += 1
        s_us = loud.offset_us + (a + i) * hop
        e_us = loud.offset_us + (a + j) * hop
        peak = float(db[a + i:a + j].max() - loud.speech_db)
        inside = mids[(mids >= s_us) & (mids < e_us)].size if mids.size else 0
        if MIN_US <= e_us - s_us <= GLUED_MAX_US and peak >= GLUED_PEAK_DB and not inside:
            out.append((s_us, e_us, round(peak, 1)))
        i = j
    return out


def cut_range(loud: Loudness, s_us: int, e_us: int) -> tuple[int, int]:
    """Звук и тишина вокруг до голоса (с запасом PAD_US у речи), не дальше REACH_US."""
    db, hop = loud.db, loud.hop_us
    voice = loud.voice_db
    reach = REACH_US // hop
    i, j = loud.idx(s_us), loud.idx(e_us)
    left = i
    while left > 0 and i - left < reach and db[left - 1] < voice:
        left -= 1
    right = j
    while right < len(db) and right - j < reach and db[right] < voice:
        right += 1
    cut_start = loud.offset_us + left * hop + (PAD_US if left > 0 and db[left - 1] >= voice else 0)
    cut_end = loud.offset_us + right * hop - (PAD_US if right < len(db) and db[right] >= voice else 0)
    return min(cut_start, s_us), max(cut_end, e_us)


def hear_with_whisper(wav16: Path, offset_us: int, model: Path, language: str = "ru") -> Callable[[int, int], str]:
    """Перечитка одного звука: Whisper на нём одном, с тишиной по краям."""
    with wave.open(str(wav16)) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2")

    def hear(s_us: int, e_us: int) -> str:
        a = max(0, (s_us - offset_us) * rate // 1_000_000)
        b = min(len(audio), (e_us - offset_us) * rate // 1_000_000)
        pad = np.zeros(int(PAD_S * rate), "<i2")
        with tempfile.TemporaryDirectory() as tmp:
            snip = Path(tmp) / "звук.wav"
            with wave.open(str(snip), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(np.concatenate([pad, audio[a:b], pad]).tobytes())
            done = subprocess.run(["whisper-cli", "-m", str(model), "-l", language, "-f", str(snip), "-np", "-nt"],
                                  capture_output=True, text=True, timeout=120)
        return " ".join(done.stdout.split())

    return hear


def detect(loud: Loudness, word_mids: list[int], start_us: int, end_us: int, hear: Callable[[int, int], str],
           cancel: CancelCheck | None = None,
           progress: Callable[[int, int], None] | None = None) -> list[Noise]:
    return detect_in(loud, word_mids, [(start_us, end_us)], hear, cancel, progress)


def detect_in(loud: Loudness, word_mids: list[int], ranges: list[tuple[int, int]], hear: Callable[[int, int], str],
              cancel: CancelCheck | None = None,
              progress: Callable[[int, int], None] | None = None) -> list[Noise]:
    """Поиск в отрезках записи (не пересекаются)."""
    found, glued = [], []
    for start_us, end_us in ranges:
        own = candidates(loud, word_mids, start_us, end_us)
        found += own
        # Второй проход — вплотную к фразе; то, что нашёл первый, не повторяем
        glued += [g for g in glued_candidates(loud, word_mids, start_us, end_us)
                  if not any(s < g[1] and g[0] < e for s, e, _ in own)]
    total = len(found) + len(glued)
    out = []
    for k, (s, e, peak) in enumerate([*found, *glued]):
        if cancel and (reason := cancel()):
            raise Cancelled(reason)
        if progress:
            progress(k, total)
        heard = hear(s, e)
        if k >= len(found) and is_noise(heard):
            # У начала слова Whisper на более широких границах слышит слово
            wider = hear(s - GLUED_WIDER_US, e + GLUED_WIDER_US)
            heard = heard if is_noise(wider) else wider
        if is_noise(heard):
            out.append(Noise(s, e, *cut_range(loud, s, e), heard=heard, peak_db=peak))
    return sorted(out, key=lambda x: x.start_us)


def merge(ranges) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted((int(a), int(b)) for a, b in ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract(ranges, done) -> list[tuple[int, int]]:
    """Отрезки ranges без отрезков done; обрезки короче звука не нужны."""
    out = []
    for a, b in merge(ranges):
        cur = a
        for x, y in merge(done):
            if y <= cur or x >= b:
                continue
            if x > cur:
                out.append((cur, x))
            cur = max(cur, y)
        if cur < b:
            out.append((cur, b))
    return [(a, b) for a, b in out if b - a >= MIN_US]


def kept_ranges(edl, asset_id: str) -> list[tuple[int, int]]:
    """Что от записи осталось в версии — время записи, с запасом MARGIN_US."""
    from longform.timebase import frame_to_us

    if asset_id not in edl.sources:
        return []
    fps = edl.sources[asset_id].fps
    return merge((max(0, frame_to_us(c.src_start_frame, fps) - MARGIN_US), frame_to_us(c.src_end_frame, fps) + MARGIN_US)
                 for c in edl.clips if c.asset_id == asset_id)


def to_scan(conn: sqlite3.Connection, asset_id: str, edl, transcript_id: str) -> list[tuple[int, int]]:
    """Что из оставленного в версии ещё не проверено этим правилом по этой расшифровке."""
    scan = scanned(conn, asset_id)
    fresh = scan and scan["transcript_id"] == transcript_id and scan["version"] == VERSION
    return subtract(kept_ranges(edl, asset_id), scan["ranges"] if fresh else [])


def save(conn: sqlite3.Connection, asset_id: str, noises: list[Noise], transcript_id: str | None,
         ranges: list[tuple[int, int]] | None = None) -> None:
    """Найденное в отрезках ranges (None — вся запись). Новая расшифровка или
    правило — всё прежнее стирается; иначе перепроверенные отрезки заменяются,
    номера уже найденных звуков не меняются (на них ссылаются правки)."""
    ranges = merge(ranges) if ranges is not None else [WHOLE]
    with write(conn):
        scan = conn.execute("SELECT * FROM noise_scans WHERE asset_id = ?", (asset_id,)).fetchone()
        if scan is None or scan["transcript_id"] != transcript_id or scan["version"] != VERSION:
            conn.execute("DELETE FROM noises WHERE asset_id = ?", (asset_id,))
            done = []
        else:
            done = json.loads(scan["ranges_json"]) if scan["ranges_json"] else [WHOLE]
            for a, b in ranges:
                conn.execute("DELETE FROM noises WHERE asset_id = ? AND start_us < ? AND end_us > ?", (asset_id, b, a))
        first = conn.execute("SELECT COALESCE(MAX(idx) + 1, 0) FROM noises WHERE asset_id = ?", (asset_id,)).fetchone()[0]
        conn.executemany(
            "INSERT INTO noises (asset_id, idx, start_us, end_us, cut_start_us, cut_end_us, heard, peak_db, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(asset_id, first + i, n.start_us, n.end_us, n.cut_start_us, n.cut_end_us, n.heard, n.peak_db, _now())
             for i, n in enumerate(noises)])
        total = conn.execute("SELECT COUNT(*) FROM noises WHERE asset_id = ?", (asset_id,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO noise_scans (asset_id, transcript_id, found, created_at, ranges_json, version) "
                      "VALUES (?,?,?,?,?,?)",
                      (asset_id, transcript_id, total, _now(), json.dumps(merge([*done, *ranges])), VERSION))


def list_noises(conn: sqlite3.Connection, asset_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM noises WHERE asset_id = ? ORDER BY idx", (asset_id,))]


def scanned(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM noise_scans WHERE asset_id = ?", (asset_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["ranges"] = [tuple(r) for r in json.loads(d.pop("ranges_json"))] if d.get("ranges_json") else [WHOLE]
    return d


def word_mids(conn: sqlite3.Connection, asset_id: str) -> list[int]:
    """Середины слов текущей расшифровки — время исходника, мкс."""
    from longform import asset_data

    t = asset_data.current_transcript(conn, asset_id)
    if t is None:
        return []
    return [(r[0] + r[1]) // 2 for r in conn.execute(
        "SELECT w.start_us, w.end_us FROM transcript_words w JOIN transcript_segments s ON s.id = w.segment_id "
        "WHERE s.transcript_id = ?", (t["id"],))]
