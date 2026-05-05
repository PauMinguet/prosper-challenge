"""Thin async HTTP client for the local EHR service."""

import os
from typing import Any, Optional

import httpx

EHR_BASE_URL = os.environ.get("EHR_BASE_URL", "http://localhost:8000")


class EHRClient:
    def __init__(
        self,
        base_url: str = EHR_BASE_URL,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(base_url=base_url, timeout=10.0)
            self._owns_client = True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def create_patient(
        self,
        name: str,
        dob: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/patients",
            json={"name": name, "dob": dob, "phone": phone, "email": email},
        )
        resp.raise_for_status()
        return resp.json()

    async def find_patient(self, name: str, dob: str) -> Optional[dict[str, Any]]:
        resp = await self._client.get("/patients/find", params={"name": name, "dob": dob})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def list_slots(self, from_: str, to: str) -> list[dict[str, Any]]:
        resp = await self._client.get("/slots", params={"from": from_, "to": to})
        resp.raise_for_status()
        return resp.json()

    async def list_patient_appointments(self, patient_id: int) -> list[dict[str, Any]]:
        resp = await self._client.get(f"/patients/{patient_id}/appointments")
        resp.raise_for_status()
        return resp.json()

    async def create_appointment(self, patient_id: int, slot_id: int) -> dict[str, Any]:
        resp = await self._client.post(
            "/appointments",
            json={"patient_id": patient_id, "slot_id": slot_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def cancel_appointment(self, appointment_id: int) -> dict[str, Any]:
        resp = await self._client.delete(f"/appointments/{appointment_id}")
        resp.raise_for_status()
        return resp.json()
