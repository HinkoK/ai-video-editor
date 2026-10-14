"""Расшифровка через локальный whisper.cpp кусками, с кешем и пометками.

Что проверено на синтетической речи macOS (не на голосе автора):
- время в JSON абсолютное и при обработке куска с отступом (-ot);
- фраза, которая начинается в куске и кончается за его краем, теряется,
  а у начала куска время слов ошибается, если кусок начался посреди фразы.
  Поэтому куски перекрываются на 30 с с обеих сторон, а фраза берётся из
  того куска, в чьей середине (собственной зоне) она началась;
- границы сегментов приблизительны и захватывают паузы. Где внутри
  сегмента речь, решает громкость (analysis.speech_span);
- время слов по DTW (-dtw, работает только без ускоренного внимания -nfa)
  ставит начало слова с ошибкой 0–230 мс против секунд у обычного времени
  токенов. Поиск речи (--vad) сдвигает время токенов, поэтому с DTW он
  выключен: выдумки на тишине ловят громкость и детектор речи;
- -mc 0: не опираться на предыдущий текст — рвёт петли повторов;
- Whisper ошибается в смысловых местах: «без платной» стало «бесплатной»
  (на слух почти не различаются). Фразы с отрицаниями, ограничителями и
  числами помечаются.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from longform.ffmpeg import QUIET, CancelCheck, run

MODEL = Path(os.environ.get("LONGFORM_WHISPER_MODEL", "~/.cache/whisper/ggml-large-v3-q5_0.bin")).expanduser()
VAD_MODEL = Path(os.environ.get("LONGFORM_VAD_MODEL", "~/.cache/whisper/ggml-silero-v6.2.0.bin")).expanduser()
CHUNK_MS = 600_000
OVERLAP_MS = 30_000
# Меняется при изменении способа расшифровки: старые результаты перестают
# совпадать по отпечатку задачи и пересчитываются по запросу
ENGINE_VERSION = "whisper.cpp-dtw-3-repair"

# Слова, от которых зависит смысл: отрицания, ограничители, условия
MARKERS = {
    "ru": ["не", "ни", "нет", "без", "но", "однако", "только", "кроме", "если", "хотя", "зато", "нельзя",
           "никогда", "никто", "ничего", "платн\\w*", "бесплатн\\w*"],
    "uk": ["не", "ні", "немає", "без", "але", "проте", "тільки", "лише", "крім", "якщо", "хоча", "не можна",
           "ніколи", "ніхто", "нічого", "платн\\w*", "безкоштовн\\w*"],
}
NUMBER = re.compile(r"\d+([.,]\d+)?|%|\$|€|₽|₴|доллар\w*|рубл\w*|евро|процент\w*|гривн\w*|тысяч\w*|миллион\w*",
                    re.IGNORECASE)
# Известные выдумки Whisper на тишине и музыке в русскоязычных роликах
SUSPECT = re.compile(r"субтитр|редактор субтитров|корректор|продолжение следует|dimatorzok|подписывайтесь на канал",
                     re.IGNORECASE)


@dataclass(frozen=True)
class Word:
    start_ms: int  # от первого отсчёта звука; по DTW, приблизительно
    end_ms: int
    text: str
    p: float | None


@dataclass(frozen=True)
class Segment:
    start_ms: int  # от первого отсчёта звука
    end_ms: int
    text: str
    avg_p: float | None
    words: tuple[Word, ...] = ()


# Набор «голов внимания» для DTW зависит от модели
DTW_PRESETS = {"large-v3-turbo": "large.v3.turbo", "large-v3": "large.v3", "large-v2": "large.v2",
               "large-v1": "large.v1", "medium": "medium", "small": "small", "base": "base", "tiny": "tiny"}


def dtw_preset(model: Path) -> str | None:
    name = model.name.lower()
    for key, preset in DTW_PRESETS.items():  # длинные ключи раньше коротких
        if key in name:
            return preset
    return None


def high_impact(text: str, language: str = "ru") -> list[str]:
    """Отрицания, ограничители и числа в тексте. Грубо: лишнее допустимо, пропуск — нет."""
    found = []
    for word in MARKERS.get(language, MARKERS["ru"]):
        for m in re.finditer(rf"(?<!\w){word}(?!\w)", text, re.IGNORECASE):
            found.append(m.group(0).lower())
    found += [m.group(0).lower() for m in NUMBER.finditer(text)]
    return sorted(set(found))


WORD = re.compile(r"\w+", re.UNICODE)
RETAKE_SHARE = 0.75  # какая доля слов вырезанного дубля должна быть в оставленном


def markers_preserved(removed: str, kept: str, language: str = "ru") -> bool:
    """Можно ли снять пометку «важные слова» с выреза дубля.

    Только если оставленный дубль — действительно повтор вырезанного
    (большая часть его слов есть в оставленном) и все важные слова
    вырезанного есть и там. Если в вырезанном важных слов нет (защиту
    поставили по другой причине), пометка не снимается никогда.
    """
    marks = set(high_impact(removed, language))
    if not marks or not marks <= set(high_impact(kept, language)):
        return False
    words = {w.lower() for w in WORD.findall(removed)}
    kept_words = {w.lower() for w in WORD.findall(kept)}
    return bool(words) and len(words & kept_words) >= RETAKE_SHARE * len(words)


def suspect(text: str) -> bool:
    return bool(SUSPECT.search(text))


def make_16k(src: Path, dst: Path, stream_index: int, cancel: CancelCheck | None = None) -> None:
    """16 кГц моно для Whisper. Начинается с того же первого отсчёта, что и WAV исходника."""
    part = dst.with_name(dst.name + ".part")
    part.unlink(missing_ok=True)
    run(["ffmpeg", *QUIET, "-n", "-i", str(src), "-map", f"0:{stream_index}", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-f", "wav", str(part)], cancel=cancel)
    part.rename(dst)


def chunk_plan(duration_ms: int) -> list[tuple[int, int, int, int]]:
    """[(начало куска, длина, начало и конец его собственной зоны)].

    Кусок шире своей зоны на OVERLAP_MS с обеих сторон: у края куска
    распознавание хуже, а фраза берётся только из куска, в чьей зоне она
    началась, — то есть всегда из середины куска.
    """
    plan, own_start = [], 0
    while own_start < duration_ms:
        own_end = min(own_start + CHUNK_MS, duration_ms)
        start = max(0, own_start - OVERLAP_MS)
        end = min(duration_ms, own_end + OVERLAP_MS)
        plan.append((start, end - start, own_start, own_end))
        own_start = own_end
    return plan


def run_chunk(wav16: Path, out_json: Path, start_ms: int, length_ms: int, language: str,
              cancel: CancelCheck | None = None) -> None:
    """Один кусок. Результат пишется во временный файл и переименовывается:
    готовый кусок в кеше всегда целый, и после сбоя его не считаем заново."""
    stem = out_json.with_name(out_json.stem + ".part")
    preset = dtw_preset(MODEL)
    dtw = ["-nfa", "-dtw", preset] if preset else ["--vad", "-vm", str(VAD_MODEL)] if VAD_MODEL.exists() else []
    run(["whisper-cli", "-m", str(MODEL), "-l", language, "-f", str(wav16), "-ot", str(start_ms),
         "-d", str(length_ms), "-mc", "0", *dtw, "-ojf", "-of", str(stem), "-np"], cancel=cancel)
    Path(str(stem) + ".json").rename(out_json)


def _words(tokens: list[dict], seg_start: int, seg_end: int) -> tuple[Word, ...]:
    """Токены -> слова. Новое слово начинается с токена с пробелом впереди;
    знаки препинания и куски слова прилипают к предыдущему. Начало слова —
    время DTW первого токена (если посчитано), конец — начало следующего."""
    groups: list[list[dict]] = []
    for t in tokens:
        text = t.get("text", "")
        if not text.strip():
            continue
        if not groups or (text.startswith(" ") and any(ch.isalnum() for ch in text)):
            groups.append([t])
        else:
            groups[-1].append(t)
    starts = []
    for g in groups:
        dtw = g[0].get("t_dtw", -1)
        start = int(dtw) * 10 if dtw is not None and dtw >= 0 else int(g[0]["offsets"]["from"])
        starts.append(min(max(start, seg_start), seg_end))
    for i in range(1, len(starts)):  # время слов не должно идти назад
        starts[i] = max(starts[i], starts[i - 1])
    words = []
    for i, g in enumerate(groups):
        end = starts[i + 1] if i + 1 < len(groups) else seg_end
        probs = [t["p"] for t in g if "p" in t]
        words.append(Word(starts[i], max(end, starts[i]), "".join(t["text"] for t in g).strip(),
                          round(sum(probs) / len(probs), 3) if probs else None))
    return tuple(words)


def _bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _join_split_letters(tokens: list[dict]) -> list[dict]:
    """whisper.cpp пишет токены байтами: русская буква бывает разрезана между
    двумя токенами (" \\xd1" + "\\x8f…" = " я…", запись 13.10, 10:46), и такой
    JSON не читается как UTF-8. Токены с недописанной буквой склеиваются со
    следующими; время и DTW — у первого, уверенность — средняя."""
    out, buf, parts = [], b"", []
    for t in tokens:
        buf += _bytes(t.get("text", ""))
        parts.append(t)
        try:
            text = buf.decode("utf-8")
        except UnicodeDecodeError:
            if len(parts) < 4:  # буква дописывается в следующем токене
                continue
            text = buf.decode("utf-8", "replace")
        merged = dict(parts[0], text=text)
        probs = [p["p"] for p in parts if "p" in p]
        if probs:
            merged["p"] = sum(probs) / len(probs)
        out.append(merged)
        buf, parts = b"", []
    if parts:
        out.append(dict(parts[0], text=buf.decode("utf-8", "replace")))
    return out


def parse_chunk(path: Path) -> list[Segment]:
    # surrogateescape: недописанные буквы в токенах не роняют разбор, их склеит _join_split_letters
    data = json.loads(path.read_bytes().decode("utf-8", "surrogateescape"))
    out = []
    for s in data.get("transcription", []):
        tokens = _join_split_letters([t for t in s.get("tokens", []) if not t.get("text", "").startswith("[_")])
        probs = [t["p"] for t in tokens if "p" in t]
        text = _bytes(s.get("text", "")).decode("utf-8", "replace").strip()
        if text:
            a, b = int(s["offsets"]["from"]), int(s["offsets"]["to"])
            out.append(Segment(a, b, text, round(sum(probs) / len(probs), 3) if probs else None,
                               _words(tokens, a, b)))
    return out


# Перечитка пропусков. Whisper читает окнами по 30 с; если окно начинается
# с тишины, он бывает выдаёт «Субтитры сделал DimaTorzok» на всё окно и
# теряет настоящую речь в нём (запись автора: 5:11 и 55:07). Тот же кусок,
# поданный отдельно и с начала речи, он слышит правильно.
REPAIR_MIN_MS = 1500  # речь без текста короче этого не перечитываем
REPAIR_JOIN_MS = 1000  # отрезки речи ближе — один кусок
REPAIR_PAD_MS = 250
REPAIR_MAX_MS = 28_000  # меньше окна Whisper
INVENTED_P = 0.2  # средняя уверенность ниже — скорее выдумка


def looks_invented(seg: Segment) -> bool:
    return suspect(seg.text) or (seg.avg_p is not None and seg.avg_p < INVENTED_P)


def repair_windows(segments: list[Segment], speech_ms: list[tuple[int, int]],
                   duration_ms: int | None = None) -> list[tuple[int, int]]:
    """Куски, где детектор слышит речь, а настоящего текста нет: [начало, конец)
    в мс от начала звука, с небольшим запасом, не длиннее REPAIR_MAX_MS."""
    good = sorted((s.start_ms, s.end_ms) for s in segments if not looks_invented(s))
    regions: list[list[int]] = []
    for a, b in sorted(speech_ms):
        if regions and a - regions[-1][1] < REPAIR_JOIN_MS:
            regions[-1][1] = max(regions[-1][1], b)
        else:
            regions.append([a, b])
    windows = []
    for a, b in regions:
        pieces, cur = [], a
        for g0, g1 in good:  # вычесть то, что уже прочитано
            if g1 <= cur or g0 >= b:
                continue
            if g0 > cur:
                pieces.append((cur, g0))
            cur = max(cur, g1)
        if cur < b:
            pieces.append((cur, b))
        for p0, p1 in pieces:
            if p1 - p0 < REPAIR_MIN_MS:
                continue
            start = max(0, p0 - REPAIR_PAD_MS)
            end = p1 + REPAIR_PAD_MS if duration_ms is None else min(duration_ms, p1 + REPAIR_PAD_MS)
            while start < end:
                windows.append((start, min(end, start + REPAIR_MAX_MS)))
                start += REPAIR_MAX_MS
    return windows


def apply_repair(segments: list[Segment], repaired: list[tuple[tuple[int, int], list[Segment]]]) -> list[Segment]:
    """Выдумки в перечитанных кусках уходят, новые фразы встают на их место.
    Новая фраза не берётся, если сама похожа на выдумку или её середина уже
    внутри настоящей фразы."""
    windows = [w for w, _ in repaired]

    def in_window(s: Segment) -> bool:
        return any(min(s.end_ms, b) > max(s.start_ms, a) for a, b in windows)

    kept = [s for s in segments if not (looks_invented(s) and in_window(s))]
    good = [(s.start_ms, s.end_ms) for s in kept if not looks_invented(s)]
    added = []
    for (a, b), found in repaired:
        for s in found:
            mid = (s.start_ms + s.end_ms) // 2
            if looks_invented(s) or not a <= mid < b or any(g0 <= mid < g1 for g0, g1 in good):
                continue
            added.append(s)
            good.append((s.start_ms, s.end_ms))
    return sorted(kept + added, key=lambda s: s.start_ms)


def merge(chunks: list[tuple[tuple[int, int, int, int], list[Segment]]]) -> list[Segment]:
    """Склейка кусков по детерминированному правилу: сегмент принадлежит
    куску, в чьей собственной зоне он начался. Перекрытие нужно только для
    того, чтобы фраза у края зоны распознавалась целиком и без краевых ошибок."""
    merged: list[Segment] = []
    for (_, _, own_start, own_end), segments in chunks:
        for s in segments:
            if own_start <= s.start_ms < own_end:
                merged.append(s)
    merged.sort(key=lambda s: s.start_ms)
    return merged
