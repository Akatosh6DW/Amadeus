"""Small, process-local records of persona context passed to the model."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from threading import RLock


class PersonaSkillMonitor:
    def __init__(self, limit=24):
        self.limit = limit
        self._scopes = {}
        self._lock = RLock()

    def record(self, *, scope, request_id, conversation_id, user_message, purpose,
               diagnostic, prompt_sent, kernel_sent):
        if not request_id:
            return
        mode = diagnostic.get("mode", "off")
        retrieved = [item["id"] if "id" in item else item["skillId"]
                     for item in diagnostic.get("selectedSkills", []) if item.get("id") or item.get("skillId")]
        record = {
            "requestId": request_id, "conversationId": conversation_id,
            "userMessage": str(user_message)[:180], "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "purpose": purpose, "snapshotId": diagnostic.get("snapshotId", ""), "mode": mode,
            "activeSkillIds": retrieved if mode == "on" and prompt_sent else [],
            "retrievedSkillIds": retrieved, "kernelActive": mode == "on" and kernel_sent,
            "legacyModuleIds": list(diagnostic.get("legacyModuleIds", [])) if mode != "on" and prompt_sent else [],
            "fallbackReason": diagnostic.get("fallbackReason", ""),
            "contextSource": diagnostic.get("retrievalContextSource", ""),
            "promptSent": bool(prompt_sent),
        }
        with self._lock:
            records = self._scopes.setdefault(scope, OrderedDict())
            # One record per reply; tool continuations update that reply's actual input.
            records[request_id] = record
            records.move_to_end(request_id)
            while len(records) > self.limit:
                records.popitem(last=False)

    def snapshot(self, scope):
        with self._lock:
            return deepcopy(list(reversed(list(self._scopes.get(scope, {}).values()))))


def persona_skill_catalog(root: Path):
    """Read labels only; opening a diagnostic panel never initializes retrieval."""
    skills = []
    for path in sorted((root / "skills" / "persona").glob("*/manifest.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            skills.append({"id": str(item["id"]), "name": str(item.get("name") or item["id"]),
                           "description": str(item.get("description") or "")})
        except (OSError, ValueError, KeyError, TypeError):
            skills.append({"id": path.parent.name, "name": path.parent.name, "description": "无法读取此 skill 的说明。"})
    return skills
