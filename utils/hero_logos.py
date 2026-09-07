import json
from pathlib import Path

HERO_LOGOS_FILE = Path('static/uploads/hero_logos.json')


def get_hero_logos() -> dict:
    if HERO_LOGOS_FILE.exists():
        try:
            return json.loads(HERO_LOGOS_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_hero_logos(data: dict) -> None:
    HERO_LOGOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HERO_LOGOS_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')
