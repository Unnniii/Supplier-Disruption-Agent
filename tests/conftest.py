import os
import runpy
from pathlib import Path

# Tests never download embedding models or call an API.
os.environ.setdefault("SDA_RETRIEVER", "keyword")

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "data" / "erp.db").exists():
    runpy.run_path(str(ROOT / "scripts" / "seed_erp.py"), run_name="__main__")
