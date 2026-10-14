"""Текстовые правила расшифровки без Whisper."""

from longform.transcribe import high_impact, markers_preserved


def test_markers():
    assert high_impact("Это не работает без ключа, но стоит 5 долларов") == ["5", "без", "долларов", "не", "но"]
    assert high_impact("Сегодня мы разберём, как настроить агента") == []


def test_markers_preserved_only_in_a_real_retake():
    take = "Это не работает без ключа"
    retake = "Повторю: это не работает без ключа, проверьте"
    assert markers_preserved(take, retake)
    # «Оставленный дубль» о другом — пометку снимать нельзя
    assert not markers_preserved(take, "Повторяю ещё раз, не забудьте подписаться без ключа")
    # В дубле нет нужного числа
    assert not markers_preserved("Стоит 5 долларов", "Стоит пару долларов")
    # Нет важных слов в вырезанном — пометка от защиты без слов не снимается
    assert not markers_preserved("Здесь показываю результат", "Здесь показываю результат")


def test_words_from_tokens_use_dtw_and_glue_pieces(tmp_path):
    """Слово начинается с токена с пробелом; куски слова и знаки прилипают;
    начало — время DTW (в сотых секунды), конец — начало следующего слова."""
    import json

    from longform.transcribe import parse_chunk

    chunk = {"transcription": [{
        "offsets": {"from": 1000, "to": 3000}, "text": " Стоимость часа.",
        "tokens": [
            {"text": "[_BEG_]", "offsets": {"from": 1000, "to": 1000}, "p": 1.0, "t_dtw": -1},
            {"text": " С", "offsets": {"from": 1000, "to": 1100}, "p": 0.9, "t_dtw": 140},
            {"text": "тоимость", "offsets": {"from": 1100, "to": 1500}, "p": 0.8, "t_dtw": 150},
            {"text": " часа", "offsets": {"from": 1500, "to": 2500}, "p": 0.7, "t_dtw": 210},
            {"text": ".", "offsets": {"from": 2500, "to": 2600}, "p": 0.9, "t_dtw": 260},
        ]}]}
    path = tmp_path / "chunk.json"
    path.write_text(json.dumps(chunk, ensure_ascii=False))
    (seg,) = parse_chunk(path)
    assert [(w.text, w.start_ms, w.end_ms) for w in seg.words] == [("Стоимость", 1400, 2100), ("часа.", 2100, 3000)]


def test_letter_split_between_tokens(tmp_path):
    """Как на записи 13.10 (10:46): whisper.cpp разрезал «я» между токенами
    (" \\xd1" + "\\x8f…"), файл — не UTF-8. Буква склеивается, слово — своё."""
    from longform.transcribe import parse_chunk

    def tok(raw: bytes, dtw: int) -> bytes:
        return (b'{"text": "' + raw + b'", "offsets": {"from": 0, "to": 0}, "p": 0.8, "t_dtw": '
                + str(dtw).encode() + b"}")

    ya = "я".encode()
    tokens = b", ".join([tok(" Вот".encode(), 100), tok(b" " + ya[:1], 150), tok(ya[1:], 160),
                         tok("ндекс".encode(), 170), tok(".".encode(), 200)])
    raw = (b'{"transcription": [{"offsets": {"from": 1000, "to": 3000}, "text": "'
           + " Вот яндекс.".encode() + b'", "tokens": [' + tokens + b"]}]}")
    path = tmp_path / "chunk.json"
    path.write_bytes(raw)
    (seg,) = parse_chunk(path)
    assert seg.text == "Вот яндекс."
    assert [(w.text, w.start_ms) for w in seg.words] == [("Вот", 1000), ("яндекс.", 1500)]


def test_chunks_overlap_on_both_sides():
    from longform.transcribe import CHUNK_MS, OVERLAP_MS, chunk_plan

    total = 2 * CHUNK_MS + 1000
    plan = chunk_plan(total)
    assert [(own_start, own_end) for _, _, own_start, own_end in plan] == [
        (0, CHUNK_MS), (CHUNK_MS, 2 * CHUNK_MS), (2 * CHUNK_MS, total)]
    start, length, own_start, _ = plan[1]
    # Средний кусок шире своей зоны с обеих сторон, но не выходит за конец записи
    assert start == own_start - OVERLAP_MS and start + length == min(total, 2 * CHUNK_MS + OVERLAP_MS)


def test_repair_finds_speech_hidden_behind_an_invented_phrase():
    """Как бывает на настоящей записи: Whisper выдал «Субтитры сделал
    DimaTorzok» на всё окно, а речь после него потерял."""
    from longform.transcribe import Segment, apply_repair, repair_windows

    invented = Segment(0, 30_000, "Субтитры сделал DimaTorzok", 0.3)
    real = Segment(30_000, 34_000, "показал, как устроен монтаж", 0.8)
    speech = [(8_000, 20_000), (20_500, 34_500)]  # речь идёт с 8 с
    windows = repair_windows([invented, real], speech, duration_ms=40_000)
    assert windows == [(7_750, 30_250)]  # всё до настоящей фразы, с запасом
    # Короткая речь без текста (меньше 1,5 с) не перечитывается
    assert repair_windows([real], [(29_000, 30_000)]) == []
    # Длинный пропуск режется на окна не длиннее 28 с
    long = repair_windows([], [(0, 70_000)])
    assert all(b - a <= 28_000 for a, b in long) and long[0][0] == 0 and long[-1][1] == 70_250

    found = [Segment(8_000, 21_000, "Сначала откроем папку с записью", 0.8),
             Segment(21_000, 30_000, "и посмотрим пример, в котором я", 0.8),
             Segment(29_800, 32_000, "показал, как устроен", 0.8),  # уже есть в настоящей фразе
             Segment(25_000, 26_000, "Продолжение следует...", 0.9)]  # снова выдумка
    out = apply_repair([invented, real], [((7_750, 30_250), found)])
    assert [s.text for s in out] == ["Сначала откроем папку с записью", "и посмотрим пример, в котором я",
                                     "показал, как устроен монтаж"]


def test_repair_leaves_good_phrases_alone():
    from longform.transcribe import Segment, apply_repair

    good = Segment(1_000, 4_000, "Нормальная фраза", 0.9)
    out = apply_repair([good], [((0, 5_000), [Segment(1_200, 3_000, "дубль той же фразы", 0.9)])])
    assert out == [good]
