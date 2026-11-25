from services.dashboard_service import DashboardService
from services.session_service import SessionService


class Sessions:
    def list_sessions(self, **_kwargs):
        return [{
            "session_id": "synthetic-session",
            "owner_id": "teacher-1",
            "capture_started_at": "2026-11-17T00:10:00Z",
            "fecha": "2026-11-17",
            "hora": "00:10",
            "status": "completed",
            "model_processing": {"status": "pending"},
        }]

    _get_date = staticmethod(SessionService._get_date)
    _get_time = staticmethod(SessionService._get_time)


def test_dashboard_uses_chile_date_for_utc_capture_timestamps():
    dashboard = DashboardService(session_service=Sessions())
    overview = dashboard.get_date_overview(today="2026-11-16", owner_id="teacher-1")
    recent = dashboard.get_recent_sessions(owner_id="teacher-1")
    chart = dashboard.get_sessions_by_date(owner_id="teacher-1")

    assert overview["today"] == {"fecha": "2026-11-16", "total": 1}
    assert recent[0]["fecha"] == "2026-11-16"
    assert recent[0]["hora"] == "21:10"
    assert recent[0]["capture_started_at"] == "2026-11-17T00:10:00Z"
    assert chart[0]["fecha"] == "2026-11-16"
