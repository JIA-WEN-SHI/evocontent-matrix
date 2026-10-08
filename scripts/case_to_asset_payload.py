from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build asset payload from case payload with optional account strategy context.",
    )
    parser.add_argument("--case-payload", required=True, help="Input payload_case.json")
    parser.add_argument("--output-file", required=True, help="Output payload_asset.json")
    parser.add_argument("--account-strategy-file", default="", help="Optional strategy json file")
    parser.add_argument("--account-id", default="", help="Optional account id")
    parser.add_argument("--domain-slug", default="", help="Override domain slug from case payload")
    parser.add_argument("--source", default="asset_builder", help="Payload source")
    parser.add_argument("--source-run-id", default="", help="Optional source run id")
    parser.add_argument("--max-items", type=int, default=200, help="Max generated assets")
    parser.add_argument("--min-score", type=float, default=0.0, help="Minimum metric score")
    return parser.parse_args()


def as_text(value: Any) -> str:
    return str(value or "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def to_list(value: Any, max_items: int = 8) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        s = as_text(item)
        if not s:
            continue
        out.append(s)
        if len(out) >= max_items:
            break
    return out


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def metric_score(metrics: Dict[str, Any]) -> float:
    def to_num(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    likes = to_num(metrics.get("likes"))
    collects = to_num(metrics.get("collects"))
    comments = to_num(metrics.get("comments_count")) + to_num(metrics.get("comments"))
    shares = to_num(metrics.get("shares"))
    return likes + 1.5 * collects + 1.2 * comments + 1.0 * shares


def infer_asset_type(title: str, content: str) -> str:
    text = f"{title} {content}".lower()
    if re.search(r"(step|sop|template|checklist|流程|步骤|清单|模板|攻略)", text):
        return "sop"
    if re.search(r"(hook|开头|标题|爆点|吸引)", text):
        return "hook"
    if re.search(r"(cta|转化|私信|咨询|引导|call to action)", text):
        return "cta"
    if re.search(r"(误区|避坑|对比|原因|why|区别)", text):
        return "insight"
    return "structure"


def clean_line(line: str) -> str:
    s = as_text(line)
    s = re.sub(r"#\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_steps(content: str, limit: int = 4) -> List[str]:
    raw_lines = re.split(r"[\r\n]+", content or "")
    lines = [clean_line(x) for x in raw_lines]
    lines = [x for x in lines if x]
    if not lines:
        return []
    out: List[str] = []
    for line in lines:
        if len(line) < 6:
            continue
        out.append(line[:120])
        if len(out) >= limit:
            break
    return out


def dedup_keep_order(items: List[str], limit: int = 30) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        s = as_text(item)
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def normalize_strategy(raw: Dict[str, Any]) -> Dict[str, Any]:
    src = raw
    if isinstance(raw.get("strategy_profile"), dict):
        src = raw["strategy_profile"]
    return {
        "persona_name": as_text(src.get("persona_name")),
        "ip_positioning": as_text(src.get("ip_positioning")),
        "tone_style": as_text(src.get("tone_style")),
        "primary_goal": as_text(src.get("primary_goal")) or "lead_conversion",
        "cta_style": as_text(src.get("cta_style")),
        "audience": to_list(src.get("audience"), max_items=6),
        "pain_points": to_list(src.get("pain_points"), max_items=6),
        "content_pillars": to_list(src.get("content_pillars"), max_items=6),
        "forbidden_claims": to_list(src.get("forbidden_claims"), max_items=8),
        "focus_keywords": to_list(src.get("focus_keywords"), max_items=8),
    }


def build_asset_content(
    *,
    case_title: str,
    case_content: str,
    strategy: Dict[str, Any],
    asset_type: str,
) -> str:
    steps = extract_steps(case_content, limit=4)
    persona = strategy.get("persona_name", "")
    positioning = strategy.get("ip_positioning", "")
    tone = strategy.get("tone_style", "")
    goal = strategy.get("primary_goal", "lead_conversion")
    pillars = strategy.get("content_pillars", []) or []
    forbidden = strategy.get("forbidden_claims", []) or []
    cta_style = strategy.get("cta_style", "")

    lines: List[str] = []
    lines.append("[Account Context]")
    lines.append(f"- Persona: {persona or 'default'}")
    lines.append(f"- Positioning: {positioning or 'default'}")
    lines.append(f"- Tone: {tone or 'clear and practical'}")
    lines.append(f"- Primary Goal: {goal}")
    if pillars:
        lines.append(f"- Pillars: {', '.join(pillars[:4])}")

    lines.append("")
    lines.append(f"[Reusable Method | {asset_type}]")
    lines.append(f"- Source Topic: {case_title[:140]}")
    if steps:
        for idx, step in enumerate(steps, start=1):
            lines.append(f"{idx}. {step}")
    else:
        lines.append("1. Lead with a specific hook that frames urgency or decision context.")
        lines.append("2. Provide a compact structure: conclusion -> evidence -> action.")
        lines.append("3. End with a clear CTA aligned to account conversion goal.")

    lines.append("")
    lines.append("[Execution Constraints]")
    if forbidden:
        lines.append(f"- Avoid Claims: {', '.join(forbidden[:5])}")
    else:
        lines.append("- Avoid exaggerated guarantees and unverifiable promises.")
    lines.append(f"- CTA Style: {cta_style or 'soft consult invite'}")

    return "\n".join(lines)[:20000]


def build_asset_summary(case_title: str, strategy: Dict[str, Any], asset_type: str) -> str:
    goal = as_text(strategy.get("primary_goal")) or "conversion"
    pillar = ""
    pillars = strategy.get("content_pillars")
    if isinstance(pillars, list) and pillars:
        pillar = as_text(pillars[0])
    if pillar:
        return f"[{asset_type}] {goal} | {pillar} | {case_title[:80]}"
    return f"[{asset_type}] {goal} | {case_title[:90]}"


def main() -> int:
    args = parse_args()
    case_payload_path = Path(args.case_payload)
    output_file = Path(args.output_file)
    if not case_payload_path.exists():
        raise SystemExit(f"case payload not found: {case_payload_path}")

    case_payload = load_json(case_payload_path)
    items = case_payload.get("items")
    if not isinstance(items, list):
        raise SystemExit("invalid case payload: items must be a list")

    strategy_raw: Dict[str, Any] = {}
    if args.account_strategy_file:
        strategy_path = Path(args.account_strategy_file)
        if not strategy_path.exists():
            raise SystemExit(f"account strategy file not found: {strategy_path}")
        strategy_raw = load_json(strategy_path)
    strategy = normalize_strategy(strategy_raw)

    out_items: List[Dict[str, Any]] = []
    seen_refs = set()
    timestamp = now_iso()

    for case_item in items:
        if not isinstance(case_item, dict):
            continue

        title = as_text(case_item.get("title"))
        content = as_text(case_item.get("content"))
        url = as_text(case_item.get("url"))
        platform = as_text(case_item.get("platform")) or "xiaohongshu"
        source_ref_case = as_text(case_item.get("source_ref")) or url or title
        metrics = case_item.get("metrics") if isinstance(case_item.get("metrics"), dict) else {}
        tags = case_item.get("tags") if isinstance(case_item.get("tags"), list) else []

        if not (title or content):
            continue
        score = metric_score(metrics)
        if score < float(args.min_score):
            continue

        asset_type = infer_asset_type(title, content)
        summary = build_asset_summary(title, strategy, asset_type)
        method_content = build_asset_content(
            case_title=title,
            case_content=content,
            strategy=strategy,
            asset_type=asset_type,
        )

        pillar_tags = strategy.get("content_pillars") if isinstance(strategy.get("content_pillars"), list) else []
        focus_tags = strategy.get("focus_keywords") if isinstance(strategy.get("focus_keywords"), list) else []
        merged_tags = dedup_keep_order(
            [asset_type] + [as_text(x) for x in tags] + [as_text(x) for x in pillar_tags] + [as_text(x) for x in focus_tags],
            limit=30,
        )

        asset_ref = f"asset:{source_ref_case}:{asset_type}"
        suffix = 1
        while asset_ref in seen_refs:
            suffix += 1
            asset_ref = f"asset:{source_ref_case}:{asset_type}:{suffix}"
        seen_refs.add(asset_ref)

        out_items.append(
            {
                "title": summary[:500],
                "content": method_content[:20000],
                "url": url[:2000],
                "author": "asset_builder",
                "platform": platform[:80],
                "metrics": metrics,
                "tags": merged_tags,
                "captured_at": timestamp,
                "raw": {
                    "asset_type": asset_type,
                    "case_source_ref": source_ref_case,
                    "case_title": title,
                    "case_url": url,
                    "score": score,
                    "strategy_snapshot": strategy,
                },
                "source_ref": asset_ref[:1000],
            }
        )

        if len(out_items) >= int(args.max_items):
            break

    domain_slug = args.domain_slug or as_text(case_payload.get("domain_slug")) or "japan_immigration"
    source_run_id = args.source_run_id or f"asset-builder-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    payload = {
        "domain_slug": domain_slug,
        "account_id": as_text(args.account_id),
        "source": as_text(args.source) or "asset_builder",
        "source_run_id": source_run_id,
        "entity_type": "asset",
        "items": out_items,
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"case_items={len(items)}")
    print(f"asset_items={len(out_items)}")
    print(f"output={output_file}")
    print(f"source_run_id={source_run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
