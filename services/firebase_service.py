import os
from pathlib import Path
from weakref import ref

import firebase_admin
from firebase_admin import credentials, firestore
import sys


class FirebaseService:
    """
    Servicio singleton para administrar Firestore.

    Incluye logs de operaciones Firestore para auditoría en desarrollo.
    - En entorno ``frozen`` (PyInstaller / marcha blanca): los logs se
      desactivan por defecto a menos que FIRESTORE_LOGS=1.
    - En desarrollo: los logs se activan por defecto y pueden apagarse
      con FIRESTORE_LOGS=0.
    NUNCA se imprimen datos sensibles (passwords, tokens, credenciales).
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FirebaseService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):

        if self._initialized:
            return

        self.db = None

        # -------------------------------------------------------------
        # Configuración de logs de Firestore
        # -------------------------------------------------------------
        env_flag = os.getenv("FIRESTORE_LOGS")
        if getattr(sys, "frozen", False):
            # Producción / marcha blanca: OFF a menos que se indique explícitamente.
            self._logs_enabled = env_flag in ("1", "true", "True", "TRUE")
        else:
            # Desarrollo: ON por defecto.
            self._logs_enabled = env_flag not in ("0", "false", "False", "FALSE")

        self._initialize()

        self._initialized = True

    # ------------------------------------------------------------------
    # Helpers: Logs de operaciones
    # ------------------------------------------------------------------
    def _log(self, operacion: str, detalle: str) -> None:
        if not self._logs_enabled:
            return
        print(f"[FIRESTORE] {operacion} {detalle}", flush=True)

    def _initialize(self):
        """
        Inicializa Firebase únicamente una vez.

        Busca la configuración externa de Firebase tanto
        en desarrollo como en la aplicación empaquetada.
        """

        if firebase_admin._apps:
            self.db = firestore.client()
            return

        # ---------------------------------------------------------
        # DETERMINAR UBICACIÓN DE CONFIGURACIÓN
        # ---------------------------------------------------------

        if getattr(sys, "frozen", False):

            # EmotionLabBackend.exe:
            #
            # resources/
            # ├── backend/
            # │   └── EmotionLabBackend.exe  <- sys.executable
            # └── config/
            #     └── firebase_key.json
            #
            base_path = Path(sys.executable).resolve().parent.parent

        else:

            # Desarrollo:
            #
            # EmotionLab-Backend/
            # ├── config/
            # │   └── firebase_key.json
            # └── services/
            #
            base_path = Path(__file__).resolve().parent.parent

        key_path = base_path / "config" / "firebase_key.json"

        if not key_path.exists():
            raise FileNotFoundError(
                f"No existe la llave de Firebase:\n{key_path}"
            )

        cred = credentials.Certificate(str(key_path))

        firebase_admin.initialize_app(cred)

        self.db = firestore.client()
        self._log("INIT", f"Firestore client ready; config={key_path}")

    # ---------------------------------------------------------
    # Métodos genéricos
    # ---------------------------------------------------------

    def collection(self, name: str):
        """
        Retorna una colección.
        """

        return self.db.collection(name)

    def document(self, collection: str, document_id: str):
        """
        Retorna una referencia a un documento.
        """

        return self.db.collection(collection).document(document_id)

    def server_timestamp(self):
        """
        Timestamp generado por Firebase.
        """

        return firestore.SERVER_TIMESTAMP

    def batch(self):
        """
        Batch de escritura.
        """
        self._log("BATCH", "new write batch created")
        return self.db.batch()

    def transaction(self):
        """
        Inicia una transacción.
        """
        self._log("TX", "new transaction started")
        return self.db.transaction()

    # ---------------------------------------------------------
    # Métodos CRUD genéricos
    # ---------------------------------------------------------

    def create_document(
        self,
        collection: str,
        document_id: str,
        data: dict,
    ):
        self._log("WRITE", f"SET {collection}/{document_id}")
        ref = self.document(collection, document_id)

        ref.set(data)

        return ref

    def get_document(
        self,
        collection: str,
        document_id: str,
    ):
        """
        Obtiene un documento.
        """
        self._log("GET", f"{collection}/{document_id}")

        doc = self.document(collection, document_id).get()

        if not doc.exists:
            return None

        return doc.to_dict()

    def update_document(
        self,
        collection: str,
        document_id: str,
        data: dict,
    ):
        """
        Actualiza un documento.
        """
        self._log("UPDATE", f"{collection}/{document_id} fields={sorted(data.keys())}")

        self.document(collection, document_id).update(data)

    def delete_document(
        self,
        collection: str,
        document_id: str,
    ):
        """
        Elimina un documento.
        """
        self._log("DELETE", f"{collection}/{document_id}")

        self.document(collection, document_id).delete()

    def list_documents(
        self,
        collection: str,
    ):
        """
        Lista todos los documentos de una colección.

        ATENCIÓN: No usar esta operación para colecciones muy grandes en
        marcha blanca; preferir ``query_documents`` con filtros y/o
        paginación. Se deja aquí para backwards compatibility.
        """
        self._log("STREAM", f"{collection} (all documents)")

        docs = self.collection(collection).stream()

        resultado = []

        for doc in docs:

            data = doc.to_dict()

            data["id"] = doc.id

            resultado.append(data)

        return resultado

    def query_documents(
        self,
        collection: str,
        field: str,
        operator: str,
        value,
    ):
        """
        Consulta documentos usando un filtro.
        """
        safe_value = "<multi>" if isinstance(value, (list, tuple, set)) else type(value).__name__
        self._log("QUERY", f"{collection} where {field} {operator} ({safe_value})")

        docs = (
            self.collection(collection)
            .where(field, operator, value)
            .stream()
        )

        resultado = []

        for doc in docs:

            data = doc.to_dict()
            data["id"] = doc.id

            resultado.append(data)

        return resultado

# ---------------------------------------------------------
# Singleton global
# ---------------------------------------------------------

firebase = FirebaseService()