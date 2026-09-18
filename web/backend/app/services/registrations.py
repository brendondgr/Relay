"""Endpoints that another program registers and removes by a stable key.

The launcher in ~/Models/LLMs (lcpp) starts llama.cpp servers on ports of its
choosing and publishes each one here as ``lcpp:<port>``. What it needs, and
what ``POST /admin/endpoints`` cannot give it, is *idempotence*: "make this
key exist with these settings" must be safe to repeat on every start, retry
and reconcile, and "remove this key" safe when it is already gone.

Resolution order for an upsert:

1. the row already holding ``key``;
2. otherwise an **unowned** row with the same ``base_url``, which is adopted
   rather than duplicated. Its id is what telemetry rows point at, so this
   keeps a hand-added endpoint's history continuous when a program takes it
   over. Its name and alias are the operator's and are kept, so clients
   routing by that alias keep working;
3. otherwise a new row.

Model ids that another endpoint already routes are dropped from the allowlist
and reported back as ``skipped_models`` instead of failing the registration:
two instances of the same model on different ports are normal, and the second
is still reachable by its alias. An alias collision *is* an error (409),
because an alias is the one name the caller explicitly asked for.
"""

import re

from app.core.logging import get_logger
from app.schemas import EndpointCreate, EndpointPatch, RegistrationOut, RegistrationUpsert

log = get_logger("registrations")

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RegistrationError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def base_url_for(spec: RegistrationUpsert) -> str:
    if spec.base_url:
        if not spec.base_url.startswith(("http://", "https://")):
            raise RegistrationError("base_url must start with http:// or https://")
        return spec.base_url.rstrip("/")
    if spec.port is None:
        raise RegistrationError("give either port or base_url")
    return f"http://{spec.host}:{spec.port}/v1"


async def upsert(app, key: str, spec: RegistrationUpsert) -> RegistrationOut:
    if not KEY_RE.match(key):
        raise RegistrationError("key must be letters/digits and . _ : - (max 128)")
    router = app.state.router
    url = base_url_for(spec)
    row = router.find_by_key(key)
    adopted = False
    if row is None:
        row = router.find_unowned_by_url(url)
        adopted = row is not None

    exclude = row["id"] if row else None
    wanted = [m.strip() for m in spec.available_models if m.strip()]
    clash = router.conflicting_models(wanted, exclude_id=exclude)
    if spec.alias:
        clash.discard(spec.alias.lower())  # the alias check below owns that case
    models = [m for m in wanted if m.lower() not in clash]
    skipped = [m for m in wanted if m.lower() in clash]

    try:
        if row is None:
            row = router.create(EndpointCreate(
                name=spec.name, base_url=url, alias=spec.alias,
                server_type=spec.server_type, available_models=models))
        else:
            keep_names = adopted and (row.get("alias") or row.get("name"))
            router.patch(row["id"], EndpointPatch(
                name=None if keep_names else spec.name,
                alias=None if (keep_names and row.get("alias")) else spec.alias,
                base_url=url, server_type=spec.server_type,
                available_models=models, enabled=True))
    except ValueError as e:
        status = 409 if "already used" in str(e) or "already the alias" in str(e) else 422
        raise RegistrationError(str(e), status) from e
    router.set_ownership(row["id"], spec.owner, key, spec.meta)
    log.info("registration upserted", extra={"data": {
        "key": key, "owner": spec.owner, "url": url, "adopted": adopted,
        "models": models, "skipped": skipped}})
    # Probe now so the dashboard and /v1/models reflect it immediately.
    await app.state.prober._probe(row["id"])
    return RegistrationOut(key=key, owner=spec.owner, adopted=adopted,
                           skipped_models=skipped, endpoint=router.out(row["id"]))


def remove(app, key: str) -> bool:
    router = app.state.router
    row = router.find_by_key(key)
    if row is None:
        return False
    router.delete(row["id"])
    log.info("registration removed", extra={"data": {"key": key, "owner": row.get("owner")}})
    return True


def listing(app, owner: str | None) -> list[RegistrationOut]:
    router = app.state.router
    return [
        RegistrationOut(key=row["external_key"], owner=row.get("owner"), endpoint=router.out(eid))
        for eid, row in router.endpoints.items()
        if row.get("external_key") and (owner is None or row.get("owner") == owner)
    ]
