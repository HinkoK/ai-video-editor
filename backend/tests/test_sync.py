"""Синхронизация камеры с записью экрана по звуку."""

from fractions import Fraction

import numpy as np
import pytest

from longform.sync import SR, synchronize


def speechy(seconds: float, seed: int = 1) -> np.ndarray:
    """Похоже на речь: всплески шума по 80–300 мс с паузами, громкость гуляет."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    env = np.zeros(n, np.float32)
    i = 0
    while i < n:
        burst = int(rng.uniform(0.08, 0.3) * SR)
        env[i: i + burst] = rng.uniform(0.3, 1.0)
        i += burst + int(rng.uniform(0.03, 0.6) * SR)
    return (rng.standard_normal(n).astype(np.float32) * env * 3000).astype(np.float32)


def camera_from(main: np.ndarray, offset_us: float, ppm: float, seconds: float, seed: int = 7) -> np.ndarray:
    """Звук камеры: та же речь, хуже микрофон, шум комнаты, свои часы.
    cam_us = offset_us + main_us * (1 + ppm / 1e6)."""
    rng = np.random.default_rng(seed)
    tau = np.arange(int(seconds * SR)) / SR * 1e6  # время камеры, мкс
    t = (tau - offset_us) / (1 + ppm / 1e6)  # то же мгновение по экрану
    x = np.interp(t * SR / 1e6, np.arange(len(main)), main, left=0.0, right=0.0)
    x = np.convolve(x, np.ones(6) / 6, mode="same")  # встроенный микрофон глуше
    x[400:] += 0.3 * x[:-400]  # эхо комнаты 50 мс
    return (x + rng.standard_normal(len(x)) * 150).astype(np.float32)


def test_offset_and_drift_are_found():
    main = speechy(240)
    # Камеру включили на 3,2 с раньше экрана; её часы спешат на 80 ppm (≈0,29 с за час)
    cam = camera_from(main, offset_us=3_200_000, ppm=80, seconds=250)
    r = synchronize(main, cam, Fraction(0), Fraction(0))
    assert r.state == "ok", r
    assert r.offset_us == pytest.approx(3_200_000, abs=500)
    assert r.drift_ppm == pytest.approx(80, abs=10)
    assert r.residual_us <= 1_000
    # Конец записи: модель держит синхронность до долей миллисекунды
    assert r.camera_us(230_000_000) == pytest.approx(3_200_000 + 230_000_000 * (1 + 80e-6), abs=600)


def test_camera_started_later_and_audio_offsets_are_respected():
    main = speechy(200, seed=3)
    cam = camera_from(main, offset_us=-12_500_000, ppm=0, seconds=170, seed=4)
    # У камеры звук начинается на 21 мс позже первого кадра — сдвиг звука это учитывает
    r = synchronize(main, cam, Fraction(0), Fraction(21, 1000))
    assert r.state == "ok"
    assert r.offset_us == pytest.approx(-12_500_000 + 21_000, abs=500)
    assert abs(r.drift_ppm) < 10


def test_unrelated_sound_is_not_synchronized():
    r = synchronize(speechy(120, seed=5), speechy(120, seed=6), Fraction(0), Fraction(0))
    assert r.state == "failed" and r.offset_us is None


@pytest.fixture(scope="module")
def recorded_pair(tmp_path_factory):
    """Экран 25 к/с с «микрофоном» и камера 23,976 к/с, включённая на 1,5 с
    раньше, со своими часами (+60 ppm) и худшим звуком."""
    import wave

    from media_fixtures import make_fixture

    d = tmp_path_factory.mktemp("пара")
    rate = 48000
    main48 = np.repeat(speechy(75, seed=11), rate // SR)  # 8 кГц -> 48 кГц ступеньками: для корреляции хватает
    tau = np.arange(int(80 * rate)) / rate * 1e6
    t = (tau - 1_500_000) / (1 + 60e-6)
    cam48 = np.interp(t * rate / 1e6, np.arange(len(main48)), main48, left=0.0, right=0.0)
    cam48 = np.convolve(cam48, np.ones(12) / 12, mode="same") + np.random.default_rng(3).standard_normal(len(cam48)) * 120

    def wav(name, x):
        p = d / name
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(np.clip(x, -32767, 32767).astype("<i2").tobytes())
        return p

    main = make_fixture(d / "экран.mp4", fps=Fraction(25), frames=75 * 25, audio_wav=wav("m.wav", main48))
    cam = make_fixture(d / "камера.mp4", fps=Fraction(24000, 1001), frames=int(80 * 24000 / 1001),
                       audio_wav=wav("c.wav", cam48), first_number=40000)
    return main, cam


@pytest.mark.media
def test_camera_is_registered_synced_and_kept_out_of_the_cut(tmp_path, recorded_pair):
    from fastapi.testclient import TestClient

    from longform import jobs, store, worker
    from longform.api import Settings, create_app
    from longform.db import connect, migrate
    from longform.edl import EdlError
    from longform.ops import Op
    from longform.probe import probe
    from longform.workspace import Workspace

    main, cam = recorded_pair
    ws = Workspace(tmp_path / "рабочая")
    ws.root.mkdir()
    conn = connect(ws.db)
    migrate(conn)
    client = TestClient(create_app(Settings(
        db_path=ws.db, user_token="u", agent_token="a", allowed_hosts=frozenset({"testserver"}),
        allowed_origins=frozenset(), workspace_root=ws.root)))
    user, agent = {"Authorization": "Bearer u"}, {"Authorization": "Bearer a"}
    pid = client.post("/api/projects", json={"name": "камера и экран"}, headers=user).json()["id"]
    # Камеру без записи экрана свести не с чем
    r = client.post(f"/api/projects/{pid}/assets", json={"path": str(cam.path), "role": "camera"}, headers=user)
    assert r.status_code == 422 and r.json()["code"] == "no_main"
    m = client.post(f"/api/projects/{pid}/assets", json={"path": str(main.path)}, headers=user).json()
    c = client.post(f"/api/projects/{pid}/assets", json={"path": str(cam.path), "role": "camera"}, headers=user).json()
    assert c["role"] == "camera" and c["sync_state"] == "pending" and c["fps"] == "24000/1001"
    worker.serve(ws, stop={}, once=True)  # синхронизация и лёгкая копия камеры

    c = next(a for a in client.get(f"/api/projects/{pid}", headers=user).json()["assets"] if a["id"] == c["id"])
    pm, pc = probe(main.path), probe(cam.path)
    m_off = float(pm.audio.start - pm.video.start) * 1e6
    c_off = float(pc.audio.start - pc.video.start) * 1e6
    expected = 1_500_000 + c_off - m_off * (1 + 60e-6)
    assert c["sync_state"] == "ok" and c["sync_to"] == m["id"], c["sync"]
    assert c["sync_offset_us"] == pytest.approx(expected, abs=1_000)
    assert c["sync_drift_ppm"] == pytest.approx(60, abs=25)
    states = {j["type"]: j["state"] for j in client.get(f"/api/projects/{pid}/jobs", headers=user).json()}
    assert states == {"sync": "completed", "proxy": "completed"}

    # Первая версия — только запись экрана; камера в монтажный лист не входит
    rev = client.post(f"/api/projects/{pid}/revisions/initial", headers=user).json()
    assert {cl["asset_id"] for cl in rev["clips"]} == {m["id"]}
    # Расшифровка и правки по камере не принимаются
    assert client.post(f"/api/projects/{pid}/assets/{c['id']}/transcribe", json={}, headers=user).status_code == 422
    with pytest.raises(EdlError, match="камеру не режут"):
        store.create_proposal(conn, pid, base_revision_id=rev["id"], pass_="cleanup", rationale="r",
                              policy_version="t", actor="agent",
                              ops=[Op("remove", c["id"], 1_000_000, 2_000_000, "technical_dead_air", "x")])
    # Ручная подстройка — только автор; после неё состояние «вручную»
    url = f"/api/projects/{pid}/assets/{c['id']}/sync/nudge"
    assert client.post(url, json={"delta_us": 41_708}, headers=agent).status_code == 403
    moved = client.post(url, json={"delta_us": 41_708}, headers=user).json()
    assert moved["sync_state"] == "manual" and moved["sync_offset_us"] == c["sync_offset_us"] + 41_708
    # Пересчитать можно, даже если такая задача уже была
    again = client.post(f"/api/projects/{pid}/assets/{c['id']}/sync", headers=user).json()
    assert again["state"] == "queued"
    conn.close()
