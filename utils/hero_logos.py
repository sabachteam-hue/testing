import json
from pathlib import Path
from utils.storage import get_storage_root

HERO_LOGOS_FILE = Path('static/uploads/hero_logos.json')


def _target_file() -> Path:
    storage_file = get_storage_root() / 'uploads' / 'hero_logos.json'
    if storage_file.exists():
        return storage_file
    return HERO_LOGOS_FILE


def get_hero_logos() -> dict:
    f = _target_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            return {}
    if HERO_LOGOS_FILE.exists():
        try:
            return json.loads(HERO_LOGOS_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_hero_logos(data: dict) -> None:
    for target in (HERO_LOGOS_FILE, get_storage_root() / 'uploads' / 'hero_logos.json'):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except Exception:
            pass

