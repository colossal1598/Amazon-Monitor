"""One-time migration from legacy config.yaml to SQLite settings/asins tables."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from settings_store import migrate_yaml_to_db  # noqa: E402


def main() -> int:
    yaml_path = PROJECT_ROOT / "config.yaml"
    loaded = {}
    if yaml_path.is_file():
        parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            loaded = parsed
    raw_db_path = str(loaded.get("db_path", "data/monitor.db"))
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    migrated = migrate_yaml_to_db(str(yaml_path), str(db_path))
    if migrated:
        print(f"Migrated settings from {yaml_path} into {db_path}.")
    else:
        print(f"Settings table is not empty in {db_path}; nothing imported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
