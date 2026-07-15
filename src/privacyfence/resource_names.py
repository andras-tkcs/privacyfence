"""Name resolution for auto-accept grants (see resource_grants.py).

The menu bar stores grants by ID (the ground truth the evaluator matches
on) but should *display* the resource's real name — a folder name, a task
list name, a channel name — not the opaque ID. This module resolves an ID to
a name via the relevant connector's own read API (each ``GrantResourceType``
in resource_grants.py declares its own ``resolver``), and caches the result
so the menu doesn't hit the network on every rebuild and still has something
to show immediately after a cold start, before any connector has
reconnected this session.

Two cache layers:
  - in-memory, short TTL — avoids re-resolving on every menu rebuild
    (``_rebuild()`` in menu_bar.py can fire fairly often: config changes,
    connector auth, rule hot-reload).
  - on-disk, no TTL — best-effort "last known name", so a name is available
    the instant the menu is built after a daemon restart, before any
    connector has been re-authenticated this session. Never authoritative;
    a resolution failure never blocks or changes an auto-accept decision,
    it only affects what a menu label says.

Resolution never makes a network call beyond what the connector is already
authorized for (it calls the same client methods used elsewhere in the
daemon), and only when a live, already-authenticated client is available.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .paths import data_dir
from .resource_grants import GrantResourceType

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 15 * 60

# States a caller (the menu) can render when a resolved/cached name isn't
# available. Distinguished so the menu can explain *why* rather than just
# showing the raw ID.
RESOLVING = "resolving"
NOT_CONNECTED = "not_connected"
NOT_FOUND = "not_found"


def _cache_file() -> Path:
    return data_dir() / "resource_name_cache.json"


def _cache_key(rt: GrantResourceType, resource_id: str) -> str:
    return f"{rt.connector}.{rt.config_key}:{resource_id}"


class ResourceNameResolver:
    """Not a singleton — the menu bar owns one instance for its lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[str, float]] = {}
        self._disk: dict[str, str] = self._load_disk_cache()

    def _load_disk_cache(self) -> dict[str, str]:
        try:
            with open(_cache_file(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Could not load resource name cache: %s", exc)
            return {}

    def _save_disk_cache(self) -> None:
        try:
            with open(_cache_file(), "w", encoding="utf-8") as f:
                json.dump(self._disk, f, indent=2, sort_keys=True)
        except Exception as exc:
            logger.warning("Could not save resource name cache: %s", exc)

    def cached_name(self, rt: GrantResourceType, resource_id: str) -> str | None:
        """Best-known name with no network call — memory, then disk."""
        key = _cache_key(rt, resource_id)
        with self._lock:
            hit = self._memory.get(key)
            if hit is not None:
                return hit[0]
            return self._disk.get(key)

    def resolve(self, rt: GrantResourceType, resource_id: str, client: Any | None) -> str | None:
        """Resolve now, live if possible. Safe to call from a background
        thread — this never touches menu bar/AppKit state itself.

        Returns the freshly resolved name, or the last-known cached name if
        resolution isn't possible right now (no client) or fails, or None
        if there's no name available from any source.
        """
        key = _cache_key(rt, resource_id)
        with self._lock:
            hit = self._memory.get(key)
            fresh = hit is not None and (time.monotonic() - hit[1]) < CACHE_TTL_SECONDS
        if fresh:
            return hit[0]

        if client is not None:
            try:
                name = rt.resolver(client, resource_id)
            except Exception as exc:
                logger.warning(
                    "Name resolution failed for %s %r: %s", rt.config_key, resource_id, exc
                )
                name = None
            if name:
                with self._lock:
                    self._memory[key] = (name, time.monotonic())
                    self._disk[key] = name
                self._save_disk_cache()
                return name

        # Live resolution unavailable or failed — fall back to whatever we
        # last knew, without penalizing it as newly "fresh" (so the next
        # call retries live resolution instead of trusting a stale hit).
        with self._lock:
            return self._disk.get(key)


_INSTANCE: ResourceNameResolver | None = None


def get_resolver() -> ResourceNameResolver:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ResourceNameResolver()
    return _INSTANCE
