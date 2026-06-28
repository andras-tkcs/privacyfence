"""Thread-safe store for reads that are pending user approval in Cowork.

When a review-gate tool (gate="review") is called:
  1. The actual data is fetched from the connector.
  2. It is stored here under a request_id.
  3. The tool returns a {status: "pending_approval", request_id, preview, ...}
     dict to Claude immediately (non-blocking).
  4. Claude presents the preview to the user via AskUserQuestion in Cowork.
  5. The user picks Accept / Deny / Show Details.
  6. Claude calls loopline_confirm, loopline_deny, or loopline_show_details
     with the request_id, which resolves the pending entry here.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

_store: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def store(filtered_data: Any, details_text: str) -> str:
    """Store pending read data and return a new request_id."""
    request_id = uuid.uuid4().hex
    with _lock:
        _store[request_id] = {
            "filtered_data": filtered_data,
            "details_text": details_text,
        }
    return request_id


def confirm(request_id: str) -> Any:
    """Pop and return filtered_data. Raises KeyError if not found."""
    with _lock:
        entry = _store.pop(request_id, None)
    if entry is None:
        raise KeyError(request_id)
    return entry["filtered_data"]


def deny(request_id: str) -> None:
    """Cancel a pending read."""
    with _lock:
        _store.pop(request_id, None)


def get(request_id: str) -> dict[str, Any]:
    """Return the stored entry without removing it. Raises KeyError if gone."""
    with _lock:
        entry = _store.get(request_id)
    if entry is None:
        raise KeyError(request_id)
    return entry


def remove(request_id: str) -> None:
    with _lock:
        _store.pop(request_id, None)
