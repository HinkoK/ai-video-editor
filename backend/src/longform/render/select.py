"""Способ Б: один проход по оригиналу.

Видео: ffmpeg читает исходник один раз и пропускает дальше только кадры
с нужными номерами (фильтр select), затем расставляет им время заново:
кадр N выходного ролика стоит ровно на N / fps. Звук собирается отдельно
(render/audio.py) и подкладывается готовым WAV. Сжатие одно, на выходе.

Для пробного рендера диапазона используется тот же код: CompiledEdl.slice()
даёт короткий монтажный лист, а перед чтением делается точная перемотка
к первому нужному кадру.

Несколько файлов подряд: идущие подряд куски одного файла образуют серию.
У каждой серии свой вход ffmpeg со своей перемоткой и выбором кадров,
серии склеиваются фильтром concat. Камера, потом экран — это две серии.
"""

import functools
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, lcm
from pathlib import Path

from longform.edl import CompiledEdl, EdlError, PlacedClip
from longform.ffmpeg import QUIET, CancelCheck, ensure_free_space, run
from longform.probe import ProbeResult, probe
from longform.render.audio import CROSSFADE, build_edited_wav, camera_audio, extract_wav
from longform.timebase import us_to_frame


@dataclass(frozen=True)
class EncodeSettings:
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 18
    pix_fmt: str = "yuv420p"
    audio_bitrate: str = "192k"
    # Сведение звука на стыках; 0 — жёсткая склейка, для сравнения до и после
    crossfade: Fraction = CROSSFADE
    # Высота кадра (ширина по пропорции); None — как у исходника
    height: int | None = None
    # Опорный кадр не реже чем раз в столько секунд: браузер перематывает
    # только к опорным кадрам, и редкие опорные делают перемотку медленной
    keyframe_every: Fraction | None = None
    # Выровнять громкость до стольких LUFS одним усилением (render/loudness.py);
    # None — не трогать
    loudness_lufs: float | None = None


# Финальный экспорт: из оригинала, в полном разрешении
FINAL = EncodeSettings()
# Лёгкая копия для просмотра в браузере: время то же, что у исходника
PROXY = EncodeSettings(crf=28, audio_bitrate="128k", height=540, keyframe_every=Fraction(1))
# Пробный ролик стыка: точный, но лёгкий
PREVIEW = EncodeSettings(crf=23, height=720, keyframe_every=Fraction(1))


@dataclass(frozen=True)
class RenderResult:
    path: Path
    frames: int
    seconds: float
    temp_bytes: int  # сколько промежуточных файлов лежало на диске в пике
    loudness: dict | None = None  # что сделано с громкостью, для отчёта


class RenderCheckError(RuntimeError):
    """ffmpeg завершился без ошибки, а результат не тот. Так тоже бывает."""


def track_timescale(fps) -> int:
    """Шкала времени дорожки mp4, в которой длительность кадра — целое число.

    Для 30000/1001 это 30000 (кадр = 1001 тик), для 25 — 10000 (кадр = 400).
    """
    return fps.numerator * ceil(10_000 / fps.numerator)


def select_expression(clips: list[PlacedClip], first_frame: int) -> str:
    """Выражение для select: оставить кадр n, если он входит в какой-то кусок.

    Номера считаются после перемотки (кадр first_frame становится n = 0).
    Цепочка «between(...)+between(...)+...» ломается на 101-м куске:
    ffmpeg не разбирает выражения глубже 100 уровней. Поэтому строим дерево
    поиска по началу куска: if(lt(n, середина), левая половина, правая).
    Глубина растёт как log2 от числа кусков, и на каждый кадр уходит
    около дюжины сравнений, а не по одному на каждый кусок.
    """
    ranges = [(c.src_start_frame - first_frame, c.src_end_frame - 1 - first_frame) for c in clips]

    def node(lo: int, hi: int) -> str:
        if hi - lo == 1:
            a, b = ranges[lo]
            return f"between(n,{a},{b})"
        mid = (lo + hi) // 2
        return f"if(lt(n,{ranges[mid][0]}),{node(lo, mid)},{node(mid, hi)})"

    return node(0, len(ranges))


# Каждая серия — отдельный декодер. Камера и экран — две серии; если их
# десятки, монтаж уже не чистка, и такой лист лучше собирать иначе.
MAX_RUNS = 16
# Прыжок вперёд по исходнику длиннее этого внутри серии — тоже новая серия
# со своей перемоткой: select раскодирует всё, что пропускает, и после
# перестановки (кусок из конца записи в середину) это минуты лишней работы
JUMP_SPLIT_US = 20_000_000


def split_runs(edl: CompiledEdl) -> list[list[PlacedClip]]:
    """Подряд идущие куски одного файла в порядке исходника — одна серия.

    select пропускает кадры только в порядке исходника, поэтому кусок,
    стоящий в исходнике раньше предыдущего, начинает новую серию. Прыжок
    вперёд длиннее JUMP_SPLIT_US тоже режет серию — самые длинные сначала,
    пока серий не больше MAX_RUNS.
    """
    runs: list[list[PlacedClip]] = []
    for c in edl.clips:
        prev = runs[-1][-1] if runs else None
        if prev and prev.asset_id == c.asset_id and c.src_start_frame >= prev.src_end_frame:
            runs[-1].append(c)
        else:
            runs.append([c])
    if len(runs) > MAX_RUNS:
        raise EdlError("too_many_runs", f"{len(runs)} переходов между файлами, больше {MAX_RUNS} не поддерживается")
    jumps = []
    for r, run_ in enumerate(runs):
        fps = edl.sources[run_[0].asset_id].fps if run_[0].asset_id in edl.sources else edl.profile.fps
        limit = us_to_frame(JUMP_SPLIT_US, fps)
        for k in range(1, len(run_)):
            gap = run_[k].src_start_frame - run_[k - 1].src_end_frame
            if gap > limit:
                jumps.append((-gap, r, k))
    cuts = {(r, k) for _, r, k in sorted(jumps)[:MAX_RUNS - len(runs)]}
    if not cuts:
        return runs
    out: list[list[PlacedClip]] = []
    for r, run_ in enumerate(runs):
        out.append([run_[0]])
        for k in range(1, len(run_)):
            if (r, k) in cuts:
                out.append([])
            out[-1].append(run_[k])
    return out


def check_single_source_in_order(edl: CompiledEdl) -> str:
    """Для способа А из замера: один файл, куски в порядке исходника."""
    runs = split_runs(edl)
    if len(runs) != 1:
        raise EdlError("multi_run", "способ А умеет только один файл в порядке исходника")
    return runs[0][0].asset_id


def _seek_args(src: ProbeResult, first: int, fps: Fraction) -> list[str]:
    if first == 0:
        return []
    # Перемотка на полкадра раньше нужного кадра: ffmpeg при точной
    # перемотке выбрасывает всё, что раньше указанного времени, и первым
    # останется именно кадр first. На mp4 и mkv он попадает в кадр и без
    # запаса (сам округляет время к шкале потока), запас — страховка для
    # контейнеров, которые округляют время кадров иначе.
    # -ss считается от начала контейнера, а не от первого кадра видео.
    target = src.video.start + Fraction(2 * first - 1, 2) / fps - src.container_start
    return ["-ss", f"{float(target):.6f}"]


# Запас чтения после последнего нужного кадра серии, секунды
READ_MARGIN = Fraction(1)


def input_args(src: ProbeResult, run_: list[PlacedClip], fps: Fraction) -> list[str]:
    """Перемотка к первому кадру серии и предел чтения: дальше её последнего
    кадра файл не читается. Без предела пробный ролик в 6 с из начала часовой
    записи читал её до конца — почти минуту (найдено на настоящей записи)."""
    first = run_[0].src_start_frame
    span = Fraction(run_[-1].src_end_frame - first, 1) / fps + READ_MARGIN
    return [*_seek_args(src, first, fps), "-t", f"{float(span):.6f}"]


def _read_progress(path: Path) -> int | None:
    """Последнее out_time_us из файла -progress ffmpeg."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if line.startswith("out_time_us="):
            value = line.split("=", 1)[1]
            return int(value) if value.lstrip("-").isdigit() else None
    return None


# Выше этого грубая оценка проверяется пробным сжатием: запись экрана OBS
# идёт в 21 Мбит/с, а сжимается в 0,9 — грубая оценка ошибалась в 30 раз и
# не давала собрать даже лёгкую копию при 14 ГБ свободных
SAMPLE_ABOVE_BYTES = 1 << 30
SAMPLE_SECONDS = 3
SAMPLE_POINTS = 6
SAMPLE_SAFETY = 3
# Пробы могут не попасть на тяжёлое место (прокрутка, видео на экране);
# поэтому не ниже десятой доли грубой оценки. Промах в разы всё равно
# покрывает запас места на диске (ffmpeg.RESERVE_BYTES)
SAMPLE_FLOOR = Fraction(1, 10)


def estimate_video_bytes(edl: CompiledEdl, media: dict[str, ProbeResult], settings: EncodeSettings,
                         workdir: Path, cancel: CancelCheck | None = None) -> int:
    """Сколько места займёт ролик, с запасом.

    Грубо — по битрейту исходников. Для длинной сборки этого мало: экран
    сжимается в десятки раз лучше, чем его пишет OBS. Тогда сжимаем по
    SAMPLE_SECONDS в SAMPLE_POINTS местах ролика с теми же настройками и
    берём самое тяжёлое место с запасом — между SAMPLE_FLOOR от грубой
    оценки и ею самой.
    """
    used = {c.asset_id for c in edl.clips}
    duration = edl.total_frames / edl.profile.fps
    rough = int(max(media[a].size_bytes / max(media[a].duration, 1) for a in used) * duration * 1.5)
    if rough <= SAMPLE_ABOVE_BYTES or duration < SAMPLE_SECONDS * SAMPLE_POINTS * 2:
        return rough
    worst = 0.0
    for share in (Fraction(k, SAMPLE_POINTS + 1) for k in range(1, SAMPLE_POINTS + 1)):
        frame = int(edl.total_frames * share)
        clip = next(c for c in edl.clips if c.out_start_frame <= frame < c.out_end_frame)
        src = media[clip.asset_id]
        at = (clip.src_start_frame + frame - clip.out_start_frame) / edl.profile.fps
        sample = workdir / f"size-sample-{uuid.uuid4().hex}.mp4"
        scale = ["-vf", f"scale=-2:'min({settings.height},ih)'"] if settings.height else []
        gop = ["-g", str(max(1, round(edl.profile.fps * settings.keyframe_every)))] if settings.keyframe_every else []
        try:
            run(["ffmpeg", *QUIET, "-y", "-ss", f"{float(src.video.start + at):.3f}", "-t", str(SAMPLE_SECONDS),
                 "-i", str(src.path), "-map", "0:v:0", "-an", *scale, "-c:v", settings.video_codec,
                 "-preset", settings.preset, "-crf", str(settings.crf), *gop, "-pix_fmt", settings.pix_fmt,
                 str(sample)], cancel=cancel)
            worst = max(worst, sample.stat().st_size / SAMPLE_SECONDS)
        finally:
            sample.unlink(missing_ok=True)
    audio = int(settings.audio_bitrate.rstrip("k")) * 1000 / 8 if settings.audio_bitrate.endswith("k") else 24_000
    sampled = int((worst * SAMPLE_SAFETY + audio) * duration)
    return min(rough, max(sampled, int(rough * SAMPLE_FLOOR)))


def _edited_audio(edl: CompiledEdl, media: dict[str, ProbeResult], used: set[str], workdir: Path,
                  settings: "EncodeSettings", cancel: CancelCheck | None,
                  audio_from: dict[str, tuple[str, int, float]] | None, temp_paths: list[Path]):
    """Звук монтажа одним WAV (и выравнивание громкости): (путь, отчёт о громкости)."""
    wavs, shifts = {}, {}
    for a in used:
        if audio_from and a in audio_from:
            cam_id, offset_us, drift = audio_from[a]
            wavs[a], shifts[a] = camera_audio(media[a], media[cam_id], offset_us, drift,
                                              edl.profile.sample_rate, workdir, cancel)
        else:
            wavs[a] = workdir / f"{a}.wav"
            if not wavs[a].exists():
                extract_wav(media[a].path, wavs[a], media[a].audio.index, cancel=cancel)
        temp_paths.append(wavs[a])
    edited_wav = workdir / f"edited-{uuid.uuid4().hex}.wav"
    build_edited_wav(edl, wavs, edited_wav, crossfade=settings.crossfade, cancel=cancel, shifts=shifts)
    temp_paths.append(edited_wav)
    loudness = None
    if settings.loudness_lufs is not None:
        from longform.render.loudness import normalize

        louder = workdir / f"loud-{uuid.uuid4().hex}.wav"
        temp_paths.append(louder)
        try:
            loudness = normalize(edited_wav, louder, settings.loudness_lufs, cancel)
        except BaseException:
            edited_wav.unlink(missing_ok=True)
            louder.unlink(missing_ok=True)
            raise
        if loudness["applied"]:
            # Звук до выравнивания больше не нужен — не держим его на диске
            edited_wav.unlink(missing_ok=True)
            edited_wav = louder
    return edited_wav, loudness


@functools.lru_cache(maxsize=64)
def _rate(path: str) -> Fraction:
    """Частота кадров клипа или фона."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return Fraction(out) if out and out != "0/0" else Fraction(30)


def _clip_input(path: str, at: float) -> tuple[list[str], str]:
    """Перемотка клипа (фона) на его кадр и фильтр, ставящий время кадров
    ровно на его сетку: перевод в частоту ролика (fps) тогда повторяет каждый
    кадр одинаково. Без этого перемотка между кадрами клипа (на полкадра)
    даёт неровные повторы — фон замирает и догоняет рывком (автор, 08.10)."""
    rate = _rate(path)
    k = int(Fraction(str(at)) * rate)
    seek = ["-ss", f"{float((k - Fraction(1, 2)) / rate):.6f}"] if k > 0 else []
    return seek, f"setpts=N*{rate.denominator}/({rate.numerator}*TB),"


@functools.lru_cache(maxsize=16)
def _duration(path: str) -> float:
    """Длительность файла (видео-фона), секунды."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 1.0


def _render_by_shots(edl: CompiledEdl, media: dict[str, ProbeResult], dst: Path, workdir: Path,
                     settings: "EncodeSettings", timeout: float | None, cancel: CancelCheck | None,
                     on_progress: Callable[[float], None] | None, visual: "VisualPlan",
                     overlays: list | None, overlay_fps: int,
                     audio_from: dict[str, tuple[str, int, float]] | None) -> RenderResult:
    """Ролик со слоем кадра — по участкам: каждый участок отдельной сборкой
    (в одном графе ветки участков копят кадры, пока собирается соседний, и
    часовой ролик не помещается в память), потом склейка без перекодирования
    и звук целиком поверх."""
    from dataclasses import replace

    from longform.render.compose import VisualPlan

    if dst.exists():
        raise FileExistsError(f"{dst} уже существует, готовые ролики не перезаписываются")
    started = time.monotonic()
    fps = edl.profile.fps
    total = edl.total_frames
    used = {c.asset_id for c in edl.clips}
    shots = visual.shots
    temp_paths: list[Path] = []
    parts: list[Path] = []
    tag = uuid.uuid4().hex
    edited_wav = loudness = None
    try:
        if all(media[a].audio for a in used):
            edited_wav, loudness = _edited_audio(edl, media, used, workdir, settings, cancel, audio_from, temp_paths)
        for i, sh in enumerate(shots):
            n = sh.end - sh.start
            one = replace(sh, start=0, end=n,
                          whip_in=sh.whip_in if sh.whip_in is not None else (sh.enter == "whip" and i > 0),
                          whip_out=sh.whip_out if sh.whip_out is not None
                          else (i + 1 < len(shots) and shots[i + 1].enter == "whip"))
            plan = VisualPlan(visual.width, visual.height, (one,), visual.cameras)
            # Графика этого участка: со своего кадра, с нужного рисунка
            own = []
            for o in overlays or []:
                frames_dir, start, count = o[0], o[1], o[2]
                first = o[3] if len(o) > 3 else 0
                opts = o[4] if len(o) > 4 else {}
                a, b = max(start, sh.start), min(start + count, sh.end)
                if a < b:
                    # Рисунки идут со своей частотой (overlay_fps), кадры ролика — со своей.
                    # Переход PAN считается от начала графики: в участке — сколько уже прошло;
                    # нарисованных кадров в участке — до конца рисунка, дальше держится последний
                    drawn = opts.get("drawn", count)
                    part_opts = {"pan": opts.get("pan", 0), "elapsed": a - start,
                                 "drawn": max(0, min(b, start + drawn) - a)}
                    own.append((frames_dir, a - sh.start, b - a,
                                first + round((min(a, start + max(drawn - 1, 0)) - start) * overlay_fps / fps), part_opts))
            part = workdir / f"shot-{tag}-{i:04d}.mp4"
            parts.append(part)
            done = sh.start

            def tick(f: float, done=done, n=n) -> None:
                if on_progress and total:
                    on_progress((done + f * n) / total)
            render_select(edl.slice(sh.start, sh.end), media, part, workdir, settings, timeout, cancel, tick,
                          visual=plan, overlays=own or None, overlay_fps=overlay_fps, video_only=True)
        listing = workdir / f"shots-{tag}.txt"
        temp_paths.append(listing)
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        argv = [
            "ffmpeg", *QUIET, "-n", "-f", "concat", "-safe", "0", "-i", str(listing),
            *(["-i", str(edited_wav)] if edited_wav else []),
            "-map", "0:v:0",
            *(["-map", "1:a:0", "-c:a", "aac", "-b:a", settings.audio_bitrate] if edited_wav else []),
            "-c:v", "copy", "-fps_mode", "passthrough",
            "-video_track_timescale", str(track_timescale(fps)),
            "-movie_timescale", str(lcm(track_timescale(fps), edl.profile.sample_rate)),
            "-movflags", "+faststart", str(dst),
        ]
        try:
            run(argv, timeout=timeout, cancel=cancel)
        except BaseException:
            dst.unlink(missing_ok=True)
            raise
    finally:
        temp_bytes = sum(p.stat().st_size for p in [*temp_paths, *parts] if p.exists())
        for p in parts:
            p.unlink(missing_ok=True)
        if edited_wav:
            edited_wav.unlink(missing_ok=True)
        for p in temp_paths:
            if p.name.startswith(("edited-", "loud-", "shots-")):
                p.unlink(missing_ok=True)
    check = probe(dst)
    if check.video.frame_count != total or not check.video.is_cfr:
        raise RenderCheckError(
            f"в результате {check.video.frame_count} кадров (ожидалось {total}), "
            f"постоянная частота: {check.video.is_cfr}"
        )
    return RenderResult(dst, total, time.monotonic() - started, temp_bytes, loudness)


def _pan_in(k: int, below: str, start: int, elapsed: int, pan: int, width: int) -> str:
    """Переход PAN: за pan кадров всё, что под графикой
    (лицо, экран, прошлая графика), уезжает влево, графика въезжает справа
    вплотную к нему; пока едут — размыты по горизонтали, сильнее в середине.
    Плавно в начале и в конце (smoothstep). start — первый кадр графики в этой
    сборке, elapsed — сколько кадров перехода прошло до неё (сборка по участкам)."""
    p = f"((n-{start}+{elapsed})/{pan})"
    s = f"({p}*{p}*(3-2*{p}))"
    on = f"between(n-{start}+{elapsed},0,{pan - 1})"
    mid = f"between(n-{start}+{elapsed},{round(pan * 0.2)},{round(pan * 0.8)})"
    on_g = f"lt(n+{elapsed},{pan})"
    mid_g = f"between(n+{elapsed},{round(pan * 0.2)},{round(pan * 0.8)})"
    soft, hard = max(4, width // 160), max(12, width // 48)
    return (
        # Нижний слой: вдвое шире чёрным справа, окно едет вправо — картинка влево
        f";{below}pad=2*iw:ih:0:0:color=black,crop=iw/2:ih:'if({on},iw/2*{s},0)':0,"
        f"gblur=sigma={soft}:sigmaV=0.01:enable='{on}',gblur=sigma={hard}:sigmaV=0.01:enable='{mid}'[pb{k}]"
        f";[g{k}]gblur=sigma={soft}:sigmaV=0.01:enable='{on_g}',gblur=sigma={hard}:sigmaV=0.01:enable='{mid_g}'[gm{k}]"
        f";[pb{k}][gm{k}]overlay=x='if({on},W*(1-{s}),0)':y=0:eval=frame:eof_action=pass:repeatlast=0:format=auto[q{k}]"
    )


# Метки цвета ролика. Камера пишет без меток, запись экрана — BT.709, и без
# явной пометки участки с лицом и со слайдом выходили с разными метками:
# плееры могли показать их цвет по-разному, а склейка ffmpeg на смене меток
# перестраивается и теряет кадры. setparams меняет только подпись, пиксели те же.
BT709 = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"


def _tag_colors(graph: str) -> str:
    if graph.count("[v]") != 1:
        raise ValueError("в графе сборки ожидался один выход [v]")
    return graph.replace("[v]", "[vu]") + f";[vu]{BT709}[v]"


def render_select(
    edl: CompiledEdl,
    media: dict[str, ProbeResult],
    dst: Path,
    workdir: Path,
    settings: EncodeSettings = EncodeSettings(),
    timeout: float | None = None,
    cancel: CancelCheck | None = None,
    on_progress: Callable[[float], None] | None = None,
    visual: "VisualPlan | None" = None,
    overlays: list[tuple[Path, int, int]] | None = None,
    overlay_fps: int = 30,
    audio_from: dict[str, tuple[str, int, float]] | None = None,
    video_only: bool = False,
) -> RenderResult:
    """visual — слой кадра (render/compose.py): камеры в media по своим id.
    overlays — графика поверх: (папка кадров PNG с частотой overlay_fps,
    первый кадр ролика, сколько кадров). Без них ролик — кадры записи
    экрана, как раньше. audio_from — звук камеры вместо звука записи экрана:
    {запись экрана: (камера, смещение мкс, уплывание ppm)}, камера в media.
    video_only — без звука (части при сборке по участкам)."""
    if visual is not None and len(visual.shots) > 1 and not video_only:
        return _render_by_shots(edl, media, dst, workdir, settings, timeout, cancel, on_progress, visual,
                                overlays, overlay_fps, audio_from)
    runs = split_runs(edl)
    if dst.exists():
        raise FileExistsError(f"{dst} уже существует, готовые ролики не перезаписываются")
    used = {c.asset_id for c in edl.clips}
    if len({(media[a].video.width, media[a].video.height) for a in used}) != 1:
        raise EdlError("size_mismatch", "у файлов разное разрешение, приведение пока не поддерживается")
    fps = edl.profile.fps
    duration = edl.total_frames / fps
    out_estimate = estimate_video_bytes(edl, media, settings, workdir, cancel)
    wav_estimate = int(sum(media[a].duration for a in used) * edl.profile.sample_rate * 2 * 2 + duration * 192_000)
    ensure_free_space(dst.parent, out_estimate + wav_estimate)
    started = time.monotonic()

    has_audio = all(media[a].audio for a in used) and not video_only
    temp_paths: list[Path] = []
    edited_wav = None
    loudness = None
    if has_audio:
        edited_wav, loudness = _edited_audio(edl, media, used, workdir, settings, cancel, audio_from, temp_paths)

    inputs: list[str] = []
    chains: list[str] = []
    # Со слоем кадра или графикой ролик сначала собирается в полном размере,
    # и только в конце уменьшается
    post = visual is not None or bool(overlays)
    # Уменьшаем до заданной высоты, но никогда не растягиваем вверх. Со слоем
    # кадра — после раскладки: экран и лицо раскладываются в полном размере
    scale = f"scale=-2:'min({settings.height},ih)'," if settings.height else ""
    for i, run_ in enumerate(runs):
        src = media[run_[0].asset_id]
        first = run_[0].src_start_frame
        inputs += [*input_args(src, run_, fps), "-i", str(src.path)]
        chains.append(
            f"[{i}:v:0]select='{select_expression(run_, first)}',"
            f"settb={fps.denominator}/{fps.numerator},setpts=N,"
            f"{'' if post else scale}format={settings.pix_fmt}[r{i}]"
        )
    n_inputs = len(runs)
    screen_out = "[S]" if post else "[v]"
    if len(runs) == 1:
        graph = chains[0].replace("[r0]", screen_out)
    else:
        joined = "".join(f"[r{i}]" for i in range(len(runs)))
        # После concat время кадров выставляем заново тем же правилом N / fps
        graph = ";".join(chains) + (
            f";{joined}concat=n={len(runs)}:v=1:a=0,"
            f"settb={fps.denominator}/{fps.numerator},setpts=N{screen_out}"
        )
    if visual:
        from longform.render.compose import camera_chain, camera_input_args, compose_graph

        cam_size = (2, 2)
        camera_label = None
        if visual.uses_camera:
            cam_chains = []
            for i, run_ in enumerate(runs):
                source = visual.cameras.get(run_[0].asset_id)
                frames = sum(c.out_end_frame - c.out_start_frame for c in run_)
                if source is None:
                    # У этой записи экрана нет камеры: план не ставит сюда лицо,
                    # а поток камеры должен быть сплошным — чёрная заглушка
                    cam_chains.append(f"color=c=black:s=16x16:r={fps.numerator}/{fps.denominator},"
                                      f"trim=end_frame={frames},setpts=N[k{i}]")
                    continue
                cam = media[source.asset_id]
                cam_size = (cam.video.width, cam.video.height)
                args, j0 = camera_input_args(cam, source, run_, fps)
                inputs += [*args, "-i", str(cam.path)]
                cam_chains.append(camera_chain(f"[{n_inputs}:v:0]", f"[k{i}]", cam, source, j0,
                                               run_[0].src_start_frame, fps,
                                               select_expression(run_, run_[0].src_start_frame), frames))
                n_inputs += 1
            graph += ";" + ";".join(cam_chains)
            if len(runs) == 1:
                camera_label = "[k0]"
            else:
                graph += ";" + "".join(f"[k{i}]" for i in range(len(runs))) + (
                    f"concat=n={len(runs)}:v=1:a=0,settb={fps.denominator}/{fps.numerator},setpts=N[C]")
                camera_label = "[C]"
        clip_inputs = {}
        for i, sh in enumerate(visual.shots):
            if sh.layout == "broll" and sh.clip:
                # Свой кадр: клип с нужной секунды (пробный ролик с середины участка — дальше на skip)
                args, retime = _clip_input(sh.clip, sh.clip_at + float(sh.skip / fps))
                inputs += [*args, "-i", sh.clip]
                clip_inputs[i] = f"[{n_inputs}:v:0]{retime}"
                n_inputs += 1
            elif sh.layout == "side" and sh.bg:
                # Видео-фон по кругу; участок начинает его с того места, где фон
                # сейчас в ролике — на склейке он не прыгает
                args, retime = _clip_input(sh.bg, (sh.bg_at + float(sh.skip / fps)) % _duration(sh.bg))
                inputs += ["-stream_loop", "-1", *args, "-i", sh.bg]
                clip_inputs[i] = f"[{n_inputs}:v:0]{retime}"
                n_inputs += 1
        scr = media[runs[0][0].asset_id].video
        graph += ";" + compose_graph(visual, "[S]", camera_label, fps, cam_size, (scr.width, scr.height),
                                     clip_inputs).replace("setpts=N[v]", "setpts=N[P]")
    elif post:
        graph += ";[S]null[P]"
    if post:
        last = "[P]"
        width = visual.width if visual else media[runs[0][0].asset_id].video.width
        for k, o in enumerate(overlays or []):
            frames_dir, start, count = o[0], o[1], o[2]
            first = o[3] if len(o) > 3 else 0
            opts = o[4] if len(o) > 4 else {}
            drawn = min(count, opts.get("drawn", count))
            pan, elapsed = opts.get("pan", 0), opts.get("elapsed", 0)
            inputs += ["-framerate", str(overlay_fps), "-start_number", str(first), "-i", str(frames_dir / "%05d.png")]
            # Кадров рисунка нет (участок целиком в удержании) — держим один, последний
            hold = f",tpad=stop_mode=clone:stop={count - max(drawn, 1)}" if count > max(drawn, 1) else ""
            graph += (f";[{n_inputs}:v]format=rgba,fps={fps.numerator}/{fps.denominator},trim=end_frame={max(drawn, 1)}{hold},"
                      f"settb={fps.denominator}/{fps.numerator},setpts=N+{start}[g{k}]")
            if pan and elapsed < pan:
                graph += _pan_in(k, last, start, elapsed, pan, width)
            else:
                graph += f";{last}[g{k}]overlay=0:0:eof_action=pass:repeatlast=0:format=auto[q{k}]"
            last = f"[q{k}]"
            n_inputs += 1
        graph += f";{last}{scale}format={settings.pix_fmt}[v]"
    audio_input = n_inputs
    graph = _tag_colors(graph)
    script = workdir / f"filter-{uuid.uuid4().hex}.txt"
    script.write_text(graph)
    progress_file = workdir / f"progress-{uuid.uuid4().hex}.txt"
    gop = ["-g", str(max(1, round(fps * settings.keyframe_every)))] if settings.keyframe_every else []
    argv = [
        "ffmpeg", *QUIET, "-n",
        *inputs,
        *(["-i", str(edited_wav)] if edited_wav else []),
        "-/filter_complex", str(script),
        "-map", "[v]",
        *(["-map", f"{audio_input}:a:0", "-c:a", "aac", "-b:a", settings.audio_bitrate] if edited_wav else []),
        "-c:v", settings.video_codec, "-preset", settings.preset, "-crf", str(settings.crf), *gop,
        # Не давать ffmpeg добавлять или выбрасывать кадры по своему разумению
        "-fps_mode", "passthrough",
        "-video_track_timescale", str(track_timescale(fps)),
        # Шкала заголовка mp4 (по умолчанию миллисекунды) должна точно выражать
        # и длину видео, и длину звука, иначе контейнер округлит их до мс
        "-movie_timescale", str(lcm(track_timescale(fps), edl.profile.sample_rate)),
        "-movflags", "+faststart",
        "-progress", str(progress_file),
        str(dst),
    ]
    temp_bytes = sum(p.stat().st_size for p in temp_paths if p.exists())
    total_us = float(duration) * 1_000_000

    def tick() -> None:
        done = _read_progress(progress_file)
        if on_progress and done is not None and total_us > 0:
            on_progress(min(1.0, max(0.0, done / total_us)))

    try:
        run(argv, timeout=timeout, cancel=cancel, on_tick=tick)
    except BaseException:
        dst.unlink(missing_ok=True)  # полуготовый файл не должен остаться
        raise
    finally:
        script.unlink(missing_ok=True)
        progress_file.unlink(missing_ok=True)
        if edited_wav:
            edited_wav.unlink(missing_ok=True)

    check = probe(dst)
    if check.video.frame_count != edl.total_frames or not check.video.is_cfr:
        raise RenderCheckError(
            f"в результате {check.video.frame_count} кадров (ожидалось {edl.total_frames}), "
            f"постоянная частота: {check.video.is_cfr}"
        )
    return RenderResult(dst, edl.total_frames, time.monotonic() - started, temp_bytes, loudness)
