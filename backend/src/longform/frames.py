"""Лист кадров — «глаза» Claude (идея из HelpFreedom/kadr, код свой).

Claude видит расшифровку, но не картинку. Чтобы решить, техническая ли
пауза или зритель в это время читает результат на экране, ему нужен кадр.
Здесь собирается одна картинка из нескольких кадров оригинала в заданные
моменты, по порядку слева направо и сверху вниз. На диск ничего не пишется.
"""

import subprocess
import tempfile
from fractions import Fraction

import numpy as np

from longform.edl import EdlError
from longform.ffmpeg import QUIET, CancelCheck, run, spawn
from longform.probe import ProbeResult
from longform.timebase import frame_at

MAX_FRAMES = 12
GAP = 4  # полоса между кадрами, пикселей


def _frame(src: ProbeResult, src_us: int, width: int, height: int, cancel: CancelCheck | None) -> np.ndarray:
    """Кадр оригинала, видимый в момент src_us (время от первого кадра видео).

    Точная перемотка ffmpeg оставляет первый кадр, который НАЧИНАЕТСЯ не
    раньше указанного времени. Момент внутри кадра k дал бы кадр k+1 (а у
    последнего кадра — ничего). Поэтому сначала находим видимый кадр и
    перематываем на полкадра раньше его начала — как в склейке.
    """
    fps = src.video.fps
    k = min(max(frame_at(src_us, fps), 0), src.video.frame_count - 1)
    seek = []
    if k > 0:
        t = src.video.start + Fraction(2 * k - 1, 2) / fps - src.container_start
        seek = ["-ss", f"{float(t):.6f}"]
    raw = run(["ffmpeg", *QUIET, *seek, "-i", str(src.path), "-map", "0:v:0",
               "-frames:v", "1", "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
              cancel=cancel)
    if len(raw) < width * height * 3:
        raise EdlError("no_frame", f"в этом месте файла нет кадра ({src_us / 1e6:.2f} с)")
    return np.frombuffer(raw[: width * height * 3], np.uint8).reshape(height, width, 3)


def contact_sheet(points: list[tuple[ProbeResult, int]], width: int = 480, cols: int = 4,
                  cancel: CancelCheck | None = None) -> bytes:
    """PNG из кадров points = [(файл, время исходника в мкс)]."""
    if not 1 <= len(points) <= MAX_FRAMES:
        raise EdlError("bad_points", f"кадров от 1 до {MAX_FRAMES}")
    width = max(120, min(int(width), 1280)) // 2 * 2
    cols = max(1, min(cols, len(points)))
    first = points[0][0].video
    height = max(2, round(width * first.height / first.width / 2) * 2)
    frames = [_frame(src, us, width, height, cancel) for src, us in points]
    rows = -(-len(frames) // cols)
    sheet = np.zeros((rows * height + (rows - 1) * GAP, cols * width + (cols - 1) * GAP, 3), np.uint8)
    for i, f in enumerate(frames):
        r, c = divmod(i, cols)
        y, x = r * (height + GAP), c * (width + GAP)
        sheet[y:y + height, x:x + width] = f
    h, w = sheet.shape[:2]
    # Результат — во временный файл, а не в трубу: пока мы пишем вход,
    # непрочитанная труба выхода могла бы остановить ffmpeg
    with tempfile.TemporaryFile() as out:
        with spawn(["ffmpeg", *QUIET, "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", "pipe:0",
                    "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"],
                   stdin=subprocess.PIPE, stdout=out, cancel=cancel) as proc:
            proc.stdin.write(sheet.tobytes())
        out.seek(0)
        return out.read()
