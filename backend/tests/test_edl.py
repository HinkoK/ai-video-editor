import random
from fractions import Fraction

import pytest

from longform.edl import Clip, EdlError, OutputProfile, SourceInfo, compile_edl
from longform.timebase import frame_to_us

FPS = Fraction(25)
PROFILE = OutputProfile(fps=FPS, sample_rate=48000)
SRC = SourceInfo(
    asset_id="a1",
    fps=FPS,
    frame_count=1000,
    sample_rate=48000,
    channels=1,
    audio_offset=Fraction(0),
)


def clip(cid, start_frame, end_frame, asset="a1", fps=FPS):
    return Clip(cid, asset, frame_to_us(start_frame, fps), frame_to_us(end_frame, fps))


def compile_(clips, sources=(SRC,), profile=PROFILE):
    return compile_edl(clips, {s.asset_id: s for s in sources}, profile)


def test_clips_are_placed_back_to_back_on_output_frames():
    edl = compile_([clip("c1", 0, 100), clip("c2", 300, 350), clip("c3", 500, 1000)])
    placed = [(p.out_start_frame, p.out_end_frame) for p in edl.clips]
    assert placed == [(0, 100), (100, 150), (150, 650)]
    assert edl.total_frames == 650


def test_audio_samples_follow_output_frame_grid():
    edl = compile_([clip("c1", 0, 100), clip("c2", 300, 350)])
    # 25 fps, 48 кГц: ровно 1920 отсчётов на кадр
    assert [(p.out_start_sample, p.out_end_sample) for p in edl.clips] == [
        (0, 192_000),
        (192_000, 288_000),
    ]
    assert edl.clips[1].src_start_sample == 300 * 1920
    assert edl.total_samples == 288_000


def test_audio_offset_shifts_source_samples():
    # звук начинается на 0.5 с позже первого кадра: кадр 0 приходится
    # на отсчёт -24000, то есть на тишину до начала звука
    late_audio = SourceInfo("a1", FPS, 1000, 48000, 1, audio_offset=Fraction(1, 2))
    edl = compile_([clip("c1", 0, 10), clip("c2", 100, 110)], sources=(late_audio,))
    assert edl.clips[0].src_start_sample == -24_000
    assert edl.clips[1].src_start_sample == 100 * 1920 - 24_000


def test_edited_to_source_and_back():
    edl = compile_([clip("c1", 0, 100), clip("c2", 300, 350)])
    assert edl.edited_to_source(0) == ("c1", "a1", 0)
    assert edl.edited_to_source(99) == ("c1", "a1", 99)
    assert edl.edited_to_source(100) == ("c2", "a1", 300)
    assert edl.edited_to_source(149) == ("c2", "a1", 349)
    with pytest.raises(IndexError):
        edl.edited_to_source(150)
    assert edl.source_to_edited("a1", 320) == 120
    # вырезанный кадр не переносится на соседний кусок
    assert edl.source_to_edited("a1", 200) is None
    assert edl.source_to_edited("a1", 350) is None


@pytest.mark.parametrize(
    "clips, code",
    [
        ([], "empty"),
        ([clip("c1", 0, 10, asset="nope")], "unknown_asset"),
        ([clip("c1", 10, 10)], "too_short"),
        ([clip("c1", 10, 11)], "too_short"),
        ([clip("c1", 990, 1001)], "out_of_bounds"),
        ([clip("c1", 0, 50), clip("c2", 40, 80)], "overlap"),
        ([clip("c1", 0, 50), clip("c1", 60, 80)], "duplicate_id"),
        ([Clip("c1", "a1", 0, 39_999)], "off_grid"),
        ([Clip("c1", "a1", -40_000, 40_000)], "out_of_bounds"),
    ],
)
def test_invalid_edls_are_rejected_with_reason(clips, code):
    with pytest.raises(EdlError) as err:
        compile_(clips)
    assert err.value.code == code
    assert err.value.message  # причина по-русски, для интерфейса и для агента


def test_source_fps_must_match_profile_for_now():
    ntsc = SourceInfo("a1", Fraction(30000, 1001), 1000, 48000, 1, Fraction(0))
    with pytest.raises(EdlError) as err:
        compile_([clip("c1", 0, 10, fps=ntsc.fps)], sources=(ntsc,))
    assert err.value.code == "fps_mismatch"


def test_slice_keeps_source_mapping():
    edl = compile_([clip("c1", 0, 100), clip("c2", 300, 350), clip("c3", 500, 600)])
    part = edl.slice(90, 160)
    assert part.total_frames == 70
    assert [(p.clip_id, p.src_start_frame, p.src_end_frame) for p in part.clips] == [
        ("c1", 90, 100),
        ("c2", 300, 350),
        ("c3", 500, 510),
    ]
    for j in range(70):
        assert part.edited_to_source(j)[2] == edl.edited_to_source(90 + j)[2]


def test_slice_rejects_empty_or_outside_range():
    edl = compile_([clip("c1", 0, 100)])
    for a, b in [(10, 10), (-1, 5), (50, 101)]:
        with pytest.raises(EdlError):
            edl.slice(a, b)


def random_edl(rng, frame_count=5000):
    cuts = sorted(rng.sample(range(1, frame_count), rng.randint(2, 40)))
    bounds = [0, *cuts, frame_count]
    ranges = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a >= 2]
    kept = [r for r in ranges if rng.random() < 0.6] or ranges[:1]
    src = SourceInfo("a1", Fraction(30000, 1001), frame_count, 48000, 2, Fraction(0))
    profile = OutputProfile(fps=src.fps, sample_rate=48000)
    clips = [clip(f"c{i}", a, b, fps=src.fps) for i, (a, b) in enumerate(kept)]
    return compile_(clips, sources=(src,), profile=profile), kept


def test_random_edls_map_every_frame_both_ways():
    rng = random.Random(20261006)
    for _ in range(200):
        edl, kept = random_edl(rng)
        assert edl.total_frames == sum(b - a for a, b in kept)
        for j in range(0, edl.total_frames, 7):
            _, _, src = edl.edited_to_source(j)
            assert edl.source_to_edited("a1", src) == j
        # отсчёты звука идут без дыр и наложений
        for prev, cur in zip(edl.clips, edl.clips[1:]):
            assert prev.out_end_sample == cur.out_start_sample


def test_random_reordered_edls_map_every_frame_both_ways():
    """Перестановка кусков (step-29): куски не по порядку исходника —
    каждый кадр туда и обратно, звук без дыр, отсчёт куска от его кадра."""
    rng = random.Random(20261013)
    for _ in range(200):
        _, kept = random_edl(rng)
        rng.shuffle(kept)
        src = SourceInfo("a1", Fraction(30000, 1001), 5000, 48000, 2, Fraction(0))
        clips = [clip(f"c{i}", a, b, fps=src.fps) for i, (a, b) in enumerate(kept)]
        edl = compile_(clips, sources=(src,), profile=OutputProfile(fps=src.fps, sample_rate=48000))
        assert [(c.src_start_frame, c.src_end_frame) for c in edl.clips] == kept
        for j in range(0, edl.total_frames, 5):
            _, _, s = edl.edited_to_source(j)
            assert edl.source_to_edited("a1", s) == j
        for prev, cur in zip(edl.clips, edl.clips[1:]):
            assert prev.out_end_sample == cur.out_start_sample
        for c in edl.clips:
            assert c.src_start_sample == round(Fraction(c.src_start_frame) / src.fps * 48000)
