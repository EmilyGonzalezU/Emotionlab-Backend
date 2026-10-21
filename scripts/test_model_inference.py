"""Run one valid inference against the serialized production model."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.predictor import Predictor


FEATURES = {
    "edad": 16,
    "hora": 17,
    "dia_semana": 3,
    "tiempo_total_vr": 423,
    "intentos": 4,
    "difficulty_level": "dificil",
    "escena": "oficina",
    "genero": "Femenino",
}


def main() -> None:
    prediction = Predictor().predecir(FEATURES)
    required = {"probabilidad", "clasificacion", "prediccion_raw", "confianza", "umbral_usado"}
    missing = required.difference(prediction)
    if missing:
        raise SystemExit(f"Inference response missing: {sorted(missing)}")
    print(f"Model inference: OK ({prediction['clasificacion']}, {prediction['probabilidad']}%)")


if __name__ == "__main__":
    main()
