import json
import os
from typing import List, Optional
from datetime import datetime
from models import Instance

DATA_FILE = os.path.join(os.environ.get("DATA_DIR", "/data"), "instances.json")


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"instances": [], "settings": {}}
    data = json.load(open(DATA_FILE))
    if "settings" not in data:
        data["settings"] = {}
    return data


def _save(data: dict):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_all() -> List[Instance]:
    data = _load()
    return [Instance(**i) for i in data["instances"]]


def get(instance_id: str) -> Optional[Instance]:
    for i in get_all():
        if i.id == instance_id:
            return i
    return None


def save(instance: Instance):
    data = _load()
    instances = data["instances"]
    for idx, i in enumerate(instances):
        if i["id"] == instance.id:
            instance.updated_at = datetime.now().isoformat()
            instances[idx] = instance.model_dump()
            _save(data)
            return
    instances.append(instance.model_dump())
    _save(data)


def get_settings() -> dict:
    return _load().get("settings", {})


def save_settings(settings: dict):
    data = _load()
    data["settings"] = settings
    _save(data)


def delete(instance_id: str) -> bool:
    data = _load()
    before = len(data["instances"])
    data["instances"] = [i for i in data["instances"] if i["id"] != instance_id]
    if len(data["instances"]) < before:
        _save(data)
        return True
    return False
