"""
Administración de participantes y su relación con sesiones.

ETAPA 2:
- Persistencia en DynamoDB emotionlab-participants.
- PK = participant_id (ej: JPGU).
- GSIs np_1..np_4 para búsqueda parcial por prefijos de nombre sin full scan.
- Mantiene helpers estáticos y una API pública estable.

Regla de identificación:
- El participante debe registrar exactamente:
  2 nombres + 2 apellidos.
- Ejemplo:
  Juan Pablo González Ulloa
- El sistema genera un identificador anonimizado:
  JPGU
- No se utiliza RUT ni otro identificador sensible.
"""

from __future__ import annotations

import secrets
import unicodedata
from typing import Optional

from boto3.dynamodb.conditions import Attr, Key

from core.constants import (
    DYNAMODB_TABLE_PARTICIPANTS,
    PK_TYPE_PARTICIPANT,
)
from core.exceptions import ParticipantNotFound
from services.dynamodb_service import DynamoDBService, dynamodb

try:
    from services.audit_service import AuditService, audit_service
except Exception:  # pragma: no cover - fallback para evitar circular import temprana
    AuditService = None  # type: ignore

    def audit_service():
        return None  # type: ignore


class ParticipantService:
    """Encapsula los documentos de participantes en DynamoDB."""

    def __init__(
        self,
        dynamodb_service: Optional[DynamoDBService] = None,
        audit_svc: Optional["AuditService"] = None,  # type: ignore
    ):
        self.dynamo: DynamoDBService = dynamodb_service or dynamodb
        self.table = DYNAMODB_TABLE_PARTICIPANTS
        self.audit = (
            audit_svc if audit_svc is not None else audit_service()
        )

    # ==========================================================
    # CREAR PARTICIPANTE
    # ==========================================================

    def create_participant(
        self,
        participant_id: str,
        nombre: str,
        *,
        observaciones: Optional[str] = None,
        actor_ctx: Optional[dict] = None,
    ) -> dict:
        """Crea un participante. Lanza excepción si ya existe."""

        if not participant_id or not isinstance(participant_id, str):
            raise ValueError("participant_id es obligatorio")

        participant_id = participant_id.strip().upper()

        if not participant_id:
            raise ValueError("participant_id es obligatorio")

        nombre = self._validate_nombre(nombre)

        now = DynamoDBService.now_iso()
        nombre_normalizado = self._normalise_name(nombre)

        np_1, np_2, np_3, np_4 = self._compute_name_prefixes(
            nombre_normalizado
        )

        data = {
            "participant_id": participant_id,
            "nombre": nombre,
            "nombre_normalizado": nombre_normalizado,
            "observaciones": observaciones,
            "pk_type": PK_TYPE_PARTICIPANT,
            "np_1": np_1,
            "np_2": np_2,
            "np_3": np_3,
            "np_4": np_4,
            "created_at": now,
            "updated_at": now,
        }

        try:
            created = self.dynamo.put_item(
                self.table,
                data,
                condition_expression=Attr(
                    "participant_id"
                ).not_exists(),
            )

            if not created:
                raise ValueError(
                    "Ya existe un participante con ese ID"
                )

        except Exception as exc:
            if (
                "ConditionalCheckFailed" in type(exc).__name__
                or (
                    hasattr(exc, "response")
                    and exc.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                )
            ):
                raise ValueError("Ya existe un participante con ese ID")

            raise

        # Registrar auditoría si existe contexto del actor.
        if self.audit and actor_ctx:
            try:
                self.audit.log_participant_created(
                    actor_uid=actor_ctx.get("actor_uid", ""),
                    actor_email=actor_ctx.get("actor_email", ""),
                    actor_rol=actor_ctx.get("actor_rol", ""),
                    participant_id=participant_id,
                )
            except Exception:
                # La auditoría no debe impedir la creación.
                pass

        return data

    # ==========================================================
    # CREAR PARTICIPANTE AUTOMÁTICAMENTE
    # ==========================================================

    def create_participant_auto(
        self,
        nombre: str,
        *,
        observaciones: Optional[str] = None,
        actor_ctx: Optional[dict] = None,
    ) -> dict:
        """
        Crea un participante generando automáticamente su ID.

        Siempre crea un nuevo participante. La selección de un participante
        existente debe hacerse explícitamente mediante su participant_id.
        """

        nombre = self._validate_nombre(nombre)

        # Generar identificador anonimizado.
        participant_id = self._generate_participant_id(nombre)

        attempts = 0

        while True:
            attempts += 1

            if attempts > 100:
                raise RuntimeError(
                    "No fue posible generar un ID único después de 100 intentos"
                )

            existing_by_id = self.get_participant(participant_id)

            if existing_by_id:
                participant_id = self._generate_participant_id(nombre)
                continue

            try:
                return self.create_participant(
                    participant_id,
                    nombre,
                    observaciones=observaciones,
                    actor_ctx=actor_ctx,
                )

            except ValueError as exc:
                if "Ya existe un participante con ese ID" not in str(exc):
                    raise

                participant_id = self._generate_participant_id(nombre)

    # ==========================================================
    # ACTUALIZAR OBSERVACIONES
    # ==========================================================

    def update_observations(
        self,
        participant_id: str,
        observaciones: Optional[str],
    ) -> None:
        """Actualiza exclusivamente las observaciones."""

        self._require_participant(participant_id)

        if observaciones is not None and not isinstance(
            observaciones, str
        ):
            raise ValueError(
                "observaciones debe ser texto o null"
            )

        self.dynamo.update_item(
            self.table,
            key={
                "participant_id": participant_id.upper()
            },
            attribute_updates={
                "observaciones": observaciones,
                "updated_at": DynamoDBService.now_iso(),
            },
        )

    # ==========================================================
    # OBTENER PARTICIPANTE
    # ==========================================================

    def get_participant(
        self,
        participant_id: str,
    ) -> Optional[dict]:
        """Obtiene un participante por su ID anonimizado."""

        if not participant_id:
            return None

        return self.dynamo.get_item(
            self.table,
            {
                "participant_id": participant_id.strip().upper()
            },
        )

    # ==========================================================
    # BUSCAR POR NOMBRE
    # ==========================================================

    def search_by_name(
        self,
        nombre: str,
    ) -> list[dict]:
        """
        Busca participantes por coincidencia parcial utilizando
        los GSIs np_1..np_4.

        No realiza un full scan como estrategia normal.

        Ejemplos:

        "j"
            → GSI np_1

        "ju"
            → GSI np_2

        "jua"
            → GSI np_3

        "juan"
            → GSI np_4

        "juan p"
            → GSI np_4 + filtro en memoria
        """

        query = (
            nombre.strip().casefold()
            if isinstance(nombre, str)
            else ""
        )

        if not query:
            return []

        nq = self._normalise_name(query)

        if not nq:
            return []

        index_name = None
        pk_attr = None
        pk_value = None

        qlen = len(nq)

        if qlen >= 4:
            pk_attr = "np_4"
            index_name = "gsi-pnp-4"
            pk_value = nq[:4]

        elif qlen == 3:
            pk_attr = "np_3"
            index_name = "gsi-pnp-3"
            pk_value = nq[:3]

        elif qlen == 2:
            pk_attr = "np_2"
            index_name = "gsi-pnp-2"
            pk_value = nq[:2]

        else:
            pk_attr = "np_1"
            index_name = "gsi-pnp-1"
            pk_value = nq[:1]

        try:
            candidates = self.dynamo.query_all(
                self.table,
                Key(pk_attr).eq(pk_value),
                index_name=index_name,
            )

        except Exception:
            raise

        results = []

        for participant in candidates:
            participant_name = participant.get(
                "nombre",
                "",
            )

            participant_normalized = participant.get(
                "nombre_normalizado"
            ) or self._normalise_name(participant_name)

            if nq in participant_normalized:
                results.append(participant)

        return sorted(
            results,
            key=lambda item: item.get(
                "nombre",
                "",
            ).casefold(),
        )

    # ==========================================================
    # BÚSQUEDA EXACTA
    # ==========================================================

    def find_exact_name_matches(
        self,
        nombre: str,
    ) -> list[dict]:
        """Devuelve todos los participantes con el nombre normalizado exacto."""

        nombre_normalizado = self._normalise_name(nombre)

        if not nombre_normalizado:
            return []

        return [
            participant
            for participant in self.search_by_name(nombre)
            if (
                participant.get("nombre_normalizado")
                or self._normalise_name(participant.get("nombre", ""))
            )
            == nombre_normalizado
        ]

    def find_exact_name(
        self,
        nombre: str,
    ) -> Optional[dict]:
        """
        Busca un participante por nombre exacto normalizado.

        Ignora:
        - mayúsculas/minúsculas
        - tildes
        - espacios duplicados
        """

        matches = self.find_exact_name_matches(nombre)
        return matches[0] if matches else None

    # ==========================================================
    # LISTAR
    # ==========================================================

    def list_participants(self) -> list[dict]:
        """Lista todos los participantes."""

        return self.dynamo.scan_all(self.table)

    # ==========================================================
    # VALIDAR PARTICIPANTE
    # ==========================================================

    def _require_participant(
        self,
        participant_id: str,
    ) -> dict:
        participant = self.get_participant(
            participant_id
        )

        if not participant:
            raise ParticipantNotFound(
                "El participante no existe"
            )

        return participant

    # ==========================================================
    # VALIDAR NOMBRE
    # ==========================================================

    @staticmethod
    def _validate_nombre(
        nombre: str,
    ) -> str:
        """
        Valida el nombre completo del participante.

        Formato obligatorio:

            2 nombres + 2 apellidos

        Ejemplo válido:

            Juan Pablo González Ulloa

        Ejemplos inválidos:

            Juan Pérez
            Juan Pablo Pérez
            Juan Pérez González
            Juan Pablo González
            Juan Pablo González Ulloa Soto
        """

        if not isinstance(nombre, str):
            raise ValueError(
                "El nombre debe ser texto"
            )

        # Eliminar espacios duplicados.
        nombre = " ".join(
            nombre.strip().split()
        )

        if not nombre:
            raise ValueError(
                "El nombre es obligatorio"
            )

        partes = nombre.split()

        # REGLA DEL PROYECTO:
        # exactamente 4 partes:
        # 2 nombres + 2 apellidos.
        if len(partes) != 4:
            raise ValueError(
                "Debe ingresar exactamente 2 nombres y 2 apellidos"
            )

        if len(nombre) > 150:
            raise ValueError(
                "El nombre no puede superar los 150 caracteres"
            )

        return nombre

    # ==========================================================
    # NORMALIZAR NOMBRE
    # ==========================================================

    @staticmethod
    def _normalise_name(
        nombre: str,
    ) -> str:
        """
        Normaliza nombres para comparaciones.

        Ignora:
        - mayúsculas
        - minúsculas
        - tildes
        - espacios duplicados
        """

        if not isinstance(nombre, str):
            return ""

        value = nombre.strip().casefold()

        value = unicodedata.normalize(
            "NFKD",
            value,
        )

        value = "".join(
            char
            for char in value
            if not unicodedata.combining(char)
        )

        value = " ".join(
            value.split()
        )

        return value

    # ==========================================================
    # CALCULAR PREFIXES np_1..np_4
    # ==========================================================

    @staticmethod
    def _compute_name_prefixes(
        normalised_name: str,
    ) -> tuple[str, str, str, str]:
        """
        Calcula np_1..np_4 a partir del nombre normalizado
        sin espacios.

        Ejemplo:

            Juan Pablo González Ulloa

        base:

            juanpablogonzalezulloa

        resultado:

            np_1 = juan...
            np_2 = ju...
            np_3 = jua
            np_4 = juan

        """

        base = normalised_name.replace(
            " ",
            "",
        )

        if len(base) == 0:
            return (
                "",
                "",
                "",
                "",
            )

        return (
            base[:1],
            base[:2] if len(base) >= 2 else base,
            base[:3] if len(base) >= 3 else base,
            base[:4] if len(base) >= 4 else base,
        )

    # ==========================================================
    # GENERAR ID ANONIMIZADO
    # ==========================================================

    @classmethod
    def _generate_participant_id(
        cls,
        nombre: str,
    ) -> str:
        """
        Genera un identificador anonimizado utilizando
        las iniciales de las 4 partes del nombre.

        Ejemplo:

            Juan Pablo González Ulloa
                     ↓
                    JPGU
        """

        parts = nombre.strip().split()

        initials = "".join(
            cls._first_letter(part)
            for part in parts
        )

        initials = initials.upper()[:4]

        if not initials:
            raise ValueError(
                "No fue posible generar el identificador"
            )

        suffix = f"{secrets.randbelow(999999) + 1:06d}"
        return f"{initials}{suffix}"

    # ==========================================================
    # PRIMERA LETRA
    # ==========================================================

    @staticmethod
    def _first_letter(
        value: str,
    ) -> str:
        """Obtiene la primera letra válida de una palabra."""

        if not value:
            return ""

        value = unicodedata.normalize(
            "NFKD",
            value,
        )

        for char in value:
            if char.isalpha():
                return char

        return ""

    def find_or_create(
        self,
        nombre: str,
        *,
        participant_id: Optional[str] = None,
        observaciones: Optional[str] = None,
        actor_ctx: Optional[dict] = None,
    ) -> dict:
        """
        Selecciona explícitamente un participante o crea uno nuevo.
        """

        nombre = self._validate_nombre(
            nombre
        )

        if participant_id:
            participant = self.get_participant(participant_id)
            if not participant:
                raise ValueError("Participante no encontrado")
            return participant

        return self.create_participant_auto(
            nombre,
            observaciones=observaciones,
            actor_ctx=actor_ctx,
        )
