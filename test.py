import requests

from services.session_parser import SessionParser
from utils.predictor import Predictor

URL = "https://c2a1zfsvcj.execute-api.us-east-1.amazonaws.com/prod/data"

predictor = Predictor()

response = requests.get(URL)
response.raise_for_status()

data = response.json()

sesiones = data["sesiones"]

print(f"\nSe recibieron {len(sesiones)} sesiones\n")

for i, sesion in enumerate(sesiones, start=1):

    print("=" * 100)
    print(f"SESIÓN {i}")
    print(sesion.get("s3_key"))

    try:
        parser = SessionParser(sesion)

        features = parser.get_prediction_features()

        print("Features:")
        print(features)

        resultado = predictor.predecir(features)

        print("Predicción:")
        print(resultado)

    except Exception as e:
        print("ERROR:")
        print(type(e).__name__, "-", e)