# =========================================================
# EMOTIONLAB - INFERENCIA INDEPENDIENTE: 12 VARIABLES
# Mediana PERSONAL: solo indicador con respuestas ORIGINALES
# =========================================================
import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.utils.validation import check_is_fitted


def aprender_perfiles_centroides(modelo, escalador, variables):
    """La asignación permanece a cargo de K-Means; solo se orienta su eje."""
    variables = list(variables)
    centros = np.asarray(modelo.cluster_centers_, dtype=float)
    if centros.shape != (2, len(variables)) or not np.isfinite(centros).all():
        raise ValueError("Se necesitan dos centroides finitos.")
    # Orientación semántica: según el cuestionario, valores altos de estos
    # seis indicadores representan mayor tensión. No se fijan pesos 70/30.
    indicadores_tension = ["nerviosismo_pre", "estado_pre", "nerviosismo_facil",
                            "estado_facil", "nerviosismo_dificil", "estado_dificil"]
    centros_originales = escalador.inverse_transform(centros)
    referencias = centros_originales[:, [variables.index(v) for v in indicadores_tension]].mean(axis=1)
    # Si los perfiles no se pueden orientar, no inventamos un significado.
    if np.isclose(referencias[0], referencias[1], rtol=0, atol=1e-10):
        return None
    mayor = int(np.argmax(referencias))
    menor = 1 - mayor
    diferencia = centros[mayor] - centros[menor]
    norma2 = float(diferencia @ diferencia)
    if norma2 <= 1e-12:
        return None
    coef = diferencia / norma2
    aporte = np.abs(diferencia) / np.abs(diferencia).sum()
    return {
        "grupo_menor_tension": menor, "grupo_mayor_tension": mayor,
        "referencia_tension_centroides": {str(g): float(referencias[g]) for g in (0, 1)},
        "centroide_menor_escalado": centros[menor].tolist(),
        "coeficientes_escalados": dict(zip(variables, map(float, coef))),
        "pesos_relativos_absolutos": dict(zip(variables, map(float, aporte))),
    }


def resultado_centroides(perfiles, escalada, grupo, variables):
    # El eje orientado conecta ambos centroides en el espacio estandarizado.
    # 0 y 100 son los centroides; no son grados clínicos ni probabilidades.
    if grupo is None or perfiles is None:
        return {"perfil_tension": None, "indice_relativo_centroides": None}
    x = np.asarray(escalada, dtype=float).reshape(-1)
    origen = np.asarray(perfiles["centroide_menor_escalado"], dtype=float)
    coef = np.array([perfiles["coeficientes_escalados"][v] for v in variables])
    posicion = float((x - origen) @ coef)
    etiqueta = ("mayor_tension_relativa" if grupo == perfiles["grupo_mayor_tension"]
                else "menor_tension_relativa")
    return {"perfil_tension": etiqueta,
            "indice_relativo_centroides": round(float(np.clip(posicion * 100, 0, 100)), 6)}


class EmotionLabPredictor:
    @staticmethod
    def _sha256(ruta):
        h = hashlib.sha256()
        with open(ruta, 'rb') as f:
            for bloque in iter(lambda: f.read(1024 * 1024), b''):
                h.update(bloque)
        return h.hexdigest()

    def __init__(self, directorio_modelo):
        self.directorio = Path(directorio_modelo)
        rutas = {n: self.directorio / n for n in [
            'metadatos_v1.json', 'configuracion_inferencia_v1.json',
            'modelo_kmeans_v1.joblib', 'escalador_v1.joblib'
        ]}
        ruta_manifest = self.directorio / 'manifest_sha256_v1.json'
        if not ruta_manifest.is_file():
            raise FileNotFoundError('Falta el manifiesto de integridad.')
        manifest = json.loads(ruta_manifest.read_text(encoding='utf-8'))
        for nombre, ruta in rutas.items():
            if not ruta.is_file() or nombre not in manifest:
                raise FileNotFoundError(f'Falta archivo o hash: {nombre}')
            if self._sha256(ruta) != manifest[nombre]:
                raise ValueError(f'Integridad incorrecta: {nombre}')
        self.metadatos = json.loads(rutas['metadatos_v1.json'].read_text(encoding='utf-8'))
        self.configuracion = json.loads(rutas['configuracion_inferencia_v1.json'].read_text(encoding='utf-8'))
        if sklearn.__version__ != self.metadatos['versiones']['scikit-learn']:
            raise RuntimeError('La versión de scikit-learn es distinta a la del entrenamiento.')
        # Solo cargar modelos locales confiables propios: Joblib puede ejecutar código al cargar.
        self.modelo = joblib.load(rutas['modelo_kmeans_v1.joblib'])
        self.escalador = joblib.load(rutas['escalador_v1.joblib'])
        check_is_fitted(self.modelo)
        check_is_fitted(self.escalador)
        cfg = self.configuracion
        self.variables = list(cfg['variables'])
        self.fases = list(cfg['fases'].values())
        self.indicadores = list(cfg['indicadores'].values())
        self.min_observadas = int(cfg['criterios_minimos']['respuestas_observadas'])
        self.min_fases = int(cfg['criterios_minimos']['fases_observadas'])
        self.escala_minima = cfg['escala']['minimo']
        self.escala_maxima = cfg['escala']['maximo']
        if len(self.variables) != 12 or len(set(self.variables)) != 12:
            raise ValueError('Se requieren doce variables únicas.')
        if self.variables != self.metadatos['variables']:
            raise ValueError('Metadatos y configuración difieren.')
        for componente in (self.modelo, self.escalador):
            if componente.n_features_in_ != 12:
                raise ValueError('Modelo/escalador no entrenados con doce características.')
            if hasattr(componente, 'feature_names_in_') and list(componente.feature_names_in_) != self.variables:
                raise ValueError('Orden de características incompatible.')
        if self.modelo.n_clusters != self.metadatos['numero_grupos']:
            raise ValueError('Número de grupos incompatible.')
        for grupos in (self.fases, self.indicadores):
            lista = [v for grupo in grupos for v in grupo]
            if len(lista) != 12 or set(lista) != set(self.variables):
                raise ValueError('Fases/indicadores incompatibles.')
        imp = cfg['imputacion']
        if imp['metodo'] != 'mediana_personal_solo_indicador_con_observaciones':
            raise ValueError('Método de imputación incompatible.')
        if (imp.get('usar_mediana_de_otros_estudiantes') is not False
                or imp.get('usar_solo_valores_originales_del_mismo_estudiante') is not True):
            raise ValueError('No se permiten medianas externas.')
        if self.min_observadas != 1 or self.min_fases != 1:
            raise ValueError('Cero respuestas debe rechazarse.')
        if set(imp['variables_no_repetidas']) != {'preocupacion_pre', 'preparacion_pre'}:
            raise ValueError('Variables no repetidas incompatibles.')
        if imp.get('usar_mediana_global_personal') is not False:
            raise ValueError('No se permite mediana global.')

        self.perfiles_centroides = aprender_perfiles_centroides(self.modelo, self.escalador, self.variables)
        if self.perfiles_centroides != self.metadatos.get("perfiles_centroides"):
            raise ValueError("Los perfiles exportados no coinciden con los centroides.")

    def predecir(self, respuestas):
        if not isinstance(respuestas, dict):
            raise TypeError('Las respuestas deben formar un diccionario.')
        extra = set(respuestas) - set(self.variables)
        if extra:
            raise ValueError(f'Variables desconocidas: {sorted(extra)}')
        originales = {}
        for v in self.variables:
            valor = respuestas.get(v)
            if isinstance(valor, (bool, list, tuple, dict, set, np.ndarray)):
                raise ValueError(f'Tipo de respuesta inválido: {v}')
            if valor is None or (isinstance(valor, str) and not valor.strip()) or pd.isna(valor):
                originales[v] = np.nan
                continue
            try:
                numero = float(valor)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f'Respuesta no numérica: {v}') from exc
            if not (np.isfinite(numero) and numero.is_integer()
                    and self.escala_minima <= numero <= self.escala_maxima):
                raise ValueError(f'Respuesta fuera de escala: {v}')
            originales[v] = numero
        n = sum(pd.notna(v) for v in originales.values())
        faltantes = [v for v in self.variables if pd.isna(originales[v])]
        sin_datos, estimables = [], {}
        for grupo in self.indicadores:
            datos = [originales[v] for v in grupo if pd.notna(originales[v])]
            for v in grupo:
                if pd.isna(originales[v]):
                    if len(grupo) == 1 or not datos:
                        sin_datos.append(v)
                    else:
                        estimables[v] = float(np.median(datos))
        if n == 0 or sin_datos:
            return {
                'estado': ('informacion_insuficiente' if n == 0 else 'sin_datos_suficientes'),
                'grupo': None, 'observadas': n, 'estimadas': [], 'fuentes_imputacion': {},
                'variables_procesadas': None,
                'variables_sin_datos_suficientes': sin_datos, 'mensaje': 'sin datos suficientes',
                **resultado_centroides(None, None, None, self.variables),
            }
        completo = originales.copy()
        completo.update(estimables)
        if len(completo) != 12 or not np.isfinite(list(completo.values())).all():
            raise AssertionError('El ML requiere 12 valores numéricos finitos.')
        entrada = pd.DataFrame([completo], columns=self.variables)
        escalada = pd.DataFrame(self.escalador.transform(entrada), columns=self.variables)
        grupo = int(self.modelo.predict(escalada)[0])
        return {
            'estado': ('calculado_con_estimacion' if faltantes else 'calculado_sin_estimacion'),
            'grupo': grupo, 'observadas': n, 'estimadas': faltantes,
            'fuentes_imputacion': {v: 'mediana_personal_mismo_indicador' for v in faltantes},
            'variables_procesadas': {v: float(completo[v]) for v in self.variables},
            'variables_sin_datos_suficientes': [], 'mensaje': None,
            **resultado_centroides(self.perfiles_centroides, escalada, grupo, self.variables),
        }
