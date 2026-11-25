"""
EmotionLab - Estandarización de resultados JSON.

Este módulo recibe registros de Unity y reutiliza
el pipeline de Machine Learning desarrollado
en la Etapa 7.4.

Entrega resultados con un contrato consistente.

No modifica el backend, Unity ni AWS.
No reentrena ni modifica K-Means o StandardScaler.
"""

import types
import json
from pathlib import Path


def cargar_modulo_salida(ruta, nombre):

    ruta = Path(ruta)
    modulo = types.ModuleType(nombre)
    modulo.__file__ = str(ruta)
    exec(compile(ruta.read_text(encoding="utf-8"), str(ruta), "exec"), modulo.__dict__)
    return modulo


class EmotionLabSalidaJSON:

    def __init__(self, directorio_modelo):

        self.directorio = Path(directorio_modelo)

        ruta_contrato = (
            self.directorio / "contrato_salida_unity_v1.json"
        )

        with ruta_contrato.open(
            "r", encoding="utf-8"
        ) as archivo:
            contrato = json.load(archivo)

        self.version = contrato["version_contrato"]
        self.variables = contrato["variables_modelo"]
        self.campos = contrato["campos_obligatorios"]
        self.estados = set(
            contrato["estados_permitidos"]
        )
        self.grupos = set(
            contrato["grupos_permitidos"]
        )

        # Reutilizamos el pipeline existente.

        modulo_pipeline = cargar_modulo_salida(
            self.directorio / "emotionlab_pipeline_unity.py",
            "emotionlab_pipeline_salida"
        )

        self.pipeline = (
            modulo_pipeline.EmotionLabPipelineUnity(
                self.directorio
            )
        )

        # El contrato debe coincidir con las variables
        # originales del modelo congelado.

        if (
            list(self.variables)
            != list(self.pipeline.adaptador.variables)
        ):
            raise ValueError(
                "El contrato no coincide con el orden "
                "de variables del modelo."
            )

        if (
            len(self.campos) != 12
            or len(set(self.campos)) != 12
            or len(self.variables) != 12
            or len(set(self.variables)) != 12
        ):
            raise ValueError(
                "La configuración del contrato es inválida."
            )

    # -----------------------------------------------------
    # IDENTIFICADOR DE LA SESIÓN
    # -----------------------------------------------------

    @staticmethod
    def obtener_identificador(registro):

        if not isinstance(registro, dict):
            return None

        sesion = registro.get(
            "session_data",
            registro
        )

        if not isinstance(sesion, dict):
            return None

        identificador = sesion.get(
            "idParticipante"
        )

        if (
            not isinstance(identificador, str)
            or not identificador.strip()
        ):
            return None

        return identificador

    # -----------------------------------------------------
    # VALIDACIÓN DE LA ESTRUCTURA DE SALIDA
    # -----------------------------------------------------

    def validar_resultado(self, resultado):

        if set(resultado) != set(self.campos):
            raise ValueError(
                "La salida no contiene los campos definidos."
            )

        estado = resultado["estado"]
        grupo = resultado["grupo"]

        if estado not in self.estados:
            raise ValueError(
                "El estado de salida no está permitido."
            )

        if (
            resultado["version_contrato"]
            != self.version
        ):
            raise ValueError(
                "La versión del contrato no coincide."
            )

        if (
            resultado["id_participante"] is not None
            and not isinstance(
                resultado["id_participante"], str
            )
        ):
            raise ValueError(
                "Identificador de participante inválido."
            )

        if not isinstance(
            resultado["estimadas"], list
        ):
            raise ValueError(
                "La lista de estimaciones es inválida."
            )

        pendientes = resultado["variables_sin_datos_suficientes"]
        if (not isinstance(pendientes, list)
                or len(pendientes) != len(set(pendientes))
                or not set(pendientes).issubset(self.variables)):
            raise ValueError("Variables sin datos suficientes inválidas.")

        if estado.startswith("calculado_"):
            if pendientes:
                raise ValueError("Un resultado calculado no puede tener variables pendientes.")

            if (
                type(grupo) is not int
                or grupo not in self.grupos
            ):
                raise ValueError(
                    "Una evaluación calculada requiere "
                    "un grupo válido."
                )

            if (
                type(resultado["observadas"]) is not int
                or not (1 <= resultado["observadas"] <= 12)
            ):
                raise ValueError(
                    "Cantidad de respuestas incompatible."
                )

            for campo in [
                "variables_originales",
                "variables_procesadas"
            ]:

                valores = resultado[campo]

                if (
                    not isinstance(valores, dict)
                    or list(valores) != self.variables
                ):
                    raise ValueError(
                        f"Estructura inválida: {campo}"
                    )

            if resultado["error"] is not None:
                raise ValueError(
                    "Una inferencia válida no debe "
                    "contener errores."
                )

            estimadas = resultado["estimadas"]

            if (
                len(set(estimadas)) != len(estimadas)
                or not set(estimadas).issubset(
                    self.variables
                )
            ):
                raise ValueError(
                    "Variables estimadas inválidas."
                )

            if (
                estado == "calculado_sin_estimacion"
                and (
                    estimadas
                    or resultado["observadas"] != 12
                )
            ):
                raise ValueError(
                    "Inconsistencia en inferencia completa."
                )

            if (
                estado == "calculado_con_estimacion"
                and not estimadas
            ):
                raise ValueError(
                    "Faltan variables estimadas."
                )

            # El modelo siempre debe recibir 12 valores válidos.
            # La lista de imputadas debe coincidir exactamente
            # con las respuestas que faltaban en la sesión original.
            import math
            original = resultado["variables_originales"]
            procesadas = resultado["variables_procesadas"]
            ausentes = [v for v in self.variables if original[v] is None]

            if (set(estimadas) != set(ausentes)
                    or len(estimadas) != 12 - resultado["observadas"]):
                raise ValueError(
                    "Las variables imputadas no coinciden con las faltantes."
                )

            for variable in self.variables:
                valor = procesadas[variable]
                if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                    raise ValueError("El resultado contiene valores no numéricos.")
                if not math.isfinite(valor):
                    raise ValueError("El resultado contiene valores no finitos.")
                if original[variable] is not None:
                    if not math.isclose(
                        float(original[variable]), float(valor),
                        rel_tol=0, abs_tol=1e-9
                    ):
                        raise ValueError(
                            f"Se modificó una respuesta original: {variable}."
                        )

            if (estado == "calculado_con_estimacion"
                    and not (1 <= resultado["observadas"] <= 11)):
                raise ValueError("Estado de imputación incompatible.")

        else:

            if grupo is not None:
                raise ValueError(
                    "Una evaluación no calculada "
                    "no debe tener grupo."
                )

            if (
                resultado["variables_procesadas"] is not None
                or resultado["estimadas"]
            ):
                raise ValueError(
                    "No se deben presentar estimaciones "
                    "en resultados no calculados."
                )

            if estado == "sin_datos_suficientes":
                originales = resultado["variables_originales"]
                if (type(resultado["observadas"]) is not int
                        or not 1 <= resultado["observadas"] <= 11
                        or resultado["error"] is not None
                        or not isinstance(originales, dict)
                        or list(originales) != self.variables
                        or not pendientes
                        or any(originales[v] is not None for v in pendientes)
                        or sum(v is not None for v in originales.values())
                        != resultado["observadas"]):
                    raise ValueError("Resultado sin datos suficientes incompatible.")

            if estado == "informacion_insuficiente":
                if pendientes and not all(
                    resultado["variables_originales"].get(v) is None
                    for v in pendientes
                ):
                    raise ValueError("Pendientes incompatibles con la sesión vacía.")

                if (
                    resultado["observadas"] != 0
                    or resultado["error"] is not None
                    or not isinstance(
                        resultado["variables_originales"],
                        dict
                    )
                    or list(
                        resultado["variables_originales"]
                    ) != self.variables
                ):
                    raise ValueError(
                        "Resultado insuficiente incompatible."
                    )
                if any(
                    valor is not None
                    for valor in resultado["variables_originales"].values()
                ):
                    raise ValueError(
                        "No se debe rechazar una sesión con respuestas válidas."
                    )

            if estado == "error_entrada":
                if pendientes:
                    raise ValueError("Una entrada inválida no puede tener variables pendientes.")

                if (
                    resultado["observadas"] is not None
                    or resultado["variables_originales"]
                    is not None
                    or not isinstance(
                        resultado["error"], dict
                    )
                ):
                    raise ValueError(
                        "Estructura de error incompatible."
                    )

        # Comprobación de los campos aprendidos a partir de los centroides.
        # Se permite None en ambos si los centroides no tienen orientación inequívoca.
        if grupo is None:
            if resultado["perfil_tension"] is not None or resultado["indice_relativo_centroides"] is not None:
                raise ValueError("Una sesión no evaluable no tiene perfil ni índice.")
        else:
            import math
            perfil = resultado["perfil_tension"]
            indice = resultado["indice_relativo_centroides"]
            if (perfil is None) != (indice is None):
                raise ValueError("Perfil e índice deben estar presentes juntos.")
            if perfil is not None:
                if perfil not in {"mayor_tension_relativa", "menor_tension_relativa"}:
                    raise ValueError("Perfil de tensión no válido.")
                if (isinstance(indice, bool) or not isinstance(indice, (int, float))
                        or not math.isfinite(indice) or not 0 <= indice <= 100):
                    raise ValueError("Índice relativo fuera de rango [0, 100].")
            referencia = self.pipeline.predictor.predecir(resultado["variables_originales"])
            for campo in ("grupo", "perfil_tension", "indice_relativo_centroides"):
                if resultado[campo] != referencia[campo]:
                    raise ValueError(f"Resultado inconsistente: {campo}")

        # Verificamos que el resultado completo
        # pueda serializarse sin NaN ni infinitos.

        json.dumps(
            resultado,
            ensure_ascii=False,
            allow_nan=False
        )

        return True

    # -----------------------------------------------------
    # PROCESAMIENTO DE UNA SESIÓN
    # -----------------------------------------------------

    def procesar_sesion(self, registro):

        identificador = self.obtener_identificador(
            registro
        )

        try:

            resultado_pipeline = (
                self.pipeline.procesar_sesion(
                    registro
                )
            )

        except ValueError as error_entrada:

            # Los errores de entrada no generan grupos.
            # Los errores de carga o integridad del modelo
            # no se ocultan aquí.

            resultado = {
                "version_contrato": self.version,
                "id_participante": identificador,
                "estado": "error_entrada",
                "grupo": None,
                "observadas": None,
                "estimadas": [],
                "variables_sin_datos_suficientes": [],
                "variables_originales": None,
                "variables_procesadas": None,
                "error": {
                    "codigo": "DATOS_INVALIDOS",
                    "detalle": str(error_entrada)[:240]
                }
            }

        else:

            adaptacion = resultado_pipeline[
                "adaptacion"
            ]

            inferencia = resultado_pipeline[
                "inferencia"
            ]

            valores_procesados = inferencia[
                "variables_procesadas"
            ]

            resultado = {
                "version_contrato": self.version,
                "id_participante": identificador,
                "estado": inferencia["estado"],
                "grupo": inferencia["grupo"],
                "observadas": inferencia["observadas"],
                "estimadas": list(
                    inferencia["estimadas"]
                ),
                "variables_sin_datos_suficientes": list(
                    inferencia.get("variables_sin_datos_suficientes", [])
                ),
                "variables_originales": dict(
                    adaptacion["variables"]
                ),
                "variables_procesadas": (
                    dict(valores_procesados)
                    if valores_procesados is not None
                    else None
                ),
                "error": None
            }

            resultado["perfil_tension"] = inferencia["perfil_tension"]
            resultado["indice_relativo_centroides"] = inferencia["indice_relativo_centroides"]

        if resultado["estado"] == "error_entrada":
            resultado["perfil_tension"] = None
            resultado["indice_relativo_centroides"] = None
        self.validar_resultado(resultado)

        return resultado

    # -----------------------------------------------------
    # PROCESAMIENTO DE UN LOTE DE SESIONES
    # -----------------------------------------------------

    def procesar_lote(self, registros):

        if not isinstance(registros, list):
            raise ValueError(
                "El lote debe ser una lista JSON."
            )

        return [
            self.procesar_sesion(registro)
            for registro in registros
        ]
