"""Roles autorizados dentro de EmotionLab."""

ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"

VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_TEACHER})


def normalize_role(value: object) -> str | None:
	"""Return the canonical application role or None for unknown values."""
	if not isinstance(value, str):
		return None
	role = value.strip().lower()
	aliases = {
		"profesor": ROLE_TEACHER,
		"especialista": ROLE_TEACHER,
		"administrador": ROLE_ADMIN,
	}
	role = aliases.get(role, role)
	return role if role in VALID_ROLES else None
