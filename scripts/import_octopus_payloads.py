from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from supabase import Client, create_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Directly import octopus payload JSON files into Supabase.")
    parser.add_argument(
        "--payload",
        action="append",
        required=True,
        help="Payload file path. Repeat this option to import multiple files.",
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="Optional account_id override. If omitted, payload account_id is used; if still empty, auto-pick latest active xiaohongshu account.",
    )
    parser.add_argument("--created-by", default="script:import_octopus_payloads", help="Audit actor value.")
    return parser.parse_args()


def load_env_file(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    text = env_path.read_text(encoding="utf-8-sig", errors="ignore")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_client() -> Client:
    url = str(os.environ.get("SUPABASE_URL") or "").strip()
    key = str(os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise SystemExit("missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(url, key)


def auto_account_id(client: Client) -> str:
    rows = (
        client.table("channel_accounts")
        .select("id")
        .eq("channel", "xiaohongshu")
        .eq("is_active", True)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return str(rows[0]["id"]) if rows else ""


def load_payload(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    api_root = root / "services" / "api"
    sys.path.insert(0, str(api_root))

    # Deferred import to avoid app path conflicts before PYTHONPATH is prepared.
    from app.models import KbOctopusImportRequest
    from app.services.kb_service import import_octopus_payload

    load_env_file(root)
    client = get_client()

    default_account_id = str(args.account_id or "").strip() or auto_account_id(client)
    payload_paths: List[Path] = [Path(p).expanduser().resolve() for p in args.payload]

    report: List[Dict[str, Any]] = []
    for payload_path in payload_paths:
        if not payload_path.exists():
            report.append(
                {
                    "file": str(payload_path),
                    "status": "missing_file",
                }
            )
            continue

        payload = load_payload(payload_path)
        payload_account_id = str(payload.get("account_id") or "").strip()
        if not payload_account_id and default_account_id:
            payload["account_id"] = default_account_id

        try:
            req = KbOctopusImportRequest(**payload)
            result = import_octopus_payload(client, req, created_by=str(args.created_by or "").strip())
            report.append({"file": str(payload_path), "status": "ok", "result": result})
        except Exception as exc:  # noqa: BLE001
            report.append({"file": str(payload_path), "status": "error", "error": str(exc)})

    print(json.dumps({"imports": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
