import unittest

from services.session_parser import SessionParser


class SessionParserTests(unittest.TestCase):
    def test_reads_the_real_aws_session_shape(self):
        parser = SessionParser(
            {
                "s3_key": "data/session.json",
                "session_data": {
                    "fechaInicio": "2026-07-17 00:49:39",
                    "fechaFin": "2026-07-17 00:54:12",
                    "difficulty_level": "dificil",
                    "respuestas": [
                        {"idPregunta": "edad", "respuestaLabel": "16"},
                        {"idPregunta": "genero", "respuestaLabel": "Femenino"},
                    ],
                    "eventos": [{"nombre": "session_started"}],
                },
            }
        )

        self.assertEqual(parser.get_age(), 16)
        self.assertEqual(parser.get_gender(), "Femenino")
        self.assertEqual(parser.get_scene(), "consulta_virtual")
        self.assertEqual(parser.get_duration(), 273)
        self.assertEqual(parser.get_attempts(), 1)
        self.assertEqual(parser.get_difficulty(), "dificil")
        self.assertEqual(
            parser.get_prediction_features(),
            {
                "edad": 16,
                "hora": 0,
                "dia_semana": 4,
                "tiempo_total_vr": 273,
                "intentos": 1,
                "difficulty_level": "dificil",
                "escena": "consulta_virtual",
                "genero": "Femenino",
            },
        )
        self.assertEqual(
            parser.get_dashboard_summary(),
            {
                "fecha": "2026-07-17",
                "hora": "00:49",
                "edad": 16,
                "genero": "Femenino",
                "difficulty": "dificil",
                "duracion": 273,
            },
        )

    def test_raises_when_session_is_incomplete_for_prediction(self):
        parser = SessionParser({"session_data": {"fechaInicio": "2026-07-17 10:00:00"}})

        self.assertFalse(parser.can_predict())
        with self.assertRaises(ValueError):
            parser.get_prediction_features()

    def test_normalizes_answer_ids_and_age_text(self):
        parser = SessionParser(
            {
                "session_data": {
                    "fechaInicio": "2026-07-17 10:00:00",
                    "fechaFin": "2026-07-17 10:05:00",
                    "difficulty_level": "medio",
                    "respuestas": [
                        {"idPregunta": "Edad ", "respuestaLabel": "17 años"},
                        {"idPregunta": "Genero", "respuestaLabel": "F"},
                        {"idPregunta": "escena", "respuestaLabel": "ignored", "escena": "waiting_room_inicial"},
                        {"idPregunta": "escena_real", "respuestaLabel": "ignored", "escena": "consulta_virtual"},
                    ],
                    "eventos": [],
                }
            }
        )

        self.assertEqual(parser.get_age(), 17)
        self.assertEqual(parser.get_gender(), "Femenino")
        self.assertEqual(parser.get_scene(), "consulta_virtual")

    def test_uses_session_completed_event_when_fecha_fin_is_missing(self):
        parser = SessionParser(
            {
                "session_data": {
                    "fechaInicio": "2026-07-17 00:49:39",
                    "fechaFin": "",
                    "difficulty_level": "dificil",
                    "respuestas": [
                        {"idPregunta": "edad", "respuestaLabel": "16"},
                        {"idPregunta": "genero", "respuestaLabel": "Femenino"},
                    ],
                    "eventos": [
                        {"nombre": "session_started", "timestamp": "2026-07-17 00:49:39.000"},
                        {"nombre": "session_completed", "timestamp": "2026-07-17 00:54:12.661"},
                    ],
                }
            }
        )

        self.assertTrue(parser.can_predict())
        self.assertEqual(parser.get_end_date().strftime("%Y-%m-%d %H:%M:%S.%f"), "2026-07-17 00:54:12.661000")
        self.assertEqual(parser.get_duration(), 273)
