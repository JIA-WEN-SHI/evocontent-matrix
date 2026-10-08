from fastapi import APIRouter, Depends
from supabase import Client

from app.db import get_supabase
from app.models import KbOctopusImportRequest, LeadEventPayload
from app.security import verify_webhook_signature
from app.services.kb_service import import_octopus_payload
from app.services.lead_service import ingest_lead_event

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/leads", dependencies=[Depends(verify_webhook_signature)])
def receive_lead_event(
    payload: LeadEventPayload,
    client: Client = Depends(get_supabase),
):
    return ingest_lead_event(client, payload)


@router.post("/kb-octopus", dependencies=[Depends(verify_webhook_signature)])
def receive_kb_octopus_event(
    payload: KbOctopusImportRequest,
    client: Client = Depends(get_supabase),
):
    return import_octopus_payload(client, payload, created_by="system:webhook.octopus")
