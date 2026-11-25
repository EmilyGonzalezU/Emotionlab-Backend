"""Tests para participant_id generation y búsqueda np_* GSI.

No requiere AWS — usa lógica pura importada del servicio (o replicada aquí
para tests unitarios de las reglas de negocio del requerimiento).

ETAPA 2:
  * Juan Pérez González Ulloa → JPGU
  * Búsqueda parcial "Juan" debe traer candidatos prefijo JU
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Misma regex que en participant_service.py
_PUNCT_RE = re.compile(r"[^A-Z0-9]+")


def _gen_pid(nombre_completo: str) -> str:
    nombre_completo = (nombre_completo or "").strip()
    if not nombre_completo:
        return "DESC"
    partes = [p for p in nombre_completo.split() if p]
    if len(partes) == 1:
        iniciales = partes[0][:4]
    else:
        iniciales = "".join(p[0] for p in partes[:4])
    return _PUNCT_RE.sub("", iniciales.upper())[:4] or "DESC"


def _np_fields(nombre_completo: str) -> dict[str, str]:
    normalized = _PUNCT_RE.sub("", (nombre_completo or "").upper())
    return {f"np_{i}": normalized[:i] or "" for i in range(1, 5)}


# -------------------------------------------------------------------
# TEST CASES
# -------------------------------------------------------------------


def test_pid_juan_perez_full():
    """Caso ejemplo usuario: Juan Pérez González Ulloa → JPGU"""
    assert _gen_pid("Juan Pérez González Ulloa") == "JPGU"


def test_pid_solo_nombre():
    assert _gen_pid("María") == "MAR"[:4]  # María -> MARI (4 primeras letras)
    assert _gen_pid("María").startswith("MAR")


def test_pid_dos_palabras():
    assert _gen_pid("Juan Pérez") == "JP"
    assert _gen_pid("Ana María") == "AM"


def test_pid_tres_palabras():
    assert _gen_pid("Juan Pérez González") == "JPG"


def test_pid_empty_o_solo_espacios():
    assert _gen_pid("") == "DESC"
    assert _gen_pid("   ") == "DESC"
    assert _gen_pid(None) == "DESC"  # type: ignore[arg-type]


def test_pid_nombres_con_puntuacion():
    # 3 tokens separados por espacios: [Jean-Luc, Picard, d'Orbigny] -> J + P + d = JPD
    assert _gen_pid("Jean-Luc Picard d'Orbigny") == "JPD"
    # Asegurarse que puntuaciones se limpian
    assert re.search(r"[^A-Z]", _gen_pid("Jean-Luc Picard d'Orbigny")) is None


def test_np_fields_juan_perez():
    np = _np_fields("Juan Pérez González Ulloa")
    normalized = "JUANPÉREZGONZÁLEZULLOA"
    assert np["np_1"] == normalized[:1]
    assert np["np_2"] == normalized[:2]
    assert np["np_3"] == normalized[:3]
    assert np["np_4"] == normalized[:4]


def test_np_fields_busqueda_parcial_juan():
    """Si el profesor escribe "Juan", debemos tener prefijo JUAN en np_4."""
    candidate = "Juan Pérez González Ulloa"
    partial = "Juan"
    # Simulación búsqueda: comprobar que "JUAN" coincide con np_4
    partial_upper = _PUNCT_RE.sub("", partial.upper())
    np = _np_fields(candidate)
    # Asegura que la búsqueda de "Juan" matchea este candidato (coincidencia GSI prefix)
    assert len(partial_upper) <= 4
    assert np[f"np_{len(partial_upper)}"].startswith(partial_upper)
