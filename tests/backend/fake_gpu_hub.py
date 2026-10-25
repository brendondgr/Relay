"""A fake GAIFA GPU queue (`/api/gpu/*`, GAIFA plan 31.1) for the relay's lease tests: one slot, groups
share it, FIFO, long-poll acquire. Only what the relay's client uses."""

import asyncio
import itertools

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class FakeGpuHub:
    def __init__(self) -> None:
        self.held: dict[str, dict] = {}
        self.waiting: list[dict] = []
        self.ended: dict[str, str] = {}      # lease id → released | cancelled
        self.acquires: list[dict] = []        # every acquire body seen
        self.cond = asyncio.Condition()
        self._ids = itertools.count(1)
        self.app = self._make()

    # direct helpers for tests -------------------------------------------------
    def hold(self, service: str, group: str | None = None) -> str:
        lid = f"gl_test{next(self._ids)}"
        self.held[lid] = {"id": lid, "service": service, "group": group or service, "label": service}
        return lid

    async def release(self, lid: str) -> None:
        async with self.cond:
            self.held.pop(lid, None)
            self.waiting = [w for w in self.waiting if w["id"] != lid]
            self.ended.setdefault(lid, "released")
            self.cond.notify_all()

    async def cancel_waiting(self) -> None:
        async with self.cond:
            for w in self.waiting:
                self.ended[w["id"]] = "cancelled"
            self.waiting = []
            self.cond.notify_all()

    def group(self) -> str | None:
        return next(iter(self.held.values()))["group"] if self.held else None

    # the rule ----------------------------------------------------------------------
    def _grantable(self, entry: dict) -> bool:
        if not self.held:
            before = self.waiting[: self.waiting.index(entry)]
            return all(w["group"] == entry["group"] for w in before)
        if self.group() != entry["group"]:
            return False
        before = self.waiting[: self.waiting.index(entry)]
        return all(w["group"] == entry["group"] for w in before)

    def _make(self) -> FastAPI:
        app = FastAPI()

        @app.post("/api/gpu/acquire")
        async def acquire(request: Request, wait: float = 0):
            body = await request.json()
            self.acquires.append(body)
            lid = body.get("lease") or f"gl_auto{next(self._ids)}"
            async with self.cond:
                if lid in self.held:
                    return {"lease": lid, "state": "granted"}
                if body.get("parent"):            # nested work runs under its parent's lease
                    self.held[lid] = {"id": lid, "service": body["service"], "group": self.group() or body["service"],
                                      "label": body.get("label") or "", "parent": body["parent"]}
                    return {"lease": lid, "state": "granted"}
                if lid in self.ended:
                    return {"lease": lid, "state": self.ended[lid]}
                entry = next((w for w in self.waiting if w["id"] == lid), None)
                if entry is None:
                    entry = {"id": lid, "service": body["service"], "group": body.get("group") or body["service"],
                             "label": body.get("label") or "", "thread": body.get("thread"), "class": body.get("class")}
                    self.waiting.append(entry)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + wait
                while not self._grantable(entry):
                    left = deadline - loop.time()
                    if left <= 0:
                        ahead = [{"service": h["service"], "label": h["label"], "state": "held", "eta_s": 120}
                                 for h in self.held.values()]
                        return JSONResponse({"lease": lid, "state": "waiting", "ahead": ahead, "eta_s": 120},
                                            status_code=202)
                    try:
                        await asyncio.wait_for(self.cond.wait(), left)
                    except TimeoutError:
                        pass
                    if lid in self.ended:
                        return {"lease": lid, "state": self.ended[lid]}
                self.waiting.remove(entry)
                self.held[lid] = entry
                self.cond.notify_all()
                return {"lease": lid, "state": "granted"}

        @app.post("/api/gpu/{lid}/renew")
        async def renew(lid: str):
            return {"lease": lid, "state": "granted" if lid in self.held else "unknown"}

        @app.post("/api/gpu/{lid}/release")
        async def release(lid: str):
            await self.release(lid)
            return {"lease": lid, "state": "released"}

        return app
