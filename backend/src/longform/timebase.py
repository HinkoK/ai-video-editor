"""Перевод между кадрами, микросекундами и отсчётами звука.

Все вычисления в точных дробях (Fraction), без float. Частота 29.97 — это
ровно 30000/1001, и при float ошибки округления копились бы на длинном видео.

Договорённость о времени:
- время исходника хранится в целых микросекундах от первого кадра видео;
- время монтажа хранится в целых кадрах выходного ролика;
- звук считается в целых отсчётах, позиция каждого кадра вычисляется от нуля,
  а не накоплением длительностей.
"""

from fractions import Fraction
from math import floor

US_PER_SECOND = 1_000_000


def parse_rate(text: str) -> Fraction:
    """'30000/1001' или '25' -> положительная дробь. Ноль и мусор отклоняются."""
    try:
        rate = Fraction(text.strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"не частота кадров: {text!r}") from exc
    if rate <= 0:
        raise ValueError(f"частота кадров должна быть положительной: {text!r}")
    return rate


def round_half_up(x: Fraction) -> int:
    """Округление к ближайшему целому, половина всегда вверх.

    Встроенный round() округляет 0.5 к чётному, и тогда соседние границы
    кадров округлялись бы в разные стороны.
    """
    return floor(x + Fraction(1, 2))


def frame_to_us(frame: int, fps: Fraction) -> int:
    """Начало кадра в микросекундах (округлено до ближайшей микросекунды)."""
    return round_half_up(Fraction(frame) * US_PER_SECOND / fps)


def us_to_frame(us: int, fps: Fraction) -> int:
    """Ближайшая к моменту граница кадра."""
    return round_half_up(Fraction(us) * fps / US_PER_SECOND)


def frame_to_sample(frame: int, fps: Fraction, sample_rate: int) -> int:
    """Отсчёт звука, на который приходится начало кадра."""
    return round_half_up(Fraction(frame) * sample_rate / fps)


def frame_at(us: int, fps: Fraction) -> int:
    """Кадр, который виден в момент us (а не ближайшая граница кадра)."""
    return floor(Fraction(us) * fps / US_PER_SECOND)
