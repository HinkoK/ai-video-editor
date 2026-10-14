"""Правки монтажа: применить операции к версии и посчитать пометки риска.

Чистая логика, без базы и ffmpeg. Версия — это набор оставленных интервалов
каждого исходника плюс упорядоченный слой перестановок. Без перестановок интервалы выкладываются в порядке файлов.
«Убрать» и «вернуть» — операции над множествами кадров, поэтому любое
подмножество правок предложения применяется однозначно (на этом держится
частичное принятие). Перестановка привязана ко времени исходника, как
комментарии: она меняет только раскладку (layout) и переживает следующие
вырезы и возвраты.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from longform.edl import MIN_CLIP_FRAMES, Clip, EdlError, SourceInfo
from longform.timebase import frame_to_us, us_to_frame

REMOVE_REASONS = (
    "failed_take",  # неудачный дубль, в kept_alternative — какой оставлен
    "false_start",  # фальстарт
    "restart_gap",  # пауза перед повтором
    "technical_dead_air",  # техническая пауза
    "recording_setup",  # подготовка записи
    "waiting",  # сокращённое ожидание результата: начало и конец остаются
    "vocal_noise",  # кашель, «кхм» — звук голоса без слов (longform.noises)
    "other",  # только с объяснением, всегда рискованная
)
RESTORE_REASON = "restore"
# Перестановка куска и её снятие («вернуть как было» в проверке)
MOVE_REASON = "reorder"
MOVE_SIDES = ("after", "before")
# Вырез длиннее этого в режиме чистки принимается только поштучно
LONG_CUT_US = 20_000_000
# Тихий огрызок короче этого между вырезами — не материал, а лишний стык
QUIET_GAP_US = 300_000

Interval = tuple[int, int]  # кадры [начало, конец)
Piece = tuple[str, int, int]  # кусок раскладки: (исходник, кадр начала, кадр конца)


@dataclass(frozen=True)
class Op:
    kind: str  # "remove" | "restore" | "move" | "unmove"
    asset_id: str
    src_start_us: int
    src_end_us: int
    reason: str
    why: str
    # Для неудачного дубля: какой кусок исходника оставлен вместо него
    kept_alternative: tuple[str, int, int] | None = None
    segment_ids: tuple[str, ...] = ()
    # Точная правка по словам: граница — в ближайший тихий момент рядом,
    # без запаса от речи (иначе слово-паразит внутри фразы не вырезать)
    precise: bool = False
    # Перестановка: куда поставить кусок — (исходник, время исходника, сторона).
    # 'after' — сразу после кадра, который кончается в этот момент; 'before' —
    # перед кадром, который в этот момент начинается
    target: tuple[str, int, str] | None = None
    # Снятие перестановки: id правки move, которую снимаем
    undo_of: str | None = None


@dataclass(frozen=True)
class Move:
    """Запись слоя перестановок версии: все оставленные кадры файла из
    [src_start_us, src_end_us) идут подряд сразу после кадра исходника
    target_src_us − 1 (side='after') или перед кадром target_src_us
    (side='before'). op_id — правка, которая её сделала: по нему её снимает
    «вернуть как было»."""

    op_id: str
    asset_id: str
    src_start_us: int
    src_end_us: int
    target_asset_id: str
    target_src_us: int
    side: str

    def as_dict(self) -> dict:
        return {"op_id": self.op_id, "asset_id": self.asset_id, "src_start_us": self.src_start_us,
                "src_end_us": self.src_end_us, "target_asset_id": self.target_asset_id,
                "target_src_us": self.target_src_us, "side": self.side}

    @staticmethod
    def from_dict(d: Mapping) -> "Move":
        return Move(d["op_id"], d["asset_id"], d["src_start_us"], d["src_end_us"], d["target_asset_id"],
                    d["target_src_us"], d["side"])


@dataclass(frozen=True)
class Protection:
    asset_id: str
    src_start_us: int
    src_end_us: int
    kind: str  # "hard" (ставит автор) | "soft" (пометка кода или агента)


@dataclass(frozen=True)
class SnappedOp:
    op: Op
    start_frame: int
    end_frame: int
    start_us: int
    end_us: int
    # Перестановка: место на границе кадра (P — кадр, перед которым или после
    # предыдущего которого встаёт кусок)
    target_frame: int | None = None
    target_us: int | None = None
    # id правки в базе — нужен записи слоя перестановок (Move.op_id)
    op_id: str | None = None


@dataclass(frozen=True)
class Applied:
    clips: list[Clip]
    notes: list[str]
    moves: list[Move] = field(default_factory=list)


def _subtract(intervals: list[Interval], cut: Interval) -> list[Interval]:
    a, b = cut
    out = []
    for x, y in intervals:
        if y <= a or x >= b:
            out.append((x, y))
            continue
        if x < a:
            out.append((x, a))
        if y > b:
            out.append((b, y))
    return out


def _add(intervals: list[Interval], piece: Interval) -> list[Interval]:
    merged: list[Interval] = []
    for x, y in sorted([*intervals, piece]):
        if merged and x <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], y))
        else:
            merged.append((x, y))
    return merged


def _overlap(a: Interval, b: Interval) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def kept_intervals(clips: Iterable[Clip], sources: Mapping[str, SourceInfo]) -> dict[str, list[Interval]]:
    kept: dict[str, list[Interval]] = {}
    for c in clips:
        fps = sources[c.asset_id].fps
        kept[c.asset_id] = _add(kept.get(c.asset_id, []),
                                (us_to_frame(c.src_start_us, fps), us_to_frame(c.src_end_us, fps)))
    return kept


def _anchor(rest: list[Piece], m: Move, sources: Mapping[str, SourceInfo]) -> tuple[list[Piece], int] | None:
    """Куда вставить кусок перестановки m в раскладку rest (без самого куска).

    after P: после куска с кадром P−1 (кусок режется на P); если P−1
    вырезан — после оставленного куска того же файла с наибольшим концом ≤ P;
    иначе перед первым куском этого файла. before P — зеркально. None —
    кусков этого файла не осталось, ставить некуда."""
    t = m.target_asset_id
    if t not in sources:
        return None
    p = us_to_frame(m.target_src_us, sources[t].fps)
    rest = list(rest)
    own = [i for i, piece in enumerate(rest) if piece[0] == t]
    if not own:
        return None
    if m.side == "after":
        for i in own:
            _, x, y = rest[i]
            if x <= p - 1 < y:
                if p < y:
                    rest[i:i + 1] = [(t, x, p), (t, p, y)]
                return rest, i + 1
        before = [i for i in own if rest[i][2] <= p]
        if before:
            return rest, max(before, key=lambda i: rest[i][2]) + 1
        return rest, own[0]
    for i in own:
        _, x, y = rest[i]
        if x <= p < y:
            if x < p:
                rest[i:i + 1] = [(t, x, p), (t, p, y)]
                return rest, i + 1
            return rest, i
    after = [i for i in own if rest[i][1] >= p]
    if after:
        return rest, min(after, key=lambda i: rest[i][1])
    return rest, own[-1] + 1


def _place_move(seq: list[Piece], m: Move, sources: Mapping[str, SourceInfo]) -> list[Piece]:
    """Вынуть оставленные кадры [начало, конец) файла (разрезая куски) и
    вставить их подряд, в прежнем порядке, у якоря перестановки."""
    if m.asset_id not in sources:
        return seq
    fps = sources[m.asset_id].fps
    a, b = us_to_frame(m.src_start_us, fps), us_to_frame(m.src_end_us, fps)
    rest: list[Piece] = []
    block: list[Piece] = []
    for piece in seq:
        asset, x, y = piece
        if asset != m.asset_id or y <= a or x >= b:
            rest.append(piece)
            continue
        if x < a:
            rest.append((asset, x, a))
        block.append((asset, max(x, a), min(y, b)))
        if y > b:
            rest.append((asset, b, y))
    if not block:
        return seq  # всё в куске уже вырезано — переставлять нечего
    found = _anchor(rest, m, sources)
    if found is None:
        return seq
    rest, i = found
    return rest[:i] + block + rest[i:]


def _glue(seq: list[Piece]) -> list[Piece]:
    """Соседние куски одного файла, идущие встык и в исходнике, — один кусок."""
    out: list[Piece] = []
    for asset, x, y in seq:
        if out and out[-1][0] == asset and out[-1][2] == x:
            out[-1] = (asset, out[-1][1], y)
        else:
            out.append((asset, x, y))
    return out


def layout(kept: Mapping[str, list[Interval]], moves: Sequence[Move], sources: Mapping[str, SourceInfo],
           asset_order: list[str]) -> list[Piece]:
    """Раскладка версии: оставленные интервалы по порядку файлов, затем
    перестановки по очереди. Без перестановок — ровно прежний порядок файлов."""
    seq: list[Piece] = [(asset, a, b) for asset in asset_order for a, b in kept.get(asset, [])]
    for m in moves:
        seq = _place_move(seq, m, sources)
    return _glue(seq)


def neighbours(kept: Mapping[str, list[Interval]], moves: Sequence[Move], sources: Mapping[str, SourceInfo],
               asset_order: list[str], asset: str, a: int, b: int) -> tuple[tuple[str, int] | None, tuple[str, int] | None]:
    """Где в ролике стояли бы кадры [a, b) файла, если их вернуть: что идёт
    перед ними — (файл, кадр конца) — и после них — (файл, кадр начала).
    None — край ролика. Так вырез узнаёт свой стык и рядом с перестановкой:
    стык выреза — между этими соседями."""
    k = {x: list(v) for x, v in kept.items()}
    k[asset] = _add(k.get(asset, []), (a, b))
    seq = layout(k, moves, sources, asset_order)
    i = next((n for n, p in enumerate(seq) if p[0] == asset and p[1] <= a < p[2]), None)
    if i is None:
        return None, None
    _, x, y = seq[i]
    prev = (asset, a) if x < a else ((seq[i - 1][0], seq[i - 1][2]) if i > 0 else None)
    if y > b:
        nxt = (asset, b)
    else:
        nxt = (seq[i + 1][0], seq[i + 1][1]) if i + 1 < len(seq) else None
    return prev, nxt


def lands_next_to(kept: Mapping[str, list[Interval]], moves: Sequence[Move], sources: Mapping[str, SourceInfo],
                  asset_order: list[str], asset: str, a: int, b: int, after_frame: int | None,
                  before_frame: int | None) -> bool:
    """Встанут ли возвращённые кадры [a, b) в ролике вплотную: сразу после
    кадра after_frame или сразу перед кадром before_frame того же файла (а
    не, например, в переставленный кусок в другом месте ролика)."""
    k = {x: list(v) for x, v in kept.items()}
    k[asset] = _add(k.get(asset, []), (a, b))
    seq = layout(k, moves, sources, asset_order)

    def same_piece(f: int, g: int) -> bool:
        return any(p[0] == asset and p[1] <= f < p[2] and p[1] <= g < p[2] for p in seq)
    if after_frame is not None and not same_piece(after_frame, a):
        return False
    return before_frame is None or same_piece(b - 1, before_frame)


def _to_clips(seq: list[Piece], sources: Mapping[str, SourceInfo]) -> list[Clip]:
    clips = []
    for asset, a, b in seq:
        fps = sources[asset].fps
        start = frame_to_us(a, fps)
        clips.append(Clip(f"{asset}@{start}", asset, start, frame_to_us(b, fps)))
    return clips


def build_clips(kept: Mapping[str, list[Interval]], sources: Mapping[str, SourceInfo],
                asset_order: list[str], moves: Sequence[Move] = ()) -> list[Clip]:
    """Оставленные интервалы и перестановки -> куски ролика по порядку.

    id куска выводится из исходника и начала: у неизменившегося куска он
    одинаковый во всех версиях (кадр в версии один раз — id не повторяется).
    """
    return _to_clips(layout(kept, moves, sources, asset_order), sources)


def snap(op: Op, sources: Mapping[str, SourceInfo], op_id: str | None = None) -> SnappedOp:
    """Граница правки -> ближайшая граница кадра. Тишину ищет этап 4."""
    src = sources.get(op.asset_id)
    if src is None:
        raise EdlError("unknown_asset", f"исходник {op.asset_id} не найден")
    if op.kind not in ("remove", "restore", "move", "unmove"):
        raise EdlError("bad_op", f"неизвестная операция {op.kind!r}")
    if op.kind == "remove" and op.reason not in REMOVE_REASONS:
        raise EdlError("bad_reason", f"причина {op.reason!r} не из списка: {', '.join(REMOVE_REASONS)}")
    if op.kind == "restore" and op.reason != RESTORE_REASON:
        raise EdlError("bad_reason", f"у возврата причина должна быть {RESTORE_REASON!r}")
    if op.kind in ("move", "unmove") and op.reason != MOVE_REASON:
        raise EdlError("bad_reason", f"у перестановки причина должна быть {MOVE_REASON!r}")
    if not op.why.strip():
        raise EdlError("no_why", "у каждой правки должно быть объяснение")
    a, b = us_to_frame(op.src_start_us, src.fps), us_to_frame(op.src_end_us, src.fps)
    if a < 0 or b > src.frame_count:
        raise EdlError("out_of_bounds", f"диапазон {op.src_start_us}–{op.src_end_us} мкс вне исходника")
    if b <= a:
        raise EdlError("too_short", "после выравнивания по кадрам от диапазона ничего не осталось")
    target_frame = target_us = None
    if op.kind == "move":
        if op.target is None:
            raise EdlError("no_target", "у перестановки должно быть место: после или перед каким моментом")
        t_asset, t_us, side = op.target
        if side not in MOVE_SIDES:
            raise EdlError("bad_side", "место перестановки: after (после) или before (перед)")
        t_src = sources.get(t_asset)
        if t_src is None:
            raise EdlError("unknown_asset", f"исходник {t_asset} не найден")
        if t_asset != op.asset_id:
            raise EdlError("move_between_files", "переставлять можно только внутри одного файла записи")
        target_frame = us_to_frame(t_us, t_src.fps)
        if not 0 <= target_frame <= t_src.frame_count:
            raise EdlError("out_of_bounds", f"место перестановки {t_us} мкс вне исходника")
        target_us = frame_to_us(target_frame, t_src.fps)
    if op.kind == "unmove" and not op.undo_of:
        raise EdlError("no_undo_of", "снять можно только названную перестановку")
    return SnappedOp(op, a, b, frame_to_us(a, src.fps), frame_to_us(b, src.fps), target_frame, target_us, op_id)


def validate_ops(snapped: list[SnappedOp], base: Mapping[str, list[Interval]], moves: Sequence[Move] = ()) -> None:
    """Правки одного предложения не пересекаются и каждая что-то меняет."""
    if len(snapped) > 1 and any(s.op.kind in ("move", "unmove") for s in snapped):
        raise EdlError("move_alone", "перестановка — отдельным предложением: одна перестановка, без вырезов "
                                     "и возвратов")
    by_asset: dict[str, list[tuple[Interval, int]]] = {}
    for i, s in enumerate(snapped):
        by_asset.setdefault(s.op.asset_id, []).append(((s.start_frame, s.end_frame), i))
        kept = base.get(s.op.asset_id, [])
        present = sum(_overlap((s.start_frame, s.end_frame), k) for k in kept)
        if s.op.kind == "remove" and present == 0:
            raise EdlError("already_removed", f"правка {i}: этого диапазона уже нет в версии")
        if s.op.kind == "restore" and present == s.end_frame - s.start_frame:
            raise EdlError("already_present", f"правка {i}: этот диапазон и так в версии")
        if s.op.kind == "move":
            if present == 0:
                raise EdlError("nothing_to_move", f"правка {i}: этого куска нет в версии — переставлять нечего")
            anchor = s.target_frame - 1 if s.op.target[2] == "after" else s.target_frame
            if s.start_frame <= anchor < s.end_frame:
                raise EdlError("move_into_itself", f"правка {i}: место перестановки внутри самого куска")
        if s.op.kind == "unmove" and not any(m.op_id == s.op.undo_of for m in moves):
            raise EdlError("not_moved", f"правка {i}: этой перестановки в версии уже нет")
    for ranges in by_asset.values():
        ranges.sort()
        for (prev, i), (cur, j) in zip(ranges, ranges[1:]):
            if cur[0] < prev[1]:
                raise EdlError("ops_overlap", f"правки {i} и {j} задевают одни и те же кадры")


def _resnap(s: SnappedOp, a: int, b: int, sources: Mapping[str, SourceInfo]) -> SnappedOp:
    fps = sources[s.op.asset_id].fps
    start, end = frame_to_us(a, fps), frame_to_us(b, fps)
    return replace(s, op=replace(s.op, src_start_us=start, src_end_us=end), start_frame=a, end_frame=b,
                   start_us=start, end_us=end)


def _raw_kept(base: Mapping[str, list[Interval]], snapped: list[SnappedOp],
              sources: Mapping[str, SourceInfo]) -> dict[str, list[Interval]]:
    kept = {a: list(ivs) for a, ivs in base.items()}
    for s in snapped:
        if s.op.kind == "remove":
            kept[s.op.asset_id] = _subtract(kept.get(s.op.asset_id, []), (s.start_frame, s.end_frame))
        elif s.op.kind == "restore":
            frame_count = sources[s.op.asset_id].frame_count
            kept[s.op.asset_id] = _add(kept.get(s.op.asset_id, []), (s.start_frame, min(s.end_frame, frame_count)))
    return kept


def guard_slivers(snapped: list[SnappedOp], base: Mapping[str, list[Interval]], moves: Sequence[Move],
                  sources: Mapping[str, SourceInfo], asset_order: list[str]) -> tuple[list[SnappedOp], list[str | None]]:
    """Огрызок короче MIN_CLIP_FRAMES, который вырез или возврат оставил бы у
    стыка (края оставленного куска или границы перестановки), уходит в саму
    правку: вырез расширяется до стыка, возврат сужается. Так ни один кадр не
    выбрасывается молча, а «вернуть как было» откатывает правку ровно — её
    диапазон уже включает огрызок (раньше огрызок у края
    перестановки выбрасывался и «вернуть» его не возвращало). Возврат, от
    которого остался бы один огрызок, — ошибка sliver."""
    ops = list(snapped)
    notes: list[str | None] = [None] * len(ops)
    if not ops or any(s.op.kind in ("move", "unmove") for s in ops):
        return ops, notes
    for _ in range(4 * len(ops) + 4):
        seq = layout(_raw_kept(base, ops, sources), moves, sources, asset_order)
        short = next((p for p in seq if p[2] - p[1] < MIN_CLIP_FRAMES), None)
        if short is None:
            return ops, notes
        asset, x, y = short
        for i, s in enumerate(ops):
            if s.op.asset_id != asset:
                continue
            new = None
            if s.op.kind == "remove":
                if s.end_frame == x:
                    new = (s.start_frame, y)
                elif s.start_frame == y:
                    new = (x, s.end_frame)
            elif s.start_frame <= x and y <= s.end_frame:
                if y == s.end_frame and x > s.start_frame:
                    new = (s.start_frame, x)
                elif x == s.start_frame and y < s.end_frame:
                    new = (y, s.end_frame)
            if new is None:
                continue
            ops[i] = _resnap(s, *new, sources)
            what = "уходит с вырезом" if s.op.kind == "remove" else "не возвращается"
            note = f"огрызок в {y - x} кадр у стыка {what} (кадры {x}–{y})"
            notes[i] = f"{notes[i]}; {note}" if notes[i] else note
            break
        else:
            if any(o.op.kind == "restore" and o.op.asset_id == asset and o.start_frame <= x and y <= o.end_frame
                   for o in ops):
                raise EdlError("sliver", f"от возврата остался бы кусок в {y - x} кадр ({asset}, кадры {x}–{y}): "
                                         "короче двух кадров ролик не собрать — верните его вместе с соседним "
                                         "вырезанным")
            raise EdlError("sliver", f"правка оставила бы кусок в {y - x} кадр ({asset}, кадры {x}–{y}) у стыка: "
                                     "сдвиньте границу к стыку или дальше от него")
    raise EdlError("sliver", "границы правки не удалось поставить без огрызков у стыков")


def apply_ops(
    base_clips: list[Clip],
    snapped: list[SnappedOp],
    sources: Mapping[str, SourceInfo],
    asset_order: list[str],
    is_quiet: Callable[[str, int, int], bool] | None = None,
    moves: Sequence[Move] = (),
) -> Applied:
    """is_quiet(asset, start_us, end_us): нет ли речи в интервале. Если
    задана, тихие огрызки короче QUIET_GAP_US убираются вместе с вырезами.
    moves — слой перестановок базовой версии; в ответе — слой новой версии
    (move добавляет запись, unmove снимает запись своей правки)."""
    kept = kept_intervals(base_clips, sources)
    new_moves = list(moves)
    reorder = False
    for s in snapped:
        piece = (s.start_frame, s.end_frame)
        if s.op.kind == "remove":
            kept[s.op.asset_id] = _subtract(kept.get(s.op.asset_id, []), piece)
        elif s.op.kind == "restore":
            frame_count = sources[s.op.asset_id].frame_count
            kept[s.op.asset_id] = _add(kept.get(s.op.asset_id, []), (piece[0], min(piece[1], frame_count)))
        elif s.op.kind == "move":
            reorder = True
            new_moves.append(Move(s.op_id or "", s.op.asset_id, s.start_us, s.end_us, s.op.target[0],
                                  s.target_us, s.op.target[2]))
        else:  # unmove
            reorder = True
            new_moves = [m for m in new_moves if m.op_id != s.op.undo_of]
    # Огрызок короче двух кадров между вырезами — щелчок, а не речь
    notes = []
    for asset, intervals in kept.items():
        slivers = [iv for iv in intervals if iv[1] - iv[0] < MIN_CLIP_FRAMES]
        for a, b in slivers:
            notes.append(f"{asset}: огрызок в {b - a} кадр между вырезами убран (кадры {a}–{b})")
        kept[asset] = [iv for iv in intervals if iv[1] - iv[0] >= MIN_CLIP_FRAMES]
        if is_quiet is not None:
            fps = sources[asset].fps
            # Только огрызок, зажатый между двумя вырезами этого предложения:
            # старые короткие куски и отдельные возвраты не трогаем
            cut_ends = {s.end_frame for s in snapped if s.op.kind == "remove" and s.op.asset_id == asset}
            cut_starts = {s.start_frame for s in snapped if s.op.kind == "remove" and s.op.asset_id == asset}
            survivors = []
            for a, b in kept[asset]:
                start, end = frame_to_us(a, fps), frame_to_us(b, fps)
                between = a in cut_ends and b in cut_starts
                if between and end - start < QUIET_GAP_US and is_quiet(asset, start, end):
                    notes.append(f"{asset}: тихий огрызок {(end - start) // 1000} мс между вырезами убран "
                                 f"(кадры {a}–{b})")
                else:
                    survivors.append((a, b))
            kept[asset] = survivors
    seq = layout(kept, new_moves, sources, asset_order)
    # Граница перестановки режет оставленный интервал: огрызок короче двух
    # кадров не собрать (compile_edl отклонит). У самой перестановки это
    # ошибка move_sliver — границу ставят к стыку; у выреза и возврата огрызок
    # заранее уходит в саму правку (guard_slivers), а если всё же остался —
    # ошибка: молча выброшенный кадр «вернуть как было» уже не вернул бы
    short = next((p for p in seq if p[2] - p[1] < MIN_CLIP_FRAMES), None)
    if short is not None:
        asset, a, b = short
        if reorder:
            raise EdlError("move_sliver", f"после перестановки остаётся кусок в {b - a} кадр ({asset}, кадры "
                                          f"{a}–{b}): границу куска или место — к стыку")
        raise EdlError("sliver", f"правка оставила бы кусок в {b - a} кадр ({asset}, кадры {a}–{b}) у края "
                                 "перестановки: сдвиньте границу к стыку")
    if not seq:
        raise EdlError("empty", "после правок в ролике ничего не осталось")
    if any(s.op.kind == "move" for s in snapped) and seq == layout(kept, moves, sources, asset_order):
        raise EdlError("move_nowhere", "кусок и так стоит на этом месте: порядок ролика не меняется")
    clips = _to_clips(seq, sources)
    _check_alternatives(snapped, kept_intervals(clips, sources), sources)
    return Applied(clips, notes, new_moves)


def _check_alternatives(snapped: list[SnappedOp], kept: Mapping[str, list[Interval]],
                        sources: Mapping[str, SourceInfo]) -> None:
    """Если вырезан неудачный дубль, оставленный вместо него сам должен остаться."""
    for i, s in enumerate(snapped):
        alt = s.op.kept_alternative
        if alt is None:
            continue
        asset, start_us, end_us = alt
        if asset not in sources:
            raise EdlError("unknown_asset", f"правка {i}: оставленный дубль ссылается на {asset}")
        fps = sources[asset].fps
        a, b = us_to_frame(start_us, fps), us_to_frame(end_us, fps)
        present = sum(_overlap((a, b), k) for k in kept.get(asset, []))
        if b <= a or present < b - a:
            raise EdlError(
                "alternative_removed",
                f"правка {i}: дубль, оставленный вместо вырезанного, сам вырезан этой версией",
            )


def moved_protected(asset_id: str, start_us: int, end_us: int, target_us: int | None,
                    protections: Iterable[Protection]) -> bool:
    """Перестановка задевает жёсткую защиту: защищённое внутри куска или
    место перестановки режет защищённое пополам."""
    return any(
        p.kind == "hard" and p.asset_id == asset_id
        and (_overlap((start_us, end_us), (p.src_start_us, p.src_end_us)) > 0
             or (target_us is not None and p.src_start_us < target_us < p.src_end_us))
        for p in protections)


def risk_flags(s: SnappedOp, protections: Iterable[Protection]) -> list[str]:
    """Пометки риска. Это категории для проверки, а не вероятности.

    Перестановка всегда рискованная (reorder): проверяется поштучно, но
    применение в режиме «сразу» не блокирует — блокирует только жёсткая
    защита (protected). Мягкая защита у перестановки пометки не ставит:
    кусок переезжает целиком, слова не теряются."""
    if s.op.kind == "move":
        protected = moved_protected(s.op.asset_id, s.start_us, s.end_us, s.target_us, protections)
        return ["protected", MOVE_REASON] if protected else [MOVE_REASON]
    if s.op.kind != "remove":
        return []
    flags = []
    for p in protections:
        if p.asset_id != s.op.asset_id:
            continue
        if _overlap((s.start_us, s.end_us), (p.src_start_us, p.src_end_us)) > 0:
            flag = "protected" if p.kind == "hard" else "high_impact"
            if flag not in flags:
                flags.append(flag)
    if s.op.reason == "other":
        flags.append("reason_other")
    if s.end_us - s.start_us > LONG_CUT_US:
        flags.append("long_cut")
    return flags
