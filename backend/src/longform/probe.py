"""Свойства медиафайла через ffprobe.

Метаданным контейнера целиком не верим. Частота кадров в заголовке может
быть округлённой, а запись экрана часто идёт с плавающей частотой. Поэтому
читаем время каждого кадра и проверяем, что кадр k действительно стоит на
k / fps. От этого зависит, совпадёт ли номер кадра при выборе кадров
со временем звука.
"""

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from longform.edl import SourceInfo
from longform.ffmpeg import run
from longform.timebase import parse_rate


class UnsupportedMedia(ValueError):
    pass


@dataclass(frozen=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    pix_fmt: str
    fps: Fraction
    time_base: Fraction
    start: Fraction  # секунды, время первого показываемого кадра
    frame_count: int
    is_cfr: bool
    # Насколько самый «гуляющий» кадр отходит от сетки k / fps, в кадрах
    max_jitter_frames: Fraction


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    sample_rate: int
    channels: int
    start: Fraction  # время первого раскодированного отсчёта


@dataclass(frozen=True)
class ProbeResult:
    path: Path
    size_bytes: int
    duration: Fraction
    container_start: Fraction
    video: VideoStream
    audio: AudioStream | None

    def source_info(self, asset_id: str) -> SourceInfo:
        if not self.video.is_cfr:
            raise UnsupportedMedia(
                f"{self.path.name}: плавающая частота кадров (кадры отходят от сетки "
                f"до {float(self.video.max_jitter_frames):.2f} кадра). "
                "Такие файлы пока не поддерживаются"
            )
        a = self.audio
        return SourceInfo(
            asset_id=asset_id,
            fps=self.video.fps,
            frame_count=self.video.frame_count,
            sample_rate=a.sample_rate if a else None,
            channels=a.channels if a else 0,
            audio_offset=(a.start - self.video.start) if a else Fraction(0),
        )


def _stream_start(stream: dict) -> Fraction:
    tb = Fraction(stream["time_base"])
    if stream.get("start_pts") not in (None, "N/A"):
        return Fraction(int(stream["start_pts"])) * tb
    return Fraction(stream.get("start_time", "0"))


def _frame_timing(path: Path, fps: Fraction, tb: Fraction) -> tuple[int, Fraction, bool, Fraction]:
    """Число кадров, время первого кадра, постоянна ли частота и наибольшее
    отклонение от сетки k / fps (в кадрах).

    Пакеты с флагом D контейнер велит выбросить после декодирования (так
    обрезают начало через edit list). Их не считаем, иначе номер кадра
    при выборе кадров разошёлся бы с временем.
    """
    out = run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts,flags", "-of", "csv=p=0", str(path)]
    )
    pts = []
    for line in out.decode().splitlines():
        value, _, flags = line.partition(",")
        if value in ("", "N/A") or "D" in flags:
            continue
        pts.append(int(value))
    pts.sort()
    if not pts:
        raise UnsupportedMedia(f"{path.name}: в видео нет кадров с временем")
    step = 1 / (fps * tb)  # длительность кадра в тиках шкалы потока
    jitter = max(abs(p - pts[0] - i * step) for i, p in enumerate(pts))
    return len(pts), pts[0] * tb, is_constant_rate(jitter, step), jitter / step


# Отметки времени могут отходить от сетки k / fps из-за округления при
# записи: OBS пишет 60 к/с с отклонением до 1/50 кадра (0,3 мс), и это не
# накапливается. Кадр k всё равно однозначно k-й, а склейка выбирает кадры
# по номеру. Пропущенный или лишний кадр сдвигает все следующие на целый
# кадр — такое отклонение остаётся плавающей частотой.
CFR_TOLERANCE_FRAMES = Fraction(1, 4)


def is_constant_rate(jitter_ticks: Fraction, step_ticks: Fraction) -> bool:
    """Частота постоянна, если ни одна отметка не ушла от сетки дальше
    четверти кадра (или одного тика, если шкала грубее)."""
    return jitter_ticks <= max(1, step_ticks * CFR_TOLERANCE_FRAMES)


def _first_audio_time(path: Path, fallback: Fraction) -> Fraction:
    """Время первого раскодированного отсчёта звука.

    Заголовок потока тут врёт: у AAC в начале 1024 служебных отсчёта.
    Если контейнер велит их выбросить, первый кадр после декодирования
    начинается позже, чем говорит заголовок, если не велит, они звучат.
    Извлечение WAV начинается ровно с этого кадра, поэтому берём его время.
    """
    out = run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-read_intervals", "%+#1",
         "-show_entries", "frame=pts,time_base", "-show_entries", "stream=time_base",
         "-of", "json", str(path)]
    )
    info = json.loads(out)
    frames = info.get("frames") or []
    if not frames or frames[0].get("pts") in (None, "N/A"):
        return fallback
    tb = Fraction(info["streams"][0]["time_base"])
    return int(frames[0]["pts"]) * tb


def probe(path: Path) -> ProbeResult:
    path = Path(path)
    if not path.is_file():
        raise UnsupportedMedia(f"файл не найден: {path}")
    info = json.loads(
        run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)])
    )
    streams = info.get("streams", [])
    videos = [
        s for s in streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos:
        raise UnsupportedMedia(f"{path.name}: нет видеопотока")
    v = videos[0]
    fps = parse_rate(v["r_frame_rate"])
    tb = Fraction(v["time_base"])
    frame_count, video_start, is_cfr, jitter = _frame_timing(path, fps, tb)
    video = VideoStream(
        index=v["index"],
        codec=v.get("codec_name", "?"),
        width=v["width"],
        height=v["height"],
        pix_fmt=v.get("pix_fmt", "?"),
        fps=fps,
        time_base=tb,
        start=video_start,
        frame_count=frame_count,
        is_cfr=is_cfr,
        max_jitter_frames=jitter,
    )
    audio = None
    if audios:
        a = audios[0]
        audio = AudioStream(
            index=a["index"],
            codec=a.get("codec_name", "?"),
            sample_rate=int(a["sample_rate"]),
            channels=int(a["channels"]),
            start=_first_audio_time(path, _stream_start(a)),
        )
    fmt = info["format"]
    return ProbeResult(
        path=path,
        size_bytes=int(fmt.get("size", path.stat().st_size)),
        duration=Fraction(fmt.get("duration", "0")),
        container_start=Fraction(fmt.get("start_time", "0")),
        video=video,
        audio=audio,
    )


_CACHE: dict[tuple[str, int, int], ProbeResult] = {}


def probe_cached(path: Path) -> ProbeResult:
    """probe с памятью по (путь, размер, время изменения): часовая запись
    читается по кадрам секунды, а сервер спрашивает её на каждый лист кадров.
    Изменился файл — изменился ключ, значит прочитаем заново."""
    path = Path(path)
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _CACHE:
        if len(_CACHE) > 32:
            _CACHE.clear()
        _CACHE[key] = probe(path)
    return _CACHE[key]
