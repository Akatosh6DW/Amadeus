"""运行状态与端口角色的公开诊断投影，不公开模型密钥或 URL 凭据。

诊断按调用读取当前设置和服务状态；这里不启动子进程、不修改任何配置。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import os
import time
import urllib.parse
from backend.common.values import clamp_int

@dataclass(frozen=True)
class RuntimeStatusPorts:
    host: Callable[[], str]
    port: Callable[[], int]
    vision_endpoint: Callable[[], str]
    settings: Callable[[], dict[str, Any]]
    clients: Callable[[], list[dict[str, Any]]]
    configured: Callable[..., bool]
    endpoint: Callable[..., str]
    transport: Callable[..., dict[str, Any]]
    tts_status: Callable[..., dict[str, Any]]
    vision_status: Callable[..., dict[str, Any]]
    build: Callable[[], str]
    mode: Callable[[], str]
    started_at: Callable[[], float]
    server_log: Callable[[], Path]
    network_log: Callable[[], Path]
    tts_log: Callable[[], Path]

class RuntimeStatus:
    def __init__(self, ports: RuntimeStatusPorts) -> None:
        self.ports = ports

    def server_address_payload(self, server_address: Any = None) -> dict[str, Any]:
        host = self.ports.host()
        port = self.ports.port()
        if isinstance(server_address, (tuple, list)) and len(server_address) >= 2:
            host = str(server_address[0] or host)
            port = clamp_int(server_address[1], 1, 65535, port)
        display_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
        return {
            "host": display_host,
            "port": port,
            "endpoint": f"http://{display_host}:{port}",
        }


    def runtime_port_roles(self, settings: dict[str, Any], server_address: Any = None) -> list[dict[str, Any]]:
        main = self.server_address_payload(server_address)
        tts_port = clamp_int(settings.get("gptSovitsPort"), 1024, 65535, 9881)
        vision_port = clamp_int(settings.get("visionPort"), 1024, 65535, 9892)
        return [
            {
                "id": "main",
                "label": "Amadeus System 桌面后端" if main["port"] == 5174 else "Amadeus System 本地后端",
                "host": main["host"],
                "port": main["port"],
                "endpoint": main["endpoint"],
                "enabled": True,
            },
            {
                "id": "tts",
                "label": "GPT-SoVITS 语音",
                "host": "127.0.0.1",
                "port": tts_port,
                "endpoint": f"http://127.0.0.1:{tts_port}/tts",
                "enabled": settings.get("ttsProvider") == "gpt-sovits" and bool(settings.get("voiceEnabled", True)),
            },
            {
                "id": "vision",
                "label": "DeepSeek 云端视觉",
                "host": "api.deepseek.com",
                "port": 443,
                "endpoint": self.ports.vision_endpoint(),
                "remote": True,
                "enabled": bool(settings.get("visionEnabled", True)),
            },
        ]


    def runtime_port_conflicts(self, settings: dict[str, Any], server_address: Any = None) -> list[dict[str, Any]]:
        by_port: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for role in self.runtime_port_roles(settings, server_address):
            if role.get("enabled") and not role.get("remote"):
                by_port.setdefault((str(role["host"]), int(role["port"])), []).append(role)
        return [
            {
                "host": host,
                "port": port,
                "services": [str(item["id"]) for item in roles],
                "message": f"端口 {port} 同时分配给：" + "、".join(str(item["label"]) for item in roles),
            }
            for (host, port), roles in by_port.items()
            if len(roles) > 1
        ]


    def redacted_upstream_status(self, settings: dict[str, Any]) -> dict[str, Any]:
        endpoint = self.ports.endpoint(settings.get("apiEndpoint") or "") if settings.get("apiEndpoint") else ""
        parsed = urllib.parse.urlparse(endpoint)
        try:
            parsed_port = parsed.port
        except ValueError:
            parsed_port = None
        return {
            "configured": self.ports.configured(settings),
            "model": str(settings.get("apiModel") or ""),
            "scheme": parsed.scheme,
            "host": parsed.hostname or "",
            "port": parsed_port or (443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None),
            "path": parsed.path or "",
            "transport": self.ports.transport(endpoint, settings),
        }


    def runtime_status(self, server_address: Any = None) -> dict[str, Any]:
        settings = self.ports.settings()
        clients = self.ports.clients()
        return {
            "ok": True,
            "service": "amadeus-runtime",
            "build": self.ports.build(),
            "runtimeMode": self.ports.mode(),
            "pid": os.getpid(),
            "uptimeSeconds": round(max(0.0, time.time() - self.ports.started_at()), 1),
            "main": self.server_address_payload(server_address),
            "ports": self.runtime_port_roles(settings, server_address),
            "portConflicts": self.runtime_port_conflicts(settings, server_address),
            "clients": clients,
            "activeClients": len(clients),
            "upstream": self.redacted_upstream_status(settings),
            "tts": self.ports.tts_status(settings),
            "vision": self.ports.vision_status(settings),
            "logs": {
                "server": str(self.ports.server_log()),
                "network": str(self.ports.network_log()),
                "tts": str(self.ports.tts_log()),
                "vision": str(self.ports.server_log()),
            },
        }
