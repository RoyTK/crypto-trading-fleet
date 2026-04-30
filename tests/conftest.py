"""Pytest config — registers the project root on sys.path so framework/ and
monitoring/ resolve without needing an editable install."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
