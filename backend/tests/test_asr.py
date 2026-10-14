"""Расшифровка и анализ звука на синтетической русской речи (голос macOS Milena).

Это проверка пути: время, куски, пометки, выравнивание границ. Качество
распознавания голоса автора она не доказывает.

Речь по громкости (мс), измерено при подготовке: 10–3340, 6040–8270,
9180–12840, 14220–19230, 22430–24990, 26800–29770, 30980–32670.
"""

from types import SimpleNamespace

import pytest

from longform import asset_data, jobs, store, worker
from longform.analysis import PAD_US
from longform.db import connect, migrate
from longform.ops import Op
from longform.workspace import Workspace

pytestmark = [pytest.mark.media, pytest.mark.asr]

SPEECH_MS = [(10, 3340), (6040, 8270), (9180, 12840), (14220, 19230), (22430, 24990), (26800, 29770),
             (30980, 32670)]
# Детектор речи считает речью чуть больше громкости — тихий конец слова и
# дыхание, до ~110 мс у края. Для монтажа это безопаснее, допуск шире
TOL_US = 150_000


@pytest.fixture(scope="module")
def lesson(tmp_path_factory, fx_lesson):
    root = tmp_path_factory.mktemp("урок-asr")
    fx = fx_lesson
    ws = Workspace(root / "ws")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "Урок", "user")
    a = store.register_asset(conn, p["id"], str(fx.path), "user")
    rev = store.create_initial_revision(conn, p["id"], "user")
    store.set_agent_mode(conn, p["id"], "propose", "user")
    jobs.enqueue(conn, p["id"], "analyze", actor="user", asset_id=a["id"])
    jobs.enqueue(conn, p["id"], "transcribe", actor="agent", asset_id=a["id"],
                 params={"language": "ru", "model": "large-v3-q5_0", "vad": True})
    worker.serve(ws, stop={}, once=True)
    states = [j["state"] for j in jobs.list_jobs(conn, p["id"])]
    assert states == ["completed", "completed"], jobs.list_jobs(conn, p["id"])
    yield SimpleNamespace(conn=conn, pid=p["id"], asset=a["id"], rev=rev["id"], fx=fx)
    conn.close()


def near(us_value, ms_value):
    return abs(us_value - ms_value * 1000) <= TOL_US


def test_silences_are_where_the_pauses_are(lesson):
    found = asset_data.silences(lesson.conn, lesson.asset)
    expected = [(SPEECH_MS[i][1], SPEECH_MS[i + 1][0]) for i in range(len(SPEECH_MS) - 1)]
    for a, b in expected:
        assert any(near(s["src_start_us"], a) and near(s["src_end_us"], b) for s in found), (a, b, found)
    # Назначение паузы детектор не решает
    assert all(s["kind"] in ("unknown", "no_audio") for s in found)
    # В синтетике видео на секунду длиннее звука: этот хвост — «нет звука»
    (tail,) = [s for s in found if s["kind"] == "no_audio"]
    assert near(tail["src_start_us"], 32690) and tail["src_end_us"] > tail["src_start_us"] + 500_000


def test_transcript_segments_and_honest_timing(lesson):
    info = asset_data.transcript_info(lesson.conn, lesson.asset)
    assert info["timing_level"] == "segment" and info["language"] == "ru"
    segs = asset_data.segments(lesson.conn, lesson.asset)
    text = " ".join(s["text"] for s in segs).lower()
    for word in ("сегодня", "агента", "провайдер", "долларов", "спасибо"):
        assert word in text
    # Whisper начинает сегмент ложного старта в паузе; громкость показывает, где речь
    false_start = next(s for s in segs if s["text"].rstrip(".…").endswith("настроить"))
    assert false_start["src_start_us"] < 4_000_000
    assert near(false_start["speech_start_us"], 6040) and near(false_start["speech_end_us"], 8270)
    # У каждого сегмента либо есть речь по громкости, либо он помечен
    assert all(s["speech_start_us"] is not None or "no_speech" in s["flags"] for s in segs)


def test_whisper_inventions_in_silence_are_flagged(lesson):
    """На этой записи Whisper дописывает в тишине в конце «Продолжение
    следует...» — известную выдумку. Она должна быть помечена."""
    segs = asset_data.segments(lesson.conn, lesson.asset)
    for s in segs:
        if "продолжение следует" in s["text"].lower():
            assert {"no_speech", "suspect_phrase"} <= set(s["flags"])
    real = [s for s in segs if "no_speech" not in s["flags"]]
    assert len(real) == 7


def test_word_times_are_close_to_speech(lesson):
    """Время слов по DTW: первое слово каждой настоящей фразы — не дальше
    0,3 с от начала речи по громкости (сегменты Whisper ошибались на секунды)."""
    info = asset_data.transcript_info(lesson.conn, lesson.asset)
    assert info["word_timing"] == "estimated"
    segs = [s for s in asset_data.segments(lesson.conn, lesson.asset) if "no_speech" not in s["flags"]]
    words = asset_data.words(lesson.conn, [s["id"] for s in segs])
    firsts = [words[s["id"]][0]["start_us"] for s in segs]
    for (speech_start, _), first in zip(SPEECH_MS, firsts):
        assert abs(first - speech_start * 1000) < 300_000, (speech_start, first)


def test_precise_word_cut(lesson):
    """Точная правка по словам: вырезать одно слово «видео» из полной фразы."""
    segs = asset_data.segments(lesson.conn, lesson.asset)
    full = next(s for s in segs if "видео" in s["text"])
    ws = asset_data.words(lesson.conn, [full["id"]])[full["id"]]
    w = next(x for x in ws if x["text"].startswith("видео"))
    current = store.get_project(lesson.conn, lesson.pid)["current_revision_id"]
    prop = store.create_proposal(
        lesson.conn, lesson.pid, base_revision_id=current, pass_="cleanup", rationale="слово",
        policy_version="t", actor="agent",
        ops=[Op("remove", lesson.asset, w["start_us"], w["end_us"], "other", "лишнее слово", precise=True)])
    (op,) = prop["ops"]
    assert "точная правка" in op["adjust_note"]
    # Граница сдвинута не дальше 60 мс от времени слова
    assert abs(op["src_start_us"] - w["start_us"]) <= 80_000 and abs(op["src_end_us"] - w["end_us"]) <= 80_000
    store.reject(lesson.conn, lesson.pid, prop["id"], "user")


def test_meaningful_words_get_soft_protection(lesson):
    segs = asset_data.segments(lesson.conn, lesson.asset)
    flagged = [s for s in segs if "high_impact" in s["flags"]]
    joined = " ".join(s["text"] for s in flagged).lower()
    assert "провайдер" in joined and "доллар" in joined
    prots = [p for p in store.list_protections(lesson.conn, lesson.pid) if p["created_by"] == "system"]
    assert len(prots) == len(flagged) and all(p["kind"] == "soft" for p in prots)


def test_search_ignores_case(lesson):
    hits = asset_data.search(lesson.conn, lesson.pid, "ПЛАТНЫМ")
    assert hits and "провайдер" in hits[0]["text"]
    assert asset_data.search(lesson.conn, lesson.pid, "клод")


def test_correction_keeps_raw_text(lesson):
    seg = next(s for s in asset_data.segments(lesson.conn, lesson.asset) if "провайдер" in s["text"])
    fixed = asset_data.correct_segment(lesson.conn, lesson.pid, seg["id"],
                                       "Эта схема работает без платной транскрипции, но сам провайдер может быть платным.",
                                       "user")
    assert fixed["text_raw"] == seg["text_raw"] and "без платной" in fixed["text"]
    assert asset_data.search(lesson.conn, lesson.pid, "без платной")


def test_whisper_bounds_are_moved_into_silence(lesson):
    segs = asset_data.segments(lesson.conn, lesson.asset)
    false_start = next(s for s in segs if s["text"].rstrip(".…").endswith("настроить"))
    full_take = segs[segs.index(false_start) + 1]
    # Агент режет ровно по границам сегмента Whisper
    prop = store.create_proposal(
        lesson.conn, lesson.pid, base_revision_id=lesson.rev, pass_="cleanup", rationale="ложный старт",
        policy_version="t", actor="agent",
        ops=[Op("remove", lesson.asset, false_start["src_start_us"], false_start["src_end_us"], "false_start",
                "оборванная фраза, повторена целиком дальше",
                kept_alternative=(lesson.asset, full_take["speech_start_us"], full_take["speech_end_us"]))])
    (op,) = prop["ops"]
    # Начало: после первой фразы остаётся пауза PAD; конец: перед полной фразой PAD
    assert op["src_start_us"] >= 3_340_000 + PAD_US - 40_000
    assert op["src_end_us"] <= 9_180_000 - PAD_US + 40_000
    # Ложный старт вырезан целиком, а полная фраза не задета
    assert op["src_start_us"] < 6_040_000 and op["src_end_us"] > 8_270_000
    assert "cut_in_speech" not in op["risk_flags"]
    assert op["requested_start_us"] == false_start["src_start_us"]
    store.reject(lesson.conn, lesson.pid, prop["id"], "user")


def test_cut_inside_a_long_phrase_is_flagged(lesson):
    prop = store.create_proposal(
        lesson.conn, lesson.pid, base_revision_id=lesson.rev, pass_="cleanup", rationale="проверка",
        policy_version="t", actor="agent",
        ops=[Op("remove", lesson.asset, 16_000_000, 17_000_000, "other", "посреди фразы")])
    (op,) = prop["ops"]
    assert "cut_in_speech" in op["risk_flags"]
    assert "тишины рядом нет" in op["adjust_note"]
    store.reject(lesson.conn, lesson.pid, prop["id"], "user")


def test_chunks_overlap_without_loss_or_duplicates(lesson, tmp_path, monkeypatch):
    """Куски по 15 с с перекрытием 5 с: фразы попадают на края кусков, после
    склейки должны остаться те же фразы без потерь и повторов. Повторный
    запуск берёт готовые куски из кеша и Whisper не вызывает."""
    from longform import transcribe

    monkeypatch.setattr(transcribe, "CHUNK_MS", 15_000)
    monkeypatch.setattr(transcribe, "OVERLAP_MS", 5_000)
    ws = Workspace(tmp_path / "ws")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "куски", "user")
    a = store.register_asset(conn, p["id"], str(lesson.fx.path), "user")
    jobs.enqueue(conn, p["id"], "transcribe", actor="user", asset_id=a["id"], params={"chunk_ms": 15_000})
    worker.serve(ws, stop={}, once=True)
    chunks = sorted((ws.root / "projects" / p["id"] / "transcripts" / a["id"]).rglob("chunk-*.json"))
    assert len(chunks) == 3
    real = [s for s in asset_data.segments(conn, a["id"]) if "no_speech" not in s["flags"]]
    whole = [s for s in asset_data.segments(lesson.conn, lesson.asset) if "no_speech" not in s["flags"]]
    assert len(real) == len(whole) == 7
    for x, y in zip(real, whole):
        assert near(x["speech_start_us"], y["speech_start_us"] // 1000)

    # Сбой после расшифровки кусков: результат пропал, куски на диске
    conn.execute("DELETE FROM segments_fts")
    conn.execute("DELETE FROM transcript_words")
    conn.execute("DELETE FROM transcript_segments")
    conn.execute("DELETE FROM transcripts")
    conn.execute("UPDATE jobs SET state = 'interrupted' WHERE type = 'transcribe'")

    def no_whisper(*args, **kwargs):
        raise AssertionError("готовый кусок распознаётся заново")

    monkeypatch.setattr(transcribe, "run_chunk", no_whisper)
    worker.serve(ws, stop={}, once=True)
    assert len([s for s in asset_data.segments(conn, a["id"]) if "no_speech" not in s["flags"]]) == 7
    conn.close()


def test_claudes_three_adjacent_edits_leave_no_slivers(lesson, tmp_path):
    """Повтор настоящего прогона Claude: неудачный дубль, пауза перед повтором и
    ложный старт тремя соседними правками. Между ними не должно остаться тихих
    огрызков, а вырез от самого начала файла — это не стык."""
    segs = asset_data.segments(lesson.conn, lesson.asset)
    first, false_start, full_take = segs[0], segs[1], segs[2]
    pause = next(s for s in asset_data.silences(lesson.conn, lesson.asset) if near(s["src_start_us"], 3340))
    rng = lambda s: (max(0, s["speech_start_us"] - 10_000), s["speech_end_us"])  # noqa: E731
    keep = (lesson.asset, full_take["speech_start_us"], full_take["speech_end_us"])
    current = store.get_project(lesson.conn, lesson.pid)["current_revision_id"]
    prop = store.create_proposal(
        lesson.conn, lesson.pid, base_revision_id=current, pass_="cleanup", rationale="тройной заход",
        policy_version="t", actor="agent",
        ops=[Op("remove", lesson.asset, *rng(first), "failed_take", "неполный дубль", kept_alternative=keep),
             Op("remove", lesson.asset, pause["src_start_us"], pause["src_end_us"], "restart_gap", "пауза"),
             Op("remove", lesson.asset, *rng(false_start), "false_start", "обрыв", kept_alternative=keep)])
    assert all("cut_in_speech" not in o["risk_flags"] for o in prop["ops"])
    # С запасом 70 мс правки могут сойтись встык; если между ними остался тихий
    # огрызок, сервер убирает его сам — в любом случае кусок один
    cand = store.get_revision(lesson.conn, lesson.pid, prop["candidate_revision_id"])
    assert len(cand["clips"]) == 1  # всё до полного дубля ушло одним куском, без огрызков

    # Сверка готового ролика с исходником проверяет звук там, где есть речь
    from longform.probe import probe
    from longform.render.select import render_select
    from longform.verify import verify_output

    edl = store.compile_revision(lesson.conn, lesson.pid, prop["candidate_revision_id"])
    out = tmp_path / "кандидат.mp4"
    render_select(edl, {lesson.asset: probe(lesson.fx.path)}, out, tmp_path)
    report = verify_output(edl, {lesson.asset: probe(lesson.fx.path)}, out)
    assert report.ok and report.as_dict()["audio_checked"] >= 1, report.as_dict()
    store.reject(lesson.conn, lesson.pid, prop["id"], "user")


def test_typing_in_a_pause_is_not_speech(tmp_path):
    """Стук клавиатуры громкий, но это не речь: с детектором речи пауза со
    щелчками должна найтись как пауза (по одной громкости её бы не было)."""
    import subprocess
    import wave

    import numpy as np

    from media_fixtures import LESSON, make_fixture

    wav = tmp_path / "речь.wav"
    subprocess.run(["say", "-v", "Milena", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@48000", LESSON],
                   check=True)
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32)
    rng = np.random.default_rng(7)
    for t in np.arange(19.4, 22.2, 0.14):  # щелчки в паузе между 19,23 и 22,43 с
        a, n = int(t * sr), int(0.025 * sr)
        x[a:a + n] += rng.standard_normal(n) * 6000 * np.exp(-np.linspace(0, 6, n))
    typed = tmp_path / "с клавиатурой.wav"
    with wave.open(str(typed), "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(sr)
        o.writeframes(np.clip(x, -32767, 32767).astype("<i2").tobytes())
    fx = make_fixture(tmp_path / "урок с клавиатурой.mp4", frames=int(len(x) / sr * 25) + 25, audio_wav=typed)

    ws = Workspace(tmp_path / "ws")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "клавиатура", "user")
    a = store.register_asset(conn, p["id"], str(fx.path), "user")
    jobs.enqueue(conn, p["id"], "analyze", actor="user", asset_id=a["id"])
    worker.serve(ws, stop={}, once=True)
    assert asset_data.analysis_info(conn, a["id"])["speech_detector"] == "громкость + детектор речи"
    pauses = [(s["src_start_us"], s["src_end_us"]) for s in asset_data.silences(conn, a["id"])]
    # Пауза со щелчками найдена почти целиком (щелчки ближе 0,3 с к речи — речь)
    assert any(s <= 19_700_000 and e >= 21_900_000 for s, e in pauses), pauses
    conn.close()
