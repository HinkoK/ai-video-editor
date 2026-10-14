"""Субтитры и главы для YouTube по версии монтажа."""

from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from longform import asset_data, captions, store
from longform.api import Settings, create_app
from longform.captions import Cue, Token, build_cues, chapter_list, edited_tokens, segment_tokens, to_srt
from longform.db import connect, migrate
from longform.edl import Clip, OutputProfile, SourceInfo, compile_edl
from longform.ops import Op
from longform.timebase import frame_to_us
from longform.transcribe import Segment, Word

S = 1_000_000


def edl_two_pieces():
    """Файл 25 к/с, 40 с; в версии кадры [0, 100) и [200, 300): вырезано 4–8 с."""
    src = SourceInfo("a", Fraction(25), 1000, 48000, 1, Fraction(0))
    clips = [Clip("c1", "a", 0, frame_to_us(100, 25)), Clip("c2", "a", frame_to_us(200, 25), frame_to_us(300, 25))]
    return compile_edl(clips, {"a": src}, OutputProfile(Fraction(25), 48000))


def test_cut_words_disappear_and_kept_ones_move_to_edited_time():
    toks = [Token("было", "a", 1 * S, 1 * S + 400_000), Token("вырезано", "a", 5 * S, 5 * S + 500_000),
            Token("стало", "a", 9 * S, 9 * S + 400_000)]
    out = edited_tokens(edl_two_pieces(), toks)
    assert [(t.text, t.start_us) for t in out] == [("было", 1 * S), ("стало", 5 * S)]


def test_word_straddling_a_cut_follows_its_middle():
    # Слово 3,8–4,4 с: середина 4,1 с — уже в вырезанном, слово уходит
    out = edited_tokens(edl_two_pieces(), [Token("край", "a", 3_800_000, 4_400_000)])
    assert out == []
    # 3,7–4,1 с: середина 3,9 с — остаётся, конец обрезан по куску
    out = edited_tokens(edl_two_pieces(), [Token("край", "a", 3_700_000, 4_100_000)])
    assert [(t.start_us, t.end_us) for t in out] == [(3_700_000, 4_000_000)]


def test_hallucinations_are_skipped_until_author_fixes_them():
    seg = {"asset_id": "a", "flags": ["no_speech"], "text_corrected": None, "text_raw": "Продолжение следует...",
           "src_start_us": 0, "src_end_us": 2 * S, "speech_start_us": None, "speech_end_us": None}
    assert segment_tokens(seg, []) == []
    fixed = {**seg, "text_corrected": "Спасибо, что досмотрели"}
    toks = segment_tokens(fixed, [])
    assert [t.text for t in toks] == ["Спасибо,", "что", "досмотрели"]
    # Исправленный текст ложится на время фразы целиком, по длине слов
    assert toks[0].start_us == 0 and toks[-1].end_us == 2 * S
    assert toks[0].end_us == toks[1].start_us


def test_uncorrected_phrase_uses_word_times():
    seg = {"asset_id": "a", "flags": [], "text_corrected": None, "text_raw": "раз два",
           "src_start_us": 0, "src_end_us": S}
    words = [{"text": " раз", "start_us": 100_000, "end_us": 300_000}, {"text": " два", "start_us": 400_000,
                                                                         "end_us": 600_000}]
    assert segment_tokens(seg, words) == [Token("раз", "a", 100_000, 300_000), Token("два", "a", 400_000, 600_000)]


def words_at(text, start=0, step=300_000):
    return [Token(w, "a", start + i * step, start + i * step + 250_000) for i, w in enumerate(text.split())]


def test_cues_respect_lines_length_and_pauses():
    long = words_at("Сегодня покажу как я монтирую длинные видео вместе с Claude и что из этого получается "
                    "когда запись идёт целый час без перерыва")
    cues = build_cues(long)
    assert len(cues) >= 2
    for c in cues:
        assert 1 <= len(c.lines) <= 2 and all(len(line) <= 42 for line in c.lines)
        assert c.end_us - c.start_us <= captions.MAX_CUE_US
    # Пауза длиннее 0,7 с начинает новый титр
    gap = words_at("первая фраза") + words_at("вторая фраза", start=3 * S)
    assert [c.lines for c in build_cues(gap)] == [("первая фраза",), ("вторая фраза",)]
    # Конец предложения тоже, если титр уже не крошечный
    sent = words_at("Это первое предложение целиком. А это второе")
    assert [" ".join(c.lines) for c in build_cues(sent)] == ["Это первое предложение целиком.", "А это второе"]


def test_short_cue_is_stretched_but_never_overlaps_the_next():
    # Пауза 0,8 с — два титра; первый растянут до секунды, но с зазором до второго
    cues = build_cues([Token("Да.", "a", 0, 200_000), Token("Нет", "a", 1_000_000, 1_200_000)])
    assert cues[0].end_us == 1_000_000 - captions.MIN_GAP_US
    assert cues[1].end_us == 1_000_000 + captions.MIN_CUE_US


def test_srt_format():
    text = to_srt([Cue(1_500_000, 3_250_000, ("Привет",)), Cue(3_725_000_000, 3_726_000_000, ("две", "строки"))])
    assert text == ("1\n00:00:01,500 --> 00:00:03,250\nПривет\n\n"
                    "2\n01:02:05,000 --> 01:02:06,000\nдве\nстроки\n")


def mark(n, title, frame, inside=True):
    return {"kind": "chapter", "number": n, "label": title, "edited_frame": frame, "in_revision": inside}


def test_chapters_follow_youtube_rules():
    fps = Fraction(25)
    ok = chapter_list([mark(1, "Вступление", 0), mark(2, "Файлы", 25 * 30), mark(3, "Итог", 25 * 90)],
                      120 * S, fps)
    assert ok["ok"] and ok["text"] == "00:00 Вступление\n00:30 Файлы\n01:30 Итог"
    # Первая глава не с нуля — сдвигается на 00:00 с пояснением
    shifted = chapter_list([mark(1, "Начало", 25 * 5), mark(2, "Б", 25 * 30), mark(3, "В", 25 * 60)], 120 * S, fps)
    assert shifted["items"][0]["start_us"] == 0 and shifted["notes"] and shifted["ok"]
    bad = chapter_list([mark(1, "А", 0), mark(2, "Б", 25 * 5), mark(4, "Вырезана", 25 * 50, inside=False),
                        {"kind": "mark", "number": 5, "label": "", "edited_frame": 10, "in_revision": True}],
                       120 * S, fps)
    assert not bad["ok"]
    assert any("не меньше 3" in p for p in bad["problems"])
    assert any("«А» короче 10 секунд" in p for p in bad["problems"])
    assert any("«Вырезана»" in p for p in bad["problems"])
    assert [it["title"] for it in bad["items"]] == ["А", "Б"]  # обычная метка — не глава
    # Ролик длиннее часа — часы в отметках
    hour = chapter_list([mark(1, "А", 0), mark(2, "Б", 25 * 1800), mark(3, "В", 25 * 3700)], 4000 * S, fps)
    assert hour["text"].splitlines()[-1] == "1:01:40 В"


@pytest.mark.media
def test_api_captions_and_chapters_for_a_cut_version(tmp_path, fx_plain):
    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "субтитры", "user")
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    first = store.create_initial_revision(conn, p["id"], "user")
    segs = [Segment(1000, 3000, " Первая фраза остаётся.", 0.9,
                    (Word(1000, 1800, " Первая", 0.9), Word(1800, 2400, " фраза", 0.9), Word(2400, 3000, " остаётся.", 0.9))),
            Segment(5000, 7000, " Эта уходит.", 0.9, (Word(5000, 6000, " Эта", 0.9), Word(6000, 7000, " уходит.", 0.9))),
            Segment(9000, 11000, " А эта остаётся.", 0.9,
                    (Word(9000, 9500, " А", 0.9), Word(9500, 10000, " эта", 0.9), Word(10000, 11000, " остаётся.", 0.9)))]
    asset_data.save_transcript(conn, cam["id"], p["id"], params_hash="t", model="test", language="ru",
                               segments=segs, audio_offset_us=0)
    store.create_proposal(conn, p["id"], base_revision_id=first["id"], pass_="cleanup", rationale="r",
                          policy_version="t", actor="agent",
                          ops=[Op("remove", cam["id"], 4_500_000, 7_500_000, "failed_take", "дубль")], align=False)
    rid = store.get_project(conn, p["id"])["current_revision_id"]
    store.add_marker(conn, p["id"], actor="agent", asset_id=cam["id"], src_us=0, label="Начало", kind="chapter")
    store.add_marker(conn, p["id"], actor="user", asset_id=cam["id"], src_us=20 * S, label="Середина", kind="chapter")
    store.add_marker(conn, p["id"], actor="user", asset_id=cam["id"], src_us=40 * S, label="Конец", kind="chapter")
    with pytest.raises(Exception, match="название"):
        store.add_marker(conn, p["id"], actor="user", asset_id=cam["id"], src_us=0, kind="chapter")

    client = TestClient(create_app(Settings(
        db_path=tmp_path / "editor.db", user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset())))
    auth = {"Authorization": "Bearer u"}
    srt = client.get(f"/api/projects/{p['id']}/revisions/{rid}/captions.srt", headers=auth)
    assert srt.status_code == 200 and "attachment" in srt.headers["content-disposition"]
    assert "уходит" not in srt.text
    assert "Первая фраза остаётся." in srt.text and "А эта остаётся." in srt.text
    # «А эта» стояла на 9 с; вырезано 3 с — в версии она на 6 с
    assert "00:00:06,000 -->" in srt.text
    ch = client.get(f"/api/projects/{p['id']}/revisions/{rid}/chapters", headers=auth).json()
    assert ch["ok"] and ch["text"] == "00:00 Начало\n00:17 Середина\n00:37 Конец"
    conn.close()


@pytest.mark.media
def test_refresh_recomputes_speech_and_flags_but_keeps_text(tmp_path, fx_plain):
    """Пересчёт пометок по готовой расшифровке: сплошная речь через две фразы
    больше не «нет речи», исправление автора остаётся."""
    import json

    import numpy as np

    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "пересчёт", "user")
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    # Громкость по 10 мс: тишина 0,5 с, речь 10 с подряд, тишина
    db = np.full(6000, -80.0)
    db[50:1050] = -20.0
    asset_data.save_analysis(conn, cam["id"], "a", db, tmp_path / "db.npy", audio_offset_us=0)
    segs = [Segment(3000, 5000, " Первая часть мысли,", 0.9), Segment(5000, 7000, " вторая её часть.", 0.9)]
    asset_data.save_transcript(conn, cam["id"], p["id"], params_hash="t", model="m", language="ru",
                               segments=segs, audio_offset_us=0)
    rows = conn.execute("SELECT id FROM transcript_segments ORDER BY idx").fetchall()
    # Как было до исправления: обе фразы «без речи»
    conn.execute("UPDATE transcript_segments SET flags_json = '[\"no_speech\"]', speech_start_us = NULL, "
                 "speech_end_us = NULL")
    conn.commit()
    asset_data.correct_segment(conn, p["id"], rows[1]["id"], "вторая её половина.", "user")
    out = asset_data.refresh_transcript(conn, cam["id"], p["id"])
    assert out == {"segments": 2, "changed": 2}
    after = conn.execute("SELECT * FROM transcript_segments ORDER BY idx").fetchall()
    assert [json.loads(r["flags_json"]) for r in after] == [[], []]
    assert (after[0]["speech_start_us"], after[0]["speech_end_us"]) == (3 * S, 5 * S)
    assert after[1]["text_corrected"] == "вторая её половина."
    conn.close()
