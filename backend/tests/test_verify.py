"""Проверка результата по исходнику: должна подтверждать верное и ловить сдвиг."""

import pytest

from longform.edl import Clip, OutputProfile, compile_edl
from longform.probe import probe
from longform.render.select import render_select
from longform.timebase import frame_to_us
from longform.verify import sample_frames, verify_output

pytestmark = pytest.mark.media

RANGES = [(0, 120), (200, 260), (261, 400), (640, 731), (900, 1490)]


def compile_ranges(fx, ranges):
    r = probe(fx.path)
    info = r.source_info("a1")
    clips = [Clip(f"c{i}", "a1", frame_to_us(a, info.fps), frame_to_us(b, info.fps))
             for i, (a, b) in enumerate(ranges)]
    return compile_edl(clips, {"a1": info}, OutputProfile(info.fps, info.sample_rate)), {"a1": r}


@pytest.fixture(scope="module")
def rendered(fx_ntsc_offset, tmp_path_factory):
    edl, media = compile_ranges(fx_ntsc_offset, RANGES)
    out = tmp_path_factory.mktemp("verify") / "результат.mp4"
    render_select(edl, media, out, out.parent)
    return edl, media, out


def test_sample_points_cover_both_sides_of_every_joint(rendered):
    edl, _, _ = rendered
    frames = sample_frames(edl)
    for c in edl.clips[1:]:
        assert c.out_start_frame - 1 in frames and c.out_start_frame in frames
    assert 0 in frames and edl.total_frames - 1 in frames


def test_correct_render_passes(rendered):
    edl, media, out = rendered
    report = verify_output(edl, media, out)
    assert report.ok, report.as_dict()
    assert all(p.video == "ok" for p in report.points)
    assert report.as_dict()["audio_checked"] >= len(edl.clips)


def test_one_frame_shift_is_caught(rendered, fx_ntsc_offset):
    edl, media, out = rendered
    # Проверяем результат по листу, сдвинутому на кадр: так выглядела бы
    # ошибка склейки на один кадр
    shifted, _ = compile_ranges(fx_ntsc_offset, [(a + 1, b + 1) for a, b in RANGES])
    report = verify_output(shifted, media, out)
    assert not report.ok
    assert report.video_mismatches
    assert report.audio_mismatches
    lag = abs(report.audio_mismatches[0].audio_lag_ms)
    assert 30 < lag < 37  # один кадр при 29.97 — 33,4 мс


# ---------- перестановка кусков (step-29) ----------

MOVED = [(0, 120), (640, 731), (200, 260), (261, 400), (900, 1490)]


@pytest.fixture(scope="module")
def moved(fx_ntsc_offset, tmp_path_factory):
    edl, media = compile_ranges(fx_ntsc_offset, MOVED)
    out = tmp_path_factory.mktemp("verify-moved") / "перестановка.mp4"
    render_select(edl, media, out, out.parent)
    return edl, media, out


def test_moved_render_passes_and_one_frame_shift_is_caught(moved, fx_ntsc_offset):
    edl, media, out = moved
    report = verify_output(edl, media, out, must=[120, 211, 271])
    assert report.ok, report.as_dict()
    shifted, _ = compile_ranges(fx_ntsc_offset, [(a + 1, b + 1) for a, b in MOVED])
    bad = verify_output(shifted, media, out, must=[120, 211, 271])
    assert not bad.ok and bad.video_mismatches


def test_move_joints_are_always_sampled(moved):
    """Стыков больше, чем берёт выборка, — стыки перестановки в ней всё равно есть."""
    edl, _, _ = moved
    frames = sample_frames(edl, max_joints=1, must=[211, 271])
    assert {210, 211, 270, 271} <= set(frames)
    assert 270 not in sample_frames(edl, max_joints=1)


def test_lag_is_not_pulled_to_a_louder_place():
    """Голос периодичен: сдвиг на период тона с громким местом исходника
    давал больше сырой корреляции, чем верное место. Сравнение — нормированное
    (найдено на стыке перестановки 13.10: ложные 3,75 мс при точном звуке)."""
    import numpy as np

    from longform.verify import _lag

    rng = np.random.default_rng(1)
    t = np.arange(8000, dtype=np.float64)
    voice = (np.sin(2 * np.pi * 267 * t / 8000) * (1 + 3 * t / 8000) + 0.05 * rng.standard_normal(8000)) * 3000
    voice = voice.astype(np.float32)
    ref = voice[1000:2600]
    pos, strength = _lag(voice[1400:2200], ref)
    assert pos == 400 and strength > 0.99
