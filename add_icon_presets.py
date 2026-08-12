"""
YEH SCRIPT JAB BHI CHAHO CHALA SAKTE HO (existing/live database par) — safe
hai, dobara chalane se duplicate nahi banega, sirf ID update ho jayegi agar
naam already maujood ho.

Neeche PRESETS list mein jo bhi custom emoji preset add karni ho wo daal do
(name + Telegram custom_emoji_id + fallback emoji), phir yeh script chala do
- woh sab Icon Presets admin page (/admin/icon-presets) ke dropdown mein
apne aap dikhne lag jayenge, category/service/payment-method edit karte waqt
seedha select kiye ja sakte hain.

CHALANE KA TAREEQA:
    Railway / server / apne local machine par, project folder ke andar:
        python add_icon_presets.py

    (Wahi environment/DATABASE_URL use hoga jo bot khud use karta hai, kyunke
    yeh database/models.py wala hi engine/session import karti hai.)
"""

from database.models import IconPreset, SessionLocal

# Yahan naye presets add karte raho - name unique hona chahiye.
PRESETS = [
    {"name": "Cat", "emoji_id": "5796185041717433060", "fallback_emoji": "😺", "sort_order": 1},
]


def run() -> None:
    db = SessionLocal()
    try:
        for preset_data in PRESETS:
            existing = db.query(IconPreset).filter(IconPreset.name == preset_data["name"]).first()
            if existing:
                existing.emoji_id = preset_data["emoji_id"]
                existing.fallback_emoji = preset_data["fallback_emoji"]
                existing.sort_order = preset_data["sort_order"]
                print(f"Updated preset: {preset_data['name']}")
            else:
                db.add(
                    IconPreset(
                        name=preset_data["name"],
                        emoji_id=preset_data["emoji_id"],
                        fallback_emoji=preset_data["fallback_emoji"],
                        sort_order=preset_data["sort_order"],
                    )
                )
                print(f"Added new preset: {preset_data['name']}")
        db.commit()
    finally:
        db.close()

    print("Done! Check /admin/icon-presets to see it in the list.")


if __name__ == "__main__":
    run()
