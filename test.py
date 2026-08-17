features = {
    "edad": 17,
    "hora": 15,
    "dia_semana": 4,
    "tiempo_total_vr": 631,
    "intentos": 1,
    "difficulty_level": "dificil",
    "escena": "consulta_virtual",
    "genero": "Femenino"
}

from services.prediction_service import PredictionService

service = PredictionService()

resultado = service.predict(features)

print("RESULTADO:")
print(resultado)