"""Caché TTL en memoria para operaciones frecuentes de persistencia.

Usar con MUY CUIDADO: sólo para datos que toleren eventual consistencia
durante el TTL. Nunca para información de seguridad (permisos, contraseñas),
pero sí para metadata de lectura repetida (get_user, list_sessions).
"""
from __future__ import annotations

import time
import threading
from collections import OrderedDict
from typing import Any, Callable, Hashable, Optional


class TTLCache:
    """Caché thread-safe simple con TTL y máximo de entradas."""

    def __init__(self, default_ttl_seconds: int = 60, max_entries: int = 1024):
        self._ttl = int(max(1, default_ttl_seconds))
        self._max = int(max(1, max_entries))
        self._data: "OrderedDict[Hashable, tuple[Any, float]]" = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Operaciones básicas
    # ------------------------------------------------------------------
    def get(self, key: Hashable) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any, ttl_seconds: Optional[int] = None) -> None:
        ttl = self._ttl if ttl_seconds is None else int(max(1, ttl_seconds))
        expires_at = time.monotonic() + ttl
        with self._lock:
            self._data[key] = (value, expires_at)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._data.pop(key, None)

    def invalidate_pattern(self, predicate: Callable[[Hashable], bool]) -> int:
        """Elimina todas las claves que cumplen ``predicate(key) is True.

        Devuelve cuántas entradas se eliminaron.
        """
        removed = 0
        with self._lock:
            for key in list(self._data.keys()):
                if predicate(key):
                    self._data.pop(key, None)
                    removed += 1
        return removed

    def invalidate_all(self) -> None:
        with self._lock:
            self._data.clear()

    # ------------------------------------------------------------------
    # Helper: memoización TTL
    # ------------------------------------------------------------------
    def memoize(
        self,
        key: Hashable,
        compute: Callable[[], Any],
        ttl_seconds: Optional[int] = None,
    ) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = compute()
        self.set(key, value, ttl_seconds=ttl_seconds)
        return value


# ----------------------------------------------------------------------
# Cachés globales (típicos del proyecto.
# ----------------------------------------------------------------------

# Usuarios individuales: 60s TTL.
# Clave: ("user", uid)
USER_CACHE = TTLCache(default_ttl_seconds=60, max_entries=256)

# Listados de sesiones: 20s TTL.
# Clave: ("sessions", status, date_from, date_to
SESSION_CACHE = TTLCache(default_ttl_seconds=20, max_entries=64)

# Índice de importación (sesiones existentes: 5 min TTL.
# Clave: ("import_index",)
IMPORT_INDEX_CACHE = TTLCache(default_ttl_seconds=300, max_entries=4)

# Actividad administrativa: 120s TTL (cambia poco, sólo cuando el admin
# crea/actualiza/elimina usuarios).
# Clave: ("activity", uid_or_None, limit_or_None)
ACTIVITY_CACHE = TTLCache(default_ttl_seconds=120, max_entries=32)


# ----------------------------------------------------------------------
# Helpers de invalidación.
# ----------------------------------------------------------------------
def invalidate_user_cache(uid: Optional[str] = None) -> None:
    """Si ``uid`` se invalida sólo ese usuario, sino TODA la caché de users."""
    if uid is None:
        USER_CACHE.invalidate_pattern(lambda k: isinstance(k, tuple) and k and k[0] == "user")
    else:
        USER_CACHE.invalidate(("user", uid))


def invalidate_sessions_cache() -> None:
    SESSION_CACHE.invalidate_pattern(lambda k: isinstance(k, tuple) and k and k[0] == "sessions")
    IMPORT_INDEX_CACHE.invalidate_pattern(lambda k: isinstance(k, tuple) and k and k[0] == "import_index")


def invalidate_activity_cache() -> None:
    """Invalida TODO el caché de actividad administrativa."""
    ACTIVITY_CACHE.invalidate_pattern(lambda k: isinstance(k, tuple) and k and k[0] == "activity")
