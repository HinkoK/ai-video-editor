"""Монтажный лист: какие куски исходника и в каком порядке входят в ролик.

Clip — то, что хранится и приходит от агента: кусок исходника в микросекундах.
compile_edl проверяет лист и раскладывает его на выходные кадры и отсчёты
звука. Результат (CompiledEdl) — единственный источник правды для рендера
и для пересчёта времени: пробный рендер и экспорт берут его одинаково.
"""

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction

from longform.timebase import frame_to_sample, frame_to_us, round_half_up, us_to_frame

# Кусок короче двух кадров на слух — щелчок, а не речь. Заодно гарантирует,
# что на стыке хватит места для короткого сведения звука.
MIN_CLIP_FRAMES = 2


class EdlError(ValueError):
    """Ошибка монтажного листа. code — для программы, message — для человека."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceInfo:
    """Что монтажному листу нужно знать об исходнике (берётся из probe)."""

    asset_id: str
    fps: Fraction
    frame_count: int
    sample_rate: int | None  # None, если в файле нет звука
    channels: int
    # На сколько секунд звук начинается позже первого кадра (может быть < 0)
    audio_offset: Fraction


@dataclass(frozen=True)
class OutputProfile:
    fps: Fraction
    sample_rate: int


@dataclass(frozen=True)
class Clip:
    """Кусок исходника [src_start_us, src_end_us). Время от первого кадра."""

    id: str
    asset_id: str
    src_start_us: int
    src_end_us: int


@dataclass(frozen=True)
class PlacedClip:
    """Кусок, разложенный на выходной ролик. Все интервалы полуоткрытые."""

    clip_id: str
    asset_id: str
    src_start_frame: int
    src_end_frame: int
    out_start_frame: int
    out_end_frame: int
    out_start_sample: int
    out_end_sample: int
    # Отсчёт исходного звука, который звучит на out_start_sample.
    # Может быть < 0 или за концом звука: там тишина.
    src_start_sample: int


@dataclass(frozen=True)
class CompiledEdl:
    profile: OutputProfile
    clips: tuple[PlacedClip, ...]
    sources: Mapping[str, SourceInfo] = field(repr=False, compare=False)

    @property
    def total_frames(self) -> int:
        return self.clips[-1].out_end_frame

    @property
    def total_samples(self) -> int:
        return self.clips[-1].out_end_sample

    def _clip_at(self, out_frame: int) -> PlacedClip:
        if not 0 <= out_frame < self.total_frames:
            raise IndexError(f"кадр {out_frame} вне ролика длиной {self.total_frames}")
        starts = [c.out_start_frame for c in self.clips]
        return self.clips[bisect_right(starts, out_frame) - 1]

    def edited_to_source(self, out_frame: int) -> tuple[str, str, int]:
        """Кадр монтажа -> (id куска, id исходника, кадр исходника)."""
        c = self._clip_at(out_frame)
        return c.clip_id, c.asset_id, c.src_start_frame + out_frame - c.out_start_frame

    def source_to_edited(self, asset_id: str, src_frame: int) -> int | None:
        """Кадр исходника -> кадр монтажа, или None, если он вырезан."""
        for c in self.clips:
            if c.asset_id == asset_id and c.src_start_frame <= src_frame < c.src_end_frame:
                return c.out_start_frame + src_frame - c.src_start_frame
        return None

    def slice(self, out_start: int, out_end: int) -> "CompiledEdl":
        """Часть монтажа [out_start, out_end) как отдельный ролик с нуля.

        Нужна для пробного рендера: тот же лист, только короче.
        """
        if not 0 <= out_start < out_end <= self.total_frames:
            raise EdlError(
                "bad_range",
                f"диапазон {out_start}–{out_end} вне ролика длиной {self.total_frames} кадров",
            )
        clips = []
        for c in self.clips:
            a = max(c.out_start_frame, out_start)
            b = min(c.out_end_frame, out_end)
            if a >= b:
                continue
            clips.append(
                (c.clip_id, c.asset_id, c.src_start_frame + a - c.out_start_frame,
                 c.src_start_frame + b - c.out_start_frame)
            )
        return _place(clips, self.profile, self.sources)


def compile_edl(
    clips: list[Clip], sources: Mapping[str, SourceInfo], profile: OutputProfile
) -> CompiledEdl:
    if not clips:
        raise EdlError("empty", "в монтаже нет ни одного куска")

    seen_ids: set[str] = set()
    frames: list[tuple[str, str, int, int]] = []
    for clip in clips:
        src = sources.get(clip.asset_id)
        if src is None:
            raise EdlError("unknown_asset", f"кусок {clip.id}: исходник {clip.asset_id} не найден")
        if src.fps != profile.fps:
            raise EdlError(
                "fps_mismatch",
                f"кусок {clip.id}: частота кадров исходника {src.fps} не совпадает "
                f"с частотой проекта {profile.fps}. Приведение частот пока не поддерживается",
            )
        start = us_to_frame(clip.src_start_us, src.fps)
        end = us_to_frame(clip.src_end_us, src.fps)
        if (frame_to_us(start, src.fps), frame_to_us(end, src.fps)) != (
            clip.src_start_us,
            clip.src_end_us,
        ):
            raise EdlError(
                "off_grid",
                f"кусок {clip.id}: границы не на границе кадра. "
                "Границы выравнивает сервер, сюда должно приходить уже выровненное",
            )
        if start < 0 or end > src.frame_count:
            raise EdlError(
                "out_of_bounds",
                f"кусок {clip.id}: кадры {start}–{end} вне исходника "
                f"длиной {src.frame_count} кадров",
            )
        if end - start < MIN_CLIP_FRAMES:
            raise EdlError(
                "too_short",
                f"кусок {clip.id}: {end - start} кадр(ов), минимум {MIN_CLIP_FRAMES}",
            )
        if clip.id in seen_ids:
            raise EdlError("duplicate_id", f"кусок {clip.id} встречается дважды")
        seen_ids.add(clip.id)
        frames.append((clip.id, clip.asset_id, start, end))

    _check_no_overlap(frames)
    return _place(frames, profile, sources)


def _check_no_overlap(frames: list[tuple[str, str, int, int]]) -> None:
    """Один и тот же кадр исходника не может попасть в ролик дважды.

    Иначе пересчёт «кадр исходника -> кадр монтажа» стал бы неоднозначным,
    и комментарий к исходнику не знал бы, к какому месту монтажа относится.
    """
    by_asset: dict[str, list[tuple[int, int, str]]] = {}
    for cid, asset, start, end in frames:
        by_asset.setdefault(asset, []).append((start, end, cid))
    for ranges in by_asset.values():
        ranges.sort()
        for (_, prev_end, prev_id), (start, _, cid) in zip(ranges, ranges[1:]):
            if start < prev_end:
                raise EdlError("overlap", f"куски {prev_id} и {cid} берут одни и те же кадры")


def _place(
    frames: list[tuple[str, str, int, int]],
    profile: OutputProfile,
    sources: Mapping[str, SourceInfo],
) -> CompiledEdl:
    placed = []
    out = 0
    sr = profile.sample_rate
    for cid, asset, start, end in frames:
        src = sources[asset]
        out_end = out + end - start
        # Время кадра start в шкале звука исходника, переведённое в отсчёты
        src_sample = round_half_up(
            (Fraction(start) / src.fps - src.audio_offset) * sr
        )
        placed.append(
            PlacedClip(
                clip_id=cid,
                asset_id=asset,
                src_start_frame=start,
                src_end_frame=end,
                out_start_frame=out,
                out_end_frame=out_end,
                out_start_sample=frame_to_sample(out, profile.fps, sr),
                out_end_sample=frame_to_sample(out_end, profile.fps, sr),
                src_start_sample=src_sample,
            )
        )
        out = out_end
    return CompiledEdl(profile=profile, clips=tuple(placed), sources=sources)
