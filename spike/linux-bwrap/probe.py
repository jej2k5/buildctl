import json, sys, tempfile
from pathlib import Path
from buildctl.supervisor.isolation import verify_backend, production_backend
backend = production_backend()
root = Path(tempfile.mkdtemp(prefix="probe-"))
repo = root / "repo"; repo.mkdir()
result = verify_backend(backend, root / "control", repo)
print(json.dumps({"backend": result["backend"], "roles": list(result["roles"])}, indent=1))
print("PASS")
