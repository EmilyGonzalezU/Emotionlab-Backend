"""Configuracion compartida relacionada con seguridad.

Las claves y secretos deben suministrarse por variables de entorno, nunca por
codigo fuente ni por archivos versionados.
"""

FERNET_KEY_ENV = "FERNET_KEY"
