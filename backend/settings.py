"""
Persistent app-wide settings — small key/value store for things like the
default destination folder to use automatically for YouTube Music links.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("/app/monitors_data/settings.json")


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Could not load settings file: %s", exc)

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._data, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Could not save settings file: %s", exc)

    def all(self) -> Dict[str, Any]:
        return dict(self._data)

    def get(self, key: str) -> Optional[Any]:
        return self._data.get(key)

    def set(self, key: str, value: Any) -> Any:
        self._data[key] = value
        self._save()
        return value

    def clear(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._save()
