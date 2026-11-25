"""
EmotionLab - Integración del adaptador Unity con ML.

Responsabilidades:
1. Recibir una sesión JSON de Unity.
2. Transformar las respuestas a doce variables.
3. Entregar esas variables al predictor existente.
4. Obtener un resultado estructurado.

Este componente NO:
- Modifica Unity.
- Se conecta a AWS.
- Modifica frontend ni backend.
- Reentrena K-Means o StandardScaler.

La estructura de salida es provisional.
Su contrato definitivo se establecerá en la Etapa 7.5.
"""

import importlib.util
import json
from pathlib import Path


def cargar_modulo(ruta, nombre):

    especificacion = importlib.util.spec_from_file_location(
        nombre,
        str(ruta)
    )

    if (
        especificacion is None
        or especificacion.loader is None
    ):
        raise ImportError(
            f"No fue posible cargar el módulo: {nombre}"
        )

    modulo = importlib.util.module_from_spec(
        especificacion
    )

    especificacion.loader.exec_module(modulo)

    return modulo


class EmotionLabPipelineUnity:

    def __init__(self, directorio_modelo):

        self.directorio = Path(directorio_modelo)

        if not self.directorio.is_dir():
            raise FileNotFoundError(
                "No se encontró el directorio del modelo."
            )

        ruta_adaptador = (
            self.directorio / "emotionlab_adaptador_unity.py"
        )

        ruta_predictor = (
            self.directorio / "emotionlab_inferencia.py"
        )

        ruta_mapeo = (
            self.directorio / "mapeo_unity_ml_v1.json"
        )

        # Verificamos la integridad de los archivos
        # previamente registrados en el manifiesto.

        ruta_manifest = (
            self.directorio / "manifest_sha256_v1.json"
        )

        import hashlib

        with ruta_manifest.open("r", encoding="utf-8") as f:
            manifest = json.load(f)

        archivos_requeridos = [
            "emotionlab_adaptador_unity.py",
            "emotionlab_inferencia.py",
            "mapeo_unity_ml_v1.json",
            "modelo_kmeans_v1.joblib",
            "escalador_v1.joblib",
            "configuracion_inferencia_v1.json"
        ]

        for nombre in archivos_requeridos:

            ruta = self.directorio / nombre

            if not ruta.is_file() or nombre not in manifest:
                raise ValueError(
                    f"Componente requerido no registrado: {nombre}"
                )

            digest = hashlib.sha256(
                ruta.read_bytes()
            ).hexdigest()

            if digest != manifest[nombre]:
                raise ValueError(
                    f"Integridad incorrecta: {nombre}"
                )

        # Cargamos los módulos existentes.
        # No duplicamos sus implementaciones.

        modulo_adaptador = cargar_modulo(
            ruta_adaptador,
            "emotionlab_adaptador_pipeline"
        )

        modulo_predictor = cargar_modulo(
            ruta_predictor,
            "emotionlab_inferencia_pipeline"
        )

        self.adaptador = (
            modulo_adaptador.AdaptadorUnityEmotionLab(
                ruta_mapeo
            )
        )

        self.predictor = (
            modulo_predictor.EmotionLabPredictor(
                self.directorio
            )
        )

        # Verificamos que ambos módulos compartan
        # las mismas variables y el mismo orden.

        if (
            list(self.adaptador.variables)
            != list(self.predictor.variables)
        ):
            raise ValueError(
                "El adaptador y el predictor no utilizan "
                "el mismo orden de variables."
            )

    # -----------------------------------------------------
    # PROCESAMIENTO DE UNA SESIÓN DE UNITY
    # -----------------------------------------------------

    def procesar_sesion(self, registro):

        # 1. Extraemos las doce variables originales.

        adaptacion = self.adaptador.transformar_sesion(
            registro
        )

        # 2. Entregamos los valores al predictor.
        #
        # El predictor decide si puede calcular un grupo
        # y si corresponde estimar respuestas.
        #
        # Enviamos una copia del diccionario.

        inferencia = self.predictor.predecir(
            dict(adaptacion["variables"])
        )

        # 3. Conservamos ambos resultados por separado.
        #
        # Esto facilita el diagnóstico y permite
        # establecer posteriormente el contrato JSON.

        resultado = {
            "adaptacion": adaptacion,
            "inferencia": inferencia
        }

        # 4. Verificamos que la estructura sea JSON válido.

        json.dumps(
            resultado,
            ensure_ascii=False,
            allow_nan=False
        )

        return resultado

    # -----------------------------------------------------
    # PROCESAMIENTO DE VARIAS SESIONES
    # -----------------------------------------------------

    def procesar_lote(self, registros):

        if not isinstance(registros, list):
            raise ValueError(
                "El lote debe ser una lista de sesiones."
            )

        return [
            self.procesar_sesion(registro)
            for registro in registros
        ]
