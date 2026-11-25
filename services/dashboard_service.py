"""Consultas agregadas para el dashboard de EmotionLab.

EmotionLab V8 reports original K-Means group IDs (0 or 1). Historical records
may still use the legacy V2 helper; V8 groups are returned directly and are
never converted to binary outcome labels.
"""

from collections import Counter
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from services.emotion_classification import clasificacion_binaria
from services.emotion_model_v2_contract import ESTADO_ANALIZADO
from services.session_service import (
    STATUS_COMPLETED,
    STATUS_IMPORT_ERROR,
    STATUS_PENDING_IDENTIFICATION,
    STATUS_IDENTIFIED,
    SessionService,
)


class DashboardService:
    """Construye indicadores a partir de sesiones ya indexadas."""

    def __init__(self, session_service: Optional[SessionService] = None):
        self.session_service = session_service or SessionService()

    def get_summary(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        genero: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> dict:
        """Devuelve conteos principales con filtros opcionales."""
        sessions = self._filtered_sessions(date_from, date_to, genero, difficulty)
        statuses = Counter(session.get("status") for session in sessions)
        analyzed = sum(1 for session in sessions if self._is_analyzed(session))
        return {
            "total_sesiones": len(sessions),
            "analizadas": analyzed,
            "pending_identification": statuses[STATUS_PENDING_IDENTIFICATION],
            "identified": statuses[STATUS_IDENTIFIED],
            "completed": statuses[STATUS_COMPLETED],
            "import_errors": statuses[STATUS_IMPORT_ERROR],
        }

    def get_date_overview(self, *, today: Optional[str] = None, owner_id: Optional[str] = None) -> dict:
        """Contrato ligero del inicio: solo dias disponibles y sus conteos."""
        today = today or datetime.now(ZoneInfo("America/Santiago")).date().isoformat()
        groups = Counter(session.get("fecha") for session in self._filtered_sessions(None, None, None, None, owner_id=owner_id))
        groups.pop(None, None)
        return {
            "today": {"fecha": today, "total": groups.get(today, 0)},
            "dates": [
                {"fecha": fecha, "total": groups[fecha]}
                for fecha in sorted(groups, reverse=True)
            ],
        }

    def get_recent_sessions(
        self,
        limit: int = 10,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        genero: Optional[str] = None,
        difficulty: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> list[dict]:
        """Lista las sesiones mas recientes, sin incluir raw_json pesado."""
        if limit < 1:
            raise ValueError("limit debe ser mayor a cero")
        sessions = self._filtered_sessions(date_from, date_to, genero, difficulty, owner_id=owner_id)
        fields = (
            "session_id",
            "nombre",
            "ai_status",
            "status",
            "fecha",
            "hora",
            "capture_started_at",
            "edad",
            "genero",
            "difficulty",
        )
        return [{field: session.get(field) for field in fields} for session in sessions[:limit]]

    def get_sessions_by_date(
        self, *, date_from: Optional[str] = None, date_to: Optional[str] = None, owner_id: Optional[str] = None
    ) -> list[dict]:
        """Serie por fecha y grupo K-Means, conservando los IDs V8."""
        sessions = self._filtered_sessions(date_from, date_to, None, None, owner_id=owner_id)
        groups: dict[str, Counter] = {}
        for session in sessions:
            fecha = self.session_service._get_date(session)
            if not fecha:
                continue
            date_key = fecha
            counter = groups.setdefault(date_key, Counter())
            counter["total"] += 1
            grupo = self._group(session)
            if grupo is not None:
                counter[f"grupo_{grupo}"] += 1
        return [
            {
                "fecha": date_key,
                "total": values["total"],
                "grupo_0": values["grupo_0"],
                "grupo_1": values["grupo_1"],
                "grupo_2": values["grupo_2"],
            }
            for date_key, values in sorted(groups.items())
        ]

    def _filtered_sessions(
        self,
        date_from: Optional[str],
        date_to: Optional[str],
        genero: Optional[str],
        difficulty: Optional[str],
        owner_id: Optional[str] = None,
    ) -> list[dict]:
        sessions = self.session_service.list_sessions(
            date_from=date_from,
            date_to=date_to,
            include_raw_json=False,
        )
        filtered = []
        for session in sessions:
            if (genero is not None and session.get("genero") != genero) or (
                difficulty is not None and session.get("difficulty") != difficulty
            ) or (owner_id is not None and session.get("owner_id") != owner_id):
                continue
            item = dict(session)
            local_date = self.session_service._get_date(session)
            local_time = self.session_service._get_time(session)
            if local_date:
                item["fecha"] = local_date
            if local_time:
                item["hora"] = local_time
            filtered.append(item)
        return filtered

    @staticmethod
    def _model_processing(session: dict) -> dict:
        processing = session.get("model_processing")
        return processing if isinstance(processing, dict) else {}

    @classmethod
    def _is_analyzed(cls, session: dict) -> bool:
        return cls._model_processing(session).get("status") == ESTADO_ANALIZADO

    @classmethod
    def _group(cls, session: dict) -> Optional[int]:
        """Return a V8 cluster ID without relabeling it as a binary outcome."""
        processing = cls._model_processing(session)
        if processing.get("model_version") == "1.0.0-experimental":
            group = processing.get("grupo")
            return group if type(group) is int and group in (0, 1) else None
        return clasificacion_binaria(processing)
