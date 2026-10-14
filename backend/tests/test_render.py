"""Склейка проверяется по раскодированному результату, а не по коду возврата.

Для каждого кадра результата читаем его номер в исходнике. Для каждого куска
находим, на сколько отсчётов сдвинут звук. Точная склейка — это все номера
кадров как в монтажном листе и сдвиг звука 0 на каждом куске.
"""

import numpy as np
import pytest

from longform.edl import Clip, OutputProfile, compile_edl
from longform.probe import probe
from longform.render.select import render_select
from longform.timebase import frame_to_us
from media_fixtures import audio_lags, audio_track_samples, decode_audio, read_frame_numbers

pytestmark = pytest.mark.media


def compile_ranges(fx, ranges):
    r = probe(fx.path)
    info = r.source_info("a1")
    clips = [Clip(f"c{i}", "a1", frame_to_us(a, info.fps), frame_to_us(b, info.fps))
             for i, (a, b) in enumerate(ranges)]
    edl = compile_edl(clips, {"a1": info}, OutputProfile(info.fps, info.sample_rate))
    return edl, {"a1": r}


def expected_frames(edl):
    return [f for c in edl.clips for f in range(c.src_start_frame, c.src_end_frame)]


def assert_exact(edl, fx, out_path):
    frames = read_frame_numbers(out_path)
    assert frames == expected_frames(edl)
    # Контейнер хранит точную длину звука: последний пакет AAC помечен
    # укороченным. А сырое декодирование отдаёт последний блок AAC целиком,
    # поэтому у него допустим хвост короче одного блока (1024 отсчёта).
    assert audio_track_samples(out_path) == edl.total_samples
    audio = decode_audio(out_path, fx.sample_rate)
    assert 0 <= len(audio) - edl.total_samples < 1024
    lags = audio_lags(edl, audio, fx)
    measured = [lag for lag in lags if lag is not None]
    assert measured, "ни один кусок не удалось проверить по звуку"
    assert measured == [0] * len(measured), f"звук сдвинут: {lags}"
    return lags


RANGES = [(0, 120), (200, 260), (261, 400), (640, 641 + 90), (900, 1500)]


def test_full_render_is_frame_and_sample_exact(fx_plain, tmp_path):
    edl, media = compile_ranges(fx_plain, RANGES)
    out = tmp_path / "результат.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_plain, out)


def test_size_estimate_by_samples_covers_the_real_file(fx_plain, tmp_path, monkeypatch):
    """Пробное сжатие вместо грубой оценки: не меньше настоящего ролика и не
    больше грубой оценки по битрейту исходника."""
    from longform.render import select

    edl, media = compile_ranges(fx_plain, [(0, 1450)])
    rough = select.estimate_video_bytes(edl, media, select.FINAL, tmp_path)
    monkeypatch.setattr(select, "SAMPLE_ABOVE_BYTES", 0)
    sampled = select.estimate_video_bytes(edl, media, select.FINAL, tmp_path)
    out = tmp_path / "оценка.mp4"
    render_select(edl, media, out, tmp_path)
    assert out.stat().st_size <= sampled <= rough
    assert not list(tmp_path.glob("size-sample-*"))  # пробы убраны


def test_mkv_with_millisecond_timestamps(fx_ntsc_mkv, tmp_path):
    edl, media = compile_ranges(fx_ntsc_mkv, RANGES)
    out = tmp_path / "из mkv.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_ntsc_mkv, out)


def test_ntsc_with_shifted_streams(fx_ntsc_offset, tmp_path):
    edl, media = compile_ranges(fx_ntsc_offset, RANGES)
    out = tmp_path / "ntsc.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_ntsc_offset, out)


def test_audio_starting_late_gives_silence_not_shift(fx_late_audio, tmp_path):
    # Первые 1/3 с звука в исходнике нет: там должна быть тишина, а звук
    # дальше обязан встать на своё место, а не съехать к началу
    edl, media = compile_ranges(fx_late_audio, [(0, 100), (150, 400)])
    out = tmp_path / "late.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_late_audio, out)
    audio = decode_audio(out, 48000)
    silent = audio[: 48000 // 3 - 2048]
    assert np.sqrt(np.mean(silent**2)) < 0.001


@pytest.mark.parametrize("fixture", ["fx_ntsc_offset", "fx_ntsc_mkv"])
@pytest.mark.parametrize("start", [150, 151, 152])
def test_preview_of_a_range_matches_the_full_render(request, fixture, tmp_path, start):
    """Пробный рендер — тот же лист, только короче, с перемоткой к началу.

    Три соседних старта дают разные остатки округления времени перемотки,
    два контейнера — разные шкалы времени (1/30000 в mp4, миллисекунды в mkv).
    """
    fx = request.getfixturevalue(fixture)
    edl, media = compile_ranges(fx, RANGES)
    # Начинается внутри куска, далеко от опорного кадра, захватывает два стыка
    part = edl.slice(start, start + 180)
    assert part.clips[0].src_start_frame == 80 + start
    out = tmp_path / "превью.mp4"
    render_select(part, media, out, tmp_path)
    assert_exact(part, fx, out)


def test_many_cuts(fx_plain, tmp_path):
    # 60 кусков по 10–15 кадров с вырезами между ними
    ranges, pos = [], 3
    for i in range(60):
        length = 10 + i % 6
        ranges.append((pos, pos + length))
        pos += length + 3 + i % 5
    edl, media = compile_ranges(fx_plain, ranges)
    out = tmp_path / "много стыков.mp4"
    render_select(edl, media, out, tmp_path)
    lags = assert_exact(edl, fx_plain, out)
    assert sum(lag is not None for lag in lags) >= 50


def test_more_than_a_hundred_cuts(fx_plain, tmp_path):
    # Цепочка из 101 условия ffmpeg уже не разбирает, дерево — да
    ranges = [(i * 9, i * 9 + 5) for i in range(160)]
    edl, media = compile_ranges(fx_plain, ranges)
    out = tmp_path / "160 кусков.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_plain, out)


def test_two_files_one_after_another(fx_plain, fx_second, tmp_path):
    """Камера, потом экран: две серии, склеенные concat. Номера кадров второго
    файла начинаются с 30000, по ним видно, из какого файла кадр."""
    media = {"cam": probe(fx_plain.path), "scr": probe(fx_second.path)}
    infos = {k: v.source_info(k) for k, v in media.items()}
    ranges = [("cam", 0, 100), ("cam", 200, 300), ("scr", 0, 100), ("scr", 150, 300)]
    clips = [Clip(f"c{i}", a, frame_to_us(s, 25), frame_to_us(e, 25)) for i, (a, s, e) in enumerate(ranges)]
    edl = compile_edl(clips, infos, OutputProfile(infos["cam"].fps, 48000))
    out = tmp_path / "камера и экран.mp4"
    render_select(edl, media, out, tmp_path)
    numbers = {"cam": 0, "scr": 30000}
    expected = [numbers[c.asset_id] + f for c in edl.clips for f in range(c.src_start_frame, c.src_end_frame)]
    assert read_frame_numbers(out) == expected
    assert audio_track_samples(out) == edl.total_samples
    lags = audio_lags(edl, decode_audio(out, 48000), {"cam": fx_plain, "scr": fx_second})
    assert lags == [0, 0, 0, 0]


def test_existing_output_is_never_overwritten(fx_plain, tmp_path):
    edl, media = compile_ranges(fx_plain, [(0, 50)])
    out = tmp_path / "уже есть.mp4"
    out.write_bytes(b"not a video")
    with pytest.raises(FileExistsError):
        render_select(edl, media, out, tmp_path)
    assert out.read_bytes() == b"not a video"


def test_each_input_stops_reading_after_its_last_frame(fx_plain, tmp_path):
    """Короткий кусок из начала длинного файла не должен читать файл до конца."""
    from longform.render.select import input_args, split_runs

    edl, media = compile_ranges(fx_plain, [(100, 150), (200, 260)])
    (run_,) = split_runs(edl)
    args = input_args(media[run_[0].asset_id], run_, edl.profile.fps)
    t = float(args[args.index("-t") + 1])
    assert t == pytest.approx((260 - 100) / 25 + 1)
    # и результат по-прежнему точный
    out = tmp_path / "кусок.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_plain, out)


# ---------- перестановка кусков ----------

MOVED = [(0, 120), (640, 731), (200, 260), (261, 400), (900, 1500)]


def test_moved_pieces_are_frame_and_sample_exact(fx_plain, tmp_path):
    """Кусок 640–731 переставлен в начало: прыжок вперёд, потом назад."""
    edl, media = compile_ranges(fx_plain, MOVED)
    out = tmp_path / "перестановка.mp4"
    render_select(edl, media, out, tmp_path)
    lags = assert_exact(edl, fx_plain, out)
    assert lags.count(0) == len(MOVED)  # звук каждого куска на месте


def test_moved_pieces_without_crossfade(fx_plain, tmp_path):
    from fractions import Fraction

    from longform.render.select import EncodeSettings

    edl, media = compile_ranges(fx_plain, MOVED)
    out = tmp_path / "без сведения.mp4"
    render_select(edl, media, out, tmp_path, EncodeSettings(crossfade=Fraction(0)))
    assert_exact(edl, fx_plain, out)


@pytest.mark.parametrize("start", [100, 115])
def test_preview_across_a_move_joint_matches_the_full_render(fx_ntsc_offset, tmp_path, start):
    """Пробный ролик через стык перестановки (кадр 120) — тот же лист, только короче."""
    edl, media = compile_ranges(fx_ntsc_offset, MOVED)
    part = edl.slice(start, start + 150)
    out = tmp_path / "проба.mp4"
    render_select(part, media, out, tmp_path)
    assert_exact(part, fx_ntsc_offset, out)


def test_long_forward_jump_starts_a_new_run(fx_plain, tmp_path):
    """Прыжок вперёд больше 20 с — своя серия с перемоткой: те же кадры, а
    читать файл меньше (иначе раскодировался бы весь пропущенный кусок)."""
    from longform.render.select import input_args, split_runs

    edl, media = compile_ranges(fx_plain, [(0, 100), (900, 1000)])  # прыжок 32 с
    runs = split_runs(edl)
    assert [[c.src_start_frame for c in r] for r in runs] == [[0], [900]]
    spans = [float(input_args(media["a1"], r, edl.profile.fps)[-1]) for r in runs]
    assert sum(spans) < (1000 / 25) / 2
    out = tmp_path / "прыжок.mp4"
    render_select(edl, media, out, tmp_path)
    assert_exact(edl, fx_plain, out)
    # Прыжок короче 20 с серию не режет
    short, _ = compile_ranges(fx_plain, [(0, 100), (400, 500)])
    assert len(split_runs(short)) == 1
