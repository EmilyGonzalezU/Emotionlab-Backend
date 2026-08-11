"""Configuración centralizada para los tokens JWT."""

import os
import sys
from pathlib import Path
from datetime import timedelta

from dotenv import load_dotenv


def _cargar_env():
    """
    Busca el archivo .env tanto en desarrollo como
    en la aplicación empaquetada con PyInstaller.

    Desarrollo:
        proyecto/.env

    Aplicación empaquetada:
        resources/config/.env
    """

    posibles_rutas = []

    # ---------------------------------------------------------
    # APLICACIÓN EMPAQUETADA CON PYINSTALLER
    # ---------------------------------------------------------

    if getattr(sys, "frozen", False):
        # EmotionLabBackend.exe:
        #
        # resources/
        # ├── backend/
        # │   └── EmotionLabBackend.exe
        # └── config/
        #     └── .env
        #
        resources_dir = Path(sys.executable).resolve().parent.parent

        posibles_rutas.append(
            resources_dir / "config" / ".env"
        )

    # ---------------------------------------------------------
    # DESARROLLO
    # ---------------------------------------------------------

    base_dir = Path(__file__).resolve().parent.parent

    posibles_rutas.append(
        base_dir / ".env"
    )

    posibles_rutas.append(
        base_dir.parent / ".env"
    )

    # ---------------------------------------------------------
    # BUSCAR
    # ---------------------------------------------------------

    for ruta in posibles_rutas:

        if ruta.exists():

            load_dotenv(
                dotenv_path=ruta,
                override=False,
            )

            return ruta

    # Fallback final:
    # permite utilizar variables de entorno del sistema.
    load_dotenv()

    return None


_ENV_PATH = _cargar_env()


JWT_SECRET = os.getenv("JWT_SECRET")


if not JWT_SECRET:
    raise RuntimeError(
        "No se encontró JWT_SECRET. "
        "Verifique que exista config/.env "
        "o que JWT_SECRET esté definida como variable de entorno."
    )


JWT_ALGORITHM = "HS256"

JWT_EXPIRATION = timedelta(hours=8)