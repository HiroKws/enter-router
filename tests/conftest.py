import sys
from pathlib import Path

# Make `src/` importable for pytest without installation (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
