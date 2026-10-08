from supabase import Client

from app.models import AuditLogEntry


def write_audit_log(client: Client, entry: AuditLogEntry) -> None:
    client.table("audit_logs").insert(entry.model_dump()).execute()

