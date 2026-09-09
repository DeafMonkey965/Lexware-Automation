from __future__ import annotations

import time
import json
import hashlib
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


BASE_URL = "https://api.lexware.io"
BLOCKED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MIN_REQUEST_INTERVAL = 0.7
MAX_ATTEMPTS = 5
RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "application/xml": ".xml",
}


class LexwareWriteAttempt(RuntimeError):
    pass


class LexwareClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        self._last_request_at: float | None = None

    def _wait_for_request_slot(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = MIN_REQUEST_INTERVAL - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value).timestamp()
            except (TypeError, ValueError, OverflowError):
                return None
            return max(0.0, retry_at - time.time())

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        method = method.upper()
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        if (self.base_url == BASE_URL or urlparse(url).hostname == "api.lexware.io") and method != "GET":
            raise LexwareWriteAttempt(f"Blocked {method} request to Lexware API")
        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_request_slot()
            response = self.session.request(method, url, timeout=60, **kwargs)
            if response.status_code != 429 or attempt == MAX_ATTEMPTS - 1:
                response.raise_for_status()
                return response
            delay = self._retry_after(response)
            time.sleep(delay if delay is not None else RETRY_DELAYS[attempt])
        raise RuntimeError("Lexware request retry loop ended unexpectedly")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        return self._request("GET", path, params=params).json()

    def profile(self) -> dict[str, Any]:
        return self.get_json("/v1/profile")  # type: ignore[return-value]

    def unchecked_vouchers(self) -> list[dict[str, Any]]:
        page = 0
        result: list[dict[str, Any]] = []
        while True:
            payload = self.get_json("/v1/voucherlist", params={
                "voucherType": "any", "voucherStatus": "unchecked", "page": page, "size": 250,
            })
            if not isinstance(payload, dict):
                break
            result.extend(payload.get("content", []))
            if payload.get("last", True):
                break
            page += 1
        return result

    def voucher(self, voucher_id: str) -> dict[str, Any]:
        return self.get_json(f"/v1/vouchers/{voucher_id}")  # type: ignore[return-value]

    def download_file(self, file_id: str, target: Path) -> Path:
        cached = self.cached_file(file_id, target.parent)
        if cached is not None:
            return cached
        response = self._request("GET", f"/v1/files/{file_id}", headers={"Accept": "*/*"})
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        suggested_name = self._suggested_filename(response.headers.get("Content-Disposition", ""))
        extension = Path(suggested_name).suffix.lower() if suggested_name else ""
        if extension not in set(MIME_EXTENSIONS.values()):
            extension = MIME_EXTENSIONS.get(content_type, "")
        if not extension:
            raise ValueError(f"Datei {file_id} hat einen nicht unterstuetzten MIME-Typ: {content_type or 'unbekannt'}")
        output = target.with_suffix(extension)
        if suggested_name and Path(suggested_name).suffix.lower() == extension:
            output = target.with_name(Path(suggested_name).name)
        if output.is_file() and output.read_bytes() != response.content:
            output = target.with_suffix(extension)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(response.content)
        index = target.parent / (hashlib.sha256(file_id.encode()).hexdigest() + ".cache.json")
        index.write_text(json.dumps({"filename": output.name}), encoding="utf-8")
        return output

    @staticmethod
    def cached_file(file_id: str, cache: Path) -> Path | None:
        index = cache / (hashlib.sha256(file_id.encode()).hexdigest() + ".cache.json")
        try:
            name = json.loads(index.read_text(encoding="utf-8"))["filename"]
            if Path(name).name == name and (cache / name).is_file():
                return cache / name
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return next((cache / f"{file_id}{ext}" for ext in MIME_EXTENSIONS.values()
                     if (cache / f"{file_id}{ext}").is_file()), None)

    @staticmethod
    def _suggested_filename(content_disposition: str) -> str | None:
        for part in content_disposition.split(";"):
            key, separator, value = part.strip().partition("=")
            if key.lower() in {"filename", "filename*"} and separator:
                value = value.strip().strip('"')
                if "''" in value:
                    value = value.split("''", 1)[1]
                value = Path(unquote(value)).name
                return value if value and Path(value).suffix else None
        return None
