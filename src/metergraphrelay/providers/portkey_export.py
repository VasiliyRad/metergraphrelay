from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Docs-verified Portkey beta Logs Export contract
# (/api-reference/admin-api/data-plane/logs/log-exports-beta/).
DEFAULT_PORTKEY_URL = "https://api.portkey.ai/v1"
EXPORTS_PATH = "/logs/exports"
_API_KEY_HEADER = "x-portkey-api-key"
PAGE_SIZE_MAX = 50000
# Exactly the fields normalize_portkey_row consumes, drawn from the requested_data enum.
REQUESTED_DATA = [
    "id", "trace_id", "created_at", "request", "response", "ai_org", "ai_model",
    "req_units", "res_units", "response_time", "cost", "response_status_code", "metadata",
]

STATUS_DRAFT = "draft"
STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
_TERMINAL = frozenset({STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED})


class PortkeyExportError(Exception):
    """Raised when the Portkey Logs Export API errors or returns an unusable body."""


@dataclass(frozen=True)
class PortkeyExport:
    export_id: str
    total: int | None
    status: str

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def is_success(self) -> bool:
        return self.status == STATUS_SUCCESS


class PortkeyExportClient:
    def __init__(self, api_key: str, *, workspace: str | None = None,
                 base_url: str = DEFAULT_PORTKEY_URL, timeout: float = 30.0):
        self._api_key = api_key
        self._workspace = workspace
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path_or_url: str, *, body: dict | None = None,
                 authed: bool = True) -> bytes:
        url = path_or_url if path_or_url.startswith("http") else f"{self._base}{path_or_url}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {}
        if authed:
            headers[_API_KEY_HEADER] = self._api_key
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise PortkeyExportError(
                f"Portkey export request failed: HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PortkeyExportError(f"Portkey export request failed: {exc.reason}") from exc

    @staticmethod
    def _parse(raw: bytes) -> dict:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PortkeyExportError(f"Portkey export returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise PortkeyExportError("Portkey export response was not a JSON object")
        return payload

    @staticmethod
    def _export_path(export_id: str, suffix: str = "") -> str:
        # export_id is an opaque server-issued value; quote it so it can never
        # break out of the path segment.
        quoted = urllib.parse.quote(export_id, safe="")
        return f"{EXPORTS_PATH}/{quoted}{suffix}"

    def create_export(self, *, window_start: str, window_end: str) -> PortkeyExport:
        body = {
            "filters": {
                "time_of_generation_min": window_start,
                "time_of_generation_max": window_end,
                "page_size": PAGE_SIZE_MAX,
                "current_page": 1,
            },
            "requested_data": list(REQUESTED_DATA),
        }
        if self._workspace:
            body["workspace_id"] = self._workspace
        payload = self._parse(self._request("POST", EXPORTS_PATH, body=body))
        if "id" not in payload:
            raise PortkeyExportError("Portkey create-export response missing id")
        total = payload.get("total")
        return PortkeyExport(
            export_id=str(payload["id"]),
            total=total if isinstance(total, int) else None,
            status=STATUS_DRAFT,
        )

    def start_export(self, export_id: str) -> None:
        self._request("POST", self._export_path(export_id, "/start"), body={})

    def get_export(self, export_id: str) -> PortkeyExport:
        payload = self._parse(self._request("GET", self._export_path(export_id)))
        total = payload.get("total")
        return PortkeyExport(
            export_id=str(payload.get("id", export_id)),
            total=total if isinstance(total, int) else None,
            status=str(payload.get("status", "")),
        )

    def cancel_export(self, export_id: str) -> None:
        self._request("POST", self._export_path(export_id, "/cancel"), body={})

    def download_to(self, export_id: str, dest_path: str) -> int:
        payload = self._parse(self._request("GET", self._export_path(export_id, "/download")))
        signed_url = payload.get("signed_url")
        if not signed_url:
            raise PortkeyExportError(
                f"export {export_id} download response missing signed_url"
            )
        raw = self._request("GET", signed_url, authed=False)  # pre-signed: no Portkey credential
        with open(dest_path, "wb") as dst:
            dst.write(raw)
        return sum(1 for line in raw.splitlines() if line.strip())
