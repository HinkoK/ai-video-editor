from fractions import Fraction

import pytest

from longform.ffmpeg import MediaToolError
from longform.probe import UnsupportedMedia, probe

pytestmark = pytest.mark.media


def test_probe_reads_exact_rate_and_frame_count(fx_plain):
    r = probe(fx_plain.path)
    assert r.video.fps == 25
    assert r.video.frame_count == 1500
    assert r.video.is_cfr
    assert (r.video.width, r.video.height) == (256, 144)
    assert r.audio.sample_rate == 48000


def test_probe_audio_start_matches_decoded_audio(fx_ntsc_offset):
    """Сдвиг звука по probe должен совпасть с тем, где реально звучит шум.

    probe считает от первого раскодированного отсчёта, а в этом файле первые
    1024 отсчёта — служебные отсчёты AAC, которые звучат. Поэтому начало
    шума на 1024 отсчёта позже начала звука.
    """
    r = probe(fx_ntsc_offset.path)
    assert r.video.fps == Fraction(30000, 1001)
    info = r.source_info("a1")
    probe_shift = info.audio_offset * 48000
    assert fx_ntsc_offset.noise_start - probe_shift == 1024


def test_vfr_is_detected_and_rejected(fx_vfr):
    r = probe(fx_vfr.path)
    assert not r.video.is_cfr
    with pytest.raises(UnsupportedMedia, match="плавающая частота"):
        r.source_info("a1")


def test_missing_file(tmp_path):
    with pytest.raises(UnsupportedMedia, match="не найден"):
        probe(tmp_path / "нет такого.mp4")


def test_corrupt_file_gives_readable_error(tmp_path):
    bad = tmp_path / "битый.mp4"
    bad.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40)
    with pytest.raises((MediaToolError, UnsupportedMedia)):
        probe(bad)


def test_rounding_jitter_is_still_constant_rate():
    """Запись экрана OBS: 60 к/с, отметки отходят от сетки до 1/50 кадра из-за
    округления. Это постоянная частота; пропущенный кадр — уже нет."""
    from longform.probe import is_constant_rate

    step = Fraction(256)  # 60 к/с в шкале 1/15360
    assert is_constant_rate(Fraction(0), step)
    assert is_constant_rate(step / 50, step)  # как у настоящей записи OBS
    assert is_constant_rate(step / 4, step)
    assert not is_constant_rate(step * 3 / 10, step)
    assert not is_constant_rate(step, step)  # пропущенный кадр сдвигает всё дальше на кадр
    # Грубая шкала, где кадр — пара тиков: допуск не меньше одного тика
    assert is_constant_rate(Fraction(1), Fraction(2))
