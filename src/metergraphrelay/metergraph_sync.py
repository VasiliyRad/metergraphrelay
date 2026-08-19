from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .push import DEFAULT_INGEST_URL  # single source of truth for the server host

ACQUIRE_PATH = "/v1/import-sync/acquire"
LEASES_PATH = "/v1/import-sync/leases"
STATE_PATH = "/v1/import-sync/state"
_LEASE_LOST_STATUSES = frozenset({404, 409, 410})
_ACQUIRED_REQUIRED_FIELDS = ("lease_id", "window_start", "window_end", "lease_expires_at")


class MeterGraphSyncError(Exception):
    """Raised when the MeterGraph import-sync API errors or returns an unusable body."""


class LeaseLostError(MeterGraphSyncError):
    """Raised when a lease is no longer held (renew/complete on an expired/stolen lease)."""


@dataclass(frozen=True)
class AcquiredLease:
    lease_id: str
    checkpoint_version: object
    window_start: str
    window_end: str
    lease_expires_at: str


@dataclass(frozen=True)
class AcquireResult:
    status: str
    lease: AcquiredLease | None = None
    retry_at: str | None = None


class MeterGraphSyncClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._base = (base_url or DEFAULT_INGEST_URL).rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, *, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base}{path}", data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            raw = response.read()
            return response.status, self._parse(raw)

    @staticmethod
    def _parse(raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MeterGraphSyncError(
                f"MeterGraph import-sync returned invalid JSON: {exc}"
            ) from exc
        return payload if isinstance(payload, dict) else {"_": payload}

    def acquire(
        self,
        *,
        source: str,
        source_scope: str,
        initial_since: str | None = None,
        max_window_seconds: int | None = None,
    ) -> AcquireResult:
        body: dict = {"source": source, "source_scope": source_scope}
        if initial_since is not None:
            body["initial_since"] = initial_since
        if max_window_seconds is not None:
            body["max_window_seconds"] = max_window_seconds
        try:
            status, payload = self._request("POST", ACQUIRE_PATH, body=body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                busy = self._parse(exc.read())
                return AcquireResult(status="busy", retry_at=busy.get("retry_at"))
            raise MeterGraphSyncError(
                f"acquire failed: HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MeterGraphSyncError(f"acquire failed: {exc.reason}") from exc
        if status == 201:
            missing = [f for f in _ACQUIRED_REQUIRED_FIELDS if f not in payload]
            if missing:
                raise MeterGraphSyncError(
                    f"acquire returned an incomplete lease, missing: {', '.join(missing)}"
                )
            return AcquireResult(
                status="acquired",
                lease=AcquiredLease(
                    lease_id=payload["lease_id"],
                    checkpoint_version=payload.get("checkpoint_version"),
                    window_start=payload["window_start"],
                    window_end=payload["window_end"],
                    lease_expires_at=payload["lease_expires_at"],
                ),
            )
        return AcquireResult(status="caught_up")

    def renew(self, lease_id: str) -> str:
        payload = self._lease_call("POST", f"{LEASES_PATH}/{self._quote(lease_id)}/renew")
        return payload.get("lease_expires_at", "")

    def complete(self, lease_id: str) -> None:
        self._lease_call("POST", f"{LEASES_PATH}/{self._quote(lease_id)}/complete")

    def abandon(self, lease_id: str) -> None:
        try:
            self._lease_call("DELETE", f"{LEASES_PATH}/{self._quote(lease_id)}")
        except LeaseLostError:
            return  # already gone — nothing to release

    def get_state(self, *, source: str, source_scope: str) -> dict:
        query = urllib.parse.urlencode({"source": source, "source_scope": source_scope})
        try:
            _, payload = self._request("GET", f"{STATE_PATH}?{query}")
        except urllib.error.HTTPError as exc:
            raise MeterGraphSyncError(
                f"get_state failed: HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MeterGraphSyncError(f"get_state failed: {exc.reason}") from exc
        return payload

    @staticmethod
    def _quote(lease_id: str) -> str:
        # Lease ids are server-issued opaque tokens; quote so any special
        # characters cannot break the path or inject extra segments.
        return urllib.parse.quote(lease_id, safe="")

    def _lease_call(self, method: str, path: str) -> dict:
        try:
            _, payload = self._request(method, path)
        except urllib.error.HTTPError as exc:
            if exc.code in _LEASE_LOST_STATUSES:
                raise LeaseLostError(
                    f"lease no longer held: HTTP {exc.code} {exc.reason}"
                ) from exc
            raise MeterGraphSyncError(
                f"{method} {path} failed: HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MeterGraphSyncError(f"{method} {path} failed: {exc.reason}") from exc
        return payload
