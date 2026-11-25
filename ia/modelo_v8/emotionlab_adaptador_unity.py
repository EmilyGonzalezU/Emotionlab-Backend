"""
EmotionLab - Adaptador experimental de registros Unity.

Origen:
Registros JSON procedentes de Unity y compartidos
por el equipo de EmotionLab, con referencias de AWS.

Responsabilidades:
- Interpretar la estructura de una sesión Unity.
- Identificar las doce variables usadas por K-Means.
- Utilizar respuestaLabel como puntuación.
- Validar la escala de 1 a 9.
- Identificar respuestas faltantes y datos inesperados.

No entrena modelos, no aplica imputación,
no ejecuta K-Means y no realiza conexiones con AWS.
"""

import json
import math
from pathlib import Path


class AdaptadorUnityEmotionLab:

    def __init__(self, ruta_configuracion):

        ruta = Path(ruta_configuracion)

        with ruta.open("r", encoding="utf-8") as archivo:
            configuracion = json.load(archivo)

        self.variables = configuracion["variables_modelo"]
        self.campo_puntuacion = configuracion["campo_puntuacion"]

        equivalencias = configuracion["equivalencias"]

        self.mapeo = {
            (item["escena"], item["idPregunta"]):
                item["variable"]
            for item in equivalencias
        }

        self.ids_reconocidos = {
            pregunta
            for _, pregunta in self.mapeo
        }

        if (
            len(self.variables) != 12
            or len(set(self.variables)) != 12
            or len(equivalencias) != 12
            or len(self.mapeo) != 12
            or set(self.mapeo.values()) != set(self.variables)
            or self.campo_puntuacion != "respuestaLabel"
        ):
            raise ValueError(
                "La configuración del adaptador es incompatible."
            )

    # -----------------------------------------------------
    # VALIDACIÓN DEL VALOR DE UNA RESPUESTA
    # -----------------------------------------------------

    @staticmethod
    def validar_puntuacion(valor, variable):

        if valor is None:
            return None

        if isinstance(valor, bool):
            raise ValueError(
                f"{variable}: los booleanos no son puntuaciones."
            )

        if isinstance(valor, str) and not valor.strip():
            return None

        try:
            numero = float(valor)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                f"{variable}: puntuación no numérica."
            ) from None

        if (
            not math.isfinite(numero)
            or not numero.is_integer()
            or not 1 <= numero <= 9
        ):
            raise ValueError(
                f"{variable}: se requiere un entero entre 1 y 9."
            )

        return int(numero)

    # -----------------------------------------------------
    # TRANSFORMACIÓN DE UNA SESIÓN
    # -----------------------------------------------------

    def transformar_sesion(self, registro):

        if not isinstance(registro, dict):
            raise ValueError(
                "La sesión debe ser un objeto JSON."
            )

        sesion = registro.get("session_data", registro)

        if not isinstance(sesion, dict):
            raise ValueError(
                "session_data debe ser un objeto JSON."
            )

        respuestas = sesion.get("respuestas", [])

        if respuestas is None:
            respuestas = []

        if not isinstance(respuestas, list):
            raise ValueError(
                "El campo respuestas debe ser una lista."
            )

        # Inicializamos las doce variables.
        # Ninguna respuesta faltante se inventa aquí.

        variables = {
            nombre: None
            for nombre in self.variables
        }

        encontradas = set()
        ignoradas = 0
        preguntas_desconocidas = []

        # Preguntas adicionales conocidas de Unity, distintas
        # del cuestionario. Las 12 preguntas ML están en self.mapeo.
        auxiliares_permitidas = {
            ("waiting_room_inicial", "genero"),
            ("waiting_room_inicial", "edad"),
        }

        for posicion, respuesta in enumerate(
            respuestas, start=1
        ):

            if not isinstance(respuesta, dict):
                raise ValueError(
                    f"Respuesta {posicion}: estructura inválida."
                )

            escena = respuesta.get("escena")
            pregunta = respuesta.get("idPregunta")

            if (
                not isinstance(escena, str)
                or not escena.strip()
                or not isinstance(pregunta, str)
                or not pregunta.strip()
            ):
                raise ValueError(
                    f"Respuesta {posicion}: faltan escena "
                    "o idPregunta."
                )

            clave = (escena, pregunta)

            # Una pregunta conocida en una escena distinta
            # requiere revisión: no suponemos equivalencias.

            if (
                clave not in self.mapeo
                and pregunta in self.ids_reconocidos
            ):
                raise ValueError(
                    "Pregunta reconocida en escena incompatible: "
                    f"{escena} / {pregunta}"
                )

            # Ignoramos únicamente preguntas ajenas al
            # modelo, como género o edad.

            if clave not in self.mapeo:
                if clave not in auxiliares_permitidas:
                    preguntas_desconocidas.append(clave)
                ignoradas += 1
                continue

            variable = self.mapeo[clave]

            # No elegimos arbitrariamente una respuesta
            # cuando la misma pregunta aparece duplicada.

            if variable in encontradas:
                raise ValueError(
                    f"Pregunta duplicada para {variable}."
                )

            encontradas.add(variable)

            if self.campo_puntuacion not in respuesta:
                raise ValueError(
                    f"Falta {self.campo_puntuacion} para {variable}."
                )
            valor = respuesta[self.campo_puntuacion]

            variables[variable] = self.validar_puntuacion(
                valor,
                variable
            )

        observadas = sum(
            valor is not None
            for valor in variables.values()
        )

        # Un ID no reconocido y alguna pregunta ML faltante
        # pueden indicar cambio de esquema, no ausencia voluntaria.
        if preguntas_desconocidas and observadas < len(self.variables):
            raise ValueError(
                "Preguntas desconocidas con variables ML faltantes: "
                f"{preguntas_desconocidas[:3]}"
            )

        ausentes = [
            nombre
            for nombre, valor in variables.items()
            if valor is None
        ]

        if observadas == len(self.variables):
            estado = "completa"
        elif observadas == 0:
            estado = "sin_variables"
        else:
            estado = "parcial"

        return {
            "estado_adaptacion": estado,
            "variables": variables,
            "observadas": observadas,
            "ausentes": ausentes,
            "preguntas_ignoradas": ignoradas
        }

    # -----------------------------------------------------
    # TRANSFORMACIÓN DE VARIAS SESIONES
    # -----------------------------------------------------

    def transformar_lote(self, registros):

        if not isinstance(registros, list):
            raise ValueError(
                "El lote debe ser una lista de sesiones."
            )

        return [
            self.transformar_sesion(registro)
            for registro in registros
        ]
