from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.aws_service import AWSService
from services.parsers.session_parser import SessionParser

data = AWSService(timeout=120).fetch_sessions()["sesiones"]

errors = Counter()
ok = 0
recovered = 0

for session in data:
    try:
        SessionParser(session).get_prediction_features()
        ok += 1
        session_data = session.get("session_data", {})
        if isinstance(session_data, dict) and (
            not session_data.get("fechaInicio")
            or not session_data.get("fechaFin")
        ):
            recovered += 1
    except Exception as error:
        errors[str(error)] += 1

print("TOTAL:", len(data))
print("OK:", ok)
print("RECUPERADAS POR FALLBACK:", recovered)
print("ERRORES REALES:", sum(errors.values()))

print("\nMOTIVOS:")
for error, cantidad in errors.most_common():
    print(f"{cantidad} -> {error}")