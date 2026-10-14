from fractions import Fraction

import pytest

from longform.edl import Clip, EdlError, SourceInfo
from longform.ops import (
    LONG_CUT_US,
    guard_slivers,
    Op,
    Protection,
    apply_ops,
    build_clips,
    kept_intervals,
    risk_flags,
    snap,
    validate_ops,
)
from longform.timebase import frame_to_us

FPS = Fraction(25)
CAM = SourceInfo("cam", FPS, 1000, 48000, 1, Fraction(0))
SCR = SourceInfo("scr", FPS, 500, 48000, 1, Fraction(0))
SOURCES = {"cam": CAM, "scr": SCR}
ORDER = ["cam", "scr"]


def us(frame):
    return frame_to_us(frame, FPS)


def full():
    return build_clips({"cam": [(0, 1000)], "scr": [(0, 500)]}, SOURCES, ORDER)


def rm(asset, a, b, reason="technical_dead_air", **kw):
    return Op("remove", asset, us(a), us(b), reason, "пауза", **kw)


def restore(asset, a, b):
    return Op("restore", asset, us(a), us(b), "restore", "вернуть")


def run(base, ops):
    snapped = [snap(o, SOURCES) for o in ops]
    validate_ops(snapped, kept_intervals(base, SOURCES))
    return apply_ops(base, snapped, SOURCES, ORDER)


def frames(clips):
    return [(c.asset_id, round(c.src_start_us * 25 / 1e6), round(c.src_end_us * 25 / 1e6)) for c in clips]


def test_files_follow_each_other_in_order():
    assert frames(full()) == [("cam", 0, 1000), ("scr", 0, 500)]


def test_remove_splits_clip_and_ids_stay_stable():
    base = full()
    out = run(base, [rm("cam", 100, 200)]).clips
    assert frames(out) == [("cam", 0, 100), ("cam", 200, 1000), ("scr", 0, 500)]
    # Неизменившиеся куски сохраняют id: к ним можно привязывать обсуждение
    assert out[0].id == base[0].id
    assert out[2].id == base[1].id


def test_restore_brings_back_part_of_a_cut():
    once = run(full(), [rm("cam", 100, 200)]).clips
    back = run(once, [restore("cam", 150, 200)]).clips
    assert frames(back)[:2] == [("cam", 0, 100), ("cam", 150, 1000)]


def test_boundaries_snap_to_nearest_frame():
    op = Op("remove", "cam", us(100) + 15_000, us(200) - 25_000, "false_start", "фальстарт")
    s = snap(op, SOURCES)
    assert (s.start_frame, s.end_frame) == (100, 199)


def test_sliver_between_cuts_is_removed_with_a_note():
    applied = run(full(), [rm("cam", 100, 200), rm("cam", 201, 300)])
    assert frames(applied.clips)[:2] == [("cam", 0, 100), ("cam", 300, 1000)]
    assert "огрызок в 1 кадр" in applied.notes[0]


@pytest.mark.parametrize(
    "ops, code",
    [
        ([rm("cam", 100, 200), rm("cam", 150, 250)], "ops_overlap"),
        ([rm("cam", 990, 1010)], "out_of_bounds"),
        ([Op("remove", "cam", us(5), us(5) + 10_000, "false_start", "x")], "too_short"),
        ([rm("cam", 1, 2, reason="boring")], "bad_reason"),
        ([Op("remove", "cam", us(1), us(9), "false_start", "  ")], "no_why"),
        ([Op("restore", "cam", us(1), us(9), "failed_take", "x")], "bad_reason"),
        ([restore("cam", 10, 20)], "already_present"),
        ([rm("nope", 1, 9)], "unknown_asset"),
        ([rm("cam", 0, 1000), rm("scr", 0, 500)], "empty"),
    ],
)
def test_invalid_ops_are_rejected(ops, code):
    with pytest.raises(EdlError) as err:
        run(full(), ops)
    assert err.value.code == code


def test_removing_already_removed_range_is_an_error():
    once = run(full(), [rm("cam", 100, 200)]).clips
    with pytest.raises(EdlError) as err:
        run(once, [rm("cam", 120, 180)])
    assert err.value.code == "already_removed"


def test_failed_take_must_keep_its_alternative():
    alt = ("cam", us(300), us(400))
    ok = rm("cam", 100, 200, reason="failed_take", kept_alternative=alt)
    run(full(), [ok])
    # Тем же предложением вырезан и «оставленный» дубль
    with pytest.raises(EdlError) as err:
        run(full(), [ok, rm("cam", 350, 360)])
    assert err.value.code == "alternative_removed"


def test_any_subset_of_ops_applies():
    """Частичное принятие: любое подмножество правок даёт корректную версию."""
    ops = [rm("cam", 100, 200), rm("cam", 300, 400), rm("scr", 10, 50)]
    for mask in range(1, 8):
        subset = [o for i, o in enumerate(ops) if mask >> i & 1]
        clips = run(full(), subset).clips
        removed = sum(1 for i in range(3) if mask >> i & 1)
        assert len(clips) == 2 + removed


def test_risk_flags():
    protections = [
        Protection("cam", us(150), us(160), "hard"),
        Protection("cam", us(500), us(510), "soft"),
        Protection("scr", us(150), us(160), "hard"),  # другой файл, не задевает
    ]
    flags = lambda op: risk_flags(snap(op, SOURCES), protections)  # noqa: E731
    assert flags(rm("cam", 100, 200)) == ["protected"]
    assert flags(rm("cam", 400, 600)) == ["high_impact"]
    assert flags(rm("cam", 10, 20, reason="other")) == ["reason_other"]
    long_frames = LONG_CUT_US // 40_000 + 1  # 40 000 мкс = один кадр при 25 fps
    assert risk_flags(snap(rm("cam", 0, long_frames), SOURCES), []) == ["long_cut"]
    assert flags(rm("cam", 10, 20)) == []
    # Касание границы — не пересечение
    assert flags(rm("cam", 140, 150)) == []
    assert flags(restore("cam", 140, 160)) == []


def test_quiet_gap_between_cuts_goes_with_them():
    """Запасы у речи могут оставить между соседними вырезами тихий огрызок.
    Это не материал, а лишний стык: он уходит, если в нём нет речи."""
    ops = [rm("cam", 100, 200), rm("cam", 205, 300)]  # между ними 5 кадров = 200 мс
    quiet = run_with(ops, lambda asset, a, b: True)
    assert frames(quiet.clips)[:2] == [("cam", 0, 100), ("cam", 300, 1000)]
    assert "тихий огрызок 200 мс" in quiet.notes[0]
    # В огрызке речь — он остаётся
    speech = run_with(ops, lambda asset, a, b: False)
    assert ("cam", 200, 205) in frames(speech.clips)


def run_with(ops, is_quiet):
    base = full()
    snapped = [snap(o, SOURCES) for o in ops]
    validate_ops(snapped, kept_intervals(base, SOURCES))
    return apply_ops(base, snapped, SOURCES, ORDER, is_quiet=is_quiet)


def test_old_short_piece_survives_an_unrelated_cut():
    """Тихий огрызок убирается, только если он зажат между вырезами этого же
    предложения. Короткий кусок из прошлой версии не трогаем."""
    base = run_with([rm("cam", 100, 200), rm("cam", 205, 300)], lambda asset, a, b: False).clips
    assert ("cam", 200, 205) in frames(base)
    later = run_with_base(base, [rm("cam", 600, 700)], lambda asset, a, b: True)
    assert ("cam", 200, 205) in frames(later.clips)


def run_with_base(base, ops, is_quiet):
    snapped = [snap(o, SOURCES) for o in ops]
    validate_ops(snapped, kept_intervals(base, SOURCES))
    return apply_ops(base, snapped, SOURCES, ORDER, is_quiet=is_quiet)


# ---------- перестановка кусков ----------

def mv(asset, a, b, target, side="after"):
    return Op("move", asset, us(a), us(b), "reorder", "автор попросил переставить",
              target=(asset, us(target), side))


def run_moves(base, ops, moves=(), is_quiet=None):
    snapped = [snap(o, SOURCES, op_id=f"op{i}") for i, o in enumerate(ops)]
    validate_ops(snapped, kept_intervals(base, SOURCES), moves)
    return apply_ops(base, snapped, SOURCES, ORDER, is_quiet=is_quiet, moves=moves)


def test_move_puts_block_after_the_anchor_frame():
    done = run_moves(full(), [mv("cam", 100, 200, 600)])
    assert frames(done.clips) == [("cam", 0, 100), ("cam", 200, 600), ("cam", 100, 200), ("cam", 600, 1000),
                                  ("scr", 0, 500)]
    (m,) = done.moves
    assert (m.op_id, m.src_start_us, m.src_end_us, m.target_src_us, m.side) == ("op0", us(100), us(200), us(600), "after")
    # «перед кадром» — то же место с другой стороны: перед 600 = после 599
    before = run_moves(full(), [mv("cam", 100, 200, 600, "before")])
    assert frames(before.clips) == frames(done.clips)
    # Переставить в начало ролика
    first = run_moves(full(), [mv("cam", 300, 400, 0, "before")])
    assert frames(first.clips)[:3] == [("cam", 300, 400), ("cam", 0, 300), ("cam", 400, 1000)]
    # id куска — из исходника и начала: у каждого свой, без повторов
    assert len({c.id for c in done.clips}) == len(done.clips)


def test_layout_rebuilds_the_same_clips():
    done = run_moves(full(), [mv("cam", 100, 200, 600)])
    again = build_clips(kept_intervals(done.clips, SOURCES), SOURCES, ORDER, done.moves)
    assert again == done.clips
    # Без перестановок — прежний порядок файлов
    assert build_clips(kept_intervals(done.clips, SOURCES), SOURCES, ORDER) == full()


def test_cut_inside_the_moved_block_stays_in_it():
    moved = run_moves(full(), [mv("cam", 100, 200, 600)])
    cut = run_moves(moved.clips, [rm("cam", 140, 160)], moved.moves)
    assert cut.moves == moved.moves  # следующая правка перестановку сохраняет
    assert frames(cut.clips)[:5] == [("cam", 0, 100), ("cam", 200, 600), ("cam", 100, 140), ("cam", 160, 200),
                                     ("cam", 600, 1000)]


def test_restore_inside_goes_into_the_block_outside_to_its_place():
    cut = run(full(), [rm("cam", 140, 160), rm("cam", 300, 320)]).clips
    moved = run_moves(cut, [mv("cam", 100, 200, 600)])
    inside = run_moves(moved.clips, [restore("cam", 140, 160)], moved.moves)
    assert frames(inside.clips)[:4] == [("cam", 0, 100), ("cam", 200, 300), ("cam", 320, 600), ("cam", 100, 200)]
    outside = run_moves(moved.clips, [restore("cam", 300, 320)], moved.moves)
    assert frames(outside.clips)[:5] == [("cam", 0, 100), ("cam", 200, 600), ("cam", 100, 140), ("cam", 160, 200),
                                         ("cam", 600, 1000)]


def test_anchor_cut_away_goes_after_the_nearest_kept_piece():
    moved = run_moves(full(), [mv("cam", 100, 200, 600)])
    # Кадр 599 вырезан позже: кусок встаёт после оставленного с наибольшим концом ≤ 600
    cut = run_moves(moved.clips, [rm("cam", 550, 650)], moved.moves)
    assert frames(cut.clips)[:4] == [("cam", 0, 100), ("cam", 200, 550), ("cam", 100, 200), ("cam", 650, 1000)]


def test_unmove_brings_back_exactly_the_previous_order():
    base = run(full(), [rm("cam", 50, 60), rm("cam", 700, 720)]).clips
    moved = run_moves(base, [mv("cam", 100, 200, 600)])
    back = run_moves(moved.clips, [Op("unmove", "cam", us(100), us(200), "reorder", "автор вернул",
                                      undo_of=moved.moves[0].op_id)], moved.moves)
    assert back.clips == base and back.moves == []
    with pytest.raises(EdlError) as err:  # второй раз снимать нечего
        run_moves(back.clips, [Op("unmove", "cam", us(100), us(200), "reorder", "x", undo_of="op0")], back.moves)
    assert err.value.code == "not_moved"


@pytest.mark.parametrize(
    "ops, code",
    [
        ([mv("cam", 100, 200, 150)], "move_into_itself"),
        ([mv("cam", 100, 200, 200)], "move_into_itself"),  # после своего последнего кадра
        ([mv("cam", 100, 200, 100)], "move_nowhere"),  # после кадра 99 — он и так перед куском
        ([mv("cam", 100, 200, 200, "before")], "move_nowhere"),
        ([mv("cam", 100, 200, 600), rm("cam", 700, 800)], "move_alone"),
        ([mv("cam", 100, 101, 600)], "move_sliver"),  # кусок в 1 кадр
        ([mv("cam", 100, 200, 501)], "move_sliver"),  # место в кадре от края оставленного (500)
        ([Op("move", "cam", us(1), us(9), "reorder", "x")], "no_target"),
        ([Op("move", "cam", us(1), us(9), "reorder", "x", target=("scr", us(10), "after"))], "move_between_files"),
        ([Op("move", "cam", us(1), us(9), "other", "x", target=("cam", us(10), "after"))], "bad_reason"),
        ([Op("move", "cam", us(1), us(9), "reorder", "x", target=("cam", us(10), "middle"))], "bad_side"),
    ],
)
def test_invalid_moves_are_rejected(ops, code):
    base = run(full(), [rm("cam", 400, 500)]).clips  # оставленный интервал начинается с кадра 500
    with pytest.raises(EdlError) as err:
        run_moves(base, ops)
    assert err.value.code == code


def test_nothing_to_move_when_block_is_cut_away():
    cut = run(full(), [rm("cam", 100, 200)]).clips
    with pytest.raises(EdlError) as err:
        run_moves(cut, [mv("cam", 120, 180, 600)])
    assert err.value.code == "nothing_to_move"


def guarded(base, ops, moves=()):
    """Как сервер: снап, огрызки у стыков — в саму правку, проверка, применение."""
    snapped = [snap(o, SOURCES, op_id=f"op{i}") for i, o in enumerate(ops)]
    snapped, notes = guard_slivers(snapped, kept_intervals(base, SOURCES), moves, SOURCES, ORDER)
    validate_ops(snapped, kept_intervals(base, SOURCES), moves)
    return snapped, notes, apply_ops(base, snapped, SOURCES, ORDER, moves=moves)


def test_cut_next_to_a_move_edge_takes_the_sliver_and_reverts_exactly():
    """Свежий взгляд: вырез 50–99 у начала переставленного куска 100–200
    оставлял кадр 99 огрызком, сервер его выбрасывал, и «вернуть как было»
    возвращало 50–99 — кадр 99 терялся навсегда. Теперь огрызок уходит в сам
    вырез (50–100), и возврат ровно тот же диапазон."""
    moved = run_moves(full(), [mv("cam", 100, 200, 600)])
    (s,), notes, cut = guarded(moved.clips, [rm("cam", 50, 99)], moved.moves)
    assert (s.start_frame, s.end_frame) == (50, 100) and "огрызок в 1 кадр у стыка" in notes[0]
    assert frames(cut.clips)[0] == ("cam", 0, 50)
    _, _, back = guarded(cut.clips, [restore("cam", 50, 100)], cut.moves)
    assert back.clips == moved.clips
    # Без подтяжки огрызок не выбрасывается молча — ошибка
    with pytest.raises(EdlError) as err:
        run_moves(moved.clips, [rm("cam", 50, 99)], moved.moves)
    assert err.value.code == "sliver"
    # Возврат, от которого остался бы один огрызок, — ошибка, а не тихая потеря
    cut1 = run(full(), [rm("cam", 100, 103)]).clips
    with pytest.raises(EdlError) as err:
        guarded(run(cut1, [rm("cam", 103, 200)]).clips, [restore("cam", 101, 102)])
    assert err.value.code == "sliver"


def test_move_risk_flags():
    s = snap(mv("cam", 100, 200, 600), SOURCES)
    assert risk_flags(s, []) == ["reorder"]
    # Мягкая защита у перестановки пометки не ставит
    assert risk_flags(s, [Protection("cam", us(150), us(160), "soft")]) == ["reorder"]
    assert risk_flags(s, [Protection("cam", us(150), us(160), "hard")]) == ["protected", "reorder"]
    # Место режет защищённое пополам
    assert risk_flags(s, [Protection("cam", us(590), us(610), "hard")]) == ["protected", "reorder"]
    assert risk_flags(s, [Protection("cam", us(600), us(610), "hard")]) == ["reorder"]


def test_random_edits_with_moves_never_lose_or_repeat_a_frame():
    """Свойство: та же цепочка вырезов и возвратов, что применена к версии с
    перестановками (с теми же границами, как их поставил сервер), без
    перестановок даёт тот же набор кадров; ни один кадр не повторён;
    раскладка из оставленного и слоя перестановок — ровно куски версии;
    правка и сразу «вернуть как было» — ровно прежняя версия (и для
    перестановки, и для выреза или возврата на версии с перестановками)."""
    import random

    from longform.edl import OutputProfile, compile_edl
    from longform.ops import layout

    rng = random.Random(29)
    profile = OutputProfile(FPS, 48000)
    applied_steps = moves_done = unmoves_done = undone_cuts = 0
    for _ in range(500):
        clips, moves, plain, n = full(), [], full(), 0
        for _ in range(rng.randint(1, 8)):
            kind = rng.choice(["remove", "restore", "move", "move", "unmove"])
            asset = rng.choice(["cam", "cam", "scr"])
            limit = SOURCES[asset].frame_count
            a = rng.randrange(0, limit - 2)
            b = rng.randint(a + 1, min(limit, a + rng.choice([5, 40, 200])))
            if kind == "remove":
                op = rm(asset, a, b)
            elif kind == "restore":
                op = restore(asset, a, b)
            elif kind == "move":
                op = mv(asset, a, b, rng.randint(0, limit), rng.choice(["after", "before"]))
            elif moves:
                m = rng.choice(moves)
                op = Op("unmove", m.asset_id, m.src_start_us, m.src_end_us, "reorder", "вернуть", undo_of=m.op_id)
            else:
                continue
            try:
                s = snap(op, SOURCES, op_id=f"op{n}")
                (s,), _ = guard_slivers([s], kept_intervals(clips, SOURCES), moves, SOURCES, ORDER)
                validate_ops([s], kept_intervals(clips, SOURCES), moves)
                out = apply_ops(clips, [s], SOURCES, ORDER, moves=moves)
            except EdlError:
                continue
            n += 1
            applied_steps += 1
            if op.kind in ("remove", "restore"):
                # Та же правка с теми же границами — в цепочке без перестановок
                plain = apply_ops(plain, [s], SOURCES, ORDER).clips
                # Правка и сразу «вернуть как было» — ровно прежняя версия (если
                # правка целиком меняла свой диапазон: вырез — весь в версии,
                # возврат — весь вырезан; иначе «вернуть» по диапазону шире правки)
                had = sum(max(0, min(y, s.end_frame) - max(x, s.start_frame))
                          for x, y in kept_intervals(clips, SOURCES).get(asset, []))
                if had == (s.end_frame - s.start_frame if op.kind == "remove" else 0):
                    undo = snap(Op("restore" if op.kind == "remove" else "remove", asset, s.start_us, s.end_us,
                                   "restore" if op.kind == "remove" else "other", "вернуть"), SOURCES)
                    (undo,), _ = guard_slivers([undo], kept_intervals(out.clips, SOURCES), out.moves, SOURCES,
                                               ORDER)
                    back = apply_ops(out.clips, [undo], SOURCES, ORDER, moves=out.moves)
                    assert back.clips == clips and back.moves == moves
                    undone_cuts += bool(moves)
            # 1) Кадры те же, что у той же цепочки без перестановок, ни один не повторён
            got = all_frames(out.clips)
            assert sorted(got) == sorted(all_frames(plain))
            assert len(got) == len(set(got))
            compile_edl(out.clips, SOURCES, profile)
            # 2) Раскладка из оставленного и слоя перестановок — ровно эта версия
            kept = kept_intervals(out.clips, SOURCES)
            assert build_clips(kept, SOURCES, ORDER, out.moves) == out.clips
            assert [(c.asset_id, c.src_start_us, c.src_end_us) for c in out.clips] == [
                (a_, us(x), us(y)) for a_, x, y in layout(kept, out.moves, SOURCES, ORDER)]
            # 3) Снятие только что сделанной перестановки — прежний порядок
            if op.kind == "move":
                moves_done += 1
                back = apply_ops(out.clips, [snap(Op("unmove", op.asset_id, op.src_start_us, op.src_end_us,
                                                     "reorder", "вернуть", undo_of=f"op{n - 1}"), SOURCES)],
                                 SOURCES, ORDER, moves=out.moves)
                assert back.clips == clips and back.moves == moves
            if op.kind == "unmove":
                unmoves_done += 1
            clips, moves = out.clips, out.moves
    # Проверка проверки: перестановки, снятия и откаты правок на версиях с перестановками случались
    assert applied_steps > 1000 and moves_done > 200 and unmoves_done > 30 and undone_cuts > 100


def all_frames(clips):
    return [(asset, f) for asset, a, b in frames(clips) for f in range(a, b)]
