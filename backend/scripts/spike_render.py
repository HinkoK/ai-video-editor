"""Замер двух способов склейки на синтетике с множеством стыков.

    uv run python scripts/spike_render.py --fps 30000/1001 --cuts 200

Каждый способ рендерится в отдельном процессе: так пиковая память ffmpeg
одного способа не смешивается с другим. Результат проверяется так же, как в
тестах: номера кадров и сдвиг звука на каждом куске.

Синтетика из плоских картинок кодируется намного быстрее настоящей съёмки,
поэтому время и память здесь сравнивают способы между собой, но ничего не
говорят о скорости на реальной записи.
"""

import argparse
import json
import random
import resource
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from longform.edl import Clip, OutputProfile, compile_edl  # noqa: E402
from longform.probe import probe  # noqa: E402
from longform.render.select import render_select  # noqa: E402
from longform.timebase import frame_to_us, parse_rate  # noqa: E402
from naive_segments import render_segments  # noqa: E402
from media_fixtures import (  # noqa: E402
    audio_lags,
    audio_track_samples,
    decode_audio,
    make_fixture,
    read_frame_numbers,
)

SPIKES = Path.home() / "Movies" / "AI_Video_Editor_Media" / "_spikes"
RENDERERS = {"select": render_select, "segments": render_segments}


def random_ranges(rng: random.Random, cuts: int, fps) -> tuple[list[tuple[int, int]], int]:
    """Куски по 1–5 с с вырезами 0.2–2 с между ними, как в черновом монтаже."""
    one = round(fps)
    ranges, pos = [], rng.randint(0, one)
    for _ in range(cuts + 1):
        length = rng.randint(one, 5 * one)
        ranges.append((pos, pos + length))
        pos += length + rng.randint(one // 5, 2 * one)
    return ranges, pos


def compile_ranges(path: Path, ranges):
    r = probe(path)
    info = r.source_info("a1")
    clips = [Clip(f"c{i}", "a1", frame_to_us(a, info.fps), frame_to_us(b, info.fps))
             for i, (a, b) in enumerate(ranges)]
    return compile_edl(clips, {"a1": info}, OutputProfile(info.fps, info.sample_rate)), {"a1": r}


def child(args) -> None:
    ranges = json.loads(Path(args.edl).read_text())
    edl, media = compile_ranges(Path(args.src), ranges)
    result = RENDERERS[args.render](edl, media, Path(args.dst), Path(args.workdir))
    # На macOS ru_maxrss в байтах
    print(json.dumps({
        "seconds": round(result.seconds, 2),
        "temp_bytes": result.temp_bytes,
        "peak_ffmpeg_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 2**20),
        "peak_python_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20),
    }))


def verify(edl, fx, out: Path) -> dict:
    expected = [f for c in edl.clips for f in range(c.src_start_frame, c.src_end_frame)]
    frames = read_frame_numbers(out)
    bad = [i for i, (a, b) in enumerate(zip(frames, expected)) if a != b]
    sr = fx.sample_rate
    lags = audio_lags(edl, decode_audio(out, sr), fx, max_lag=2 * sr)
    measured = [(i, lag) for i, lag in enumerate(lags) if lag is not None]
    worst = max(measured, key=lambda x: abs(x[1]), default=(None, 0))
    checkpoints = {}
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        i = round(frac * (len(lags) - 1))
        checkpoints[f"кусок {i}"] = None if lags[i] is None else round(lags[i] / sr * 1000, 2)
    return {
        "кадров": len(frames),
        "кадров_ожидалось": len(expected),
        "кадров_не_на_месте": len(bad) + abs(len(frames) - len(expected)),
        "первый_неверный_кадр": bad[0] if bad else None,
        "звук_в_контейнере_отсчётов": audio_track_samples(out),
        "звука_ожидалось_отсчётов": edl.total_samples,
        "кусков_со_звуком_проверено": len(measured),
        "кусков_со_сдвигом_звука": sum(lag != 0 for _, lag in measured),
        "кусков_не_найдено": sum(lag is None for lag in lags),
        "худший_сдвиг_мс": round(worst[1] / sr * 1000, 2),
        "сдвиг_звука_мс_по_ходу_ролика": checkpoints,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", default="30000/1001")
    ap.add_argument("--cuts", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--render", choices=RENDERERS)  # внутренний режим дочернего процесса
    ap.add_argument("--edl")
    ap.add_argument("--src")
    ap.add_argument("--dst")
    ap.add_argument("--workdir")
    args = ap.parse_args()
    if args.render:
        child(args)
        return

    fps = parse_rate(args.fps)
    run_dir = SPIKES / f"{datetime.now():%Y-%m-%d_%H%M%S}_{args.fps.replace('/', '-')}fps_{args.cuts}cuts"
    run_dir.mkdir(parents=True)
    ranges, total = random_ranges(random.Random(args.seed), args.cuts, fps)
    print(f"синтетика: {total} кадров ({float(total / fps) / 60:.1f} мин), {len(ranges)} кусков")
    fx = make_fixture(run_dir / "source.mp4", fps=fps, frames=total + round(fps), gop=round(2 * fps))
    edl_path = run_dir / "edl.json"
    edl_path.write_text(json.dumps(ranges))
    edl, _ = compile_ranges(fx.path, ranges)

    report = {"fps": args.fps, "стыков": args.cuts, "кадров_в_монтаже": edl.total_frames}
    for name in RENDERERS:
        out = run_dir / f"{name}.mp4"
        workdir = run_dir / f"work-{name}"
        workdir.mkdir()
        print(f"способ {name}: рендер…", flush=True)
        proc = subprocess.run(
            [sys.executable, __file__, "--render", name, "--edl", str(edl_path),
             "--src", str(fx.path), "--dst", str(out), "--workdir", str(workdir)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            report[name] = {"ошибка": proc.stderr[-2000:]}
            print(proc.stderr[-2000:])
            continue
        # Кеш WAV в рабочей папке нужен только на время рендера
        shutil.rmtree(workdir, ignore_errors=True)
        metrics = json.loads(proc.stdout.strip().splitlines()[-1])
        print(f"способ {name}: проверка результата…", flush=True)
        report[name] = {**metrics, "размер_мб": round(out.stat().st_size / 2**20, 1), **verify(edl, fx, out)}

    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nпапка замера: {run_dir}")


if __name__ == "__main__":
    main()
