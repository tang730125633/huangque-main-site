"""Portable runtime paths for the flat Hermes Flask application."""

import os
from pathlib import Path


ROOT_DIR = Path(os.environ.get("HERMES_HOME", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", ROOT_DIR / "data"))
