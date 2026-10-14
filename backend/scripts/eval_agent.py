"""Оценка предложений Claude по разметке автора.

    uv run python scripts/eval_agent.py --project prj_... --labels разметка.json \\
        [--proposal prp_...] [--run-log прогон.jsonl] [--workspace ~/Movies/AI_Video_Editor_Media]

Разметка — время исходника, как его видно в плеере (мм:сс.с), формат в
docs/eval/labels-template.json. Предложения оцениваются так, будто приняты
целиком (что предложил Claude), и отдельно — по решениям автора.

Главная метрика — потерянные обязательные места: должно быть 0, иначе это
блокер, как бы хорошо ни было остальное. Процент принятых правок сам по себе
качество не доказывает: важно, что именно пришлось отклонять и почему.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from longform import store  # noqa: E402
from longform.db import connect  # noqa: E402
from longform.workspace import Workspace  # noqa: E402

TOUCH_US = 100_000  # задело, если вырезано больше 0,1 с
CAUGHT_SHARE = 0.8  # плохой дубль найден, если вырезано 80 % и больше


def parse_time(text: str) -> int:
    parts = [float(p) for p in str(text).replace(",", ".").split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return round(seconds * 1_000_000)


def removed_overlap(ranges: list[tuple[int, int]], a: int, b: int) -> int:
    return sum(max(0, min(b, y) - max(a, x)) for x, y in ranges)


def evaluate(conn, pid: str, labels: dict, proposal_ids: list[str]) -> dict:
    assets = store.get_project(conn, pid)["assets"]
    by_ref = {f"A{i}": a["id"] for i, a in enumerate(assets, 1)}

    def labelled(kind):
        out = []
        for item in labels.get(kind, []):
            asset = by_ref[item.get("asset", "A1")]
            out.append((asset, parse_time(item["from"]), parse_time(item["to"]), item.get("what", "")))
        return out

    proposed, accepted, ops = {}, {}, []
    for prop_id in proposal_ids:
        prop = store.get_proposal(conn, pid, prop_id)
        for o in prop["ops"]:
            if o["kind"] != "remove":
                continue
            ops.append(o)
            proposed.setdefault(o["asset_id"], []).append((o["src_start_us"], o["src_end_us"]))
            if o["decision"] == "accepted":
                accepted.setdefault(o["asset_id"], []).append((o["src_start_us"], o["src_end_us"]))

    def judge(cuts):
        lost_required = [w for a, x, y, w in labelled("required") if removed_overlap(cuts.get(a, []), x, y) > TOUCH_US]
        lost_good = [w for a, x, y, w in labelled("good_takes") if removed_overlap(cuts.get(a, []), x, y) > TOUCH_US]
        bad = labelled("bad_takes")
        caught = [w for a, x, y, w in bad if removed_overlap(cuts.get(a, []), x, y) >= CAUGHT_SHARE * (y - x)]
        return {"lost_required": lost_required, "lost_good_takes": lost_good,
                "bad_takes_caught": f"{len(caught)} из {len(bad)}", "missed_bad_takes":
                    [w for a, x, y, w in bad if w not in caught]}

    decided = [o for o in ops if o["decision"] != "pending"]
    return {
        "proposals": proposal_ids,
        "removals": len(ops),
        "as_proposed": judge(proposed),
        "after_review": judge(accepted) if decided else "автор ещё не решал",
        "excluded_by_review": [{"why_agent": o["why"], "reason": o["reason"]} for o in ops if o["decision"] == "excluded"],
        "risk_flags": {f: sum(f in o["risk_flags"] for o in ops) for f in
                       ("cut_in_speech", "high_impact", "protected", "reason_other", "long_cut")},
        "blocker": bool(judge(proposed)["lost_required"]),
    }


def run_cost(log: Path) -> dict:
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    res = next((e for e in events if e.get("type") == "result"), {})
    tools = [c["name"] for e in events if e.get("type") == "assistant"
             for c in e["message"].get("content", []) if c.get("type") == "tool_use"]
    return {"cost_usd": res.get("total_cost_usd"), "turns": res.get("num_turns"),
            "duration_s": round((res.get("duration_ms") or 0) / 1000, 1),
            "non_editor_tools": sorted({t for t in tools if not t.startswith("mcp__longform__")}),
            "permission_denials": len(res.get("permission_denials") or [])}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--proposal", action="append", help="по умолчанию — все предложения агента")
    ap.add_argument("--run-log", type=Path, help="журнал claude -p --output-format stream-json")
    ap.add_argument("--workspace", type=Path)
    args = ap.parse_args()
    ws = Workspace(args.workspace.expanduser()) if args.workspace else Workspace.from_env()
    conn = connect(ws.db)
    proposals = args.proposal or [p["id"] for p in store.list_proposals(conn, args.project)
                                  if p["created_by"] == "agent"]
    report = evaluate(conn, args.project, json.loads(args.labels.read_text()), proposals)
    if args.run_log:
        report["run"] = run_cost(args.run_log)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
