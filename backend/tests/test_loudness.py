"""Громкость ролика: замер по BS.1770 и выравнивание одним усилением."""

import wave

import numpy as np
import pytest

from longform.render.loudness import Loudness, TRUE_PEAK_LIMIT, gain_for, measure, normalize

pytestmark = pytest.mark.media
SR = 48000


def write_wav(path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.clip(np.round(samples * 32767), -32768, 32767).astype(np.int16).tobytes())


def sine(db: float, seconds: float = 10.0, hz: float = 997.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return 10 ** (db / 20) * np.sin(2 * np.pi * hz * t)


def test_gain_reaches_target_or_stops_at_the_peak_limit():
    assert gain_for(Loudness(-26.0, -20.0, 3.0), -16.0) == (10.0, False)
    assert gain_for(Loudness(-12.0, -2.0, 3.0), -16.0) == (-4.0, False)  # громкий — тише
    gain, capped = gain_for(Loudness(-30.0, -3.0, 3.0), -16.0)
    assert capped and gain == pytest.approx(TRUE_PEAK_LIMIT + 3.0)
    assert gain_for(Loudness(float("-inf"), float("-inf"), 0.0), -16.0) == (0.0, False)  # тишина


def test_reference_sine_is_measured_by_the_standard(tmp_path):
    # BS.1770: синус ~1 кГц на 0 дБFS в одном канале — −3,01 LUFS
    write_wav(tmp_path / "ref.wav", sine(-20.0))
    m = measure(tmp_path / "ref.wav")
    assert m.integrated == pytest.approx(-23.0, abs=0.2)


def test_normalize_hits_target_and_keeps_length(tmp_path):
    src, dst = tmp_path / "тихо.wav", tmp_path / "ровно.wav"
    write_wav(src, sine(-30.0))
    info = normalize(src, dst, -16.0)
    assert info["applied"] and not info["capped_by_peak"]
    assert info["before_lufs"] == pytest.approx(-33.0, abs=0.2)
    assert measure(dst).integrated == pytest.approx(-16.0, abs=0.2)
    with wave.open(str(src)) as a, wave.open(str(dst)) as b:
        assert a.getnframes() == b.getnframes() and a.getnchannels() == b.getnchannels()


def test_peaks_cap_the_gain_and_report_says_so(tmp_path):
    # Тихий фон с редкими щелчками почти до нуля: до цели не поднять, не перегрузив
    x = sine(-40.0)
    x[::SR] = 0.95
    src, dst = tmp_path / "щелчки.wav", tmp_path / "после.wav"
    write_wav(src, x)
    info = normalize(src, dst, -16.0)
    assert info["capped_by_peak"]
    after = measure(dst)
    assert after.true_peak <= TRUE_PEAK_LIMIT + 0.2
    assert after.integrated < -20.0  # честно: до цели не дотянули


def test_silence_is_left_alone(tmp_path):
    src = tmp_path / "тишина.wav"
    write_wav(src, np.zeros(SR * 5))
    info = normalize(src, tmp_path / "x.wav", -16.0)
    assert not info["applied"] and not (tmp_path / "x.wav").exists()
