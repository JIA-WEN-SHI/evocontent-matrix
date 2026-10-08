from app.db import get_supabase
from app.orchestration.reflection import reflect_and_upgrade


def run_daily_reflection(domain_slug: str = "japan_immigration") -> dict:
    client = get_supabase()
    return reflect_and_upgrade(client, domain_slug)
