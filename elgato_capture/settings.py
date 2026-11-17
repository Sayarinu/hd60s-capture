import json
import os
from pathlib import Path

CONFIG_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "elgato-capture"
    / "settings.json"
)

DEFAULTS = {
    "volume": 1.0,
    "muted": False,
    "show_info": False,
    "width": 1280,
    "height": 760,
    "controls": {},
}


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        self["controls"] = {}
        try:
            stored = json.loads(CONFIG_FILE.read_text())
            if isinstance(stored, dict):
                self.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def save(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, indent=2))
        tmp.replace(CONFIG_FILE)
