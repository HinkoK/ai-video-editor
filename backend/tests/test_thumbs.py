"""Дорожка видео на таймлайне: полоса кадров из лёгкой копии."""

import json
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from longform import jobs, store, thumbs, worker
from longform.api import Settings, create_app
from longform.db import connect, migrate
from longform.ffmpeg import QUIET, run
from longform.workspace import Workspace
from media_fixtures import BITS, H, W, make_fixture

pytestmark = pytest.mark.media

FPS = 25


def sheet_pixels(path, meta) -> np.ndarray:
    raw = run(["ffmpeg", *QUIET, "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    return np.frombuffer(raw, np.uint8).reshape(meta["rows"] * meta["tile_h"], meta["cols"] * meta["tile_w"])


def tile_number(tile: np.ndarray) -> int:
    """Номер кадра по коду в уменьшенном кадре (как read_frame_number, в масштабе)."""
    sx, sy = tile.shape[1] / W, tile.shape[0] / H
    cell = W // BITS
    top = bottom = 0
    for bit in range(BITS):
        x = int((bit * cell + cell / 2) * sx)
        t = tile[int(40 * sy) - 2:int(40 * sy) + 2, x - 1:x + 2].mean() > 128
        b = tile[int(104 * sy) - 2:int(104 * sy) + 2, x - 1:x + 2].mean() > 128
        if t == b:
            return -1
        top = (top << 1) | int(t)
        bottom = (bottom << 1) | int(not b)
    return top if top == bottom else -1


@pytest.fixture(scope="module")
def proxy_like(tmp_path_factory):
    # Как лёгкая копия: опорный кадр раз в секунду
    return make_fixture(tmp_path_factory.mktemp("копия") / "лёгкая копия.mp4", frames=60 * FPS, gop=FPS)


def test_each_frame_is_within_half_a_second_of_its_time(proxy_like):
    meta = thumbs.ensure(proxy_like.path)
    assert meta["step_us"] == 2_000_000 and meta["tile_h"] == thumbs.HEIGHT
    assert meta["tile_w"] == 96  # 256×144 → 96×54
    assert 29 <= meta["count"] <= 30
    image, _ = thumbs.paths(proxy_like.path)
    pixels = sheet_pixels(image, meta)
    tw, th = meta["tile_w"], meta["tile_h"]
    for k in range(meta["count"]):
        r, c = divmod(k, meta["cols"])
        n = tile_number(pixels[r * th:(r + 1) * th, c * tw:(c + 1) * tw])
        expected = k * meta["step_us"] * FPS / 1_000_000
        assert abs(n - expected) <= FPS / 2, (k, n, expected)


def test_built_once_and_rebuilt_when_proxy_is_newer(proxy_like):
    image, meta_path = thumbs.paths(proxy_like.path)
    thumbs.ensure(proxy_like.path)
    built = image.stat().st_mtime_ns
    thumbs.ensure(proxy_like.path)
    assert image.stat().st_mtime_ns == built
    later = proxy_like.path.stat().st_mtime + 10
    os.utime(proxy_like.path, (later, later))
    thumbs.ensure(proxy_like.path)
    assert image.stat().st_mtime_ns != built
    assert json.loads(meta_path.read_text())["version"] == thumbs.VERSION


def test_step_grows_for_long_files_to_keep_the_sheet_small():
    assert thumbs.step_seconds(47) == 2
    assert thumbs.step_seconds(3600) == 6
    assert thumbs.step_seconds(3 * 3600) * thumbs.MAX_THUMBS >= 3 * 3600


def test_api_needs_a_proxy_then_serves_the_sheet(tmp_path, fx_plain):
    ws = Workspace(tmp_path / "рабочая папка")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "Урок", "user")
    cam = store.register_asset(conn, p["id"], str(fx_plain.path), "user")
    client = TestClient(create_app(Settings(
        db_path=ws.db, user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset(), workspace_root=ws.root)))
    auth = {"Authorization": "Bearer a"}
    url = f"/api/projects/{p['id']}/assets/{cam['id']}/thumbs"
    r = client.get(url, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "no_proxy"

    jobs.enqueue(conn, p["id"], "proxy", actor="user", asset_id=cam["id"])
    worker.serve(ws, stop={}, once=True)
    meta = client.get(url, headers=auth).json()
    assert meta["count"] >= 1 and meta["tile_h"] == thumbs.HEIGHT
    img = client.get(url + ".jpg", headers=auth)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert img.content[:2] == b"\xff\xd8"
    # Полоса лежит рядом с лёгкой копией: стирается вместе с ней
    proxy = jobs.output_of(conn, p["id"], conn.execute("SELECT id FROM jobs").fetchone()["id"])
    assert thumbs.paths(proxy)[0].parent == ws.project(p["id"], "proxies")
    proxy.unlink()
    assert client.get(url, headers=auth).json()["code"] == "no_proxy"
    conn.close()

