"""Minimal HTTP client for the local Meridian API (stdlib only)."""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import home


def _headers(cfg: dict, extra: dict | None = None) -> dict[str, str]:
    token = home.read_env().get("MERIDIAN_API_TOKEN", "")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


def request(cfg: dict, method: str, path: str, body: dict | None = None, timeout: float = 15.0):
    url = home.api_url(cfg) + path
    data = None
    headers = _headers(cfg)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ApiError(exc.code, detail) from None
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from None


def get(cfg: dict, path: str, timeout: float = 15.0):
    return request(cfg, "GET", path, timeout=timeout)


def post(cfg: dict, path: str, body: dict | None = None, timeout: float = 30.0):
    return request(cfg, "POST", path, body=body, timeout=timeout)


def put(cfg: dict, path: str, body: dict, timeout: float = 30.0):
    return request(cfg, "PUT", path, body=body, timeout=timeout)


def patch(cfg: dict, path: str, body: dict, timeout: float = 30.0):
    return request(cfg, "PATCH", path, body=body, timeout=timeout)


def post_public(url: str, payload: dict, timeout: float = 20.0):
    """POST JSON to a public endpoint with no credentials attached.

    Product reports must never carry the local API token (or any other
    credential) to another host.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "meridian-cli/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from None


def health(cfg: dict, timeout: float = 2.0) -> dict | None:
    try:
        return request(cfg, "GET", "/api/health", timeout=timeout)
    except Exception:
        return None


def upload_positions(cfg: dict, filepath: Path, timeout: float = 60.0) -> dict:
    """POST a CSV/Excel file to /api/imports/upload as multipart/form-data."""
    filepath = Path(filepath).expanduser()
    if not filepath.exists():
        raise FileNotFoundError(str(filepath))
    boundary = "----Meridian" + uuid.uuid4().hex
    filename = filepath.name
    ctype = mimetypes.guess_type(filename)[0] or "text/csv"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    data = head + filepath.read_bytes() + tail
    headers = _headers(cfg, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(data)),
    })
    req = urllib.request.Request(
        home.api_url(cfg) + "/api/imports/upload", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from None


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def lan_ip() -> str | None:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None
