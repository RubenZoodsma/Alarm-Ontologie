"""
paths.py — where the POC's inputs and rule files live.
"""

from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
POC_DIR = ENGINE_DIR.parent
ROOT = POC_DIR.parent
DATA_DIR = POC_DIR / "data"
REPRESENTATION_DIR = POC_DIR / "representation"
RULES_DIR = REPRESENTATION_DIR / "rules"
ACTIONS_DIR = REPRESENTATION_DIR / "actions"
