"""Local authenticated UI owns life configuration and draft approval."""
from __future__ import annotations


class LifeRoutes:
    def __init__(self, service):
        self.service = service

    def get(self, response, path):
        if path != "/api/life":
            return False
        response.send_json(self.service.snapshot())
        return True

    def post(self, response, path, body):
        if path not in {"/api/life/config", "/api/life/run", "/api/life/stop", "/api/life/draft", "/api/life/thought"}:
            return False
        try:
            if path == "/api/life/config":
                result = self.service.configure(body.get("config", {}))
            elif path == "/api/life/run":
                result = self.service.request_run()
            elif path == "/api/life/stop":
                result = self.service.stop()
            elif path == "/api/life/thought":
                result = self.service.update_thought(body.get("id"), body.get("revision"), body.get("action"))
            else:
                draft_id = body.get("id")
                if not isinstance(draft_id, str):
                    raise ValueError("缺少草稿 ID")
                if body.get("action") == "dismiss":
                    result = {"ok": True, "draft": self.service.store.dismiss(draft_id)}
                elif body.get("action") == "publish":
                    result = self.service.publish(draft_id, content=body.get("content"))
                else:
                    raise ValueError("未知草稿操作")
            response.send_json(result)
        except ValueError as error:
            response.send_json({"ok": False, "error": str(error)}, 400)
        return True
