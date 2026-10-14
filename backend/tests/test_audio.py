"""Сборка звука монтажа без ffmpeg: проверяем каждый отсчёт."""

import wave
from fractions import Fraction

import numpy as np

from longform.edl import Clip, OutputProfile, SourceInfo, compile_edl
from longform.render.audio import build_edited_wav
from longform.timebase import frame_to_us

FPS = Fraction(25)
SR = 48000
PER_FRAME = 1920  # 48000 / 25


def write_ramp(path, n):
    # Значение отсчёта однозначно говорит, откуда он взят
    values = (np.arange(n) % 20000 - 10000).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(values.tobytes())
    return values


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


def compile_frames(ranges, audio_offset=Fraction(0), frame_count=1000):
    src = SourceInfo("a1", FPS, frame_count, SR, 1, audio_offset)
    clips = [Clip(f"c{i}", "a1", frame_to_us(a, FPS), frame_to_us(b, FPS)) for i, (a, b) in enumerate(ranges)]
    return compile_edl(clips, {"a1": src}, OutputProfile(FPS, SR))


def test_bodies_are_bit_exact_and_joins_are_short_crossfades(tmp_path):
    source = write_ramp(tmp_path / "src.wav", 1000 * PER_FRAME)
    # Куски 2 и 3 идут в исходнике подряд: между ними сведения быть не должно
    edl = compile_frames([(0, 100), (200, 300), (300, 350), (400, 500)])
    build_edited_wav(edl, {"a1": tmp_path / "src.wav"}, tmp_path / "out.wav")
    out = read_wav(tmp_path / "out.wav")

    assert len(out) == edl.total_samples
    half = 240  # 5 мс при 48 кГц
    joins = [100 * PER_FRAME, 250 * PER_FRAME]
    expected = np.concatenate(
        [source[0 : 100 * PER_FRAME], source[200 * PER_FRAME : 350 * PER_FRAME],
         source[400 * PER_FRAME : 500 * PER_FRAME]]
    )
    in_join = np.zeros(len(out), bool)
    for j in joins:
        in_join[j - half : j + half] = True
    assert np.array_equal(out[~in_join], expected[~in_join])
    # На стыке смесь, а не жёсткая склейка: значения отличаются от обоих краёв
    for j in joins:
        assert not np.array_equal(out[j - half : j + half], expected[j - half : j + half])


def test_audio_before_its_start_is_silence(tmp_path):
    source = write_ramp(tmp_path / "src.wav", 1000 * PER_FRAME)
    # Звук начинается на 0.1 с (4800 отсчётов) позже картинки
    edl = compile_frames([(0, 50)], audio_offset=Fraction(1, 10))
    build_edited_wav(edl, {"a1": tmp_path / "src.wav"}, tmp_path / "out.wav")
    out = read_wav(tmp_path / "out.wav")
    gap = 4800
    assert not out[:gap].any()
    assert np.array_equal(out[gap:], source[: len(out) - gap])


def test_audio_past_its_end_is_silence(tmp_path):
    source = write_ramp(tmp_path / "src.wav", 100 * PER_FRAME)
    edl = compile_frames([(90, 110)], frame_count=110)
    build_edited_wav(edl, {"a1": tmp_path / "src.wav"}, tmp_path / "out.wav")
    out = read_wav(tmp_path / "out.wav")
    assert np.array_equal(out[: 10 * PER_FRAME], source[90 * PER_FRAME :])
    assert not out[10 * PER_FRAME :].any()


def test_crossfade_can_be_switched_off(tmp_path):
    """Для сравнения до и после: без сведения звук монтажа — точная склейка."""
    source = write_ramp(tmp_path / "src.wav", 1000 * PER_FRAME)
    edl = compile_frames([(0, 100), (200, 300), (400, 500)])
    build_edited_wav(edl, {"a1": tmp_path / "src.wav"}, tmp_path / "hard.wav", crossfade=Fraction(0))
    out = read_wav(tmp_path / "hard.wav")
    expected = np.concatenate([source[a * PER_FRAME : b * PER_FRAME] for a, b in [(0, 100), (200, 300), (400, 500)]])
    assert np.array_equal(out, expected)
