"""Правила проекта: версии, предложения, утверждение, бриф, защита, комментарии.

Это критерии готовности этапа 2 из плана.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from longform import store
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.ops import Op
from longform.probe import UnsupportedMedia
from longform.store import Conflict, Forbidden
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
def pr(conn, fx_plain, fx_second):
    p = store.create_project(conn, "Урок про агентов", "user")
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    scr = store.register_asset(conn, p["id"], str(fx_second.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    # Эти тесты проверяют ручное утверждение; режим «сразу» — в test_apply_mode.py
    store.set_agent_mode(conn, p["id"], "propose", "user")
    return SimpleNamespace(id=p["id"], cam=cam["id"], scr=scr["id"], rev=rev["id"])


def rm(asset, a, b, reason="technical_dead_air", **kw):
    return Op("remove", asset, us(a), us(b), reason, "долгая пауза", **kw)


def propose(conn, pr, ops, base=None, actor="agent"):
    return store.create_proposal(
        conn, pr.id, base_revision_id=base or current(conn, pr), pass_="cleanup",
        rationale="чистка пауз", policy_version="test", ops=ops, actor=actor)


def current(conn, pr):
    return store.get_project(conn, pr.id)["current_revision_id"]


def test_initial_revision_puts_files_one_after_another(conn, pr):
    rev = store.get_revision(conn, pr.id, pr.rev)
    assert [c["asset_id"] for c in rev["clips"]] == [pr.cam, pr.scr]
    assert rev["total_frames"] == 1500 + 500
    assert rev["state"] == "approved"


def test_database_itself_keeps_revisions_immutable(conn, pr):
    for sql in [
        "UPDATE revisions SET clips_json = '[]' WHERE id = ?",
        "DELETE FROM revisions WHERE id = ?",
        "UPDATE revisions SET state = 'discarded' WHERE id = ?",  # уже утверждена
    ]:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql, (pr.rev,))


def test_agent_proposes_user_approves(conn, pr):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200), rm(pr.scr, 10, 50)])
    assert prop["status"] == "open" and prop["created_by"] == "agent"
    # Пока не утверждено, текущая версия прежняя
    assert current(conn, pr) == pr.rev
    done = store.approve(conn, pr.id, prop["id"], actor="user")
    assert done["status"] == "approved"
    assert current(conn, pr) == prop["candidate_revision_id"]
    assert store.get_revision(conn, pr.id, current(conn, pr))["total_frames"] == 2000 - 100 - 40


def test_partial_approval_builds_new_revision(conn, pr):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 300, 400)])
    second = prop["ops"][1]["id"]
    done = store.approve(conn, pr.id, prop["id"], actor="user", exclude=[second])
    result = done["result_revision_id"]
    assert result != prop["candidate_revision_id"]
    assert store.get_revision(conn, pr.id, result)["total_frames"] == 2000 - 100
    assert [o["decision"] for o in done["ops"]] == ["accepted", "excluded"]
    cand = store.list_revisions(conn, pr.id)
    assert {r["id"]: r["state"] for r in cand}[prop["candidate_revision_id"]] == "discarded"


def test_risky_ops_need_one_by_one_review(conn, pr):
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160),
                         kind="hard", reason="тут оговорка про цену", actor="user")
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(700), src_end_us=us(710),
                         kind="soft", reason="число", actor="agent")
    prop = propose(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 650, 750), rm(pr.cam, 900, 950)])
    protected, soft, plain = prop["ops"]
    assert protected["risk_flags"] == ["protected"]
    assert soft["risk_flags"] == ["high_impact"]
    assert plain["risk_flags"] == []

    with pytest.raises(EdlError) as err:  # пакетом нельзя
        store.approve(conn, pr.id, prop["id"], actor="user")
    assert err.value.code == "needs_review"
    with pytest.raises(EdlError):  # защищённое без причины нельзя
        store.approve(conn, pr.id, prop["id"], actor="user", acknowledge=[protected["id"], soft["id"]])
    done = store.approve(conn, pr.id, prop["id"], actor="user", acknowledge=[protected["id"], soft["id"]],
                         overrides={protected["id"]: "оговорку повторяю дальше"})
    assert done["status"] == "approved"
    assert done["ops"][0]["override_reason"] == "оговорку повторяю дальше"


def test_low_risk_part_can_be_approved_in_bulk(conn, pr):
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160),
                         kind="hard", reason="оговорка", actor="user")
    prop = propose(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 900, 950)])
    done = store.approve(conn, pr.id, prop["id"], actor="user", exclude=[prop["ops"][0]["id"]])
    assert done["status"] == "approved"


def test_agent_cannot_decide(conn, pr, fx_plain):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    for call in [
        lambda: store.approve(conn, pr.id, prop["id"], actor="agent"),
        lambda: store.reject(conn, pr.id, prop["id"], "agent"),
        lambda: store.revert(conn, pr.id, pr.rev, "agent"),
        lambda: store.register_asset(conn, pr.id, str(fx_plain.path), "agent"),
        lambda: store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=0, src_end_us=us(10),
                                     kind="hard", reason="x", actor="agent"),
        lambda: store.confirm_brief(conn, pr.id, 1, "agent"),
    ]:
        with pytest.raises(Forbidden):
            call()


def test_stale_proposals(conn, pr):
    first = propose(conn, pr, [rm(pr.cam, 100, 200)])
    second = propose(conn, pr, [rm(pr.cam, 300, 400)])
    store.approve(conn, pr.id, first["id"], actor="user")
    # Второе сделано к старой версии: оно устарело и не перезапишет новую
    assert store.get_proposal(conn, pr.id, second["id"])["status"] == "stale"
    with pytest.raises(Conflict) as err:
        store.approve(conn, pr.id, second["id"], actor="user")
    assert err.value.code == "not_open"
    with pytest.raises(Conflict) as err:
        propose(conn, pr, [rm(pr.cam, 300, 400)], base=pr.rev)
    assert err.value.code == "stale_base"


def test_reject_keeps_current(conn, pr):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    store.reject(conn, pr.id, prop["id"], "user")
    assert current(conn, pr) == pr.rev
    assert store.get_proposal(conn, pr.id, prop["id"])["status"] == "rejected"


def test_revert_is_a_new_revision(conn, pr):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    store.approve(conn, pr.id, prop["id"], actor="user")
    back = store.revert(conn, pr.id, pr.rev, "user")
    assert back["id"] != pr.rev
    assert back["total_frames"] == 2000
    assert [r["number"] for r in store.list_revisions(conn, pr.id)] == [1, 2, 3]


def test_comments_survive_edits(conn, pr):
    # Два комментария в первой версии: внутри будущего выреза и после него
    inside = store.add_comment(conn, pr.id, text="тут запинка", actor="user", edited_frame=150)
    after = store.add_comment(conn, pr.id, text="хороший пример", actor="user", edited_frame=600)
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    store.approve(conn, pr.id, prop["id"], actor="user")
    listed = {c["id"]: c for c in store.list_comments(conn, pr.id, current(conn, pr))}
    # Вырезанное место не переносится на соседний кусок
    assert listed[inside["id"]]["in_revision"] is False
    assert listed[inside["id"]]["edited_frame"] is None
    # Место после выреза сдвинулось ровно на длину выреза
    assert listed[after["id"]]["edited_frame"] == 500
    # В старой версии всё на прежних местах
    old = {c["id"]: c for c in store.list_comments(conn, pr.id, pr.rev)}
    assert old[inside["id"]]["edited_frame"] == 150


def test_comment_on_second_file(conn, pr):
    c = store.add_comment(conn, pr.id, text="экран", actor="agent", edited_frame=1500 + 20)
    # Якорь — середина кадра 20 второго файла
    assert c["asset_id"] == pr.scr and us(20) < c["src_us"] < us(21)


def test_brief_draft_and_confirmation(conn, pr):
    store.save_brief(conn, pr.id, {"audience": "новички", "mode": "conservative_cleanup"}, "agent")
    brief = store.get_brief(conn, pr.id)
    assert brief["active"] is None and brief["draft"]["version"] == 1
    brief = store.confirm_brief(conn, pr.id, 1, "user")
    assert brief["active"]["version"] == 1 and brief["draft"] is None
    store.save_brief(conn, pr.id, {"audience": "практики", "mode": "conservative_cleanup"}, "user")
    assert store.get_brief(conn, pr.id)["active"]["data"]["audience"] == "практики"


def test_register_rejects_bad_files(conn, pr, fx_plain, fx_ntsc_offset, fx_vfr, tmp_path):
    with pytest.raises(EdlError, match="полный путь"):
        store.register_asset(conn, pr.id, "video.mp4", "user")
    with pytest.raises(EdlError, match="не найден"):
        store.register_asset(conn, pr.id, str(tmp_path / "нет.mp4"), "user")
    with pytest.raises(EdlError) as err:
        store.register_asset(conn, pr.id, str(fx_plain.path), "user")
    assert err.value.code == "duplicate_asset"
    with pytest.raises(EdlError) as err:
        store.register_asset(conn, pr.id, str(fx_ntsc_offset.path), "user")
    assert err.value.code == "format_mismatch"
    with pytest.raises(UnsupportedMedia, match="плавающая частота"):
        store.register_asset(conn, pr.id, str(fx_vfr.path), "user")


def test_symlink_is_stored_as_real_path(conn, fx_plain, tmp_path):
    link = tmp_path / "ссылка.mp4"
    link.symlink_to(fx_plain.path)
    p = store.create_project(conn, "ссылки", "user")
    a = store.register_asset(conn, p["id"], str(link), "user")
    assert a["path"] == str(fx_plain.path.resolve())


def test_each_op_knows_its_joint_in_the_candidate(conn, pr):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 300, 400)])
    assert [o["candidate_joint_frame"] for o in prop["ops"]] == [100, 200]


def test_nudge_joint_moves_exactly_and_is_approved(conn, pr):
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    store.approve(conn, pr.id, prop["id"], actor="user")
    rev = current(conn, pr)
    # Вернуть 10 кадров в начало куска после стыка
    store.nudge_joint(conn, pr.id, revision_id=rev, joint=0, side="after", frames=10, actor="user")
    clips = store.get_revision(conn, pr.id, current(conn, pr))["clips"]
    assert (clips[1]["src_start_us"], clips[0]["src_end_us"]) == (us(190), us(100))
    # Убрать ещё 5 кадров в конце куска перед стыком
    store.nudge_joint(conn, pr.id, revision_id=current(conn, pr), joint=0, side="before", frames=-5,
                      actor="user")
    assert store.get_revision(conn, pr.id, current(conn, pr))["clips"][0]["src_end_us"] == us(95)
    with pytest.raises(Forbidden):
        store.nudge_joint(conn, pr.id, revision_id=current(conn, pr), joint=0, side="after", frames=1,
                          actor="agent")


def test_comment_anchor_is_exact_at_ntsc(conn, fx_ntsc_offset):
    """При 29,97 начало кадра в микросекундах округлено и бывает раньше
    точного. Комментарий на кадре монтажа должен остаться на этом же кадре."""
    p = store.create_project(conn, "ntsc", "user")
    store.register_asset(conn, p["id"], str(fx_ntsc_offset.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    for frame in (1, 2, 3, 500, 1001):
        store.add_comment(conn, p["id"], text=f"кадр {frame}", actor="user", edited_frame=frame)
    listed = store.list_comments(conn, p["id"], rev["id"])
    assert [c["edited_frame"] for c in listed] == [1, 2, 3, 500, 1001]


def test_protection_added_after_proposal_still_blocks_bulk_approval(conn, pr):
    """Найдено независимой проверкой: риски считались только при создании
    предложения. Защита, поставленная позже, должна действовать."""
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    assert prop["ops"][0]["risk_flags"] == []
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160), kind="hard",
                         reason="оговорка", actor="user")
    assert store.get_proposal(conn, pr.id, prop["id"])["ops"][0]["risk_flags"] == ["protected"]
    with pytest.raises(EdlError) as err:
        store.approve(conn, pr.id, prop["id"], actor="user")
    assert err.value.code == "needs_review"


def test_markers_keep_numbers_and_survive_cuts(conn, pr):
    m1 = store.add_marker(conn, pr.id, actor="user", label="вступление", edited_frame=10)
    m2 = store.add_marker(conn, pr.id, actor="agent", label="часть 2", edited_frame=150)
    m3 = store.add_marker(conn, pr.id, actor="user", label="пример", edited_frame=600)
    assert [m["number"] for m in (m1, m2, m3)] == [1, 2, 3]
    prop = propose(conn, pr, [rm(pr.cam, 100, 200)])
    store.approve(conn, pr.id, prop["id"], actor="user")
    listed = {m["number"]: m for m in store.list_markers(conn, pr.id)}
    assert listed[2]["in_revision"] is False  # место вырезано — метка не переезжает
    assert listed[3]["edited_frame"] == 500  # после выреза 100 кадров
    with pytest.raises(Forbidden):
        store.remove_marker(conn, pr.id, m1["id"], "agent")
    store.remove_marker(conn, pr.id, m2["id"], "user")
    # Номера не переиспользуются
    assert store.add_marker(conn, pr.id, actor="user", edited_frame=20)["number"] == 4
