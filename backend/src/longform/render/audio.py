"""Звук монтажа с точностью до отсчёта.

Звук исходника один раз извлекается в WAV. Дальше монтаж собирается
обычным кодом: куски копируются байт в байт, а на каждом стыке делается
короткое сведение (10 мс), чтобы склейка посреди волны не щёлкала.
Сжатие в AAC происходит один раз, в самом конце, поэтому служебные
отсчёты кодировщика не накапливаются от куска к куску.

Сведение центрировано на стыке и берёт по 5 мс «за краем» обоих кусков,
поэтому длина ролика не меняется и звук не сдвигается относительно картинки.
"""

import math
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from longform.edl import CompiledEdl, PlacedClip
from longform.ffmpeg import QUIET, CancelCheck, Cancelled, run

CROSSFADE = Fraction(1, 100)  # секунды
CHUNK = 1 << 20  # отсчётов за раз при копировании: память не растёт с длиной видео


def extract_wav(src: Path, dst: Path, stream_index: int, cancel: CancelCheck | None = None) -> None:
    """Весь звук исходника в WAV 16 бит. Начинается с первого раскодированного
    отсчёта: это и есть ProbeResult.audio.start.

    Пишется во временный файл и переименовывается в конце: недописанный WAV
    не должен выглядеть готовым кешем.
    """
    part = dst.with_name(dst.name + ".part")
    part.unlink(missing_ok=True)
    try:
        run(["ffmpeg", *QUIET, "-n", "-i", str(src), "-map", f"0:{stream_index}",
             "-c:a", "pcm_s16le", "-rf64", "auto", "-f", "wav", str(part)], cancel=cancel)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.rename(dst)


class WavReader:
    """shift — отсчёт файла, который читается как отсчёт 0 (звук камеры на
    времени записи экрана, см. camera_audio)."""

    def __init__(self, path: Path, shift: int = 0):
        self.shift = shift
        self._w = wave.open(str(path), "rb")
        if self._w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: ожидался WAV 16 бит")
        self.channels = self._w.getnchannels()
        self.sample_rate = self._w.getframerate()
        self.length = self._w.getnframes()

    def close(self) -> None:
        self._w.close()

    def read_raw(self, start: int, count: int) -> bytes:
        """Отсчёты [start, start + count); за пределами записи тишина."""
        start += self.shift
        frame = 2 * self.channels
        before = min(max(-start, 0), count)
        a = max(start, 0)
        b = min(start + count, self.length)
        body = b""
        if b > a:
            self._w.setpos(a)
            body = self._w.readframes(b - a)
        after = count - before - len(body) // frame
        return b"\0" * (before * frame) + body + b"\0" * (after * frame)

    def read(self, start: int, count: int) -> np.ndarray:
        raw = np.frombuffer(self.read_raw(start, count), "<i2")
        return raw.reshape(-1, self.channels).astype(np.float32) / 32768


@dataclass(frozen=True)
class _Run:
    """Куски, идущие в исходнике подряд, звучат как один непрерывный кусок:
    на таком «стыке» сводить нечего, а сведение дало бы лишний всплеск."""

    asset_id: str
    out_start: int
    out_end: int
    src_start: int

    def src_at(self, out_sample: int) -> int:
        return self.src_start + out_sample - self.out_start


def _runs(clips: tuple[PlacedClip, ...]) -> list[_Run]:
    runs: list[_Run] = []
    prev: PlacedClip | None = None
    for c in clips:
        if prev and prev.asset_id == c.asset_id and prev.src_end_frame == c.src_start_frame:
            last = runs[-1]
            runs[-1] = _Run(last.asset_id, last.out_start, c.out_end_sample, last.src_start)
        else:
            runs.append(_Run(c.asset_id, c.out_start_sample, c.out_end_sample, c.src_start_sample))
        prev = c
    return runs


def _to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 32767 / 32768) * 32768).round().astype("<i2").tobytes()


def camera_audio(main, cam, offset_us: int, drift_ppm: float, sr: int, workdir: Path,
                 cancel: CancelCheck | None = None) -> tuple[Path, int]:
    """Звук камеры на времени записи экрана: (WAV, сдвиг в отсчётах).

    Отсчёт i звука записи экрана — момент main = i/sr + (звук − видео экрана);
    камера в этот момент: cam = offset + main·(1 + уплывание). Уплывание
    убирается пересчётом частоты (asetrate) — дальше остаётся постоянный сдвиг,
    который WavReader применяет при чтении. Так звук камеры встаёт в монтаж
    записи экрана отсчёт в отсчёт, без нового выравнивания.
    """
    s = 1 + drift_ppm / 1e6
    main_ao = main.audio.start - main.video.start
    cam_ao = cam.audio.start - cam.video.start
    shift = round(((float(main_ao) * s + offset_us / 1e6 - float(cam_ao)) * sr) / s)
    dst = workdir / f"camera-audio-{cam.path.stem}-{sr}-{drift_ppm:.3f}.wav"
    if not dst.exists():
        part = dst.with_name(dst.name + ".part")
        part.unlink(missing_ok=True)
        rate = f"asetrate={cam.audio.sample_rate * s:.6f}," if drift_ppm else ""
        try:
            run(["ffmpeg", *QUIET, "-n", "-i", str(cam.path), "-map", f"0:{cam.audio.index}",
                 "-af", f"{rate}aresample={sr}", "-c:a", "pcm_s16le", "-rf64", "auto", "-f", "wav", str(part)],
                cancel=cancel)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.rename(dst)
    return dst, shift


def build_edited_wav(edl: CompiledEdl, wavs: dict[str, Path], dst: Path,
                     crossfade: Fraction = CROSSFADE, cancel: CancelCheck | None = None,
                     shifts: dict[str, int] | None = None) -> None:
    """crossfade = 0 — жёсткая склейка (для сравнения до и после).
    cancel проверяется между порциями: часовой звук собирается секунды.
    shifts — сдвиг чтения по файлам (звук камеры вместо звука экрана)."""
    sr = edl.profile.sample_rate
    readers = {asset: WavReader(path, (shifts or {}).get(asset, 0)) for asset, path in wavs.items()}
    try:
        channels = {r.channels for r in readers.values()}
        if len(channels) != 1 or any(r.sample_rate != sr for r in readers.values()):
            raise ValueError("у исходников разная частота или число каналов звука, пока не поддерживается")
        (ch,) = channels
        half = round(crossfade * sr / 2)
        runs = _runs(edl.clips)
        # Половина сведения на каждом стыке, но не больше четверти куска
        halves = [
            min(half, (a.out_end - a.out_start) // 4, (b.out_end - b.out_start) // 4)
            for a, b in zip(runs, runs[1:])
        ]
        with wave.open(str(dst), "wb") as out:
            out.setnchannels(ch)
            out.setsampwidth(2)
            out.setframerate(sr)
            for i, run_ in enumerate(runs):
                reader = readers[run_.asset_id]
                h_in = halves[i - 1] if i > 0 else 0
                h_out = halves[i] if i < len(halves) else 0
                # Тело куска: копия байт в байт
                pos, end = run_.out_start + h_in, run_.out_end - h_out
                while pos < end:
                    if cancel and (reason := cancel()):
                        raise Cancelled(reason)
                    n = min(CHUNK, end - pos)
                    out.writeframes(reader.read_raw(run_.src_at(pos), n))
                    pos += n
                if h_out:
                    nxt = runs[i + 1]
                    join = run_.out_end
                    tail = reader.read(run_.src_at(join - h_out), 2 * h_out)
                    head = readers[nxt.asset_id].read(nxt.src_at(join - h_out), 2 * h_out)
                    t = (np.arange(2 * h_out, dtype=np.float32) + 0.5) / (2 * h_out)
                    # Равная мощность: для несвязанных звуков громкость не проседает
                    fade_out = np.cos(t * math.pi / 2)[:, None]
                    fade_in = np.sin(t * math.pi / 2)[:, None]
                    out.writeframes(_to_pcm(tail * fade_out + head * fade_in))
    finally:
        for r in readers.values():
            r.close()
