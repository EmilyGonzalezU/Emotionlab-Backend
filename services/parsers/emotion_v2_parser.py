"""Extractor de las seis variables del modelo V2 desde el registro de Unity.

Correspondencia semántica fijada por el proyecto (ver especificación §3):

* ``waiting_room_inicial``                      -> etapa ``inicial``
* ``Retroalimentacion_Oficina``                 -> etapa ``facil`` (PRE)
* ``Retroalimentacion_Salon``                   -> etapa ``dificil``

Las preguntas de cierre (``nerviosismo_cierre`` / ``sentir_hoy_cierre``) se
repiten entre etapas, por lo que NUNCA se toma "la primera coincidencia por
``idPregunta``": la etapa se resuelve combinando ``escena`` y orden
cronológico, con una política determinista ante duplicados.

Se usa SIEMPRE ``respuestaLabel`` (nunca ``respuestaIndex``). Los valores se
conservan tal como llegan; la normalización y validación de escala las realiza
el motor (``EmotionModelV2Service``).
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any, Mapping, Optional

from services.emotion_model_v2_service import (
    ETAPAS,
    VARIABLES_MODELO,
)

# Orden de evaluación de etapas: los tokens más específicos primero.
_ESTADIOS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("inicial", ("waiting_room_inicial", "waiting_room", "inicial")),
    ("dificil", ("retroalimentacion_salon", "salon")),
    ("facil", ("retroalimentacion_oficina", "oficina")),
)

# Preguntas de la medición inicial (sin sufijo ``_cierre``).
_PREGUNTAS_INICIAL: dict[str, str] = {
    "nerviosismo": "inicial_nerviosismo",
    "sentir_hoy": "inicial_sentir_hoy",
}

# Sufijos de las preguntas de cierre, prefijados por la etapa.
_SUFIJOS_CIERRE: dict[str, str] = {
    "nerviosismo_cierre": "nerviosismo_cierre",
    "sentir_hoy_cierre": "sentir_hoy_cierre",
}

_PREGUNTAS_CONOCIDAS = set(_PREGUNTAS_INICIAL) | set(_SUFIJOS_CIERRE)

_EDAD_KEYS = ("edad",)
_GENERO_KEYS = ("genero", "género")


def _normalizar(texto: Any) -> str:
    """Minúsculas y sin acentos, para comparar nombres de escena/pregunta."""

    if texto is None:
        return ""

    descompuesto = unicodedata.normalize("NFKD", str(texto))
    sin_acentos = descompuesto.encode("ascii", "ignore").decode("ascii")

    return sin_acentos.strip().lower()


def _parsear_timestamp(valor: Any) -> Optional[datetime]:
    if not isinstance(valor, str) or not valor.strip():
        return None

    texto = valor.strip().replace("Z", "+00:00")

    for candidato in (texto, texto.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidato)
        except ValueError:
            continue

    return None


def _etapa_de_escena(escena: Any) -> Optional[str]:
    normalizada = _normalizar(escena)

    if not normalizada:
        return None

    for etapa, tokens in _ESTADIOS:
        if any(token in normalizada for token in tokens):
            return etapa

    return None


def _variable_de(etapa: str, id_pregunta: str) -> Optional[str]:
    if etapa == "inicial":
        return _PREGUNTAS_INICIAL.get(id_pregunta)

    sufijo = _SUFIJOS_CIERRE.get(id_pregunta)

    if sufijo is None:
        return None

    variable = f"{etapa}_{sufijo}"

    return variable if variable in VARIABLES_MODELO else None


class EmotionV2Parser:
    """Extrae las seis variables del modelo V2 de un registro de Unity."""

    def __init__(self, record: Mapping[str, Any]) -> None:
        self.record = record if isinstance(record, Mapping) else {}

        session = self.record.get("session_data")

        if not isinstance(session, Mapping):
            session = self.record

        self.session: Mapping[str, Any] = session

        respuestas = self.session.get("respuestas")
        self.respuestas: list[Mapping[str, Any]] = (
            list(respuestas) if isinstance(respuestas, list) else []
        )

        eventos = self.session.get("eventos")
        self.eventos: list[Mapping[str, Any]] = (
            list(eventos) if isinstance(eventos, list) else []
        )

    # ------------------------------------------------------
    # RESPUESTAS AUXILIARES DEL ALUMNO
    # ------------------------------------------------------

    def _respuestas_ordenadas(self) -> list[tuple[int, Mapping[str, Any]]]:
        """Ordena cronológicamente conservando el orden original como desempate."""

        pares = list(enumerate(self.respuestas))

        def clave(item: tuple[int, Mapping[str, Any]]):
            indice, respuesta = item
            momento = _parsear_timestamp(
                respuesta.get("timestamp") if isinstance(respuesta, Mapping) else None
            )
            if momento is None:
                return (1, datetime.min, indice)
            return (0, momento, indice)

        return sorted(pares, key=clave)

    def _respuesta_por_pregunta(self, ids: tuple[str, ...]) -> Optional[Any]:
        """Última ``respuestaLabel`` (cronológica) para alguna de las preguntas."""

        encontrada = None

        for _indice, respuesta in self._respuestas_ordenadas():
            if not isinstance(respuesta, Mapping):
                continue

            if _normalizar(respuesta.get("idPregunta")) in ids:
                encontrada = respuesta.get("respuestaLabel")

        return encontrada

    # ------------------------------------------------------
    # EXTRACCIÓN DE LAS SEIS VARIABLES
    # ------------------------------------------------------

    def extraer_variables(self) -> dict[str, Any]:
        """Devuelve las seis variables y el reporte de extracción.

        Política determinista ante duplicados: dentro de una misma etapa y
        variable se conserva la ÚLTIMA respuesta en orden cronológico y se
        registra el duplicado. Las escenas ambiguas o no reconocidas se
        registran y no se asignan.
        """

        valores: dict[str, Any] = {variable: None for variable in VARIABLES_MODELO}
        procedencia: dict[str, dict[str, Any]] = {}
        duplicados: list[dict[str, Any]] = []
        ambiguos: list[dict[str, Any]] = []
        no_reconocidos: list[dict[str, Any]] = []

        for _indice, respuesta in self._respuestas_ordenadas():
            if not isinstance(respuesta, Mapping):
                continue

            id_pregunta = _normalizar(respuesta.get("idPregunta"))
            escena = respuesta.get("escena")
            etapa = _etapa_de_escena(escena)

            if etapa is None:
                if id_pregunta in _PREGUNTAS_CONOCIDAS:
                    ambiguos.append(
                        {
                            "idPregunta": respuesta.get("idPregunta"),
                            "escena": escena,
                            "timestamp": respuesta.get("timestamp"),
                        }
                    )
                continue

            variable = _variable_de(etapa, id_pregunta)

            if variable is None:
                no_reconocidos.append(
                    {
                        "idPregunta": respuesta.get("idPregunta"),
                        "escena": escena,
                    }
                )
                continue

            if variable in procedencia:
                duplicados.append(
                    {
                        "variable": variable,
                        "escena": escena,
                        "timestamp": respuesta.get("timestamp"),
                    }
                )

            valores[variable] = respuesta.get("respuestaLabel")
            procedencia[variable] = {
                "escena": escena,
                "idPregunta": respuesta.get("idPregunta"),
                "timestamp": respuesta.get("timestamp"),
            }

        return {
            "valores": valores,
            "procedencia": procedencia,
            "etapas": {
                etapa: {
                    "variables": list(variables),
                    "etiqueta": {"inicial": "INICIAL", "facil": "PRE", "dificil": "DIFÍCIL"}[etapa],
                }
                for etapa, variables in ETAPAS.items()
            },
            "duplicados": duplicados,
            "ambiguos": ambiguos,
            "no_reconocidos": no_reconocidos,
        }

    def get_model_values(self) -> dict[str, Any]:
        """Las seis variables en el orden del contrato (valores crudos)."""

        extraccion = self.extraer_variables()

        return {
            variable: extraccion["valores"][variable]
            for variable in VARIABLES_MODELO
        }

    def get_extraction_report(self) -> dict[str, Any]:
        extraccion = self.extraer_variables()

        return {
            "procedencia": extraccion["procedencia"],
            "etapas": extraccion["etapas"],
            "duplicados": extraccion["duplicados"],
            "ambiguos": extraccion["ambiguos"],
            "no_reconocidos": extraccion["no_reconocidos"],
        }

    # ------------------------------------------------------
    # INFORMACIÓN DEL ALUMNO (fuera del contrato del modelo)
    # ------------------------------------------------------

    def get_student_data(self) -> dict[str, Any]:
        """Información del alumno y de la experiencia para consultas/ficha."""

        edad = self._respuesta_por_pregunta(_EDAD_KEYS)
        genero = self._respuesta_por_pregunta(_GENERO_KEYS)

        if genero is None:
            genero = self.session.get("genero")

        return {
            # Se conserva la etiqueta ORIGINAL (p. ej. "+17"), sin inventar edad.
            "edad": edad,
            "genero": genero,
            "participant_id": self.session.get("idParticipante"),
            "fecha_inicio": self.session.get("fechaInicio"),
            "fecha_fin": self.session.get("fechaFin"),
            "difficulty_level": self.session.get("difficulty_level"),
            "facil_completado": self.session.get("facilCompletado"),
            "dificil_completado": self.session.get("dificilCompletado"),
            "retry_usado": self.session.get("retryUsado"),
            "ultimo_modo_completado": self.session.get("ultimoModoCompletado"),
            "total_respuestas": len(self.respuestas),
            "total_eventos": len(self.eventos),
        }
