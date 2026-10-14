"""Проверка на длинной записи (этап 8): весь путь с замерами.

    uv run python scripts/longform_gate.py /путь/камера.mp4 [/путь/экран.mp4] \\
        [--workspace ~/Movies/AI_Video_Editor_Media] [--skip-transcribe] [--interrupt-after 20]

Что делает, по стадиям, с временем и пиком памяти:
1. подключение файлов (только чтение, без копий) и первая версия;
2. лёгкая копия, громкость и паузы, расшифровка;
3. стресс-предложение: вырезать каждую паузу длиннее секунды. Это
   техническая проверка множества стыков, а не монтажное решение;
4. утверждение, пробные ролики в начале, середине и конце;
5. полный экспорт из оригиналов со сверкой с исходником по 40 стыкам;
6. по желанию: прерывание экспорта посреди работы и продолжение;
7. повторное открытие базы и чтение версии.

Отчёт печатается и сохраняется в projects/<id>/gate-report.json.
Исходники не меняются: проверяем это по отпечатку в конце.
"""

import argparse
import json
import resource
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from longform import asset_data, jobs, qa, store, worker  # noqa: E402
from longform.db import connect, migrate  # noqa: E402
from longform.ops import Op  # noqa: E402
from longform.workspace import Workspace  # noqa: E402

MB = 2**20


def peak_children_mb() -> int:
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // MB  # на macOS — байты


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class Gate:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.conn = connect(ws.db)
        migrate(self.conn)
        self.stages: list[dict] = []

    def stage(self, name: str, fn):
        print(f"→ {name}…", flush=True)
        started = time.monotonic()
        try:
            result = fn()
            ok = True
        except Exception as e:  # отчёт должен дойти до конца и сказать, где сломалось
            result, ok = repr(e), False
        took = round(time.monotonic() - started, 1)
        self.stages.append({"stage": name, "ok": ok, "seconds": took, "peak_child_mb": peak_children_mb(),
                            "result": result})
        print(f"  {'готово' if ok else 'ОШИБКА'} за {took} с: {result}", flush=True)
        return result if ok else None

    def run_queue(self):
        worker.serve(self.ws, stop={}, once=True)

    def jobs_done(self, pid: str, ids: list[str]) -> dict:
        out = {}
        for jid in ids:
            j = jobs.get_job(self.conn, pid, jid)
            out[j["type"]] = {"state": j["state"], "error": j["error"]}
        if any(v["state"] != "completed" for v in out.values()):
            raise RuntimeError(out)
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--skip-transcribe", action="store_true")
    ap.add_argument("--interrupt-after", type=float, default=0,
                    help="прервать экспорт через столько секунд и продолжить (0 — не прерывать)")
    ap.add_argument("--min-pause", type=float, default=1.0)
    ap.add_argument("--grid", type=float, default=0,
                    help="если пауз нет (синтетика): вырезать по 1 с каждые столько секунд")
    args = ap.parse_args()
    ws = Workspace(args.workspace.expanduser()) if args.workspace else Workspace.from_env()
    ws.root.mkdir(parents=True, exist_ok=True)
    gate = Gate(ws)
    conn = gate.conn
    fingerprints = {}

    project = gate.stage("проект и файлы", lambda: store.create_project(
        conn, f"Проверка на длинной записи {time.strftime('%Y-%m-%d %H:%M')}", "user"))
    pid = project["id"]
    assets = []
    for f in args.files:
        a = gate.stage(f"подключение {f.name}", lambda f=f: store.register_asset(conn, pid, str(f), "user"))
        if a is None:
            sys.exit("файл не подключился — дальше проверять нечего")
        assets.append(a)
        fingerprints[a["id"]] = a["fingerprint"]
    rev = gate.stage("первая версия", lambda: store.create_initial_revision(conn, pid, "user"))

    def prepare():
        ids = []
        for a in assets:
            ids.append(jobs.enqueue(conn, pid, "proxy", actor="user", asset_id=a["id"])["id"])
            ids.append(jobs.enqueue(conn, pid, "analyze", actor="user", asset_id=a["id"])["id"])
            if not args.skip_transcribe:
                ids.append(jobs.enqueue(conn, pid, "transcribe", actor="user", asset_id=a["id"])["id"])
        gate.run_queue()
        return gate.jobs_done(pid, ids)

    gate.stage("лёгкая копия, громкость и паузы" + ("" if args.skip_transcribe else ", расшифровка"), prepare)

    def stress():
        ops = []
        for a in assets:
            for s in asset_data.silences(conn, a["id"]):
                if s["duration_us"] >= args.min_pause * 1_000_000:
                    ops.append(Op("remove", a["id"], s["src_start_us"], s["src_end_us"], "technical_dead_air",
                                  "стресс-проверка: каждая длинная пауза"))
        if not ops and args.grid > 0:
            step = int(args.grid * 1_000_000)
            for a in assets:
                for start in range(step - 1_000_000, a["duration_us"] - step, step):
                    ops.append(Op("remove", a["id"], start, start + 1_000_000, "technical_dead_air",
                                  "стресс-проверка: равномерные вырезы"))
        if not ops:
            raise RuntimeError("пауз длиннее порога нет — стыков для проверки не будет (для синтетики --grid)")
        prop = store.create_proposal(conn, pid, base_revision_id=rev["id"], pass_="cleanup",
                                     rationale="стресс-проверка стыков", policy_version="gate", ops=ops,
                                     actor="agent")
        # В режиме «Claude правит сразу» (по умолчанию) предложение уже применено
        done = prop if prop["status"] == "approved" else store.approve(
            conn, pid, prop["id"], actor="user", acknowledge=[o["id"] for o in prop["ops"] if o["risk_flags"]])
        r = store.get_revision(conn, pid, done["result_revision_id"])
        return {"cuts": len(ops), "clips": len(r["clips"]), "duration_s": round(r["duration_us"] / 1e6, 1),
                "flagged_cut_in_speech": sum("cut_in_speech" in o["risk_flags"] for o in prop["ops"]),
                "revision": r["id"]}

    stressed = gate.stage("стресс-предложение и утверждение", stress)
    if stressed is None:
        sys.exit("без утверждённой версии дальше проверять нечего")
    rid = stressed["revision"]
    edl = store.compile_revision(conn, pid, rid)

    def previews():
        fps = edl.profile.fps
        span = int(fps * 6)
        ids = []
        for center in (0, edl.total_frames // 2, edl.total_frames - 1):
            a = max(0, min(center - span // 2, edl.total_frames - span))
            ids.append(jobs.enqueue(conn, pid, "preview", actor="user", revision_id=rid,
                                    params={"out_start_frame": a, "out_end_frame": a + span})["id"])
        gate.run_queue()
        return [jobs.get_job(conn, pid, j)["state"] for j in ids]

    gate.stage("пробные ролики: начало, середина, конец", previews)

    def export():
        job = jobs.enqueue(conn, pid, "export", actor="user", revision_id=rid)
        if args.interrupt_after > 0:
            stop: dict = {}
            t = threading.Thread(target=worker.serve, args=(ws, stop))
            t.start()
            time.sleep(args.interrupt_after)
            stop["reason"] = "shutdown"
            t.join()
            state = jobs.get_job(conn, pid, job["id"])["state"]
            if state != "interrupted":
                raise RuntimeError(f"после прерывания задача в состоянии {state}, а не interrupted")
        gate.run_queue()
        j = jobs.get_job(conn, pid, job["id"])
        if j["state"] != "completed":
            raise RuntimeError(j)
        report = qa.list_reports(conn, pid)[0]
        return {"attempts": j["attempts"], "size_mb": round(Path(j["output_path"]).stat().st_size / MB, 1),
                "critical": report["critical"], "technical": report["technical"]}

    gate.stage("полный экспорт и сверка" + (" с прерыванием" if args.interrupt_after else ""), export)

    def reopen():
        c = connect(ws.db)
        r = store.get_revision(c, pid, rid)
        c.close()
        return {"clips": len(r["clips"])}

    gate.stage("повторное открытие базы", reopen)

    def originals_untouched():
        changed = []
        for a in assets:
            if store.quick_fingerprint(Path(a["path"])) != fingerprints[a["id"]]:
                changed.append(a["path"])
        if changed:
            raise RuntimeError(f"исходники изменились: {changed}")
        return "отпечатки совпали"

    gate.stage("исходники не тронуты", originals_untouched)

    project_dir = ws.root / "projects" / pid
    report = {
        "files": [str(f) for f in args.files],
        "source_duration_s": [round(a["duration_us"] / 1e6, 1) for a in assets],
        "stages": gate.stages,
        "workspace_mb": round(dir_size(project_dir) / MB, 1),
        "free_disk_gb": round(shutil.disk_usage(ws.root).free / 2**30, 1),
        "ok": all(s["ok"] for s in gate.stages),
    }
    out = project_dir / "gate-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\nИТОГ: {'пройдено' if report['ok'] else 'есть ошибки'}; данные проекта {report['workspace_mb']} МБ, "
          f"свободно {report['free_disk_gb']} ГБ; отчёт {out}")


if __name__ == "__main__":
    main()
