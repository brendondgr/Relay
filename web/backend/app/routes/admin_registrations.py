"""Registrations: idempotent, owner-scoped endpoint upsert/remove by key.

Used by lcpp (~/Models/LLMs) to publish the llama.cpp servers it launches.
See services/registrations.py for the matching and adoption rules.
"""

from fastapi import APIRouter, HTTPException, Request, Response

from app.schemas import RegistrationOut, RegistrationUpsert
from app.services import registrations
from app.services.registrations import RegistrationError

router = APIRouter(prefix="/admin/registrations", tags=["registrations"])


@router.get("", response_model=list[RegistrationOut])
async def list_registrations(request: Request, owner: str | None = None):
    return registrations.listing(request.app, owner)


@router.put("/{key}", response_model=RegistrationOut)
async def upsert_registration(request: Request, key: str, spec: RegistrationUpsert):
    try:
        return await registrations.upsert(request.app, key, spec)
    except RegistrationError as e:
        raise HTTPException(e.status, str(e))


@router.delete("/{key}", status_code=204)
async def delete_registration(request: Request, key: str):
    # Idempotent: removing a key that is already gone is success, so a
    # caller cleaning up after a crash never has to care whether it ran.
    registrations.remove(request.app, key)
    return Response(status_code=204)
