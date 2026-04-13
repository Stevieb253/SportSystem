# data/cache.py
# Disk-based JSON cache with TTL. Single responsibility: caching only.

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

CacheValue = Union[dict, list]


class Cache:
    """Disk-backed JSON cache with TTL expiry.

    Cache files are stored as JSON in cache_dir.
    Each entry is a dict with 'data' and 'timestamp' keys.
    """

    def __init__(self, cache_dir: str, ttl_hours: int) -> None:
        """Initialise cache.

        Args:
            cache_dir: Directory path to store cache files.
            ttl_hours: Hours before a cache entry is considered expired.
        """
        self.cache_dir = Path(cache_dir)
        self.ttl_hours = ttl_hours
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Return the file path for a cache key."""
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return self.cache_dir / f"{safe_key}.json"

    def get(self, key: str, ttl_hours: float | None = None) -> CacheValue | None:
        """Return cached data or None if missing or expired.

        Args:
            key: Cache key string.
            ttl_hours: Optional TTL override in hours. Uses instance default if None.

        Returns:
            Cached data or None.
        """
        path = self._path(key)
        if not path.exists():
            return None
        if self.is_expired(key, ttl_hours=ttl_hours):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
            return envelope.get("data")
        except Exception as exc:
            logger.warning("Cache read failed (key=%s): %s", key, exc)
            return None

    def set(self, key: str, data: CacheValue) -> None:
        """Write data to cache with current timestamp.

        Args:
            key: Cache key string.
            data: Data to cache (must be JSON-serialisable).
        """
        path = self._path(key)
        envelope = {
            "timestamp": datetime.utcnow().isoformat(),
            "data":      data,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(envelope, f, default=str)
        except Exception as exc:
            logger.warning("Cache write failed (key=%s): %s", key, exc)

    def invalidate(self, key: str) -> None:
        """Delete a single cache entry.

        Args:
            key: Cache key string.
        """
        path = self._path(key)
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Cache invalidate failed (key=%s): %s", key, exc)

    def clear_all(self) -> None:
        """Delete all cache files in the cache directory."""
        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink()
            except Exception as exc:
                logger.warning("Cache clear failed (%s): %s", path, exc)

    def is_expired(self, key: str, ttl_hours: float | None = None) -> bool:
        """Return True if the cached entry is older than ttl_hours.

        Args:
            key: Cache key string.
            ttl_hours: Optional TTL override in hours. Uses instance default if None.

        Returns:
            True if expired or missing, False if still fresh.
        """
        path = self._path(key)
        if not path.exists():
            return True
        effective_ttl = ttl_hours if ttl_hours is not None else self.ttl_hours
        try:
            with open(path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
            ts = datetime.fromisoformat(envelope.get("timestamp", ""))
            return datetime.utcnow() - ts > timedelta(hours=effective_ttl)
        except Exception:
            return True
