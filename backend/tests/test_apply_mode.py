"""Режим «правки Claude применяются сразу, автор проверяет после».

Исключение: правки, задевающие жёсткую защиту,
не применяются и ждут его решения.
"""

from types import SimpleNamespace

import pytest

from longform import store
from longform.db import connect, migrate
from longform.edl import EdlError
from longform.ops import Op
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
def pr(conn, fx_plain):
    p = store.create_project(conn, "сразу", "user")
    assert p["agent_mode"] == "apply"  # по умолчанию — как решил автор
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    return SimpleNamespace(id=p["id"], cam=cam["id"], rev=rev["id"])


def current(conn, pr):
    return store.get_project(conn, pr.id)["current_revision_id"]


def rm(asset, a, b, reason="technical_dead_air"):
    return Op("remove", asset, us(a), us(b), reason, "пауза")


def agent(conn, pr, ops):
    return store.create_proposal(conn, pr.id, base_revision_id=current(conn, pr), pass_="cleanup",
                                 rationale="черновой монтаж", policy_version="t", ops=ops, actor="agent")


def frames_of(conn, pr):
    return [(c["src_start_us"], c["src_end_us"]) for c in store.get_revision(conn, pr.id, current(conn, pr))["clips"]]


def test_agent_edits_become_current_immediately(conn, pr):
    prop = agent(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 300, 400, "other")])
    assert prop["status"] == "approved" and prop["auto_applied"] == 1
    assert current(conn, pr) == prop["result_revision_id"]
    # Рискованная тоже применена, но ждёт проверки, как и остальные
    assert [o["decision"] for o in prop["ops"]] == ["accepted", "accepted"]
    assert [o["review"] for o in prop["ops"]] == ["pending", "pending"]
    assert prop["ops"][1]["risk_flags"] == ["reason_other"]
    # Для проверки после: стык правки в текущей версии
    assert prop["ops"][0]["current_joint_frame"] == 100
    assert store.agent_review_counts(conn, pr.id, current(conn, pr)) == {"unreviewed": 2, "held": 0}
    # Список предложений: счёт для значка у раздела «Проверка»
    [row] = store.list_proposals(conn, pr.id)
    assert (row["ops_count"], row["unreviewed"], row["held"]) == (2, 2, 0)


def test_hard_protection_is_held_for_author(conn, pr):
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160), kind="hard",
                         reason="оговорка", actor="user")
    prop = agent(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 300, 400)])
    held, applied = prop["ops"]
    assert held["review"] == "held" and held["decision"] == "excluded"
    assert applied["decision"] == "accepted"
    assert (us(0), us(300)) in frames_of(conn, pr)  # защищённое на месте, вырезано только 300–400
    # Применить отложенную можно только с причиной
    with pytest.raises(EdlError):
        store.review_op(conn, pr.id, prop["id"], held["id"], action="apply", actor="user")
    store.review_op(conn, pr.id, prop["id"], held["id"], action="apply", actor="user", reason="оговорка есть дальше")
    assert (us(0), us(100)) in frames_of(conn, pr)


def test_all_held_leaves_proposal_open(conn, pr):
    store.add_protection(conn, pr.id, asset_id=pr.cam, src_start_us=us(150), src_end_us=us(160), kind="hard",
                         reason="оговорка", actor="user")
    prop = agent(conn, pr, [rm(pr.cam, 100, 200)])
    assert prop["status"] == "open" and prop["ops"][0]["review"] == "held"
    assert current(conn, pr) == pr.rev


def test_keep_and_revert(conn, pr):
    prop = agent(conn, pr, [rm(pr.cam, 100, 200), rm(pr.cam, 300, 400)])
    keep, back = prop["ops"]
    store.review_op(conn, pr.id, prop["id"], keep["id"], action="keep", actor="user")
    done = store.review_op(conn, pr.id, prop["id"], back["id"], action="revert", actor="user")
    assert [o["review"] for o in done["ops"]] == ["kept", "reverted"]
    # Вернулось ровно вырезанное, первая правка на месте
    assert frames_of(conn, pr) == [(us(0), us(100)), (us(200), us(1500))]
    with pytest.raises(Conflict):
        store.review_op(conn, pr.id, prop["id"], back["id"], action="revert", actor="user")
    assert store.agent_review_counts(conn, pr.id, current(conn, pr)) == {"unreviewed": 0, "held": 0}


def test_agent_cannot_review_or_change_mode(conn, pr):
    prop = agent(conn, pr, [rm(pr.cam, 100, 200)])
    with pytest.raises(Forbidden):
        store.review_op(conn, pr.id, prop["id"], prop["ops"][0]["id"], action="revert", actor="agent")
    with pytest.raises(Forbidden):
        store.set_agent_mode(conn, pr.id, "apply", "agent")


def test_next_agent_proposal_builds_on_the_applied_one(conn, pr):
    first = agent(conn, pr, [rm(pr.cam, 100, 200)])
    second = agent(conn, pr, [rm(pr.cam, 300, 400)])
    assert second["base_revision_id"] == first["result_revision_id"]
    assert frames_of(conn, pr) == [(us(0), us(100)), (us(200), us(300)), (us(400), us(1500))]


def test_propose_mode_keeps_old_behaviour(conn, pr):
    store.set_agent_mode(conn, pr.id, "propose", "user")
    prop = agent(conn, pr, [rm(pr.cam, 100, 200)])
    assert prop["status"] == "open" and current(conn, pr) == pr.rev
