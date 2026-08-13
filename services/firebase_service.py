"""
services/firebase_service.py

Servicio base para administrar la conexión con Firebase Firestore.

Este servicio NO contiene lógica de negocio.
Su única responsabilidad es inicializar Firebase y entregar
la instancia de Firestore al resto de los servicios.

Todos los demás servicios deben importar la instancia:

    from services.firebase_service import firebase

Nunca deben importar firebase_admin directamente.
"""

from pathlib import Path
from weakref import ref

import firebase_admin
from firebase_admin import credentials, firestore


class FirebaseService:
    """
    Servicio singleton para administrar Firestore.
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

        self._initialize()

        self._initialized = True

    def _initialize(self):
        """
        Inicializa Firebase únicamente una vez.
        """

        if firebase_admin._apps:
            self.db = firestore.client()
            return

        base_path = Path(__file__).resolve().parent.parent

        key_path = base_path / "config" / "firebase_key.json"

        if not key_path.exists():
            raise FileNotFoundError(
                f"No existe la llave de Firebase:\n{key_path}"
            )

        cred = credentials.Certificate(str(key_path))

        firebase_admin.initialize_app(cred)

        self.db = firestore.client()

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

        return self.db.batch()

    def transaction(self):
        """
        Inicia una transacción.
        """

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

        self.document(collection, document_id).update(data)

    def delete_document(
        self,
        collection: str,
        document_id: str,
    ):
        """
        Elimina un documento.
        """

        self.document(collection, document_id).delete()

    def list_documents(
        self,
        collection: str,
    ):
        """
        Lista todos los documentos de una colección.
        """

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