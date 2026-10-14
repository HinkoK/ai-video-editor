"""Слой кадра при сборке: в каждом кадре ролика видно ровно то, что положено.

Экран и камера — синтетика с номером кадра в картинке (16 бит, две
строки ячеек), у камеры своя частота кадров и номера с 40000."""

import subprocess
from fractions import Fraction

import numpy as np
import pytest

from longform.edl import Clip, OutputProfile, compile_edl
from longform.probe import probe
from longform.render.compose import CameraSource, Shot, VisualPlan, alternate_levels, pip_geometry
from longform.render.select import render_select
from longform.timebase import frame_at, frame_to_us
from media_fixtures import BITS, CELL, H, W, iter_frames, make_fixture, read_frame_number

pytestmark = pytest.mark.media

MAIN_FPS = Fraction(25)
CAM_FPS = Fraction(24000, 1001)
OFFSET_US = 1_500_000  # камера включена на 1,5 с раньше экрана


def top_row_number(img: np.ndarray) -> int:
    """Номер по одной верхней строке ячеек: нижнюю может закрывать лицо в углу."""
    n = 0
    for bit in range(BITS):
        n = (n << 1) | int(img[30:50, bit * CELL + 4:(bit + 1) * CELL - 4].mean() > 128)
    return n


def scaled_top_number(region: np.ndarray) -> int:
    """Номер уменьшенного кадра по верхней строке ячеек (нижнюю может закрывать лицо)."""
    sy, sx = region.shape[0] / H, region.shape[1] / W
    n = 0
    for bit in range(BITS):
        x0, x1 = int((bit * CELL + 4) * sx), int(((bit + 1) * CELL - 4) * sx)
        n = (n << 1) | int(region[int(32 * sy):int(48 * sy), x0:x1].mean() > 128)
    return n


def scaled_number(region: np.ndarray) -> int:
    """Номер уменьшенного кадра (лицо в углу): ячейки и строки в масштабе."""
    sy, sx = region.shape[0] / H, region.shape[1] / W
    top = bottom = 0
    for bit in range(BITS):
        x0, x1 = int((bit * CELL + 4) * sx), int(((bit + 1) * CELL - 4) * sx)
        t = region[int(32 * sy):int(48 * sy), x0:x1].mean() > 128
        b = region[int(96 * sy):int(112 * sy), x0:x1].mean() > 128
        if t == b:
            return -1
        top, bottom = (top << 1) | int(t), (bottom << 1) | int(not b)
    return top if top == bottom else -1


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("кадр")
    screen = make_fixture(d / "экран.mp4", fps=MAIN_FPS, frames=1000, gop=25)
    camera = make_fixture(d / "камера.mp4", fps=CAM_FPS, frames=1100, gop=24, first_number=40000, seed=5)
    return screen, camera


def _render(edl, pm, pc, plan, out, tmp):
    render_select(edl, {"s": pm, "c": pc}, out, tmp, visual=plan)
    return out


def camera_frame_at(src_frame: int) -> int:
    """Какой кадр камеры виден в момент кадра экрана src_frame."""
    main_us = frame_to_us(src_frame, MAIN_FPS)
    return 40000 + frame_at(OFFSET_US + main_us, CAM_FPS)


def test_screen_face_and_corner_layouts_land_on_the_right_frames(sources, tmp_path):
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    # Ролик: кадры экрана 0–200 и 400–600 (вырезано 8 с между ними)
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(200, 25)), Clip("b", "s", frame_to_us(400, 25), frame_to_us(600, 25))],
                      {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    plan = VisualPlan(W, H, (
        Shot(0, 100, "screen"),
        Shot(100, 250, "camera"),  # через стык на кадре 200
        Shot(250, 400, "pip", pip_slot="br", pip_size=0.5),
    ), {"s": CameraSource("c", OFFSET_US, 0.0)})
    out = tmp_path / "раскладка.mp4"
    render_select(edl, {"s": pm, "c": pc}, out, tmp_path, visual=plan)

    frames = list(iter_frames(out))
    assert len(frames) == 400
    src_of = lambda k: k if k < 200 else 400 + (k - 200)  # noqa: E731
    wrong = []
    for k, img in enumerate(frames):
        if k < 100:
            got, want = read_frame_number(img), src_of(k)
        elif k < 250:
            got, want = read_frame_number(img), camera_frame_at(src_of(k))
        else:
            x, y, pw, ph = pip_geometry(plan.shots[2], W, H, W, H)
            got = (top_row_number(img), scaled_number(img[y:y + ph, x:x + pw]))
            want = (src_of(k), camera_frame_at(src_of(k)))
        if got != want:
            wrong.append((k, got, want))
    assert not wrong, wrong[:10]


def test_screen_zoom_reads_the_region_and_returns(tmp_path):
    # Номера с 0x5A00: старший байт — узнаваемый узор в левой половине кадра
    screen = make_fixture(tmp_path / "экран.mp4", fps=MAIN_FPS, frames=200, gop=25, first_number=0x5A00)
    pm = probe(screen.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(200, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    plan = VisualPlan(W, H, (Shot(0, 50, "screen"), Shot(50, 150, "screen", zoom=(0.0, 0.0, 0.5, 0.5)),
                             Shot(150, 200, "screen")), {})
    out = tmp_path / "приближение.mp4"
    render_select(edl, {"s": pm}, out, tmp_path, visual=plan)
    frames = list(iter_frames(out))
    assert len(frames) == 200
    # До и после приближения — обычный экран
    assert read_frame_number(frames[49]) == 0x5A00 + 49
    assert read_frame_number(frames[150]) == 0x5A00 + 150
    # Посередине — левая верхняя четверть вдвое крупнее: ячейка 32 px,
    # верхняя строка ячеек на 40–120 px; старший байт 0x5A
    mid = frames[100]
    bits = [int(mid[70:90, b * 32 + 8:b * 32 + 24].mean() > 128) for b in range(8)]
    assert int("".join(map(str, bits)), 2) == 0x5A
    # Подвод плавный: в начале участка приближение меньше, чем в середине
    first = frames[51]
    assert read_frame_number(first) == -1 or first[70:90].mean() != mid[70:90].mean()


def _zoomed(img: np.ndarray, s: float, cx: float = 0.5, cy: float = 0.5) -> np.ndarray:
    """Кадр, приближенный в s раз к точке (cx, cy) — та же рамка, что у камеры в движении."""
    h, w = img.shape
    x0 = min(max(cx * w - w / s / 2, 0), w - w / s)
    y0 = min(max(cy * h - h / s / 2, 0), h - h / s)
    xs = np.clip(x0 + np.arange(w) / s, 0, w - 1.001)
    ys = np.clip(y0 + np.arange(h) / s, 0, h - 1.001)
    xi, yi = xs.astype(int), ys.astype(int)
    fx, fy = (xs - xi)[None, :], (ys - yi)[:, None]
    f = img.astype(np.float32)
    top = f[yi][:, xi] * (1 - fx) + f[yi][:, xi + 1] * fx
    bottom = f[yi + 1][:, xi] * (1 - fx) + f[yi + 1][:, xi + 1] * fx
    return top * (1 - fy) + bottom * fy


def _zoom_of(img: np.ndarray, still: np.ndarray) -> float:
    """Во сколько раз кадр крупнее того же кадра камеры без движения."""
    inner = (slice(8, -8), slice(8, -8))
    return float(min(np.arange(1.0, 1.25, 0.0025),
                     key=lambda s: np.abs(img[inner].astype(np.float32) - _zoomed(still, s)[inner]).mean()))


def test_camera_motion_punch_returns_and_push_restarts_at_cuts(sources, tmp_path):
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(200, 25)), Clip("b", "s", frame_to_us(400, 25), frame_to_us(600, 25))],
                      {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    cams = {"s": CameraSource("c", OFFSET_US, 0.0)}
    # Укрупнение ×1,2 и возврат за 2 с; дальше наезд на 10 % по кускам: кусок
    # 100–200 короче 5 с и стоит, кусок после склейки на 200 едет
    moving = VisualPlan(W, H, (Shot(0, 100, "camera", punch=(1.2, 0.5, 0.5), punch_release=2.0),
                               Shot(100, 400, "camera", push=0.1, cuts=(100,))), cams)
    still = VisualPlan(W, H, (Shot(0, 400, "camera"),), cams)
    out_m, out_s = tmp_path / "движение.mp4", tmp_path / "стоит.mp4"
    render_select(edl, {"s": pm, "c": pc}, out_m, tmp_path, visual=moving)
    render_select(edl, {"s": pm, "c": pc}, out_s, tmp_path, visual=still)
    fm, fs = list(iter_frames(out_m)), list(iter_frames(out_s))
    assert len(fm) == len(fs) == 400
    src_of = lambda k: k if k < 200 else 400 + (k - 200)  # noqa: E731
    want = {0: 1.2, 25: 1.1, 60: 1.0, 150: 1.0, 199: 1.0, 200: 1.0, 300: 1.05, 399: 1.0995}
    got = {k: _zoom_of(fm[k], fs[k]) for k in want}
    assert all(abs(got[k] - z) < 0.01 for k, z in want.items()), got
    # Движение — только картинка: в каждом кадре тот же кадр камеры, что без него
    assert [read_frame_number(fs[k]) for k in want] == [camera_frame_at(src_of(k)) for k in want]
    # Внутри куска наезд идёт без рывков: каждый кадр не меньше предыдущего
    zs = [_zoom_of(fm[k], fs[k]) for k in range(200, 400, 20)]
    assert zs == sorted(zs) and zs[-1] > zs[0]


def test_camera_zoom_attack_is_smooth_and_can_return(sources, tmp_path):
    """Зум: лицо плавно приближается за attack, а не
    рывком; с release потом плавно возвращается к общему плану."""
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(200, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    cams = {"s": CameraSource("c", OFFSET_US, 0.0)}
    moving = VisualPlan(W, H, (Shot(0, 100, "camera", punch=(1.2, 0.5, 0.5), punch_attack=1.0),
                               Shot(100, 200, "camera", punch=(1.2, 0.5, 0.5), punch_attack=1.0,
                                    punch_release=2.0)), cams)
    still = VisualPlan(W, H, (Shot(0, 200, "camera"),), cams)
    out_m, out_s = tmp_path / "зум.mp4", tmp_path / "стоит.mp4"
    render_select(edl, {"s": pm, "c": pc}, out_m, tmp_path, visual=moving)
    render_select(edl, {"s": pm, "c": pc}, out_s, tmp_path, visual=still)
    fm, fs = list(iter_frames(out_m)), list(iter_frames(out_s))
    # 25 к/с: за секунду до ×1,2 (на середине — половина пути), дальше держит;
    # второй участок — снова с общего плана, затем за 2 с назад
    want = {0: 1.0, 25: 1.2, 80: 1.2, 100: 1.0, 125: 1.2, 150: 1.1, 175: 1.0, 199: 1.0}
    got = {k: _zoom_of(fm[k], fs[k]) for k in want}
    assert all(abs(got[k] - z) < 0.01 for k, z in want.items()), got
    assert 1.07 < _zoom_of(fm[12], fs[12]) < 1.12  # плавно, не рывком
    assert [read_frame_number(fs[k]) for k in want] == [camera_frame_at(k) for k in want]


def test_broll_shows_clip_frames_and_preview_matches(sources, tmp_path):
    """Свой кадр (broll): во весь экран идут кадры клипа с
    секунды at, в частоте ролика; до и после — снова экран. Пробный ролик с
    середины участка начинает клип с того же места, что и готовый."""
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(150, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    plan = VisualPlan(W, H, (Shot(0, 50, "screen"), Shot(50, 100, "broll", clip=str(camera.path), clip_at=2.0),
                             Shot(100, 150, "screen")), {})
    out = tmp_path / "свой кадр.mp4"
    render_select(edl, {"s": pm}, out, tmp_path, visual=plan)
    frames = list(iter_frames(out))
    assert len(frames) == 150
    assert [read_frame_number(frames[k]) for k in (0, 49, 100, 149)] == [0, 49, 100, 149]
    clip = [read_frame_number(frames[k]) for k in range(50, 100)]
    # 2,0 с клипа — кадр 47–48 камеры (23,976 к/с); дальше по времени, без скачков назад
    assert clip[0] - 40000 in (47, 48) and clip == sorted(clip)
    assert abs((clip[-1] - clip[0]) - 49 * float(CAM_FPS / MAIN_FPS)) <= 1.5
    part = tmp_path / "проба.mp4"
    render_select(edl.slice(70, 120), {"s": pm}, part, tmp_path, visual=plan.slice(70, 120))
    assert abs(read_frame_number(next(iter_frames(part))) - clip[20]) <= 1


def test_side_video_background_flows_across_shots(sources, tmp_path):
    """Видео-фон за рамкой экрана:
    вместо тёмной сетки идёт видео, и на стыке участков оно не начинается заново."""
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(100, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    cams = {"s": CameraSource("c", OFFSET_US, 0.0)}
    bg = str(camera.path)
    two = VisualPlan(W, H, (Shot(0, 50, "side", bg=bg, bg_at=0.0), Shot(50, 100, "side", bg=bg, bg_at=2.0)), cams)
    one = VisualPlan(W, H, (Shot(0, 100, "side", bg=bg, bg_at=0.0),), cams)
    plain = VisualPlan(W, H, (Shot(0, 100, "side"),), cams)
    outs = {}
    for name, plan in (("два", two), ("один", one), ("сетка", plain)):
        outs[name] = list(iter_frames(_render(edl, pm, pc, plan, tmp_path / f"{name}.mp4", tmp_path)))
    corner = lambda img: img[: H // 12, : W // 30].astype(float)  # noqa: E731 — угол фона, вне рамки и лица
    for k in (10, 49, 50, 90):
        assert np.abs(corner(outs["два"][k]) - corner(outs["один"][k])).mean() < 3, k
    assert np.abs(corner(outs["один"][10]) - corner(outs["сетка"][10])).mean() > 10


def test_broll_segment_goes_through_the_database(sources, tmp_path):
    """Участок своего кадра сохраняется (миграция 016), клип ищется в папке
    broll; без клипа — понятная ошибка; в плане — путь к клипу."""
    import shutil

    from longform import store, visual
    from longform.db import connect, migrate
    from longform.edl import EdlError

    screen, camera = sources
    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "свои кадры", "user")
    m = store.register_asset(conn, p["id"], str(screen.path), "user")
    store.create_initial_revision(conn, p["id"], "user")
    clips = tmp_path / "broll"
    clips.mkdir()
    shutil.copy(camera.path, clips / "C1.mp4")
    with pytest.raises(EdlError):
        visual.add_segment(conn, p["id"], actor="agent", asset_id=m["id"], src_start_us=1_000_000, src_end_us=3_000_000,
                           layout="broll", params={"clip": "C2.mp4"}, goal="свой кадр", clips_dir=clips)
    visual.add_segment(conn, p["id"], actor="agent", asset_id=m["id"], src_start_us=1_000_000, src_end_us=3_000_000,
                       layout="broll", params={"clip": "C1.mp4", "at": 2}, goal="свой кадр", clips_dir=clips)
    edl = store.compile_revision(conn, p["id"], store.get_project(conn, p["id"])["current_revision_id"])
    shots = [s for s in visual.build_plan(conn, p["id"], edl, W, H, clips_dir=clips).shots if s.layout == "broll"]
    assert [(s.start, s.end, s.clip, s.clip_at) for s in shots] == [(25, 75, str(clips / "C1.mp4"), 2.0)]


def test_video_background_moves_evenly_from_any_start(sources, tmp_path):
    """Фон не дёргается (автор, 08.10): фон вдвое реже кадров ролика (как 30 и
    60 к/с), участок начинает его между кадрами фона — всё равно каждый кадр
    фона показан ровно дважды, без замираний и скачков."""
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    bg = tmp_path / "фон.mp4"
    # Фон 12,5 к/с (ролик 25): яркость 24…216 растёт на 16 с каждым кадром фона
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=25/2:d=12",
                    "-vf", "geq=lum='24+mod(N*16,208)':cb=128:cr=128", "-c:v", "libx264", "-qp", "0", "-g", "12",
                    "-pix_fmt", "yuv420p", str(bg)], check=True)
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(100, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    cams = {"s": CameraSource("c", OFFSET_US, 0.0)}
    # Начало фона — посередине между его кадрами
    plan = VisualPlan(W, H, (Shot(0, 100, "side", bg=str(bg), bg_at=2.0 + 0.04),), cams)
    frames = list(iter_frames(_render(edl, pm, pc, plan, tmp_path / "ровно.mp4", tmp_path)))
    lum = [float(f[2:10, 2:10].mean()) for f in frames]
    # Номер кадра фона по яркости: кадр читается в полном диапазоне, яркость
    # фона задана в ТВ-диапазоне (16–235) — переводим обратно
    idx = [round((v * 219 / 255 + 16 - 24) / 16) for v in lum]
    runs, prev, n = [], None, 0
    for k in idx:
        if k is None:
            prev, n = None, 0
            continue
        if k == prev:
            n += 1
        else:
            if prev is not None:
                runs.append((prev, n, k - prev))
            prev, n = k, 1
    inner = runs[1:]  # первая серия могла начаться с середины
    # Шаг 1, после 12-го кадра — снова 0 (яркость по кругу)
    assert len(inner) >= 40 and all(cnt == 2 and step in (1, -12) for _, cnt, step in inner), inner


def test_broll_params():
    from longform.edl import EdlError
    from longform.visual import validate_params

    assert validate_params("broll", {"clip": "C1551.mp4", "at": 3}) == {"clip": "C1551.mp4", "at": 3.0}
    assert validate_params("broll", {"clip": "C1551.mp4", "enter": "whip"}) == {"clip": "C1551.mp4", "at": 0.0,
                                                                                 "enter": "whip"}
    for layout, params in (("broll", {}), ("broll", {"clip": "../editor.db"}), ("broll", {"clip": "a/b.mp4"}),
                           ("camera", {"clip": "C1551.mp4"}), ("broll", {"clip": "C1551.mp4", "at": -1})):
        with pytest.raises(EdlError):
            validate_params(layout, params)


def test_camera_alternate_changes_plan_at_cuts_and_preview_matches(sources, tmp_path):
    """Смена плана на склейках: общий, крупнее, кусок короче секунды не
    меняет план, снова общий. Пробный ролик с середины участка — те же
    планы, что в готовом ролике."""
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    f = lambda k: frame_to_us(k, 25)  # noqa: E731
    # Куски 0–200, 400–600, 700–720 (0,8 с), 800–900: склейки на 200, 400, 420
    edl = compile_edl([Clip("a", "s", 0, f(200)), Clip("b", "s", f(400), f(600)), Clip("c", "s", f(700), f(720)),
                       Clip("d", "s", f(800), f(900))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    cams = {"s": CameraSource("c", OFFSET_US, 0.0)}
    shot = Shot(0, 520, "camera", alternate=(0.1, 0.5, 0.5), cuts=(200, 400, 420))
    assert alternate_levels(shot, MAIN_FPS) == [(0, 200, False), (200, 400, True), (400, 420, True), (420, 520, False)]
    moving, still = VisualPlan(W, H, (shot,), cams), VisualPlan(W, H, (Shot(0, 520, "camera"),), cams)
    renders = {}
    for name, e, plan in (("движение", edl, moving), ("стоит", edl, still),
                          ("проба", edl.slice(380, 480), moving.slice(380, 480)),
                          ("проба-стоит", edl.slice(380, 480), still.slice(380, 480))):
        out = tmp_path / f"{name}.mp4"
        render_select(e, {"s": pm, "c": pc}, out, tmp_path, visual=plan)
        renders[name] = list(iter_frames(out))
    fm, fs = renders["движение"], renders["стоит"]
    assert len(fm) == len(fs) == 520
    want = {100: 1.0, 199: 1.0, 200: 1.1, 300: 1.1, 410: 1.1, 419: 1.1, 420: 1.0, 500: 1.0}
    got = {k: _zoom_of(fm[k], fs[k]) for k in want}
    assert all(abs(got[k] - z) < 0.01 for k, z in want.items()), got
    src_of = lambda k: k if k < 200 else 400 + k - 200 if k < 400 else 700 + k - 400 if k < 420 else 800 + k - 420  # noqa: E731
    assert [read_frame_number(fs[k]) for k in want] == [camera_frame_at(src_of(k)) for k in want]
    # Проба кадров 380–480: участок начат до неё, планы — как в ролике
    pm_, ps_ = renders["проба"], renders["проба-стоит"]
    assert len(pm_) == len(ps_) == 100
    got = {k: _zoom_of(pm_[k - 380], ps_[k - 380]) for k in (390, 410, 419, 420, 470)}
    assert all(abs(got[k] - want_k) < 0.01 for k, want_k in ((390, 1.1), (410, 1.1), (419, 1.1), (420, 1.0), (470, 1.0))), got


def test_alternate_segment_gets_jump_cuts(sources, tmp_path):
    from longform import store, visual
    from longform.db import connect, migrate

    screen, camera = sources
    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "смена плана", "user")
    m = store.register_asset(conn, p["id"], str(screen.path), "user")
    c = store.register_asset(conn, p["id"], str(camera.path), "user", role="camera")
    store.set_sync(conn, p["id"], c["id"], m["id"], "ok", OFFSET_US, 0.0, {})
    alt = {"step": 0.08, "cx": 0.5, "cy": 0.42}
    visual.add_segment(conn, p["id"], actor="agent", asset_id=m["id"], src_start_us=0, src_end_us=20_000_000,
                       layout="camera", params={"alternate": alt}, goal="говорит зрителю")
    src = probe(screen.path).source_info(m["id"])
    f = lambda k: frame_to_us(k, 25)  # noqa: E731
    edl = compile_edl([Clip("a", m["id"], 0, f(200)), Clip("b", m["id"], f(400), f(600))], {m["id"]: src},
                      OutputProfile(MAIN_FPS, src.sample_rate))
    shots = visual.build_plan(conn, p["id"], edl, W, H).shots
    # Участок до 20 с (кадр 500): лицо со сменой плана до кадра 300 ролика, дальше — раскладка проекта
    assert [(s.start, s.end, s.layout, s.alternate, s.cuts) for s in shots] == [
        (0, 300, "camera", (0.08, 0.5, 0.42), (200,)), (300, 400, "pip", None, ())]


def test_fresh_punch_segments_stay_separate(sources, tmp_path):
    """Два соседних участка лица с укрупнением — два укрупнения, а не одно;
    одинаковые участки без укрупнения — один участок кадра."""
    from longform import store, visual
    from longform.db import connect, migrate

    screen, camera = sources
    conn = connect(tmp_path / "editor.db")
    migrate(conn)
    p = store.create_project(conn, "укрупнения", "user")
    m = store.register_asset(conn, p["id"], str(screen.path), "user")
    c = store.register_asset(conn, p["id"], str(camera.path), "user", role="camera")
    store.set_sync(conn, p["id"], c["id"], m["id"], "ok", OFFSET_US, 0.0, {})
    store.create_initial_revision(conn, p["id"], "user")
    punch = {"punch": {"scale": 1.12, "release": 4}, "push": 0.04}
    for a, b, params in ((4, 8, punch), (8, 12, punch), (12, 16, {"push": 0.04}), (16, 20, {"push": 0.04})):
        visual.add_segment(conn, p["id"], actor="agent", asset_id=m["id"], src_start_us=a * 1_000_000,
                           src_end_us=b * 1_000_000, layout="camera", params=params, goal="говорит зрителю")
    edl = store.compile_revision(conn, p["id"], store.get_project(conn, p["id"])["current_revision_id"])
    shots = [(s.start, s.end, s.punch_release) for s in visual.build_plan(conn, p["id"], edl, W, H).shots
             if s.layout == "camera"]
    assert shots == [(100, 200, 4.0), (200, 300, 4.0), (300, 500, None)]


def test_camera_motion_params_and_jump_cuts():
    from longform.edl import EdlError, SourceInfo
    from longform.visual import _jump_cuts, validate_params

    assert validate_params("camera", {"punch": {"scale": 1.12, "release": 4}, "push": 0.04}) == {
        "punch": {"scale": 1.12, "cx": 0.5, "cy": 0.5, "release": 4.0}, "push": 0.04}
    assert validate_params("camera", {"punch": {"scale": 1.15, "attack": 0.6}}) == {
        "punch": {"scale": 1.15, "cx": 0.5, "cy": 0.5, "attack": 0.6}}
    assert validate_params("camera", {"alternate": {"step": 0.08, "cy": 0.42}}) == {
        "alternate": {"step": 0.08, "cx": 0.5, "cy": 0.42}}
    for layout, params in (("pip", {"punch": {"scale": 1.1, "release": 3}}), ("side", {"push": 0.04}),
                           ("pip", {"punch": {"scale": 1.1, "attack": 0.5}}), ("camera", {"punch": {"scale": 1.1, "attack": 9}}),
                           ("camera", {"push": 0.5}), ("camera", {"punch": {"scale": 1.1, "speed": 1}}), ("camera", {"drop": 0.08}),
                           ("side", {"alternate": {"step": 0.08}}), ("camera", {"alternate": {"step": 0.5}}),
                           ("camera", {"alternate": {"step": 0.08}, "push": 0.04}),
                           ("camera", {"alternate": {"step": 0.08}, "punch": {"scale": 1.1}}),
                           ("camera", {"alternate": {"step": 0.08, "speed": 1}}), ("camera", {"alternate": 0.08})):
        with pytest.raises(EdlError):
            validate_params(layout, params)
    src = SourceInfo("s", Fraction(25), 1000, 48000, 1, Fraction(0))
    f = lambda k: frame_to_us(k, 25)  # noqa: E731
    edl = compile_edl([Clip("a", "s", 0, f(100)), Clip("b", "s", f(100), f(200)), Clip("c", "s", f(300), f(400))],
                      {"s": src}, OutputProfile(Fraction(25), 48000))
    # Кусок, продолжающий предыдущий в исходнике, — не склейка
    assert _jump_cuts(edl, 0, 300) == (200,)
    assert _jump_cuts(edl, 150, 300) == (50,)
    assert _jump_cuts(edl, 0, 200) == ()


def test_layer_end_to_end_export_review_and_rerender(sources, tmp_path):
    """Камера сведена, по умолчанию лицо в углу; Claude ставит «лицо целиком»
    на участок, экспорт показывает его; автор возвращает — экспорт собирается
    заново и участок больше не действует."""
    from fastapi.testclient import TestClient

    from longform import jobs, qa, store, worker
    from longform.api import Settings, create_app
    from longform.db import connect, migrate
    from longform.workspace import Workspace

    screen, camera = sources
    ws = Workspace(tmp_path / "рабочая")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    client = TestClient(create_app(Settings(
        db_path=ws.db, user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset(), workspace_root=ws.root)))
    user, agent = {"Authorization": "Bearer u"}, {"Authorization": "Bearer a"}
    p = store.create_project(conn, "слой кадра", "user")
    m = store.register_asset(conn, p["id"], str(screen.path), "user")
    c = store.register_asset(conn, p["id"], str(camera.path), "user", role="camera")
    store.set_sync(conn, p["id"], c["id"], m["id"], "ok", OFFSET_US, 0.0, {})
    rev = store.create_initial_revision(conn, p["id"], "user")
    base = f"/api/projects/{p['id']}"

    v = client.get(f"{base}/visual", headers=user).json()
    assert v["default"] == {"layout": "pip", "slot": "br", "size": 0.22}  # камера сведена — лицо в углу
    # Лицо целиком на 10–14 с записи экрана: цель обязательна, камера должна снимать это место
    body = {"asset_id": m["id"], "src_start_us": 10_000_000, "src_end_us": 14_000_000, "layout": "camera",
            "params": {"punch": {"scale": 1.15, "cx": 0.5, "cy": 0.45}}, "goal": "личный вывод — крупнее"}
    seg = client.post(f"{base}/visual", json=body, headers=agent).json()
    assert seg["review"] == "pending" and seg["params"]["punch"]["scale"] == 1.15
    clash = client.post(f"{base}/visual", json={**body, "src_start_us": 12_000_000, "src_end_us": 16_000_000},
                        headers=agent)
    assert clash.status_code == 409
    assert client.post(f"{base}/visual", json={**body, "src_start_us": 39_000_000, "src_end_us": 39_900_000,
                                               "layout": "pip", "params": {"size": 0.9}}, headers=agent).status_code == 422

    job = jobs.enqueue(conn, p["id"], "export", actor="user", revision_id=rev["id"])
    worker.serve(ws, stop={}, once=True)
    done = jobs.get_job(conn, p["id"], job["id"])
    assert done["state"] == "completed", done["error"]
    tech = qa.list_reports(conn, p["id"])[0]["technical"]
    assert tech["frames"]["ok"] and tech["audio_samples"]["ok"] and tech["matches_revision"]["ok"]
    assert tech["matches_revision"]["video_composed"] == tech["matches_revision"]["points"]  # всё ролик с лицом
    frames = list(iter_frames(done["output_path"]))
    assert len(frames) == 1000
    # Кадр 300 (12 с) — лицо целиком (крупный план: номер не читается целиком, но
    # это не экран: верхняя строка экрана не совпадает); кадр 100 — экран с лицом в углу
    assert top_row_number(frames[100]) == 100
    x, y, pw, ph = pip_geometry(Shot(0, 1, "pip"), W, H, W, H)
    assert scaled_number(frames[100][y:y + ph, x:x + pw]) in (camera_frame_at(100), -1)  # лицо в углу мелкое
    assert top_row_number(frames[300]) != 300

    # Автор возвращает участок: экспорт той же версии — новая задача, участка нет
    assert client.post(f"{base}/visual/{seg['id']}/review", json={"action": "revert"}, headers=agent).status_code == 403
    client.post(f"{base}/visual/{seg['id']}/review", json={"action": "revert"}, headers=user)
    again = jobs.enqueue(conn, p["id"], "export", actor="user", revision_id=rev["id"])
    assert again["id"] != job["id"]
    worker.serve(ws, stop={}, once=True)
    frames = list(iter_frames(jobs.get_job(conn, p["id"], again["id"])["output_path"]))
    assert top_row_number(frames[300]) == 300  # снова экран с лицом в углу
    listed = client.get(f"{base}/visual?revision_id={rev['id']}", headers=user).json()["segments"]
    assert listed[0]["review"] == "reverted" and listed[0]["edited_ranges"] == [[250, 350]]
    conn.close()


def test_side_layout_frames_screen_and_face_and_whip(sources, tmp_path):
    """Формат автора: экран в рамке на тёмном фоне, лицо квадратом в белой
    рамке; шторка на входе в участок."""
    from longform.render.compose import WHIP_S, side_geometry

    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(300, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    plan = VisualPlan(W, H, (Shot(0, 100, "camera"), Shot(100, 300, "side", enter="whip")),
                      {"s": CameraSource("c", OFFSET_US, 0.0)})
    out = tmp_path / "рамка.mp4"
    render_select(edl, {"s": pm, "c": pc}, out, tmp_path, visual=plan)
    frames = list(iter_frames(out))
    assert len(frames) == 300
    g = side_geometry(plan.shots[1], W, H, W, H)
    x, y, cw, ch = g["card"]
    fx, fy, fs = g["face"]
    k = round(WHIP_S * MAIN_FPS)
    # Первый кадр шторки: экран ещё ниже кадра — сверху только тёмный фон
    assert frames[100][: H // 2].mean() < 40
    wrong = []
    for n in range(100 + k + 1, 300, 7):
        img = frames[n]
        # В рамке — тот самый кадр экрана, уменьшенный
        if scaled_top_number(img[y:y + ch, x:x + cw]) != n:
            wrong.append((n, scaled_top_number(img[y:y + ch, x:x + cw])))
        # Рамка лица белая, вокруг рамки экрана тёмный фон
        assert img[fy + 1, fx + fs // 2] > 200 and img[fy + fs // 2, fx + 1] > 200
        assert img[2, 2] < 60
    assert not wrong, wrong[:10]


def test_side_drop_lowers_the_screen_and_brings_it_back(tmp_path):
    screen = make_fixture(tmp_path / "экран.mp4", fps=MAIN_FPS, frames=100, gop=25)
    camera = make_fixture(tmp_path / "камера.mp4", fps=CAM_FPS, frames=120, gop=24, first_number=40000, seed=5)
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", 0, frame_to_us(100, 25))], {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    cams = {"s": CameraSource("c", 0, 0.0)}
    out_d, out_s = tmp_path / "опущен.mp4", tmp_path / "на месте.mp4"
    render_select(edl, {"s": pm, "c": pc}, out_d, tmp_path, visual=VisualPlan(W, H, (Shot(0, 100, "side", drop=0.1),), cams))
    render_select(edl, {"s": pm, "c": pc}, out_s, tmp_path, visual=VisualPlan(W, H, (Shot(0, 100, "side"),), cams))
    fd, fs = list(iter_frames(out_d)), list(iter_frames(out_s))
    band = slice(20, 150)  # по ширине — экран, левее квадрата лица

    def shift(k: int) -> int:
        a, b = fd[k][:, band].astype(int), fs[k][:, band].astype(int)
        return min(range(0, 25), key=lambda d: np.abs(a[d + 10:110 + d] - b[10:110]).mean())
    # Метки цвета у всех кадров одинаковые (BT.709), хотя экран и камера сняты по-разному
    tags = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "frame=color_range,color_space", "-of", "csv=p=0", str(out_d)],
                          capture_output=True, text=True, check=True).stdout.split()
    assert len(tags) == 100 and {t.rstrip(",") for t in tags} == {"tv,bt709"}
    # Опускание на 0,1 высоты (14 px из 144): в начале и в конце — на месте
    assert shift(0) == 0 and shift(99) == 0
    assert shift(50) == 14


def test_side_window_region_is_large_and_face_beside(tmp_path):
    """Область экрана (окно приложения) — крупно слева, лицо больше и справа от неё."""
    from longform.render.compose import side_geometry

    window = Shot(0, 10, "side", region=(0.019, 0.093, 0.552, 0.737))
    g = side_geometry(window, 2560, 1440, 2560, 1440)
    x, y, cw, ch = g["card"]
    fx, fy, fs = g["face"]
    assert (x, y) == (32, 64) and abs(ch - 1283) <= 2 and fs == 712
    assert fx >= x + cw and fy + fs <= 1440 and fx + fs <= 2560
    full = side_geometry(Shot(0, 10, "side"), 2560, 1440, 2560, 1440)
    assert full["card"] == (84, 88, 2250, 1266) and full["face"] == (2060, 922, 464)


def test_camera_sound_replaces_screen_sound_sample_exact(sources, tmp_path):
    """Звук камеры встаёт на время записи экрана: сверка с ним даёт нулевой сдвиг."""
    from longform.render.audio import camera_audio
    from longform.verify import verify_output

    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    edl = compile_edl([Clip("a", "s", frame_to_us(40, 25), frame_to_us(240, 25)),
                       Clip("b", "s", frame_to_us(500, 25), frame_to_us(700, 25))],
                      {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    out = tmp_path / "звук камеры.mp4"
    render_select(edl, {"s": pm, "c": pc}, out, tmp_path, audio_from={"s": ("c", OFFSET_US, 0.0)})
    wav, shift = camera_audio(pm, pc, OFFSET_US, 0.0, src.sample_rate, tmp_path)
    report = verify_output(edl, {"s": pm}, out, audio={"s": (wav, shift)})
    lags = [p.audio_lag_ms for p in report.points if p.audio_lag_ms is not None]
    assert lags and all(abs(v) <= 1 for v in lags), lags
    # Со звуком экрана этот ролик уже не совпадает
    other = verify_output(edl, {"s": pm}, out)
    assert not [p.audio_lag_ms for p in other.points if p.audio_lag_ms is not None and abs(p.audio_lag_ms) <= 1]


def test_moved_pieces_keep_camera_frame_exact(sources, tmp_path):
    """Перестановка: куски не по порядку записи — камера в каждом
    кадре та, что снимала в момент этого кадра экрана. Участков два: сборка
    идёт по участкам (часть через стык перестановки)."""
    screen, camera = sources
    pm, pc = probe(screen.path), probe(camera.path)
    src = pm.source_info("s")
    ranges = [(0, 100), (700, 800), (100, 250), (260, 400), (800, 1000)]  # прыжок 24 с, потом назад
    clips = [Clip(f"c{i}", "s", frame_to_us(a, 25), frame_to_us(b, 25)) for i, (a, b) in enumerate(ranges)]
    edl = compile_edl(clips, {"s": src}, OutputProfile(MAIN_FPS, src.sample_rate))
    plan = VisualPlan(W, H, (Shot(0, 350, "pip", pip_slot="br", pip_size=0.5), Shot(350, 690, "camera")),
                      {"s": CameraSource("c", OFFSET_US, 0.0)})
    out = tmp_path / "перестановка с камерой.mp4"
    render_select(edl, {"s": pm, "c": pc}, out, tmp_path, visual=plan)
    src_of = [f for a, b in ranges for f in range(a, b)]
    frames = list(iter_frames(out))
    assert len(frames) == len(src_of) == 690
    x, y, pw, ph = pip_geometry(plan.shots[0], W, H, W, H)
    wrong = []
    for k, img in enumerate(frames):
        if k < 350:
            got = (top_row_number(img), scaled_number(img[y:y + ph, x:x + pw]))
            want = (src_of[k], camera_frame_at(src_of[k]))
        else:
            got, want = read_frame_number(img), camera_frame_at(src_of[k])
        if got != want:
            wrong.append((k, got, want))
    assert not wrong, wrong[:10]
