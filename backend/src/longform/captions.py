"""Субтитры и главы для YouTube по версии монтажа.

Субтитры — из исправленной расшифровки, время пересчитано на монтаж.
Слово попадает в субтитры, если его середина осталась в версии; вырезанное
(ложный старт, неудачный дубль) пропадает вместе с картинкой. Фразы с
пометками «нет речи» и «похоже на выдумку» не попадают, пока автор их не
исправил: это обычно выдумки распознавания на тишине.

Время слов — по DTW (приблизительно); у исправленной фразы слова
распределяются по её речи пропорционально длине. Разбивка на титры —
стартовые значения из распространённых правил (до 42 знаков в строке, две
строки, 1–7 с), не требование YouTube.

Главы — метки kind = 'chapter'. Правила YouTube (справка «Video chapters»,
проверено 2026-10-07): первая отметка 00:00, не меньше трёх по возрастанию,
глава не короче 10 секунд.
"""

import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from longform import asset_data, store
from longform.edl import CompiledEdl
from longform.timebase import frame_to_us

MAX_LINE = 42
MAX_LINES = 2
MAX_CUE_US = 7_000_000
MIN_CUE_US = 1_000_000
GAP_SPLIT_US = 700_000  # пауза длиннее — новый титр
MIN_GAP_US = 40_000  # зазор между титрами
SENTENCE_END = (".", "!", "?", "…")
SKIP_FLAGS = {"no_speech", "suspect_phrase"}

CHAPTER_MIN_US = 10_000_000
CHAPTER_MIN_COUNT = 3


@dataclass(frozen=True)
class Token:
    text: str
    asset_id: str
    start_us: int
    end_us: int


@dataclass(frozen=True)
class Cue:
    start_us: int
    end_us: int
    lines: tuple[str, ...]


def segment_tokens(seg: dict, words: list[dict]) -> list[Token]:
    """Слова фразы со временем исходника."""
    corrected = seg.get("text_corrected")
    if corrected is None and SKIP_FLAGS & set(seg.get("flags", [])):
        return []
    if corrected is None and words:
        return [Token(w["text"].strip(), seg["asset_id"], w["start_us"], w["end_us"])
                for w in words if w["text"].strip()]
    parts = (corrected or seg.get("text_raw") or "").split()
    if not parts:
        return []
    a = seg.get("speech_start_us") or seg["src_start_us"]
    b = seg.get("speech_end_us") or seg["src_end_us"]
    total = sum(len(p) + 1 for p in parts)
    out, pos = [], 0
    for p in parts:
        start = a + (b - a) * pos // total
        pos += len(p) + 1
        out.append(Token(p, seg["asset_id"], start, a + (b - a) * pos // total))
    return out


def edited_tokens(edl: CompiledEdl, tokens: list[Token]) -> list[Token]:
    """Те же слова во времени монтажа; вырезанные пропадают."""
    out = []
    for t in tokens:
        if t.asset_id not in edl.sources:
            continue
        fps = edl.sources[t.asset_id].fps
        mid = (t.start_us + t.end_us) // 2
        for c in edl.clips:
            if c.asset_id != t.asset_id:
                continue
            a, b = frame_to_us(c.src_start_frame, fps), frame_to_us(c.src_end_frame, fps)
            if a <= mid < b:
                base = frame_to_us(c.out_start_frame, edl.profile.fps)
                out.append(Token(t.text, t.asset_id, base + max(t.start_us, a) - a, base + min(t.end_us, b) - a))
                break
    out.sort(key=lambda t: t.start_us)
    return out


def wrap(text: str) -> tuple[str, ...]:
    """Одна строка или две, разрыв у пробела ближе к середине."""
    if len(text) <= MAX_LINE:
        return (text,)
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    fits = [i for i in spaces if i <= MAX_LINE and len(text) - i - 1 <= MAX_LINE]
    cut = min(fits or spaces or [len(text) // 2], key=lambda i: abs(i - len(text) / 2))
    return (text[:cut].strip(), text[cut:].strip())


def build_cues(tokens: list[Token]) -> list[Cue]:
    groups: list[list[Token]] = []
    for t in tokens:
        g = groups[-1] if groups else None
        if g:
            text = " ".join(x.text for x in g + [t])
            sentence_done = g[-1].text.endswith(SENTENCE_END) and len(" ".join(x.text for x in g)) >= 16
            if (t.start_us - g[-1].end_us > GAP_SPLIT_US or len(text) > MAX_LINE * MAX_LINES
                    or t.end_us - g[0].start_us > MAX_CUE_US or sentence_done):
                g = None
        if g is None:
            groups.append([t])
        else:
            g.append(t)
    cues = []
    for i, g in enumerate(groups):
        start, end = g[0].start_us, max(g[-1].end_us, g[0].start_us + MIN_CUE_US)
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0].start_us - MIN_GAP_US)
        end = max(end, start + 1)
        cues.append(Cue(start, end, wrap(" ".join(x.text for x in g))))
    return cues


def _srt_time(us: int) -> str:
    ms = us // 1000
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = [f"{i}\n{_srt_time(c.start_us)} --> {_srt_time(c.end_us)}\n" + "\n".join(c.lines)
              for i, c in enumerate(cues, 1)]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def revision_cues(conn: sqlite3.Connection, pid: str, rid: str) -> list[Cue]:
    edl = store.compile_revision(conn, pid, rid)
    tokens: list[Token] = []
    for asset_id in {c.asset_id for c in edl.clips}:
        segs = asset_data.segments(conn, asset_id, limit=1_000_000)
        by_seg = asset_data.words(conn, [s["id"] for s in segs if s.get("text_corrected") is None])
        for s in segs:
            tokens += segment_tokens(s, by_seg.get(s["id"], []))
    return build_cues(edited_tokens(edl, tokens))


def _stamp(us: int, long: bool) -> str:
    s = us // 1_000_000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if long else f"{s // 60:02d}:{s % 60:02d}"


def chapter_list(marks: list[dict], total_us: int, fps: Fraction) -> dict:
    """Главы по меткам kind='chapter' версии: строки для описания и что не так."""
    chapters = [m for m in marks if m.get("kind") == "chapter"]
    inside = sorted((m for m in chapters if m["in_revision"]), key=lambda m: m["edited_frame"])
    problems = [f"глава «{m['label']}» стоит на вырезанном месте и в список не попала"
                for m in chapters if not m["in_revision"]]
    items = [{"number": m["number"], "title": m["label"], "start_us": frame_to_us(m["edited_frame"], fps)}
             for m in inside]
    notes = []
    if items and items[0]["start_us"] > 0:
        notes.append(f"YouTube требует первую главу с 00:00: «{items[0]['title']}» начинается с начала ролика")
        items[0]["start_us"] = 0
    if len(items) < CHAPTER_MIN_COUNT:
        problems.append(f"нужно не меньше {CHAPTER_MIN_COUNT} глав, сейчас {len(items)}")
    for i, it in enumerate(items):
        end = items[i + 1]["start_us"] if i + 1 < len(items) else total_us
        it["duration_us"] = end - it["start_us"]
        if it["duration_us"] < CHAPTER_MIN_US:
            problems.append(f"глава «{it['title']}» короче 10 секунд")
    long = total_us >= 3_600_000_000
    return {
        "items": items,
        "text": "\n".join(f"{_stamp(it['start_us'], long)} {it['title']}" for it in items),
        "problems": problems,
        "notes": notes,
        "ok": not problems,
    }


def revision_chapters(conn: sqlite3.Connection, pid: str, rid: str) -> dict:
    edl = store.compile_revision(conn, pid, rid)
    total = frame_to_us(edl.total_frames, edl.profile.fps)
    return chapter_list(store.list_markers(conn, pid, rid), total, edl.profile.fps)
