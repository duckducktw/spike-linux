"""Shared helpers for the host tests."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def user_data_folder() -> str:
    """Same rule as spike_host.py: appconfig.json userDataFolder (default SPIKE3)."""
    root = Path(os.environ.get("SPIKE_APP_ROOT") or ROOT / "app")
    try:
        return json.loads((root / "platform/hosts/appconfig.json").read_text(encoding="utf-8")).get("userDataFolder") or "SPIKE3"
    except (OSError, ValueError):
        return "SPIKE3"


UDF = user_data_folder()
