"""Punto de entrada canónico para interpretar sesiones de Unity.

La implementación se conserva en ``services.parsers`` para que, al añadir
otros parsers, todos compartan el mismo espacio de nombres.
"""

from services.parsers.session_parser import SessionParser

__all__ = ["SessionParser"]
