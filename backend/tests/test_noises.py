"""Кашель и «кхм»: находятся громкие звуки без слов, настоящие слова,
пропущенные расшифровкой, отсеиваются перечиткой, вырез — со всей тишиной
вокруг до запаса у речи."""

import numpy as np

from longform.analysis import PAD_US, Loudness
from longform.noises import (MARGIN_US, REACH_US, Noise, candidates, cut_range, detect, glued_candidates, is_noise,
                             kept_ranges, merge, save, scanned, subtract, to_scan)

S = 1_000_000


def track(spans: list[tuple[float, float, float]], seconds: float = 6.0) -> Loudness:
    """Громкость по 10 мс: тишина −80 дБ, отрезки — (начало, конец, дБ)."""
    db = np.full(int(seconds * 100), -80.0)
    for a, b, level in spans:
        db[round(a * 100):round(b * 100)] = level
    return Loudness(db, 0, -80.0, -30.0, -60.0)


def test_cough_found_word_and_click_left():
    loud = track([(0.5, 1.5, -30), (2.0, 2.3, -24), (2.7, 3.5, -30), (3.8, 4.1, -30), (4.6, 4.75, -55),
                  (5.5, 6.0, -30)])
    mids = [int(t * S) for t in (0.7, 1.0, 1.3, 2.9, 3.2, 5.7)]
    # Щелчок тише речи на 25 дБ — не кандидат; «навык» без слова в расшифровке — кандидат
    assert [(a, b) for a, b, _ in candidates(loud, mids, 0, 6 * S)] == [(2_000_000, 2_300_000), (3_800_000, 4_100_000)]
    heard = {2_000_000: "Кхм.", 3_800_000: "навык"}
    found = detect(loud, mids, 0, 6 * S, lambda a, b: heard[a])
    assert [(n.start_us, n.end_us, n.heard) for n in found] == [(2_000_000, 2_300_000, "Кхм.")]
    # Вырез: от конца речи до начала следующей, с запасом у речи с обеих сторон
    assert (found[0].cut_start_us, found[0].cut_end_us) == (1_500_000 + PAD_US, 2_700_000 - PAD_US)


def test_cough_glued_to_phrase_found_by_dips_not_silence():
    """«Кхм» вплотную к фразе: вокруг не тишина, а дыхание на 25–30 дБ тише
    речи (выше порога тишины −60). Первый проход видит один длинный отрезок,
    второй — всплеск между провалами."""
    loud = track([(0.5, 1.5, -30), (1.5, 2.0, -58), (2.0, 2.2, -24), (2.2, 2.4, -55), (2.4, 3.4, -30),
                  (3.4, 3.6, -57), (3.6, 3.8, -38), (3.8, 4.0, -57), (4.0, 5.0, -30)])
    mids = [int(t * S) for t in (0.7, 1.0, 1.3, 2.6, 2.9, 3.2, 4.3, 4.7)]
    assert candidates(loud, mids, 0, 6 * S) == []  # всё слилось в одну фразу длиннее 0,7 с
    # Всплеск на 3,6 с тише речи (вдох) — не кандидат; «кхм» на 2,0 с громче речи — кандидат
    assert glued_candidates(loud, mids, 0, 6 * S) == [(2_000_000, 2_200_000, 6.0)]
    heard = {(2_000_000, 2_200_000): "Продолжение следует...", (1_960_000, 2_240_000): "Кхм."}
    found = detect(loud, mids, 0, 6 * S, lambda a, b: heard[(a, b)])
    assert [(n.start_us, n.end_us, n.heard) for n in found] == [(2_000_000, 2_200_000, "Продолжение следует...")]
    # Вырез — вместе с провалами вокруг, до голоса с запасом
    assert (found[0].cut_start_us, found[0].cut_end_us) == (1_500_000 + PAD_US, 2_400_000 - PAD_US)


def test_word_onset_glued_to_phrase_is_left():
    """Начало слова, отделённое провалом: на узких границах Whisper выдумывает,
    на широких слышит слово — не трогаем."""
    loud = track([(0.5, 1.5, -30), (1.5, 2.0, -58), (2.0, 2.2, -24), (2.2, 2.4, -55), (2.4, 3.4, -30)])
    mids = [int(t * S) for t in (0.7, 1.0, 1.3, 2.6, 2.9, 3.2)]
    heard = {(2_000_000, 2_200_000): "Субтитры сделал DimaTorzok", (1_960_000, 2_240_000): "Скрипт."}
    assert detect(loud, mids, 0, 6 * S, lambda a, b: heard[(a, b)]) == []


def test_room_noise_does_not_stop_the_cut():
    # После «кхм» дыхание чуть громче порога тишины (−58 дБ) — вырез идёт до голоса
    loud = track([(0.5, 1.5, -30), (2.0, 2.3, -24), (2.4, 2.45, -58), (2.55, 2.6, -58), (2.7, 3.5, -30)])
    assert cut_range(loud, 2_000_000, 2_300_000) == (1_500_000 + PAD_US, 2_700_000 - PAD_US)


def test_cut_stops_at_reach_in_long_silence():
    loud = track([(5.0, 5.3, -25)], seconds=10.0)
    assert cut_range(loud, 5_000_000, 5_300_000) == (5_000_000 - REACH_US, 5_300_000 + REACH_US)


def test_what_counts_as_noise():
    for text in ("Кхм.", "кх-кх", "[кашель]", "*кашляет*", "", "Продолжение следует...",
                 "Субтитры сделал DimaTorzok"):
        assert is_noise(text), text
    for text in ("навык", "Или", "Скрипт.", "Пока."):
        assert not is_noise(text), text


def test_only_what_is_kept_in_the_version_is_searched():
    """Решение автора: сначала порезать, потом искать — только оставленное в
    версии, с секундой запаса по краям; проверенное второй раз не ищется."""
    from fractions import Fraction

    from longform.edl import Clip, OutputProfile, SourceInfo, compile_edl

    src = SourceInfo("a", Fraction(25), 25 * 600, 48000, 1, Fraction(0))
    edl = compile_edl([Clip("1", "a", 10 * S, 20 * S), Clip("2", "a", 21 * S, 30 * S), Clip("3", "a", 100 * S, 110 * S)],
                      {"a": src}, OutputProfile(Fraction(25), 48000))
    # Куски в 1 с друг от друга с запасом сливаются в один отрезок
    assert kept_ranges(edl, "a") == [(10 * S - MARGIN_US, 30 * S + MARGIN_US), (100 * S - MARGIN_US, 110 * S + MARGIN_US)]
    assert kept_ranges(edl, "b") == []
    assert merge([(5, 9), (1, 3), (2, 6)]) == [(1, 9)]
    assert subtract([(0, 10 * S)], [(2 * S, 3 * S), (9 * S, 12 * S)]) == [(0, 2 * S), (3 * S, 9 * S)]
    # Обрезки короче самого короткого звука не проверяются
    assert subtract([(0, 10 * S)], [(50_000, 10 * S)]) == []


def _noise(start: float) -> Noise:
    a = int(start * S)
    return Noise(a, a + 300_000, a - 100_000, a + 400_000, "Кхм.", 3.0)


def test_found_noises_accumulate_and_keep_their_numbers(tmp_path):
    from fractions import Fraction

    from longform import store
    from longform.db import connect, migrate
    from longform.edl import Clip, OutputProfile, SourceInfo, compile_edl

    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "кашель", "user")
    conn.execute("INSERT INTO assets (id, project_id, position, path, size_bytes, mtime_ns, fingerprint, fps_num, "
                 "fps_den, frame_count, sample_rate, channels, audio_offset_num, audio_offset_den, width, height, "
                 "registered_at, role) VALUES ('ast_1', ?, 0, '/x.mp4', 1, 1, 'f', 25, 1, 15000, 48000, 1, 0, 1, 64, "
                 "36, 'now', 'main')", (p["id"],))
    src = SourceInfo("ast_1", Fraction(25), 15000, 48000, 1, Fraction(0))
    edl = lambda *clips: compile_edl([Clip(str(i), "ast_1", a * S, b * S) for i, (a, b) in enumerate(clips)],  # noqa: E731
                                     {"ast_1": src}, OutputProfile(Fraction(25), 48000))
    first = edl((10, 20))
    assert to_scan(conn, "ast_1", first, "t1") == [(9 * S, 21 * S)]
    save(conn, "ast_1", [_noise(12), _noise(15)], "t1", [(9 * S, 21 * S)])
    assert to_scan(conn, "ast_1", first, "t1") == []
    # Вернули вырезанное (40–50 с) — проверяется только оно; номера прежних звуков те же
    wider = edl((10, 20), (40, 50))
    assert to_scan(conn, "ast_1", wider, "t1") == [(39 * S, 51 * S)]
    save(conn, "ast_1", [_noise(45)], "t1", [(39 * S, 51 * S)])
    rows = [(r["idx"], r["start_us"]) for r in conn.execute("SELECT idx, start_us FROM noises ORDER BY idx")]
    assert rows == [(0, 12 * S), (1, 15 * S), (2, 45 * S)]
    assert scanned(conn, "ast_1")["ranges"] == [(9 * S, 21 * S), (39 * S, 51 * S)]
    assert scanned(conn, "ast_1")["found"] == 3
    # Новая расшифровка — проверять всё заново, прежнее стирается
    assert to_scan(conn, "ast_1", wider, "t2") == [(9 * S, 21 * S), (39 * S, 51 * S)]
    save(conn, "ast_1", [_noise(16)], "t2", [(9 * S, 21 * S)])
    assert [r["start_us"] for r in conn.execute("SELECT start_us FROM noises")] == [16 * S]


def test_search_job_for_an_already_transcribed_recording(tmp_path):
    """Отдельная задача поиска ставится в очередь: её тип есть в базе."""
    from longform import jobs, store
    from longform.db import connect, migrate

    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "кашель", "user")
    conn.execute("INSERT INTO assets (id, project_id, position, path, size_bytes, mtime_ns, fingerprint, fps_num, "
                 "fps_den, frame_count, sample_rate, channels, audio_offset_num, audio_offset_den, width, height, "
                 "registered_at, role) VALUES ('ast_1', ?, 0, '/x.mp4', 1, 1, 'f', 25, 1, 100, 48000, 1, 0, 1, 64, "
                 "36, 'now', 'main')", (p["id"],))
    job = jobs.enqueue(conn, p["id"], "noises", actor="agent", asset_id="ast_1", params={"transcript": "t1"})
    assert job["state"] == "queued" and job["type"] == "noises"


def test_precise_boundary_stays_in_silence_and_leaves_sound():
    loud = track([(0.5, 1.5, -30), (2.0, 2.3, -24)])
    # В тишине — ровно где задана; в речи — в ближайший тихий момент рядом
    assert loud.precise_boundary(1_800_000) == 1_800_000
    assert loud.precise_boundary(1_480_000) != 1_480_000
    # Дыхание выше порога тишины, но тише голоса, — тоже на месте: граница
    # выреза «кхм» вплотную к фразе не уезжает к слову
    breath = track([(0.5, 1.5, -30), (1.5, 1.9, -55), (1.9, 1.95, -50), (1.95, 2.3, -24)])
    assert breath.precise_boundary(1_820_000) == 1_820_000
