"""Пробный проект для проверки интерфейса: два файла (камера и экран) с
синтетической русской речью, лёгкие копии, расшифровка, правки Claude
(часть проверена, одна ждёт решения из-за жёсткой защиты), открытое
предложение, бриф-черновик, метки, комментарии, пробный ролик и экспорт.

    uv run python scripts/demo_workspace.py <пустая папка>
    LONGFORM_WORKSPACE=<папка> LONGFORM_PORT=8790 uv run longform-server

Это не голос автора и не его запись: только чтобы экраны было на чём смотреть.
Около двух минут на ноутбуке (расшифровка whisper.cpp).
"""

import argparse
import subprocess
import sys
from pathlib import Path

from longform import asset_data, jobs, store, worker
from longform.db import connect, migrate
from longform.ffmpeg import QUIET
from longform.ops import Op
from longform.timebase import frame_to_us
from longform.workspace import Workspace

CAMERA = (
    "Привет! Сегодня покажу, как я монтирую длинные видео вместе с Claude. [[slnc 1800]] "
    "Сначала подключаем файлы записи: камеру и экран. "
    "Сейчас покажу настройки, где [[slnc 400]] нет, подождите. [[slnc 1200]] "
    "Сейчас покажу настройки, где Claude хранит бриф. Их немного, и все они на одном экране. [[slnc 2600]] "
    "Это займёт три [[slnc 500]] три минуты. [[slnc 700]] Это займёт три минуты. "
    "Если бриф не подтверждён, Claude не начнёт править. [[slnc 1500]] "
    "Дальше защищённые места. Жёсткую защиту ставлю я, мягкую программа ставит сама. [[slnc 3200]] "
    "Например, на фразы с числами и отрицаниями."
)
SCREEN = (
    "Теперь экран. Открываем раздел проверки. [[slnc 900]] "
    "Здесь видно всё, что Claude сделал с первой версией. [[slnc 2200]] "
    "Он убрал несколько мест, для двух минут записи это немало. Ну и [[slnc 600]] да. [[slnc 1500]] "
    "Справа два плеера: до и после. Сначала смотрю до, потом после. [[slnc 1000]] "
    "Стоимость одного часа в долларах [[slnc 800]] Стоимость одного часа примерно двадцать долларов. [[slnc 2000]] "
    "Если стык щёлкает, сдвигаю его на пару кадров. На этом всё."
)
RATE = "30000/1001"


def make_media(path: Path, text: str, video: str) -> None:
    wav = path.with_suffix(".wav")
    subprocess.run(["say", "-v", "Milena", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@48000", text],
                   check=True)
    subprocess.run(
        ["ffmpeg", *QUIET, "-y", "-f", "lavfi", "-i", video, "-i", str(wav), "-shortest",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-g", "60",
         "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True)
    wav.unlink()


def find(segments: list[dict], *variants: str) -> dict:
    """Фраза по любому из вариантов: Whisper пишет «три» то словом, то цифрой."""
    for s in segments:
        if any(v.lower() in s["text"].lower() for v in variants):
            return s
    raise SystemExit(f"в расшифровке нет фразы «{variants[0]}»: {[s['text'] for s in segments]}")


def span(s: dict) -> tuple[int, int]:
    return (s["speech_start_us"] or s["src_start_us"], s["speech_end_us"] or s["src_end_us"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dir", type=Path)
    args = parser.parse_args()
    root = args.dir.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        sys.exit(f"папка не пустая: {root}")
    media = root / "медиа"
    media.mkdir(parents=True)
    ws = Workspace(root / "рабочая")
    ws.root.mkdir()

    print("медиа: камера и экран (голос macOS)…", flush=True)
    camera, screen = media / "камера.mp4", media / "экран.mp4"
    make_media(camera, CAMERA, f"testsrc2=size=1920x1080:rate={RATE}")
    make_media(screen, SCREEN, f"life=size=480x270:rate={RATE}:mold=10:ratio=0.12:death_color=#202020"
                               ":life_color=#5B9BFF,scale=1920:1080:flags=neighbor")

    conn = connect(ws.db)
    migrate(conn)
    p = store.create_project(conn, "Как я монтирую с Claude", "user")
    pid = p["id"]
    cam = store.register_asset(conn, pid, str(camera), "user")["id"]
    scr = store.register_asset(conn, pid, str(screen), "user")["id"]
    for aid in (cam, scr):
        jobs.enqueue(conn, pid, "proxy", actor="user", asset_id=aid)
        jobs.enqueue(conn, pid, "transcribe", actor="user", asset_id=aid)
    print("лёгкие копии и расшифровка…", flush=True)
    worker.serve(ws, stop={}, once=True)

    first = store.create_initial_revision(conn, pid, "user")
    seg = {aid: asset_data.segments(conn, aid) for aid in (cam, scr)}
    sil = {aid: sorted(asset_data.silences(conn, aid), key=lambda s: -s["duration_us"]) for aid in (cam, scr)}

    store.save_brief(conn, pid, {
        "audience": "Авторы YouTube, которые монтируют сами",
        "promise": "Как монтировать длинное видео вместе с Claude",
        "intended_outcome": "Подключит камеру и экран, получит черновой монтаж и проверит каждую правку",
        "genre": "разговорный урок с записью экрана", "language": "ru", "mode": "conservative_cleanup",
        "required_points": ["Пример с ценами", "Показ настроек брифа целиком"],
        "forbidden_changes": ["Порядок блоков"],
    }, "agent")

    # Жёсткая защита на первом дубле фразы про цену: правка Claude здесь ждёт автора
    price = find(seg[scr], "в долларах")
    a, b = span(price)
    store.add_protection(conn, pid, asset_id=scr, src_start_us=a, src_end_us=b, kind="hard",
                         reason="пример с ценами", actor="user")

    def rm(aid, s, reason, why):
        a, b = span(s)
        return Op("remove", aid, a, b, reason, why, segment_ids=[s["id"]])

    def gap(aid, k, why):
        s = sil[aid][k]
        return Op("remove", aid, s["src_start_us"], s["src_end_us"], "technical_dead_air", why)

    ops = [
        gap(cam, 1, "Пауза после приветствия, в кадре ничего не меняется."),
        rm(cam, find(seg[cam], "нет, подождите"), "false_start",
           "Начал фразу «Сейчас покажу настройки», сбился и повторил целиком. Оставлен второй дубль."),
        gap(cam, 0, "Длинная пауза перед примером, в кадре ничего не происходит."),
        rm(cam, find(seg[cam], "три... три", "3... 3", "три три"), "failed_take",
           "Фразу «Это займёт три минуты» сказал дважды, в первый раз с запинкой. Оставлен второй дубль."),
        rm(scr, find(seg[scr], "ну и"), "other", "Оговорка без смысла в конце фразы."),
        gap(scr, 0, "Пауза, пока открывался раздел проверки."),
        rm(scr, price, "failed_take", "Первый дубль фразы про цену оборван. Оставлен второй."),
    ]
    applied = store.create_proposal(conn, pid, base_revision_id=first["id"], pass_="cleanup",
                                    rationale="Черновой монтаж: ложные старты, неудачные дубли и паузы.",
                                    policy_version="playbook-v2", ops=ops, actor="agent")
    # Часть уже проверена автором
    by_pos = {o["position"]: o for o in applied["ops"]}
    store.review_op(conn, pid, applied["id"], by_pos[0]["id"], action="keep", actor="user")
    store.review_op(conn, pid, applied["id"], by_pos[5]["id"], action="revert", actor="user")

    current = store.get_project(conn, pid)["current_revision_id"]
    rev = store.get_revision(conn, pid, current)
    at = lambda share: int(rev["total_frames"] * share)  # noqa: E731
    store.add_marker(conn, pid, actor="user", revision_id=current, edited_frame=at(0.2))
    store.add_marker(conn, pid, actor="agent", revision_id=current, edited_frame=at(0.7), label="начало экрана")
    store.add_comment(conn, pid, text="Проверить громкость после склейки", actor="user", revision_id=current,
                      edited_frame=at(0.45))
    # Комментарий на месте, которое потом вырезано: «не входит в эту версию»
    cut = sil[cam][0]  # эту паузу Claude вырезал и правка не возвращена
    store.add_comment(conn, pid, text="Здесь можно короче", actor="user", asset_id=cam,
                      src_us=(cut["src_start_us"] + cut["src_end_us"]) // 2)

    # Ещё одно предложение в режиме «только предлагает» — открытое
    store.set_agent_mode(conn, pid, "propose", "user")
    store.create_proposal(conn, pid, base_revision_id=current, pass_="cleanup",
                          rationale="Ещё две паузы, которые я пропустил в первом проходе.",
                          policy_version="playbook-v2",
                          ops=[gap(cam, 2, "Пауза перед «Например», можно короче."),
                               gap(scr, 1, "Пауза после «да», ничего не происходит.")], actor="agent")
    store.set_agent_mode(conn, pid, "apply", "user")

    joints = store.joints(store.compile_revision(conn, pid, current))
    if joints:
        j = joints[0]["out_frame"]
        jobs.enqueue(conn, pid, "preview", actor="user", revision_id=current,
                     params={"out_start_frame": max(0, j - 90), "out_end_frame": min(rev["total_frames"], j + 90)})
    jobs.enqueue(conn, pid, "export", actor="user", revision_id=current)
    print("пробный ролик и экспорт…", flush=True)
    worker.serve(ws, stop={}, once=True)
    conn.close()
    print(f"готово: LONGFORM_WORKSPACE={ws.root}")


if __name__ == "__main__":
    main()
