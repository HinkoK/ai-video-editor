from fractions import Fraction

import pytest

from longform.timebase import (
    frame_to_sample,
    frame_to_us,
    parse_rate,
    round_half_up,
    us_to_frame,
)

NTSC = Fraction(30000, 1001)


def test_parse_rate_keeps_exact_fraction():
    assert parse_rate("30000/1001") == NTSC
    assert parse_rate("25/1") == 25
    assert parse_rate("25") == 25


@pytest.mark.parametrize("bad", ["0/0", "0/1", "abc", "-25/1", ""])
def test_parse_rate_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_rate(bad)


def test_round_half_up_is_symmetric_free_of_bankers_rounding():
    # round() в Python округляет 0.5 к чётному, нам нужно всегда вверх
    assert round_half_up(Fraction(1, 2)) == 1
    assert round_half_up(Fraction(3, 2)) == 2
    assert round_half_up(Fraction(-1, 2)) == 0
    assert round_half_up(Fraction(-3, 2)) == -1


def test_frame_us_round_trip_survives_two_hours_of_ntsc():
    # 2 часа при 29.97: граница кадра не целое число микросекунд,
    # но обратный пересчёт обязан вернуть тот же кадр
    last = 2 * 60 * 60 * 30
    for frame in [0, 1, 2, 999, 1000, 1001, last - 1, last]:
        assert us_to_frame(frame_to_us(frame, NTSC), NTSC) == frame


def test_us_to_frame_snaps_to_nearest_boundary():
    # кадр при 25 fps длится 40 000 мкс
    assert us_to_frame(19_999, Fraction(25)) == 0
    assert us_to_frame(20_000, Fraction(25)) == 1
    assert us_to_frame(59_999, Fraction(25)) == 1


def test_audio_position_does_not_drift_at_fractional_samples_per_frame():
    # 48000 / 29.97 = 1601.6 отсчёта на кадр. Позиция каждого кадра
    # считается от нуля, а не суммой длительностей, поэтому ошибка
    # не копится и всегда меньше половины отсчёта.
    for frame in range(0, 300_000, 997):
        exact = Fraction(frame) * 48000 / NTSC
        assert abs(frame_to_sample(frame, NTSC, 48000) - exact) <= Fraction(1, 2)
