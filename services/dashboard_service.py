"""Consultas agregadas para el dashboard de EmotionLab."""

from collections import Counter
from datetime import date
from typing import Optional

from services.session_service import (
    STATUS_COMPLETED,
    STATUS_IMPORT_ERROR,
    STATUS_PENDING_IDENTIFICATION,
    STATUS_IDENTIFIED,
    STATUS_PENDING_PREDICTION,
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
        predictions = Counter(
            (session.get("prediction") or {}).get("bienestar") for session in sessions
        )
        return {
            "total_sesiones": len(sessions),
            "bienestar_si": predictions["SI"],
            "bienestar_no": predictions["NO"],
            "pending_prediction": statuses[STATUS_PENDING_PREDICTION],
            "pending_identification": statuses[STATUS_PENDING_IDENTIFICATION],
            "identified": statuses[STATUS_IDENTIFIED],
            "completed": statuses[STATUS_COMPLETED],
            "import_errors": statuses[STATUS_IMPORT_ERROR],
        }

    def get_date_overview(self, *, today: Optional[str] = None) -> dict:
        """Contrato ligero del inicio: solo dias disponibles y sus conteos."""
        today = today or date.today().isoformat()
        groups = Counter(session.get("fecha") for session in self.session_service.list_sessions())
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
    ) -> list[dict]:
        """Lista las sesiones mas recientes, sin incluir raw_json pesado."""
        if limit < 1:
            raise ValueError("limit debe ser mayor a cero")
        sessions = self._filtered_sessions(date_from, date_to, genero, difficulty)
        fields = (
            "session_id",
            "nombre",
            "prediction",
            "status",
            "fecha",
            "hora",
            "edad",
            "genero",
            "difficulty",
        )
        return [{field: session.get(field) for field in fields} for session in sessions[:limit]]

    def get_sessions_by_date(
        self, *, date_from: Optional[str] = None, date_to: Optional[str] = None
    ) -> list[dict]:
        """Serie para graficos: total, SI y NO agrupados por fecha de inicio."""
        sessions = self._filtered_sessions(date_from, date_to, None, None)
        groups: dict[str, Counter] = {}
        for session in sessions:
            fecha = session.get("fecha")
            if not fecha:
                continue
            date_key = fecha
            counter = groups.setdefault(date_key, Counter())
            counter["total"] += 1
            bienestar = (session.get("prediction") or {}).get("bienestar")
            if bienestar in {"SI", "NO"}:
                counter[bienestar] += 1
        return [
            {"fecha": date_key, "total": values["total"], "SI": values["SI"], "NO": values["NO"]}
            for date_key, values in sorted(groups.items())
        ]

    def _filtered_sessions(
        self,
        date_from: Optional[str],
        date_to: Optional[str],
        genero: Optional[str],
        difficulty: Optional[str],
    ) -> list[dict]:
        sessions = self.session_service.list_sessions(date_from=date_from, date_to=date_to)
        return [
            session
            for session in sessions
            if (genero is None or session.get("genero") == genero)
            and (difficulty is None or session.get("difficulty") == difficulty)
        ]
