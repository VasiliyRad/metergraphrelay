from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

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
_ALL_STATUSES = frozenset(
    {STATUS_DRAFT, STATUS_IN_PROGRESS, STATUS_SUCCESS, STATUS_FAILED, STATUS_STOPPED}
)

# The signed export is streamed to disk in bounded reads so a large export never
# lands in memory. on_progress (if given) fires once per chunk, i.e. at least
# every _DOWNLOAD_CHUNK_SIZE bytes — a bounded cadence a caller can use to renew
# a lease during a long download.
_DOWNLOAD_CHUNK_SIZE = 1 << 16  # 64 KiB


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

    # -- HTTP helpers --------------------------------------------------------

    @staticmethod
    def _check_status(status: int) -> None:
        if status != 200:
            raise PortkeyExportError(
                f"Portkey export request returned unexpected HTTP {status}"
            )

    def _api_request(self, method: str, path: str, *, body: dict | None = None) -> bytes:
        """Call a Portkey API endpoint (relative path, base URL prepended, authed)."""
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {_API_KEY_HEADER: self._api_key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                self._check_status(response.status)
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

    # -- endpoints -----------------------------------------------------------

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
        payload = self._parse(self._api_request("POST", EXPORTS_PATH, body=body))

        export_id = payload.get("id")
        if not isinstance(export_id, str) or not export_id:
            raise PortkeyExportError("Portkey create-export response has a missing or invalid id")
        total = payload.get("total")
        # The volume-split decision reads total; it must be a real nonnegative int
        # (bool is rejected even though it subclasses int) — never coerced.
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise PortkeyExportError(
                "Portkey create-export response has a missing or invalid total"
            )
        return PortkeyExport(export_id=export_id, total=total, status=STATUS_DRAFT)

    def start_export(self, export_id: str) -> None:
        self._api_request("POST", self._export_path(export_id, "/start"), body={})

    def get_export(self, export_id: str) -> PortkeyExport:
        payload = self._parse(self._api_request("GET", self._export_path(export_id)))

        if "id" in payload:
            returned_id = payload["id"]
            if not isinstance(returned_id, str) or not returned_id:
                raise PortkeyExportError("Portkey get-export response has an invalid id")
        else:
            returned_id = export_id
        status = payload.get("status")
        if not isinstance(status, str) or status not in _ALL_STATUSES:
            raise PortkeyExportError(
                f"Portkey get-export response has an invalid status: {status!r}"
            )
        return PortkeyExport(export_id=returned_id, total=None, status=status)

    def cancel_export(self, export_id: str) -> None:
        self._api_request("POST", self._export_path(export_id, "/cancel"), body={})

    def download_to(
        self, export_id: str, dest_path: str, *, on_progress: Callable[[], None] | None = None
    ) -> int:
        """Resolve the signed URL, stream the export to dest_path, return nonblank line count.

        The body is streamed in bounded chunks to a temporary sibling of dest_path
        and atomically moved into place only after a fully successful download, so a
        failure never leaves a partial file at dest_path. on_progress, if provided,
        is invoked once per streamed chunk (a bounded cadence) so a caller can renew
        a lease mid-download. The signed URL is fetched with no Portkey credential.
        """
        payload = self._parse(
            self._api_request("GET", self._export_path(export_id, "/download"))
        )
        signed_url = payload.get("signed_url")
        if not isinstance(signed_url, str) or not signed_url:
            raise PortkeyExportError(
                f"export {export_id} download response is missing signed_url"
            )
        parsed = urllib.parse.urlparse(signed_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PortkeyExportError(
                f"export {export_id} signed_url is not a valid http(s) URL"
            )
        return self._stream_to_file(signed_url, dest_path, on_progress)

    # -- signed-URL streaming ------------------------------------------------

    def _stream_to_file(
        self, signed_url: str, dest_path: str, on_progress: Callable[[], None] | None
    ) -> int:
        # No Portkey credential header: the URL is pre-signed.
        request = urllib.request.Request(signed_url, method="GET")
        tmp_path = f"{dest_path}.part"
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                self._check_status(response.status)
                with open(tmp_path, "wb") as dst:
                    lines = self._pump(response, dst, on_progress)
        except urllib.error.HTTPError as exc:
            self._discard(tmp_path)
            raise PortkeyExportError(
                f"Portkey signed download failed: HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            self._discard(tmp_path)
            raise PortkeyExportError(f"Portkey signed download failed: {exc.reason}") from exc
        except BaseException:
            self._discard(tmp_path)
            raise
        os.replace(tmp_path, dest_path)  # atomic within the same directory
        return lines

    @staticmethod
    def _pump(response, dst, on_progress: Callable[[], None] | None) -> int:
        """Stream response into dst, counting nonblank lines across chunk boundaries."""
        lines = 0
        current_has_content = False  # does the line still being assembled hold any nonblank byte?
        while True:
            chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            segments = chunk.split(b"\n")
            for i, segment in enumerate(segments):
                if segment.strip():
                    current_has_content = True
                if i < len(segments) - 1:  # a newline terminated this segment
                    if current_has_content:
                        lines += 1
                    current_has_content = False
            if on_progress is not None:
                on_progress()
        if current_has_content:  # final line had no trailing newline
            lines += 1
        return lines

    @staticmethod
    def _discard(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass
