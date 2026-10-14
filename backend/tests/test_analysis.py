"""Тишина и выравнивание границ на искусственной громкости с известной разметкой."""

import numpy as np

from longform.analysis import PAD_US, Loudness, levels, peaks

SPEECH, QUIET = -20.0, -80.0


def loudness(pattern, offset_us=0):
    """pattern: [(мс, 'речь' | 'тишина'), ...] -> громкость по 10 мс."""
    db = np.concatenate([np.full(ms // 10, SPEECH if kind == "речь" else QUIET, np.float32)
                         for ms, kind in pattern])
    floor, speech, thr = levels(db)
    return Loudness(db, offset_us, floor, speech, thr)


# слово 0–1000 мс, пауза 1000–2000, слово 2000–2400, пауза 2400–2550, слово 2550–5000, пауза 5000–6000
L = loudness([(1000, "речь"), (1000, "тишина"), (400, "речь"), (150, "тишина"), (2450, "речь"), (1000, "тишина")])


def ms(x):
    return x * 1000


def test_threshold_is_between_noise_and_speech():
    assert QUIET < L.threshold_db < SPEECH


def test_silences_ignore_gaps_between_words():
    # Промежуток 150 мс — это не пауза
    assert [(a // 1000, b // 1000) for a, b, _ in L.silences()] == [(1000, 2000), (5000, 6000)]


def test_short_dip_inside_a_word_is_speech():
    dip = loudness([(500, "речь"), (40, "тишина"), (500, "речь"), (500, "тишина")])
    assert dip.speech_span(0, ms(1540)) == (0, ms(1040))
    assert not dip.silent[dip.idx(ms(520))]


def test_boundary_in_silence_keeps_a_pad_after_speech():
    # Агент начал вырез сразу после слова: оставляем PAD тишины
    pos, note = L.keep_before(ms(1010))
    assert pos == ms(1000) + PAD_US and note is None
    # Конец выреза вплотную к следующему слову: оставляем PAD перед ним
    pos, note = L.keep_after(ms(1990))
    assert pos == ms(2000) - PAD_US and note is None


def test_boundary_inside_a_word_moves_to_keep_it():
    # Начало выреза за 100 мс до конца слова: слово остаётся целым
    pos, note = L.keep_before(ms(900))
    assert pos == ms(1000) + PAD_US and "осталось целым" in note
    # Конец выреза через 100 мс после начала слова: начало слова возвращается
    pos, note = L.keep_after(ms(2100))
    assert pos == ms(2000) - PAD_US and "начало слова" in note


def test_far_inside_a_long_word_is_flagged_not_moved():
    pos, note = L.keep_before(ms(3700))
    assert pos == ms(3700) and "тишины рядом нет" in note
    assert L.speech_near(pos)


def test_short_silence():
    # Пауза 2400–2550 вмещает запас после оставляемого слова (70 мс) и отступ
    # от вырезаемого (40 мс): граница — сразу за запасом
    pos, _ = L.keep_before(ms(2420))
    assert pos == ms(2400) + PAD_US
    # Пауза 100 мс запаса не вмещает: граница — её середина
    tight = loudness([(1000, "речь"), (100, "тишина"), (1000, "речь")])
    assert tight.keep_before(ms(1010))[0] == ms(1050)
    assert tight.keep_after(ms(1090))[0] == ms(1050)


def test_audio_offset_shifts_everything():
    late = loudness([(1000, "речь"), (1000, "тишина"), (1000, "речь")], offset_us=ms(500))
    assert [(a // 1000, b // 1000) for a, b, _ in late.silences()] == [(1500, 2500)]
    assert late.keep_before(ms(1510))[0] == ms(1500) + PAD_US


def test_speech_span_and_peaks():
    # Слова берутся целиком: 0–1000 и 2000–2400 в основном внутри диапазона
    assert L.speech_span(ms(500), ms(2300)) == (0, ms(2400))
    assert L.speech_span(ms(1100), ms(1900)) is None
    p = peaks(L.db, 0, len(L.db), 6)
    assert p == [SPEECH, QUIET, SPEECH, SPEECH, SPEECH, QUIET]


def test_continuous_speech_belongs_to_each_short_phrase():
    """Сплошная речь 10 с без пауз, Whisper режет её на фразы по 2–3 с: у
    каждой фразы есть речь — пересечение, а не «нет речи» (запись автора)."""
    flow = loudness([(500, "тишина"), (10_000, "речь"), (1000, "тишина")])
    assert flow.speech_span(ms(3000), ms(5000)) == (ms(3000), ms(5000))
    assert flow.speech_span(ms(7500), ms(10_000)) == (ms(7500), ms(10_000))
    # Заход на хвост соседа (0,2 с речи из 2,2 с) — это не речь фразы
    assert flow.speech_span(ms(10_300), ms(12_500)) is None
