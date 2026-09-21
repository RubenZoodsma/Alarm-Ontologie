"""
paths.py — where the POC's inputs live. One place, so every module agrees.
"""

from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
POC_DIR = ENGINE_DIR.parent
ROOT = POC_DIR.parent
DATA_DIR = POC_DIR / "data"
REPRESENTATION_DIR = POC_DIR / "representation"
RULES_DIR = REPRESENTATION_DIR / "rules"
LICENSE = ROOT / "RDFox.lic"
RDFOX_BIN = Path.home() / "Downloads" / "RDFox-macOS-arm64-7.6b" / "RDFox"
ACTIONS_DIR = REPRESENTATION_DIR / "actions"
