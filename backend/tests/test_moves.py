"""Перестановка кусков в редакторе: предложения,
режим «сразу», жёсткая защита, «вернуть как было», следующие правки и стыки.

Синтетика без замера громкости: границы не выравниваются по тишине, это
проверяет арифметику версий, а не голос автора."""

import sqlite3
from types import SimpleNamespace

import pytest

from longform import graphics, store
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.ops import Op
from longform.store import Conflict
from longform.timebase import frame_to_us

pytestmark = pytest.mark.media


def us(frame):
    return frame_to_us(frame, 25)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "editor.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def pr(conn, fx_plain):
    p = store.create_project(conn, "перестановка", "user")
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    return SimpleNamespace(id=p["id"], cam=cam["id"], rev=rev["id"])


def current(conn, pr):
    return store.get_project(conn, pr.id)["current_revision_id"]


def rm(asset, a, b):
    return Op("remove", asset, us(a), us(b), "technical_dead_air", "пауза")


def mv(asset, a, b, target, side="after"):
    return Op("move", asset, us(a), us(b), "reorder", "автор: «поставь блок после таймлайна» (A1#237)",
              target=(asset, us(target), side))


def agent(conn, pr, ops):
    return store.create_proposal(conn, pr.id, base_revision_id=current(conn, pr), pass_="cleanup",
                                 rationale="перестановка по указанию автора", policy_version="t", ops=ops,
                                 actor="agent")


def frames(conn, pr, rid=None):
    clips = store.get_revision(conn, pr.id, rid or current(conn, pr))["clips"]
    return [(round(c["src_start_us"] * 25 / 1e6), round(c["src_end_us"] * 25 / 1e6)) for c in clips]


def test_move_in_apply_mode_is_applied_at_once_with_reorder_flag(conn, pr):
    prop = agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    (op,) = prop["ops"]
    assert prop["status"] == "approved" and prop["auto_applied"] == 1
    assert op["decision"] == "accepted" and op["review"] == "pending"
    assert op["risk_flags"] == ["reorder"]
    assert op["target"] == {"asset_id": pr.cam, "src_us": us(600), "side": "after"}
    assert frames(conn, pr) == [(0, 100), (200, 600), (100, 200), (600, 1500)]
    # Слой перестановок версии и где кусок в ролике: вход, выход, откуда взят
    rev = store.get_revision(conn, pr.id, current(conn, pr))
    (m,) = rev["moves"]
    assert m["op_id"] == op["id"] and (m["out_start_frame"], m["out_end_frame"]) == (500, 600)
    js = store.joints(store.compile_revision(conn, pr.id, current(conn, pr)))
    assert [(j["out_frame"], j["kind"]) for j in js] == [(100, "move"), (500, "move"), (600, "move")]
    assert m["joints"] == {"in": 1, "out": 2, "from": 0}
    # Проверке «после»: стык — вход куска
    assert op["current_joint_frame"] == 500 and op["candidate_joint_frame"] == 500
    assert store.agent_review_counts(conn, pr.id, current(conn, pr)) == {"unreviewed": 1, "held": 0}


def test_hard_protection_holds_the_move_for_author(conn, pr):
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160), kind="hard",
                         reason="оговорка", actor="user")
    prop = agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    (op,) = prop["ops"]
    assert op["review"] == "held" and "protected" in op["risk_flags"] and prop["status"] == "open"
    assert current(conn, pr) == pr.rev
    store.review_op(conn, pr.id, prop["id"], op["id"], action="apply", actor="user", reason="порядок важнее")
    assert frames(conn, pr) == [(0, 100), (200, 600), (100, 200), (600, 1500)]


def test_place_that_cuts_hard_protection_is_held_too(conn, pr):
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(590), src_end_us=us(610), kind="hard",
                         reason="целая мысль", actor="user")
    prop = agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    assert prop["ops"][0]["review"] == "held"
    # Мягкая защита у перестановки пометки не ставит
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(800), src_end_us=us(810), kind="soft",
                         reason="число", actor="agent")
    ok = agent(conn, pr, [mv(pr.cam, 750, 850, 300)])
    assert ok["ops"][0]["risk_flags"] == ["reorder"] and ok["ops"][0]["review"] == "pending"


def test_revert_in_review_restores_exactly_the_previous_clips(conn, pr):
    agent(conn, pr, [rm(pr.cam, 40, 50)])
    before = current(conn, pr)
    before_json = conn.execute("SELECT clips_json FROM revisions WHERE id = ?", (before,)).fetchone()[0]
    prop = agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    done = store.review_op(conn, pr.id, prop["id"], prop["ops"][0]["id"], action="revert", actor="user")
    assert done["ops"][0]["review"] == "reverted"
    row = conn.execute("SELECT clips_json, moves_json FROM revisions WHERE id = ?", (current(conn, pr),)).fetchone()
    assert row["clips_json"] == before_json and row["moves_json"] == "[]"
    with pytest.raises(Conflict):
        store.review_op(conn, pr.id, prop["id"], prop["ops"][0]["id"], action="revert", actor="user")


def test_later_edits_keep_the_move(conn, pr):
    prop = agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    # Вырез после перестановки — внутри куска и снаружи
    agent(conn, pr, [rm(pr.cam, 140, 160), rm(pr.cam, 300, 320)])
    assert frames(conn, pr) == [(0, 100), (200, 300), (320, 600), (100, 140), (160, 200), (600, 1500)]
    # Частичное принятие следующего предложения (режим «предлагает»)
    store.set_agent_mode(conn, pr.id, "propose", "user")
    nxt = agent(conn, pr, [rm(pr.cam, 800, 820), rm(pr.cam, 900, 920)])
    store.approve(conn, pr.id, nxt["id"], actor="user", exclude=[nxt["ops"][1]["id"]])
    assert frames(conn, pr) == [(0, 100), (200, 300), (320, 600), (100, 140), (160, 200), (600, 800), (820, 1500)]
    moves = store.get_revision(conn, pr.id, current(conn, pr))["moves"]
    assert [m["op_id"] for m in moves] == [prop["ops"][0]["id"]]
    # «Вернуть» перестановку после следующих правок: вырезы остаются, порядок — исходника
    store.review_op(conn, pr.id, prop["id"], prop["ops"][0]["id"], action="revert", actor="user")
    assert frames(conn, pr) == [(0, 140), (160, 300), (320, 800), (820, 1500)]


def test_revert_of_a_revision_keeps_its_moves(conn, pr):
    agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    moved = current(conn, pr)
    agent(conn, pr, [rm(pr.cam, 900, 950)])
    store.revert(conn, pr.id, moved, "user")
    assert frames(conn, pr) == frames(conn, pr, moved)
    assert store.get_revision(conn, pr.id, current(conn, pr))["moves"][0]["op_id"] == \
        store.get_revision(conn, pr.id, moved)["moves"][0]["op_id"]


def test_cut_inside_the_next_piece_is_not_tied_to_the_move_joint(conn, pr):
    """Стык перестановки (вход куска) не принимает чужие вырезы: вырез внутри
    куска, идущего следом, смотрит на свой стык."""
    agent(conn, pr, [mv(pr.cam, 600, 700, 100)])  # [0,100) → [600,700) → [100,600) …
    prop = agent(conn, pr, [rm(pr.cam, 300, 320)])
    # Свой стык: 100 кадров до места, 100 — кусок, 200 — от 100 до 300; не вход куска на 100
    assert prop["ops"][0]["current_joint_frame"] == 400


def test_move_rules(conn, pr):
    for ops, code in [
        ([mv(pr.cam, 100, 200, 150)], "move_into_itself"),
        ([mv(pr.cam, 100, 200, 100)], "move_nowhere"),
        ([mv(pr.cam, 100, 200, 600), rm(pr.cam, 900, 950)], "move_alone"),
    ]:
        with pytest.raises(EdlError) as err:
            agent(conn, pr, ops)
        assert err.value.code == code
    agent(conn, pr, [rm(pr.cam, 100, 200)])
    with pytest.raises(EdlError) as err:
        agent(conn, pr, [mv(pr.cam, 120, 180, 600)])
    assert err.value.code == "nothing_to_move"


def test_nudge_on_a_move_joint(conn, pr):
    agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    rev = current(conn, pr)
    with pytest.raises(EdlError) as err:  # «вернуть» на стыке перестановки — кадры уже в ролике
        store.nudge_joint(conn, pr.id, revision_id=rev, joint=1, side="after", frames=5, actor="user")
    assert err.value.code == "move_joint"
    # «Убрать» работает: начало переставленного куска короче на 5 кадров
    store.nudge_joint(conn, pr.id, revision_id=rev, joint=1, side="after", frames=-5, actor="user")
    assert frames(conn, pr) == [(0, 100), (200, 600), (105, 200), (600, 1500)]


def test_graphic_split_by_the_move_is_flagged(conn, pr):
    graphics.add_graphic(conn, pr.id, actor="agent", asset_id=pr.cam, src_start_us=us(180), src_end_us=us(260),
                         template="keyword", props={"text": "Сундук"}, goal="тема")
    prop = agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    assert prop["ops"][0]["risk_flags"] == ["reorder", "splits_graphic"]
    # Части графики — не встык: в ролике два отдельных рисунка
    edl = store.compile_revision(conn, pr.id, current(conn, pr))
    drawn = [(o.start, o.frames) for o in graphics.overlays(conn, pr.id, edl) if o.template == "keyword"]
    assert drawn == [(100, 60), (580, 20)]


def test_revisions_with_moves_stay_immutable(conn, pr):
    agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE revisions SET moves_json = '[]' WHERE id = ?", (current(conn, pr),))


def test_move_boundaries_go_to_silence_and_snap_to_joints(conn, pr, tmp_path):
    """Выравнивание перестановки: начало куска — перед речью, конец — после,
    место «после фразы» — после её речи; край оставленного интервала ближе
    0,3 с без речи между — граница встаёт на стык, огрызка тишины не остаётся."""
    import numpy as np

    from longform import asset_data

    S = 100  # шагов громкости (по 10 мс) в секунде
    db = np.full(6000, -80.0)
    for a, b in [(2, 4), (5, 8), (10, 12), (20, 25), (30, 35)]:
        db[a * S:b * S] = -20.0
    asset_data.save_analysis(conn, pr.cam, "a", db, tmp_path / "db.npy", audio_offset_us=0)
    agent(conn, pr, [Op("remove", pr.cam, us(205), us(245), "technical_dead_air", "пауза"),
                     Op("remove", pr.cam, us(475), us(496), "technical_dead_air", "пауза")])
    # Как просит Claude по фразам: кусок — речь 20–25 с, место — после речи, кончившейся на 8 с
    prop = agent(conn, pr, [Op("move", pr.cam, 19_990_000, 25_000_000, "reorder", "автор попросил",
                               target=(pr.cam, 8_000_000, "after"))])
    (op,) = prop["ops"]
    assert op["src_start_us"] == us(496)  # подтянуто к стыку выреза (19,84 с), а не 19,93 с
    assert op["src_end_us"] == us(627)  # 25,07 с — запас тишины после речи, по кадру
    assert op["target_src_us"] == us(205)  # к стыку выреза 8,2 с, а не 8,07 с
    assert "подтянуто к стыку" in op["adjust_note"]
    assert op["risk_flags"] == ["reorder"]  # все три границы в тишине
    assert frames(conn, pr) == [(0, 205), (496, 627), (245, 475), (627, 1500)]
    # Сдвинуть стык «убрать» на месте, откуда взят кусок, можно
    js = store.joints(store.compile_revision(conn, pr.id, current(conn, pr)))
    assert [j["kind"] for j in js] == ["move", "move", "move"]


def test_precise_move_keeps_edges_and_protects_from_slivers(conn, pr):
    """Ручная перестановка автора (precise): граница на стыке остаётся ровно на
    нём, граница в кадре от стыка встаёт на стык."""
    agent(conn, pr, [rm(pr.cam, 300, 400), rm(pr.cam, 500, 510)])
    mine = store.create_proposal(
        conn, pr.id, base_revision_id=current(conn, pr), pass_="cleanup", rationale="таймлайн",
        policy_version="manual", actor="user",
        ops=[Op("move", pr.cam, us(400), us(499), "reorder", "вручную", precise=True,
                target=(pr.cam, us(100), "before"))])
    store.approve(conn, pr.id, mine["id"], actor="user", acknowledge=[o["id"] for o in mine["ops"]])
    assert (mine["ops"][0]["src_start_us"], mine["ops"][0]["src_end_us"]) == (us(400), us(500))
    assert frames(conn, pr) == [(0, 100), (400, 500), (100, 300), (510, 1500)]


# ---------- свежий взгляд (13.10) ----------

def test_cut_at_a_move_edge_keeps_every_frame_and_reverts_exactly(conn, pr):
    """Вырез 50–99 у начала переставленного куска 100–200: кадр 99 уходит в
    сам вырез (а не выбрасывается), «вернуть как было» — ровно прежние куски."""
    agent(conn, pr, [mv(pr.cam, 100, 200, 600)])
    before = frames(conn, pr)
    cut = agent(conn, pr, [rm(pr.cam, 50, 99)])
    (op,) = cut["ops"]
    assert (op["src_start_us"], op["src_end_us"]) == (us(50), us(100))
    assert "огрызок в 1 кадр у стыка уходит с вырезом" in op["adjust_note"]
    store.review_op(conn, pr.id, cut["id"], op["id"], action="revert", actor="user")
    assert frames(conn, pr) == before


def test_cut_of_silence_where_the_block_was_taken_has_its_joint(conn, pr):
    """Регламент v29 велит убрать тишину на месте, откуда взят кусок: у таких
    вырезов есть стык (для «После» и точного ролика), и «вернуть кадры» на
    этом стыке разрешено — кадры по ту сторону вырезаны, а не переставлены."""
    agent(conn, pr, [mv(pr.cam, 600, 700, 300, "before")])  # [0,300) [600,700) [300,600) [700,1500)
    prop = agent(conn, pr, [rm(pr.cam, 590, 600), rm(pr.cam, 700, 712)])
    assert [o["current_joint_frame"] for o in prop["ops"]] == [690, 690]
    assert [o["candidate_joint_frame"] for o in prop["ops"]] == [690, 690]
    rev = current(conn, pr)
    js = store.joints(store.compile_revision(conn, pr.id, rev))
    k = next(i for i, j in enumerate(js) if j["out_frame"] == 690)
    assert js[k]["kind"] == "move"
    store.nudge_joint(conn, pr.id, revision_id=rev, joint=k, side="before", frames=4, actor="user")
    assert frames(conn, pr) == [(0, 300), (600, 700), (300, 594), (712, 1500)]
    # А на входе куска «вернуть начало» — это кадры 596–600 места, откуда взят: туда и встали бы — нельзя
    rev = current(conn, pr)
    js = store.joints(store.compile_revision(conn, pr.id, rev))
    entry = next(i for i, j in enumerate(js) if j["out_frame"] == 300)
    with pytest.raises(EdlError) as err:
        store.nudge_joint(conn, pr.id, revision_id=rev, joint=entry, side="after", frames=4, actor="user")
    assert err.value.code == "move_joint"


def test_move_place_survives_a_cut_that_glues_it_to_its_neighbour(conn, pr):
    """Вырез между местом и куском склеивает кусок с соседом по записи:
    перестановка всё равно на месте в ролике, а не «весь кусок вырезан»."""
    agent(conn, pr, [mv(pr.cam, 600, 700, 300)])
    agent(conn, pr, [rm(pr.cam, 300, 600)])
    assert frames(conn, pr) == [(0, 300), (600, 1500)]
    (m,) = store.get_revision(conn, pr.id, current(conn, pr))["moves"]
    assert (m["out_start_frame"], m["out_end_frame"]) == (300, 400)
    assert m["joints"] == {"in": 0, "out": None, "from": None}
