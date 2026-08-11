"""
Predictor - ÚNICA responsabilidad: recibir features y predecir.
Recibe SOLO las features.
"""

import joblib
import logging
import sys
from pathlib import Path
from typing import Dict, Any

import pandas as pd


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

__version__ = "1.0.0"


DIFICULTADES_VALIDAS = (
    "facil",
    "medio",
    "dificil",
)

GENEROS_VALIDOS = (
    "Masculino",
    "Femenino",
    "No binario",
    "Prefiero no decir",
)

ESCENAS_VALIDAS = (
    "aula",
    "oficina",
    "consulta_virtual",
)

FEATURES_MODELO = (
    "edad",
    "hora",
    "dia_semana",
    "tiempo_total_vr",
    "intentos",
    "difficulty_level",
    "escena",
    "genero",
)

FEATURES_NUMERICAS = (
    "edad",
    "hora",
    "dia_semana",
    "tiempo_total_vr",
    "intentos",
)


def resource_path(relative_path: str) -> Path:
    """
    Obtiene la ruta de un recurso tanto en desarrollo
    como dentro de un ejecutable generado por PyInstaller.
    """

    if getattr(sys, "frozen", False) and hasattr(
        sys, "_MEIPASS"
    ):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).resolve().parent.parent

    return base_path / relative_path


class Predictor:
    """
    Clase que SOLO carga el modelo y hace predicciones.
    Recibe datos YA PROCESADOS en el formato correcto.
    """

    def __init__(self, model_path=None):

        if model_path is None:
            self.model_path = resource_path(
                "ia/modelo_pipeline.pkl"
            )

        else:
            model_path = Path(model_path)

            if not model_path.is_absolute():
                self.model_path = resource_path(
                    str(model_path)
                )
            else:
                self.model_path = model_path

        self.model = None
        self.umbral = 0.5
        self.features_modelo = list(FEATURES_MODELO)

        self._cargar_modelo()

    def _cargar_modelo(self):
        try:

            if not self.model_path.exists():
                raise FileNotFoundError(
                    f"No se encontró el modelo en: "
                    f"{self.model_path}"
                )

            self.model = joblib.load(
                self.model_path
            )

            logger.info(
                "Modelo cargado desde: %s",
                self.model_path,
            )

            logger.info(
                "Tipo: %s",
                type(self.model).__name__,
            )

        except FileNotFoundError:

            logger.error(
                "Modelo no encontrado: %s",
                self.model_path,
            )

            raise

        except Exception as error:

            logger.error(
                "Error al cargar modelo: %s",
                error,
            )

            raise

    def predecir(
        self,
        features: Dict[str, Any]
    ) -> Dict[str, Any]:

        if self.model is None:
            raise RuntimeError(
                "Modelo no cargado"
            )

        # 1. VALIDAR CAMPOS
        self._validar_campos(features)

        # 2. CONSTRUIR DATAFRAME
        datos_filtrados = {
            k: features[k]
            for k in self.features_modelo
        }

        df = pd.DataFrame(
            [datos_filtrados]
        )

        # Asegurar tipos numéricos
        for col in FEATURES_NUMERICAS:

            try:

                df[col] = pd.to_numeric(
                    df[col]
                )

            except (
                ValueError,
                TypeError,
            ):

                raise ValueError(
                    f"Error al convertir "
                    f"{col} a numérico"
                )

        # 3. VALIDAR RANGOS
        self._validar_rangos(df)

        # 4. PREDECIR
        probabilidad = (
            self.model
            .predict_proba(df)[0, 1]
        )

        prediccion = (
            1
            if probabilidad >= self.umbral
            else 0
        )

        # 5. CALCULAR CONFIANZA
        confianza = (
            abs(probabilidad - 0.5) * 2
        )

        return {
            "probabilidad": round(
                float(probabilidad) * 100,
                2,
            ),
            "clasificacion": (
                "Alto Bienestar"
                if prediccion == 1
                else "Bajo Bienestar"
            ),
            "prediccion_raw": prediccion,
            "confianza": round(
                float(confianza) * 100,
                2,
            ),
            "umbral_usado": self.umbral,
        }

    def _validar_campos(
        self,
        features: Dict[str, Any]
    ):

        faltantes = [
            f
            for f in self.features_modelo
            if f not in features
        ]

        if faltantes:

            raise ValueError(
                f"Faltan campos requeridos: "
                f"{faltantes}. "
                f"Se requieren: "
                f"{self.features_modelo}"
            )

    def _validar_rangos(
        self,
        df: pd.DataFrame
    ):

        # Edad
        edad = df["edad"].iloc[0]

        if not float(edad).is_integer():

            raise ValueError(
                f"Edad debe ser un entero: "
                f"{edad}"
            )

        if edad < 10 or edad > 100:

            raise ValueError(
                f"Edad fuera de rango "
                f"(10-100): {edad}"
            )

        # Hora
        hora = df["hora"].iloc[0]

        if hora < 0 or hora > 23:

            raise ValueError(
                f"Hora fuera de rango "
                f"(0-23): {hora}"
            )

        # Día semana
        dia = df["dia_semana"].iloc[0]

        if dia < 0 or dia > 6:

            raise ValueError(
                f"Día semana fuera de rango "
                f"(0-6): {dia}"
            )

        # Tiempo VR
        tiempo = df[
            "tiempo_total_vr"
        ].iloc[0]

        if tiempo <= 0:

            raise ValueError(
                f"Tiempo VR debe ser "
                f"mayor a 0: {tiempo}"
            )

        # Intentos
        intentos = df[
            "intentos"
        ].iloc[0]

        if intentos < 0:

            raise ValueError(
                f"Intentos no puede ser "
                f"negativo: {intentos}"
            )

        # Dificultad
        dificultad = df[
            "difficulty_level"
        ].iloc[0]

        if dificultad not in DIFICULTADES_VALIDAS:

            raise ValueError(
                f"Dificultad inválida: "
                f"{dificultad}. "
                f"Opciones: "
                f"{', '.join(DIFICULTADES_VALIDAS)}"
            )

        # Escena
        escena = df[
            "escena"
        ].iloc[0]

        if escena not in ESCENAS_VALIDAS:

            raise ValueError(
                f"Escena inválida: "
                f"{escena}. "
                f"Opciones: "
                f"{', '.join(ESCENAS_VALIDAS)}"
            )

        # Género
        genero = df[
            "genero"
        ].iloc[0]

        if genero not in GENEROS_VALIDOS:

            raise ValueError(
                f"Género inválido: "
                f"{genero}. "
                f"Opciones: "
                f"{', '.join(GENEROS_VALIDOS)}"
            )

    def get_info(
        self
    ) -> Dict[str, Any]:

        return {
            "modelo_cargado": (
                self.model is not None
            ),
            "tipo_modelo": (
                type(self.model).__name__
                if self.model
                else None
            ),
            "features_requeridas": (
                self.features_modelo
            ),
            "umbral": self.umbral,
            "model_path": str(
                self.model_path
            ),
            "version": __version__,
        }


if __name__ == "__main__":

    features_correctas = {
        "edad": 16,
        "hora": 17,
        "dia_semana": 3,
        "tiempo_total_vr": 423,
        "intentos": 4,
        "difficulty_level": "dificil",
        "escena": "oficina",
        "genero": "Femenino",
    }

    predictor = Predictor()

    try:

        resultado = predictor.predecir(
            features_correctas
        )

        print("\nPREDICCION:")
        print(
            f"Clasificación: "
            f"{resultado['clasificacion']}"
        )

        print(
            f"Probabilidad: "
            f"{resultado['probabilidad']}%"
        )

        print(
            f"Confianza: "
            f"{resultado['confianza']}%"
        )

        print(
            f"Umbral: "
            f"{resultado['umbral_usado']}"
        )

    except Exception as error:

        print(
            f"\nError: {error}"
        )

    features_decimal = (
        features_correctas.copy()
    )

    features_decimal["edad"] = 16.5

    try:

        predictor.predecir(
            features_decimal
        )

        print(
            "\nEsto NO debería pasar "
            "(edad decimal)"
        )

    except ValueError as error:

        print(
            f"\nError capturado correctamente: "
            f"{error}"
        )