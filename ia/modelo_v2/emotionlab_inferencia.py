import sys
import json
import hashlib
import hmac
import re
import math
import joblib
import pandas as pd

from pathlib import Path
from sklearn.pipeline import Pipeline


# =========================================================
# 1. CARACTERÍSTICAS DEL MODELO
# =========================================================

# Estas son las seis características utilizadas
# durante el entrenamiento de EmotionLab.

VARIABLES_MODELO = [
    "inicial_nerviosismo",
    "inicial_sentir_hoy",
    "facil_nerviosismo_cierre",
    "facil_sentir_hoy_cierre",
    "dificil_nerviosismo_cierre",
    "dificil_sentir_hoy_cierre"
]


# =========================================================
# 2. VERIFICACIÓN SHA256
# =========================================================

def calcular_sha256(ruta):

    calculadora = hashlib.sha256()

    with Path(ruta).open("rb") as archivo:

        for bloque in iter(
            lambda: archivo.read(1024 * 1024),
            b""
        ):
            calculadora.update(bloque)

    return calculadora.hexdigest()


# =========================================================
# 3. CARGA DEL MOTOR DE INFERENCIA
# =========================================================

def cargar_motor(carpeta):

    carpeta = Path(carpeta)

    ruta_modelo = carpeta / "emotionlab_modelo.joblib"
    ruta_config = carpeta / "emotionlab_config.json"

    # Verificamos los archivos necesarios.

    if not ruta_modelo.is_file():
        raise FileNotFoundError(
            "No se encontró emotionlab_modelo.joblib."
        )

    if not ruta_config.is_file():
        raise FileNotFoundError(
            "No se encontró emotionlab_config.json."
        )

    # Cargamos la configuración técnica.

    configuracion = json.loads(
        ruta_config.read_text(encoding="utf-8")
    )

    datos_modelo = configuracion["modelo"]
    integridad = configuracion["integridad"]

    # Comprobamos el nombre del archivo.

    if integridad["archivo_modelo"] != ruta_modelo.name:
        raise ValueError(
            "El archivo del modelo no coincide con la configuración."
        )

    # Recuperamos la huella SHA256 esperada.

    sha256_esperado = integridad["sha256_modelo"]

    if (
        not isinstance(sha256_esperado, str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            sha256_esperado
        ) is None
    ):
        raise ValueError(
            "La configuración contiene un SHA256 inválido."
        )

    # Verificamos la integridad ANTES de cargar Joblib.

    sha256_actual = calcular_sha256(ruta_modelo)

    if not hmac.compare_digest(
        sha256_actual,
        sha256_esperado
    ):
        raise ValueError(
            "La integridad del modelo no coincide."
        )

    # IMPORTANTE:
    # Solo deben cargarse modelos y configuraciones
    # obtenidos de fuentes confiables.
    #
    # SHA256 no demuestra por sí solo su autenticidad.

    pipeline = joblib.load(ruta_modelo)

    if not isinstance(pipeline, Pipeline):
        raise TypeError(
            "El archivo no contiene un Pipeline válido."
        )

    # Comprobamos los componentes del Pipeline.

    if list(pipeline.named_steps.keys()) != [
        "imputacion",
        "escalado",
        "agrupamiento"
    ]:
        raise ValueError(
            "El Pipeline no contiene los pasos esperados."
        )

    # Verificamos el orden de las características.

    variables = datos_modelo["variables_en_orden"]

    if variables != VARIABLES_MODELO:
        raise ValueError(
            "La configuración no conserva las seis "
            "características originales de EmotionLab."
        )

    if list(pipeline.feature_names_in_) != variables:
        raise ValueError(
            "Las características del Pipeline no coinciden."
        )

    if pipeline.n_features_in_ != len(variables):
        raise ValueError(
            "La cantidad de características es incorrecta."
        )

    # Confirmamos la escala utilizada en Unity.

    if datos_modelo["escala_entrada"] != [1, 9]:
        raise ValueError(
            "La escala de entrada no coincide con EmotionLab."
        )

    # Comprobamos que el estimador corresponda
    # al algoritmo documentado en la configuración.

    algoritmo = datos_modelo["algoritmo"]

    estimador = pipeline.named_steps["agrupamiento"]

    if algoritmo == "K-Means":

        from sklearn.cluster import KMeans

        if not isinstance(estimador, KMeans):
            raise ValueError(
                "El estimador no corresponde a K-Means."
            )

        grupos = estimador.n_clusters

    elif algoritmo == "GMM":

        from sklearn.mixture import GaussianMixture

        if not isinstance(estimador, GaussianMixture):
            raise ValueError(
                "El estimador no corresponde a GMM."
            )

        if not estimador.converged_:
            raise ValueError(
                "El modelo GMM no está convergido."
            )

        grupos = estimador.n_components

    else:
        raise ValueError(
            "El algoritmo de la configuración no es compatible."
        )

    if grupos != datos_modelo["grupos_solicitados"]:
        raise ValueError(
            "La cantidad de grupos no coincide con la configuración."
        )

    # Devolvemos el motor preparado para inferencia.

    return {
        "pipeline": pipeline,
        "configuracion": configuracion,
        "variables": variables
    }


# =========================================================
# 4. NORMALIZACIÓN DE RESPUESTAS
# =========================================================

def normalizar_respuesta(valor, variable, minimo, maximo):

    # No aceptamos valores booleanos.

    if isinstance(valor, bool):
        raise ValueError(
            f"Respuesta inválida en {variable}: booleano."
        )

    # Aceptamos números enteros.

    if isinstance(valor, int):

        numero = valor

    # Unity puede enviar valores numéricos como texto.
    # Por ejemplo, respuestaLabel = "7".

    elif isinstance(valor, str):

        texto = valor.strip()

        if re.fullmatch(r"[0-9]+", texto) is None:
            raise ValueError(
                f"Respuesta inválida en {variable}: "
                "se esperaba un entero."
            )

        numero = int(texto)

    else:
        raise ValueError(
            f"Respuesta inválida en {variable}: "
            "tipo de dato incompatible."
        )

    # Comprobamos el rango permitido.

    if not minimo <= numero <= maximo:
        raise ValueError(
            f"Respuesta fuera de escala en {variable}: "
            f"se esperaba un entero entre {minimo} y {maximo}."
        )

    return numero


# =========================================================
# 5. VALIDACIÓN DE SOLICITUDES
# =========================================================

def validar_solicitud(solicitud, motor):

    # -----------------------------------------------------
    # ESTRUCTURA GENERAL
    # -----------------------------------------------------

    if not isinstance(solicitud, dict):
        raise ValueError(
            "La solicitud debe ser un objeto JSON."
        )

    id_sesion = solicitud.get("id_sesion")

    if (
        not isinstance(id_sesion, str)
        or not id_sesion.strip()
        or len(id_sesion.strip()) > 128
    ):
        raise ValueError(
            "El identificador de sesión es inválido."
        )

    id_sesion = id_sesion.strip()

    respuestas = solicitud.get("respuestas")

    if not isinstance(respuestas, dict):
        raise ValueError(
            "El campo respuestas debe ser un objeto JSON."
        )

    variables = motor["variables"]

    escala = motor["configuracion"]["modelo"]["escala_entrada"]

    minimo, maximo = escala

    # -----------------------------------------------------
    # CAMPOS DESCONOCIDOS
    # -----------------------------------------------------

    # No aceptamos nombres de variables diferentes
    # a los utilizados durante el entrenamiento.

    desconocidas = [
        variable
        for variable in respuestas
        if variable not in variables
    ]

    if desconocidas:
        raise ValueError(
            "Se recibieron características desconocidas: "
            + ", ".join(desconocidas)
        )

    # -----------------------------------------------------
    # RESPUESTAS FALTANTES
    # -----------------------------------------------------

    faltantes = [
        variable
        for variable in variables
        if variable not in respuestas
        or respuestas[variable] is None
        or (
            isinstance(respuestas[variable], float)
            and math.isnan(respuestas[variable])
        )
        or (
            isinstance(respuestas[variable], str)
            and not respuestas[variable].strip()
        )
    ]

    # -----------------------------------------------------
    # VALIDACIÓN DE RESPUESTAS PRESENTES
    # -----------------------------------------------------

    respuestas_validas = {}

    for variable in variables:

        if variable in faltantes:
            continue

        respuestas_validas[variable] = normalizar_respuesta(
            respuestas[variable],
            variable,
            minimo,
            maximo
        )

    # -----------------------------------------------------
    # DATOS INSUFICIENTES
    # -----------------------------------------------------

    if len(faltantes) == len(variables):

        return {
            "estado": "datos_insuficientes",
            "id_sesion": id_sesion,
            "faltantes": faltantes,
            "analisis": None
        }

    # -----------------------------------------------------
    # DATOS VÁLIDOS
    # -----------------------------------------------------

    return {
        "estado": "listo_para_inferencia",
        "id_sesion": id_sesion,
        "faltantes": faltantes,
        "respuestas": {
            variable: respuestas_validas.get(variable)
            for variable in variables
        }
    }


# =========================================================
# 6. ANÁLISIS DE UNA SESIÓN
# =========================================================

def analizar_sesion(solicitud, motor):

    # Primero verificamos los datos.

    validacion = validar_solicitud(
        solicitud,
        motor
    )

    # No ejecutamos K-Means cuando faltan respuestas.

    if validacion["estado"] == "datos_insuficientes":
        return validacion

    variables = motor["variables"]

    respuestas = validacion["respuestas"]

    # Construimos la matriz con las seis variables
    # originales, conservando sus nombres y orden.

    X = pd.DataFrame(
        [
            {
                variable: respuestas[variable]
                for variable in variables
            }
        ],
        columns=variables
    ).astype(float)

    # El Pipeline aplica StandardScaler y predict().
    # No volvemos a entrenar el modelo.

    prediccion = motor["pipeline"].predict(X)

    grupo = int(prediccion[0])

    configuracion = motor["configuracion"]

    numero_grupos = configuracion["modelo"]["grupos_solicitados"]

    if not 0 <= grupo < numero_grupos:
        raise ValueError(
            "El modelo devolvió un grupo fuera del rango esperado."
        )

    # Las categorías heurísticas permanecen pendientes.
    # Los grupos no representan diagnósticos clínicos.

    return {
        "estado": "analisis_exploratorio",

        "id_sesion": validacion["id_sesion"],

        "analisis": {

            "perfil_machine_learning": {
                "algoritmo": configuracion["modelo"]["algoritmo"],
                "grupo": grupo
            },

            "indice_heuristico": None,

            "categoria_tension": None
        },

        "version_modelo": configuracion["version"],

        "aprobado_produccion": False
    }


# =========================================================
# 7. EJECUCIÓN INDEPENDIENTE
# =========================================================

def main():

    # Permite ejecutar el módulo de esta manera:
    #
    # python emotionlab_inferencia.py carpeta solicitud.json

    if len(sys.argv) != 3:
        raise ValueError(
            "Uso: python emotionlab_inferencia.py "
            "<carpeta_modelo> <solicitud.json>"
        )

    carpeta = Path(sys.argv[1])
    ruta_solicitud = Path(sys.argv[2])

    # Recuperamos el Pipeline previamente exportado.

    motor = cargar_motor(carpeta)

    # Recuperamos la solicitud de prueba.

    solicitud = json.loads(
        ruta_solicitud.read_text(encoding="utf-8")
    )

    # Ejecutamos el análisis sin reentrenar.

    resultado = analizar_sesion(
        solicitud,
        motor
    )

    # Devolvemos un JSON para el proceso que invoca
    # el módulo independiente.

    print(
        json.dumps(
            resultado,
            ensure_ascii=False,
            allow_nan=False
        )
    )


if __name__ == "__main__":
    main()
