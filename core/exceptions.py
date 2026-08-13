"""Excepciones de dominio de EmotionLab."""

class AuthenticationError(Exception):
    """La autenticacion del usuario no pudo completarse."""

class AuthorizationError(Exception):
    """El usuario autenticado no tiene permisos suficientes."""

class UserNotFound(Exception):
    """El usuario solicitado no existe."""

class InvalidCredentials(Exception):
    """El correo o la contrasena no son validos."""

class SessionNotFound(Exception):
    """La sesion solicitada no existe."""

class ParticipantNotFound(Exception):
    """El participante solicitado no existe."""

class SessionParseError(Exception):
    """El archivo de sesion de Unity no tiene una estructura valida."""
