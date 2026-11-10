"""Synchronous HTTP client for the local Unix socket API."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path

from .paths import api_socket


class APIError(Exception):
    def __init__(self, status: int, response: dict):
        self.status, self.response = status, response
        super().__init__(json.dumps(response, ensure_ascii=False))


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def request(method: str, path: str, data: dict | None = None, *, timeout: float = 35,
            socket_path: Path | None = None) -> dict:
    connection = UnixHTTPConnection(socket_path or api_socket(), timeout)
    try:
        body = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
        connection.request(method, path, body=body,
                           headers={"Content-Type": "application/json"} if body is not None else {})
        response = connection.getresponse()
        payload = response.read()
        value = json.loads(payload) if payload else {}
        if response.status >= 400:
            raise APIError(response.status, value)
        return value
    finally:
        connection.close()
