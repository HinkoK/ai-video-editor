"""Проверка готового ролика по исходнику, без вшитых в картинку номеров.

В синтетике номер кадра нарисован в самом кадре. В настоящей записи его нет,
поэтому для выборки мест (стыки, начало, середина, конец) сравниваем:

- кадр результата с кадрами исходника вокруг ожидаемого: лучшим должен
  оказаться именно ожидаемый. Если соседние кадры неотличимы (статичный
  экран), честно пишем «не различить», а не «верно»;
- звук результата со звуком исходника: сдвиг по взаимной корреляции,
  на 8 кГц, точность 0,125 мс.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np

from longform.edl import CompiledEdl
from longform.ffmpeg import QUIET, CancelCheck, run
from longform.probe import ProbeResult
from longform.timebase import round_half_up

W, H = 96, 54  # маленькие серые кадры: сравнение по структуре, не по деталям
LOW_RATE = 8000
RADIUS = 2  # сколько соседних кадров исходника сравнивать с каждой стороны
TIE = 0.5  # разница яркости (из 255), которую считаем неразличимой


@dataclass
class Point:
    out_frame: int
    asset_id: str
    src_frame: int
    video: str  # "ok" | "offset:N" | "ambiguous" | "composed" (в кадре лицо или приближение — не сравниваем)
    audio_lag_ms: float | None = None


@dataclass
class Report:
    points: list[Point] = field(default_factory=list)

    @property
    def video_mismatches(self) -> list[Point]:
        return [p for p in self.points if p.video.startswith("offset")]

    @property
    def audio_mismatches(self) -> list[Point]:
        return [p for p in self.points if p.audio_lag_ms is not None and abs(p.audio_lag_ms) > 0.25]

    @property
    def ok(self) -> bool:
        return not self.video_mismatches and not self.audio_mismatches

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "points": len(self.points),
            "video_mismatches": [p.__dict__ for p in self.video_mismatches],
            "audio_mismatches": [p.__dict__ for p in self.audio_mismatches],
            "video_ambiguous": sum(p.video == "ambiguous" for p in self.points),
            "video_composed": sum(p.video == "composed" for p in self.points),
            "audio_checked": sum(p.audio_lag_ms is not None for p in self.points),
        }


def sample_frames(edl: CompiledEdl, max_joints: int = 20, must: Iterable[int] = ()) -> list[int]:
    """Кадры по обе стороны выбранных стыков плюс начало, середина и конец.

    must — стыки, которые в выборке всегда (стыки перестановок):
    там сборка прыгает по исходнику, ошибка там дороже всего."""
    joints = [c.out_start_frame for c in edl.clips[1:]]
    if len(joints) > max_joints:
        step = len(joints) / max_joints
        joints = [joints[int(i * step)] for i in range(max_joints)]
    frames = {0, edl.total_frames // 2, edl.total_frames - 1}
    for j in [*joints, *must]:
        frames.update({j - 1, j})
    return sorted(f for f in frames if 0 <= f < edl.total_frames)


def _frames_expression(frames: list[int]) -> str:
    def node(lo: int, hi: int) -> str:
        if hi - lo == 1:
            return f"eq(n,{frames[lo]})"
        mid = (lo + hi) // 2
        return f"if(lt(n,{frames[mid]}),{node(lo, mid)},{node(mid, hi)})"
    return node(0, len(frames))


def _gray(raw: bytes) -> list[np.ndarray]:
    a = np.frombuffer(raw, np.uint8)
    return [f.astype(np.float32) for f in a.reshape(-1, H, W)]


def _output_frames(path: Path, frames: list[int], cancel: CancelCheck | None) -> list[np.ndarray]:
    raw = run(["ffmpeg", *QUIET, "-i", str(path), "-map", "0:v:0",
               "-vf", f"select='{_frames_expression(frames)}',scale={W}:{H}",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], cancel=cancel)
    return _gray(raw)


def _source_frames(src: ProbeResult, first: int, count: int, cancel: CancelCheck | None) -> list[np.ndarray]:
    fps = src.video.fps
    seek = []
    if first > 0:
        target = src.video.start + Fraction(2 * first - 1, 2) / fps - src.container_start
        seek = ["-ss", f"{float(target):.6f}"]
    raw = run(["ffmpeg", *QUIET, *seek, "-i", str(src.path), "-map", "0:v:0", "-frames:v", str(count),
               "-vf", f"scale={W}:{H}", "-fps_mode", "passthrough",
               "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], cancel=cancel)
    return _gray(raw)


def _low_audio(path: Path, cancel: CancelCheck | None) -> np.ndarray:
    raw = run(["ffmpeg", *QUIET, "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(LOW_RATE),
               "-f", "s16le", "pipe:1"], cancel=cancel)
    return np.frombuffer(raw, "<i2").astype(np.float32)


def _lag(seg: np.ndarray, ref: np.ndarray) -> tuple[int, float]:
    """Где в ref лучше всего совпадает seg: (сдвиг, сходство от −1 до 1).

    Корреляция нормируется на громкость окна ref при каждом сдвиге: иначе
    максимум тянется к более громкому месту исходника, и на голосе (период
    основного тона 3–10 мс) выходил ложный сдвиг на период — найдено 13.10
    на стыке перестановки, где на 48 кГц совпадение точное."""
    n = len(ref) + len(seg)
    nfft = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(ref, nfft) * np.conj(np.fft.rfft(seg, nfft)), nfft)[: len(ref) - len(seg) + 1]
    sq = np.concatenate([[0.0], np.cumsum(ref.astype(np.float64) ** 2)])
    energy = np.sqrt(np.maximum(sq[len(seg):] - sq[: len(sq) - len(seg)], 0.0))
    similarity = corr / (np.linalg.norm(seg) * energy + 1e-9)
    best = int(np.argmax(similarity))
    return best, float(similarity[best])


def _audio_lag_near(clip, j: int, fps, sr: int, out_audio: np.ndarray, src_audio: np.ndarray) -> float | None:
    """Сдвиг звука (мс) в окне 100 мс с речью рядом с кадром j внутри куска.

    Стыки специально стоят в тишине, а по тишине сдвиг не измерить. Поэтому
    в пределах 1,5 с от кадра (внутри куска, в стороне от сведения) берём
    окно, которое целиком в речи: у него самая громкая из «самых тихих
    четвертей». Окно на границе тишины и слова ненадёжно: AAC размазывает
    резкое начало звука назад, и сравнение даёт ложный сдвиг в десятки мс.
    Нет речи рядом — None, а не «верно».
    """
    win, step, margin = LOW_RATE // 10, LOW_RATE // 40, LOW_RATE // 20
    lo = (clip.out_start_sample + sr // 50) * LOW_RATE // sr
    hi = (clip.out_end_sample - sr // 50) * LOW_RATE // sr - win
    center = round_half_up(Fraction(j) * LOW_RATE / fps)
    lo, hi = max(lo, center - 3 * LOW_RATE // 2), min(hi, center + 3 * LOW_RATE // 2)
    if hi <= lo:
        return None
    best, best_energy = None, 0.0
    quarter = win // 4
    for o in range(lo, hi + 1, step):
        seg = out_audio[o: o + win]
        if len(seg) == win:
            energy = min(float(seg[k * quarter:(k + 1) * quarter].std()) for k in range(4))
            if energy > best_energy:
                best, best_energy = o, energy
    if best is None or best_energy < 100:
        return None
    o = best
    s = (clip.src_start_sample + o * sr // LOW_RATE - clip.out_start_sample) * LOW_RATE // sr
    seg = out_audio[o: o + win]
    ref = src_audio[max(0, s - margin): s + win + margin]
    if s - margin < 0 or len(ref) != win + 2 * margin:
        return None
    pos, strength = _lag(seg, ref)
    return round((pos - margin) / LOW_RATE * 1000, 3) if strength > 0.6 else None


def verify_output(
    edl: CompiledEdl, media: dict[str, ProbeResult], output: Path, *,
    max_joints: int = 20, cancel: CancelCheck | None = None, plain=None,
    audio: dict[str, tuple[Path, int]] | None = None, must: Iterable[int] = (),
) -> Report:
    """plain(кадр) — показан ли в этом кадре ролика экран как есть. Кадры с
    лицом или приближением с записью экрана не сравниваются: картинка там
    другая по замыслу; их точность проверяют тесты раскладки по номерам
    кадров. Звук сверяется везде. audio — {запись экрана: (WAV, сдвиг)}:
    в ролике звук камеры, сверяем с ним (render/audio.camera_audio). must —
    стыки, которые сверяются всегда (sample_frames)."""
    report = Report()
    frames = sample_frames(edl, max_joints, must)
    out_frames = dict(zip(frames, _output_frames(output, frames, cancel)))
    # Только файлы, которые есть в версии: проект может содержать и вырезанные целиком
    used = {c.asset_id for c in edl.clips}
    out_audio = _low_audio(output, cancel) if all(media[a].audio for a in used) else None
    src_audio: dict[str, np.ndarray] = {}
    fps = edl.profile.fps
    sr = edl.profile.sample_rate

    for j in frames:
        clip_id, asset, k = edl.edited_to_source(j)
        src = media[asset]
        if plain is not None and not plain(j):
            point = Point(j, asset, k, "composed")
        else:
            first = max(0, k - RADIUS)
            count = min(src.video.frame_count, k + RADIUS + 1) - first
            candidates = _source_frames(src, first, count, cancel)
            diffs = [float(np.abs(c - out_frames[j]).mean()) for c in candidates]
            at_k = diffs[k - first]
            best = int(np.argmin(diffs))
            if max(diffs) - min(diffs) <= TIE:
                verdict = "ambiguous"
            elif at_k <= diffs[best] + TIE:
                verdict = "ok"
            else:
                verdict = f"offset:{first + best - k}"
            point = Point(j, asset, k, verdict)

        if out_audio is not None:
            if asset not in src_audio:
                if audio and asset in audio:
                    wav, shift = audio[asset]
                    low = _low_audio(wav, cancel)
                    k = round(shift * LOW_RATE / sr)
                    # Сдвиг чтения: отсчёт k файла камеры — отсчёт 0 звука экрана
                    src_audio[asset] = low[k:] if k >= 0 else np.concatenate([np.zeros(-k, np.float32), low])
                else:
                    src_audio[asset] = _low_audio(src.path, cancel)
            clip = next(c for c in edl.clips if c.clip_id == clip_id)
            point.audio_lag_ms = _audio_lag_near(clip, j, fps, sr, out_audio, src_audio[asset])
        report.points.append(point)
    return report
