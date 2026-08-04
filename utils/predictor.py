# utils/predictor.py
"""
Predictor - ÚNICA responsabilidad: recibir features y predecir
Recibe SOLO las features, sin metadata
"""

import joblib
import logging
import pandas as pd
from typing import Dict, Any
from pathlib import Path

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


class Predictor:
    """
    Clase que SOLO carga el modelo y hace predicciones.
    Recibe datos YA PROCESADOS en el formato correcto.
    """

    def __init__(self, model_path=None):
        if model_path is None or model_path == 'modelo_pipeline.pkl':
            current_dir = Path(__file__).resolve().parent
            base_dir = current_dir.parent
            self.model_path = base_dir / 'ia' / 'modelo_pipeline.pkl'
        else:
            self.model_path = model_path
            
        self.model = None
        self.umbral = 0.5
        self.features_modelo = list(FEATURES_MODELO)

        self._cargar_modelo()

    def _cargar_modelo(self):
        try:
            self.model = joblib.load(self.model_path)
            logger.info(f"Modelo cargado desde: {self.model_path}")
            logger.info(f"Tipo: {type(self.model).__name__}")
        except FileNotFoundError:
            logger.error(f"Modelo no encontrado: {self.model_path}")
            raise
        except Exception as e:
            logger.error(f"Error al cargar modelo: {e}")
            raise

    def predecir(self, features: Dict[str, Any]) -> Dict[str, Any]:
        if self.model is None:
            raise RuntimeError("Modelo no cargado")

        # 1. VALIDAR CAMPOS
        self._validar_campos(features)

        # 2. CONSTRUIR DATAFRAME SOLO CON LAS FEATURES DEL MODELO
        datos_filtrados = {
            k: features[k]
            for k in self.features_modelo
        }

        df = pd.DataFrame([datos_filtrados])

        # Asegurar tipos numéricos
        for col in FEATURES_NUMERICAS:
            try:
                df[col] = pd.to_numeric(df[col])
            except (ValueError, TypeError):
                raise ValueError(f"Error al convertir {col} a numérico")

        # 3. VALIDAR RANGOS
        self._validar_rangos(df)

        # 4. PREDECIR
        probabilidad = self.model.predict_proba(df)[0, 1]
        prediccion = 1 if probabilidad >= self.umbral else 0

        # 5. CALCULAR CONFIANZA
        confianza = abs(probabilidad - 0.5) * 2

        return {
            'probabilidad': round(float(probabilidad) * 100, 2),  # %
            'clasificacion': 'Alto Bienestar' if prediccion == 1 else 'Bajo Bienestar',
            'prediccion_raw': prediccion,
            'confianza': round(float(confianza) * 100, 2),  # %
            'umbral_usado': self.umbral
        }

    def _validar_campos(self, features: Dict[str, Any]):
        """Valida que existan TODOS los campos requeridos"""
        faltantes = [f for f in self.features_modelo if f not in features]

        if faltantes:
            raise ValueError(
                f"Faltan campos requeridos: {faltantes}. "
                f"Se requieren: {self.features_modelo}"
            )

    def _validar_rangos(self, df: pd.DataFrame):
        """Valida rangos - NO MODIFICA datos"""
        # Edad
        edad = df['edad'].iloc[0]

        if not float(edad).is_integer():
            raise ValueError(f"Edad debe ser un entero: {edad}")
        
        if edad < 10 or edad > 100:
            raise ValueError(f"Edad fuera de rango (10-100): {edad}")

        # Hora
        hora = df['hora'].iloc[0]
        if hora < 0 or hora > 23:
            raise ValueError(f"Hora fuera de rango (0-23): {hora}")

        # Día semana
        dia = df['dia_semana'].iloc[0]
        if dia < 0 or dia > 6:
            raise ValueError(f"Día semana fuera de rango (0-6): {dia}")

        # Tiempo VR
        tiempo = df['tiempo_total_vr'].iloc[0]
        if tiempo <= 0:
            raise ValueError(f"Tiempo VR debe ser mayor a 0: {tiempo}")

        # Intentos
        intentos = df['intentos'].iloc[0]
        if intentos < 0:
            raise ValueError(f"Intentos no puede ser negativo: {intentos}")

        # Dificultad
        dificultad = df['difficulty_level'].iloc[0]
        if dificultad not in DIFICULTADES_VALIDAS:
            raise ValueError(
                f"Dificultad inválida: {dificultad}. "
                f"Opciones: {', '.join(DIFICULTADES_VALIDAS)}"
            )

        # Escena
        escena = df['escena'].iloc[0]
        if escena not in ESCENAS_VALIDAS:
            raise ValueError(
                f"Escena inválida: {escena}. "
                f"Opciones: {', '.join(ESCENAS_VALIDAS)}"
            )

        # Género
        genero = df['genero'].iloc[0]
        if genero not in GENEROS_VALIDOS:
            raise ValueError(
                f"Género inválido: {genero}. "
                f"Opciones: {', '.join(GENEROS_VALIDOS)}"
            )

    def get_info(self) -> Dict[str, Any]:
        return {
            'modelo_cargado': self.model is not None,
            'tipo_modelo': type(self.model).__name__ if self.model else None,
            'features_requeridas': self.features_modelo,
            'umbral': self.umbral,
            'model_path': self.model_path,
            'version': __version__
        }


# ============================================================
# PRUEBA
# ============================================================

if __name__ == '__main__':
    # Datos correctos (SOLO features)
    features_correctas = {
        'edad': 16,
        'hora': 17,
        'dia_semana': 3,
        'tiempo_total_vr': 423,
        'intentos': 4,
        'difficulty_level': 'dificil',
        'escena': 'oficina',
        'genero': 'Femenino'
    }

    predictor = Predictor('modelo_pipeline.pkl')

    try:
        resultado = predictor.predecir(features_correctas)
        print("\nPREDICCION:")
        print(f"Clasificación: {resultado['clasificacion']}")
        print(f"Probabilidad: {resultado['probabilidad']}%")
        print(f"Confianza: {resultado['confianza']}%")
        print(f"Umbral: {resultado['umbral_usado']}")
    except Exception as e:
        print(f"\nError: {e}")

    # Probar con edad decimal (debe fallar)
    features_decimal = features_correctas.copy()
    features_decimal['edad'] = 16.5

    try:
        resultado = predictor.predecir(features_decimal)
        print("\nEsto NO deberia pasar (edad decimal)")
    except ValueError as e:
        print(f"\nError capturado correctamente: {e}")
