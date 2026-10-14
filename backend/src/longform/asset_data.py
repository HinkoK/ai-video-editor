"""Данные об исходнике: громкость, тишина, расшифровка, мягкая защита.

Громкость лежит файлом .npy (час звука — около 1,4 МБ), всё остальное — в
базе. Сырой текст Whisper не перезаписывается: исправление автора хранится
рядом и в поиске используется оно.
"""

import json
import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np

from longform.analysis import HOP_US, MIN_SILENCE_US, Loudness, levels
from longform.db import write
from longform.edl import EdlError
from longform.store import _id, _now, _one, _only_user
from longform.transcribe import Segment, high_impact, suspect

LOW_CONFIDENCE = 0.5
VERY_LOW_CONFIDENCE = 0.2


def save_analysis(conn: sqlite3.Connection, asset_id: str, params_hash: str, db: np.ndarray, path: Path,
                  audio_offset_us: int, vad_ms: list[tuple[int, int]] | None = None) -> dict:
    np.save(path, db)
    floor, speech, thr = levels(db)
    vad = None if vad_ms is None else tuple((a * 1000 + audio_offset_us, b * 1000 + audio_offset_us) for a, b in vad_ms)
    loud = Loudness(db, audio_offset_us, floor, speech, thr, vad=vad)
    aid = _id("ana")
    with write(conn):
        conn.execute("DELETE FROM silences WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM audio_analysis WHERE asset_id = ? AND params_hash = ?", (asset_id, params_hash))
        conn.execute(
            "INSERT INTO audio_analysis (id, asset_id, params_hash, hop_us, floor_db, speech_db, threshold_db, path, "
            "audio_offset_us, created_at, vad_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (aid, asset_id, params_hash, HOP_US, floor, speech, thr, str(path), audio_offset_us, _now(),
             None if vad is None else json.dumps(vad)))
        conn.executemany(
            "INSERT INTO silences (analysis_id, asset_id, src_start_us, src_end_us, min_db) VALUES (?,?,?,?,?)",
            [(aid, asset_id, a, b, round(m, 1)) for a, b, m in loud.silences()])
    return analysis_info(conn, asset_id)


def analysis_info(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM audio_analysis WHERE asset_id = ? ORDER BY created_at DESC LIMIT 1",
                       (asset_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["silence_count"] = conn.execute("SELECT COUNT(*) FROM silences WHERE analysis_id = ?",
                                      (row["id"],)).fetchone()[0]
    d["speech_detector"] = "громкость + детектор речи" if row["vad_json"] else "только громкость"
    return d


@lru_cache(maxsize=8)
def _load(path: str, offset_us: int, floor: float, speech: float, thr: float, vad_json: str | None) -> Loudness:
    vad = None if vad_json is None else tuple(tuple(x) for x in json.loads(vad_json))
    return Loudness(np.load(path), offset_us, floor, speech, thr, vad=vad)


def loudness(conn: sqlite3.Connection, asset_id: str) -> Loudness | None:
    info = analysis_info(conn, asset_id)
    if info is None or not Path(info["path"]).exists():
        return None
    return _load(info["path"], info["audio_offset_us"], info["floor_db"], info["speech_db"], info["threshold_db"],
                 info.get("vad_json"))


def silences(conn: sqlite3.Connection, asset_id: str, from_us: int = 0, to_us: int | None = None) -> list[dict]:
    info = analysis_info(conn, asset_id)
    if info is None:
        return []
    rows = conn.execute(
        "SELECT src_start_us, src_end_us, min_db FROM silences WHERE analysis_id = ? AND src_end_us > ? "
        "AND src_start_us < ? ORDER BY src_start_us", (info["id"], from_us, to_us if to_us is not None else 2**62))
    # Класс паузы определяет не детектор: здесь всегда «неизвестно»
    out = [{**dict(r), "duration_us": r["src_end_us"] - r["src_start_us"], "kind": "unknown"} for r in rows]
    # Участки видео вообще без звука (до начала звука и после его конца)
    asset = conn.execute("SELECT frame_count, fps_num, fps_den FROM assets WHERE id = ?", (asset_id,)).fetchone()
    loud = loudness(conn, asset_id)
    if loud is not None:
        video_end = asset["frame_count"] * 1_000_000 * asset["fps_den"] // asset["fps_num"]
        audio_end = loud.us(len(loud.db))
        edges = [(0, loud.offset_us), (audio_end, video_end)]
        for a, b in edges:
            hi = to_us if to_us is not None else 2**62
            if b - a >= MIN_SILENCE_US and b > from_us and a < hi:
                out.append({"src_start_us": a, "src_end_us": b, "min_db": None, "duration_us": b - a,
                            "kind": "no_audio"})
        out.sort(key=lambda s: s["src_start_us"])
    return out


# ---------- расшифровка ----------

def _segment_flags(text: str, avg_p: float | None, span, analysed: bool, language: str,
                   repeats: int) -> tuple[list[str], list[str]]:
    """Пометки фразы и найденные важные слова (для мягкой защиты)."""
    flags = []
    # Петля повторов — частая выдумка Whisper: та же фраза подряд
    if repeats >= 1:
        flags.append("repeated")
    markers = high_impact(text, language)
    if markers:
        flags.append("high_impact")
    if analysed and span is None:
        flags.append("no_speech")  # текст есть, а звука речи нет: возможная выдумка Whisper
    # Все слова неуверенные — скорее выдумка на шуме или музыке
    if suspect(text) or (avg_p is not None and avg_p < VERY_LOW_CONFIDENCE):
        flags.append("suspect_phrase")
    if avg_p is not None and avg_p < LOW_CONFIDENCE:
        flags.append("low_confidence")
    return flags, markers


def refresh_transcript(conn: sqlite3.Connection, asset_id: str, project_id: str) -> dict:
    """Пересчитать по текущей расшифровке, где в фразах речь, пометки и
    мягкую защиту — после улучшения анализа, без нового запуска Whisper.
    Текст, исправления автора и время слов не меняются."""
    t = current_transcript(conn, asset_id)
    if t is None:
        raise EdlError("no_transcript", "расшифровки ещё нет")
    loud = loudness(conn, asset_id)
    rows = conn.execute("SELECT * FROM transcript_segments WHERE transcript_id = ? ORDER BY idx", (t["id"],)).fetchall()
    changed = 0
    with write(conn):
        conn.execute("UPDATE protected_ranges SET removed_at = ? WHERE asset_id = ? AND created_by = 'system' "
                     "AND removed_at IS NULL", (_now(), asset_id))
        prev_text, repeats = None, 0
        for r in rows:
            start, end = r["src_start_us"], r["src_end_us"]
            span = loud.speech_span(start, end) if loud else (start, end)
            repeats = repeats + 1 if r["text_raw"] == prev_text else 0
            prev_text = r["text_raw"]
            flags, markers = _segment_flags(r["text_raw"], r["avg_p"], span, loud is not None, t["language"], repeats)
            new = (span[0] if span else None, span[1] if span else None, json.dumps(flags))
            if new != (r["speech_start_us"], r["speech_end_us"], r["flags_json"]):
                changed += 1
                conn.execute("UPDATE transcript_segments SET speech_start_us = ?, speech_end_us = ?, flags_json = ? "
                             "WHERE id = ?", (*new, r["id"]))
            if markers and span:
                conn.execute(
                    "INSERT INTO protected_ranges (id, project_id, asset_id, src_start_us, src_end_us, kind, reason, "
                    "created_by, created_at) VALUES (?,?,?,?,?,'soft',?,'system',?)",
                    (_id("prt"), project_id, asset_id, span[0], span[1], "в тексте: " + ", ".join(markers), _now()))
    return {"segments": len(rows), "changed": changed}


def save_transcript(conn: sqlite3.Connection, asset_id: str, project_id: str, *, params_hash: str, model: str,
                    language: str, segments: list[Segment], audio_offset_us: int) -> dict:
    loud = loudness(conn, asset_id)
    tid = _id("trn")
    with write(conn):
        if conn.execute("SELECT 1 FROM transcripts WHERE asset_id = ? AND params_hash = ?",
                        (asset_id, params_hash)).fetchone():
            raise EdlError("transcript_exists", "такая расшифровка уже есть")
        conn.execute("INSERT INTO transcripts (id, asset_id, params_hash, model, language, timing_level, created_at) "
                     "VALUES (?,?,?,?,?,'segment',?)", (tid, asset_id, params_hash, model, language, _now()))
        # Мягкая защита от прошлой расшифровки больше не действует
        conn.execute("UPDATE protected_ranges SET removed_at = ? WHERE asset_id = ? AND created_by = 'system' "
                     "AND removed_at IS NULL", (_now(), asset_id))
        prev_text, repeats = None, 0
        for i, s in enumerate(segments):
            start = s.start_ms * 1000 + audio_offset_us
            end = max(s.end_ms * 1000 + audio_offset_us, start + 1)
            span = loud.speech_span(start, end) if loud else (start, end)
            repeats = repeats + 1 if s.text == prev_text else 0
            prev_text = s.text
            flags, markers = _segment_flags(s.text, s.avg_p, span, loud is not None, language, repeats)
            sid = f"seg_{tid[4:]}_{i:05d}"
            conn.execute(
                "INSERT INTO transcript_segments (id, transcript_id, asset_id, idx, src_start_us, src_end_us, "
                "speech_start_us, speech_end_us, text_raw, avg_p, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, tid, asset_id, i, start, end, span[0] if span else None, span[1] if span else None,
                 s.text, s.avg_p, json.dumps(flags)))
            conn.execute("INSERT INTO segments_fts (segment_id, text) VALUES (?, ?)", (sid, s.text))
            conn.executemany(
                "INSERT INTO transcript_words (segment_id, idx, text, start_us, end_us, p) VALUES (?,?,?,?,?,?)",
                [(sid, k, w.text, w.start_ms * 1000 + audio_offset_us, w.end_ms * 1000 + audio_offset_us, w.p)
                 for k, w in enumerate(s.words)])
            if markers and span:
                conn.execute(
                    "INSERT INTO protected_ranges (id, project_id, asset_id, src_start_us, src_end_us, kind, reason, "
                    "created_by, created_at) VALUES (?,?,?,?,?,'soft',?,'system',?)",
                    (_id("prt"), project_id, asset_id, span[0], span[1],
                     "в тексте: " + ", ".join(markers), _now()))
    return transcript_info(conn, asset_id)


def current_transcript(conn: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM transcripts WHERE asset_id = ? ORDER BY created_at DESC LIMIT 1",
                        (asset_id,)).fetchone()


def transcript_info(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    t = current_transcript(conn, asset_id)
    if t is None:
        return None
    d = dict(t)
    d["segments"] = conn.execute("SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?",
                                 (t["id"],)).fetchone()[0]
    words = conn.execute("SELECT COUNT(*) FROM transcript_words w JOIN transcript_segments s ON s.id = w.segment_id "
                         "WHERE s.transcript_id = ?", (t["id"],)).fetchone()[0]
    # Время слов по DTW — приблизительное, на голосе автора ещё не проверено
    d["word_timing"] = "estimated" if words else None
    return d


def words(conn: sqlite3.Connection, segment_ids: list[str]) -> dict[str, list[dict]]:
    if not segment_ids:
        return {}
    marks = ",".join("?" * len(segment_ids))
    out: dict[str, list[dict]] = {sid: [] for sid in segment_ids}
    for r in conn.execute(f"SELECT * FROM transcript_words WHERE segment_id IN ({marks}) ORDER BY segment_id, idx",
                          segment_ids):
        out[r["segment_id"]].append({"idx": r["idx"], "text": r["text"], "start_us": r["start_us"],
                                     "end_us": r["end_us"], "p": r["p"]})
    return out


def _segment_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["flags"] = json.loads(d.pop("flags_json"))
    d["text"] = d["text_corrected"] or d["text_raw"]
    return d


def segments(conn: sqlite3.Connection, asset_id: str, from_us: int = 0, to_us: int | None = None,
             limit: int = 500) -> list[dict]:
    t = current_transcript(conn, asset_id)
    if t is None:
        return []
    rows = conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id = ? AND src_end_us > ? AND src_start_us < ? "
        "ORDER BY idx LIMIT ?", (t["id"], from_us, to_us if to_us is not None else 2**62, limit))
    return [_segment_dict(r) for r in rows]


def search(conn: sqlite3.Connection, project_id: str, query: str, limit: int = 50) -> list[dict]:
    """Поиск по тексту текущих расшифровок проекта, без учёта регистра."""
    words = [w for w in query.replace('"', " ").split() if w]
    if not words:
        return []
    match = " ".join(f'"{w}"*' for w in words)
    rows = conn.execute(
        "SELECT s.* FROM segments_fts f JOIN transcript_segments s ON s.id = f.segment_id "
        "JOIN assets a ON a.id = s.asset_id WHERE segments_fts MATCH ? AND a.project_id = ? "
        "AND s.transcript_id IN (SELECT id FROM transcripts t WHERE t.created_at = "
        "(SELECT MAX(created_at) FROM transcripts t2 WHERE t2.asset_id = t.asset_id)) "
        "ORDER BY s.asset_id, s.idx LIMIT ?", (match, project_id, limit))
    return [_segment_dict(r) for r in rows]


def correct_segment(conn: sqlite3.Connection, project_id: str, segment_id: str, text: str, actor: str) -> dict:
    """Исправление текста автором. Сырой текст Whisper остаётся как был."""
    _only_user(actor, "исправить текст")
    with write(conn):
        r = _one(conn, "SELECT s.* FROM transcript_segments s JOIN assets a ON a.id = s.asset_id "
                       "WHERE s.id = ? AND a.project_id = ?", segment_id, project_id)
        corrected = text.strip() or None
        conn.execute("UPDATE transcript_segments SET text_corrected = ? WHERE id = ?", (corrected, segment_id))
        conn.execute("DELETE FROM segments_fts WHERE segment_id = ?", (segment_id,))
        conn.execute("INSERT INTO segments_fts (segment_id, text) VALUES (?, ?)",
                     (segment_id, corrected or r["text_raw"]))
    return _segment_dict(_one(conn, "SELECT * FROM transcript_segments WHERE id = ?", segment_id))


def text_in_range(conn: sqlite3.Connection, asset_id: str, start_us: int, end_us: int,
                  any_overlap: bool = False) -> str:
    """Текст фраз, чья речь в основном попадает в диапазон, или (any_overlap)
    всех фраз, чью речь диапазон хоть немного задевает. Для проверки, что
    вырез не уносит важное, нужен второй вариант: задетая фраза — уже потеря."""
    parts = []
    for s in segments(conn, asset_id, start_us, end_us):
        a = s["speech_start_us"] if s["speech_start_us"] is not None else s["src_start_us"]
        b = s["speech_end_us"] if s["speech_end_us"] is not None else s["src_end_us"]
        overlap = min(b, end_us) - max(a, start_us)
        if b > a and (overlap > 0 if any_overlap else overlap * 2 >= b - a):
            parts.append(s["text"])
    return " ".join(parts)


def range_for_segments(conn: sqlite3.Connection, project_id: str, asset_id: str,
                       segment_ids: list[str]) -> tuple[int, int]:
    """Диапазон исходника, покрывающий речь выбранных фраз.

    Так агент может сказать «убрать фразы X–Y», не считая время сам.
    Начало — за шаг до первой речи (это уже тишина), конец — сразу после
    последней речи; дальше границы выравнивает сервер.
    """
    if not segment_ids:
        raise EdlError("no_segments", "не указаны фразы")
    t = current_transcript(conn, asset_id)
    if t is None:
        raise EdlError("no_transcript", "у исходника ещё нет расшифровки")
    marks = ",".join("?" * len(segment_ids))
    rows = conn.execute(f"SELECT * FROM transcript_segments WHERE transcript_id = ? AND id IN ({marks})",
                        (t["id"], *segment_ids)).fetchall()
    found = {r["id"] for r in rows}
    missing = [s for s in segment_ids if s not in found]
    if missing:
        raise EdlError("unknown_segment", f"таких фраз нет в текущей расшифровке этого файла: {missing}")
    starts, ends = [], []
    for r in rows:
        if r["speech_start_us"] is not None:
            starts.append(r["speech_start_us"] - HOP_US)
            ends.append(r["speech_end_us"])
        else:
            starts.append(r["src_start_us"])
            ends.append(r["src_end_us"])
    return max(0, min(starts)), max(ends)
