"""Оценка предложений по разметке: потерянное обязательное — блокер."""

import sys
from pathlib import Path

import pytest

from longform import store
from longform.db import connect, migrate
from longform.ops import Op
from longform.timebase import frame_to_us

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from eval_agent import evaluate, parse_time  # noqa: E402

pytestmark = pytest.mark.media


def us(frame):
    return frame_to_us(frame, 25)


def test_parse_time():
    assert parse_time("01:02.5") == 62_500_000
    assert parse_time("1:00:00") == 3_600_000_000
    assert parse_time("7,25") == 7_250_000


def test_lost_required_is_a_blocker(tmp_path, fx_plain):
    conn = connect(tmp_path / "e.db")
    migrate(conn)
    p = store.create_project(conn, "оценка", "user")
    a = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    store.set_agent_mode(conn, p["id"], "propose", "user")
    prop = store.create_proposal(conn, p["id"], base_revision_id=rev["id"], pass_="cleanup", rationale="x",
                                 policy_version="t", actor="agent",
                                 ops=[Op("remove", a["id"], us(100), us(200), "failed_take", "дубль"),
                                      Op("remove", a["id"], us(400), us(450), "technical_dead_air", "пауза")])
    labels = {
        "required": [{"from": "00:04.5", "to": "00:05.5", "what": "оговорка"}],  # кадры 112–137: вырезаны
        "good_takes": [{"from": "00:20", "to": "00:30", "what": "хороший дубль"}],
        "bad_takes": [{"from": "00:04", "to": "00:08", "what": "плохой дубль"},
                      {"from": "00:30", "to": "00:32", "what": "ещё плохой"}],
    }
    report = evaluate(conn, p["id"], labels, [prop["id"]])
    assert report["blocker"] is True
    assert report["as_proposed"]["lost_required"] == ["оговорка"]
    assert report["as_proposed"]["bad_takes_caught"] == "1 из 2"
    assert report["as_proposed"]["missed_bad_takes"] == ["ещё плохой"]
    assert report["after_review"] == "автор ещё не решал"
    # Автор исключил правку, задевшую оговорку: после проверки блокера нет
    store.approve(conn, p["id"], prop["id"], actor="user", exclude=[prop["ops"][0]["id"]])
    report = evaluate(conn, p["id"], labels, [prop["id"]])
    assert report["after_review"]["lost_required"] == []
    assert report["excluded_by_review"][0]["reason"] == "failed_take"
