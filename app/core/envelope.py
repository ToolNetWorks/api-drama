"""Standard JSON envelope: {success, data, error, request_id}."""
from __future__ import annotations

from typing import Any, Optional


def ok(data: Any, request_id: str) -> dict:
    return {"success": True, "data": data, "error": None, "request_id": request_id}


def fail(code: str, message: str, request_id: str, status: int = 400, details: Optional[Any] = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message, "status": status}
    if details is not None:
        err["details"] = details
    return {"success": False, "data": None, "error": err, "request_id": request_id}
