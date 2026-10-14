"""Способ А: каждый кусок кодируется отдельно, потом склейка без перекодирования.

ОТКЛОНЁН, лежит только для повторения замера (docs/spikes/01-render-strategy.md).
В ядро не возвращать.

Так обычно предлагают резать видео в первую очередь: «-ss начало -t длина»
на каждый кусок и concat. Проблема:
у каждого куска свой звук AAC со своими служебными отсчётами в начале и
добивкой до блока в конце, и при склейке они накапливаются.
"""

import shutil
import time
import uuid
from fractions import Fraction
from pathlib import Path

from longform.edl import CompiledEdl
from longform.ffmpeg import QUIET, ensure_free_space, run
from longform.probe import ProbeResult
from longform.render.select import EncodeSettings, RenderResult, check_single_source_in_order


def render_segments(
    edl: CompiledEdl,
    media: dict[str, ProbeResult],
    dst: Path,
    workdir: Path,
    settings: EncodeSettings = EncodeSettings(),
    timeout: float | None = None,
) -> RenderResult:
    asset_id = check_single_source_in_order(edl)
    src = media[asset_id]
    if dst.exists():
        raise FileExistsError(f"{dst} уже существует, готовые ролики не перезаписываются")
    fps = edl.profile.fps
    duration = edl.total_frames / fps
    ensure_free_space(dst.parent, int(src.size_bytes * duration / max(src.duration, duration) * 3))
    started = time.monotonic()
    seg_dir = workdir / f"segments-{uuid.uuid4().hex}"
    seg_dir.mkdir()
    try:
        names = []
        for i, c in enumerate(edl.clips):
            name = f"{i:05d}.mp4"
            start = src.video.start + Fraction(c.src_start_frame) / fps - src.container_start
            length = Fraction(c.src_end_frame - c.src_start_frame) / fps
            run([
                "ffmpeg", *QUIET, "-n",
                "-ss", f"{float(start):.6f}", "-i", str(src.path), "-t", f"{float(length):.6f}",
                "-map", "0:v:0", *(["-map", "0:a:0"] if src.audio else []),
                "-c:v", settings.video_codec, "-preset", settings.preset, "-crf", str(settings.crf),
                "-pix_fmt", settings.pix_fmt,
                *(["-c:a", "aac", "-b:a", settings.audio_bitrate] if src.audio else []),
                str(seg_dir / name),
            ], timeout=timeout)
            names.append(name)
        temp_bytes = sum(p.stat().st_size for p in seg_dir.iterdir())
        (seg_dir / "list.txt").write_text("".join(f"file '{n}'\n" for n in names))
        run(["ffmpeg", *QUIET, "-n", "-f", "concat", "-safe", "0", "-i", str(seg_dir / "list.txt"),
             "-c", "copy", "-movflags", "+faststart", str(dst)], timeout=timeout)
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
    return RenderResult(dst, edl.total_frames, time.monotonic() - started, temp_bytes)
