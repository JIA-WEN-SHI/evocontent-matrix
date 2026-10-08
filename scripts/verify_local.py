"""Read-only local smoke checks. Never starts jobs or publishes content."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx


API_PATHS = [
    "/health", "/api/domains", "/api/accounts", "/api/pipeline/tasks?limit=5",
    "/api/audit-logs?limit=5", "/api/system/readiness", "/api/system/feature-checklist",
    "/api/ops/overview", "/api/ops/runtime-context", "/api/ops/run-ledger",
    "/api/ops/memory-items", "/api/ops/pending-strategy-items", "/api/ops/subagent-runs",
    "/api/scheduler/daily-status", "/api/scheduler/daily-reports",
    "/api/execution/route-status", "/api/kb/cases", "/api/kb/assets", "/api/kb/user-needs",
    "/api/kb/topics", "/api/kb/reviews", "/api/kb/tags", "/api/kb/ingestion-logs", "/api/kb/ai-jobs",
]
WEB_PATHS = ["/", "/dashboard", "/accounts", "/legacy-flow", "/employee", "/knowledge", "/settings/security"]


async def run(args):
    semaphore = asyncio.Semaphore(3)
    headers = {"x-user-id": "local-readonly-check", "x-user-role": "admin"}

    async with httpx.AsyncClient(timeout=args.timeout, follow_redirects=True, trust_env=False) as client:
        async def check(base, path):
            async with semaphore:
                started = time.monotonic()
                result = {"url": base + path}
                try:
                    response = await client.get(base + path, headers=headers)
                    result["http_status"] = response.status_code
                    result["status"] = "pass" if response.is_success else "failed"
                    if "application/json" in response.headers.get("content-type", ""):
                        payload = response.json()
                        if isinstance(payload, dict):
                            state = payload.get("status")
                            if state in {"degraded", "error", "unavailable", "failed"}:
                                result["status"] = "blocked"
                            if isinstance(payload.get("items"), list):
                                result["item_count"] = len(payload["items"])
                            result["data_status"] = state
                    result["final_url"] = str(response.url)
                except httpx.HTTPError as exc:
                    result.update(status="failed", error=type(exc).__name__)
                result["elapsed_seconds"] = round(time.monotonic() - started, 2)
                print(f"{result['status']:7} {path} ({result['elapsed_seconds']}s)", flush=True)
                return result

        checks = [(args.api, path) for path in API_PATHS]
        checks += [(args.web, path) for path in WEB_PATHS]
        checks += [(args.agent, "/health")]
        results = await asyncio.gather(*(check(base, path) for base, path in checks))
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Read-only HTTP checks; 200/empty does not prove real database or publication success.",
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--agent", default="http://127.0.0.1:8100")
    parser.add_argument("--web", default="http://127.0.0.1:3001")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--output", default="output/qa/local-smoke.json")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
