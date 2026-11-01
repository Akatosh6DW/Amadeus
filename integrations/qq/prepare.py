"""Prepare a separate AstrBot runtime without changing Amadeus settings.

Invoke with the dedicated qq-agent Python after installing upstream dependencies.
Secrets and downloaded upstream packages stay in ignored local directories.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

from .policy import DEFAULT_POLICY
from .amadeus_runtime import MODEL_KEYS

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / ".runtime-assets" / "qq-agent"
RUNTIME = ROOT / "data" / "qq-agent"


def prepare():
    matches = list((ASSETS / "astrbot-source").glob("*/main.py"))
    if len(matches) != 1:
        raise RuntimeError("请先下载 AstrBot v4.29.3 官方源码到 .runtime-assets/qq-agent/astrbot-source")
    source = matches[0].parent
    RUNTIME.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(RUNTIME / "astrbot")
    sys.path.insert(0, str(source))
    config_path = RUNTIME / "gateway.json"
    if not config_path.exists():
        config = {
            "control_port": 6190, "control_token": secrets.token_urlsafe(32),
            "napcat_ws": "ws://127.0.0.1:3001/", "napcat_token": secrets.token_urlsafe(32),
            "astrbot_ws": "ws://127.0.0.1:6199/ws", "astrbot_token": secrets.token_urlsafe(32),
            "policy": dict(DEFAULT_POLICY),
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not config.get("amadeus_python"):
        desktop_python = ROOT / ".conda" / "amadeus-desktop" / ("python.exe" if os.name == "nt" else "bin/python")
        config["amadeus_python"] = str(desktop_python) if desktop_python.exists() else sys.executable
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    model_path = RUNTIME / "amadeus-model.json"
    if not model_path.exists():
        owner_path = ROOT / "data" / "settings.json"
        owner = json.loads(owner_path.read_text(encoding="utf-8")) if owner_path.exists() else {}
        defaults = json.loads((ROOT / "configs" / "public_defaults.json").read_text(encoding="utf-8"))
        selected = {k: v for k, v in {**defaults, **owner}.items() if k in MODEL_KEYS}
        selected.setdefault("apiKey", "")
        model_path.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    astrbot_config = RUNTIME / "astrbot" / "data" / "cmd_config.json"
    if not astrbot_config.exists():
        from copy import deepcopy
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.utils.auth_password import hash_dashboard_password, hash_md5_dashboard_password
        password = secrets.token_urlsafe(20)
        settings = deepcopy(DEFAULT_CONFIG)
        settings["dashboard"].update({"host": "127.0.0.1", "port": 6185,
            "pbkdf2_password": hash_dashboard_password(password), "password": hash_md5_dashboard_password(password),
            "password_storage_upgraded": True, "password_change_required": True,
            "jwt_secret": secrets.token_urlsafe(32)})
        # Gateway filters incoming/outgoing, using exact account + conversation IDs.
        settings["platform"] = [{"id": "amadeus_qq", "type": "aiocqhttp", "enable": True,
            "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 6199, "ws_reverse_token": config["astrbot_token"]}]
        settings["platform_settings"].update({"unique_session": True,
            "enable_id_white_list": True, "id_whitelist": [],
            "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False,
            "ignore_bot_self_message": True, "ignore_at_all": True,
            "no_permission_reply": False})
        settings["admins_id"] = []
        settings["provider_settings"]["computer_use_runtime"] = "none"
        settings["provider_settings"]["proactive_capability"]["add_cron_tools"] = False
        settings["provider_ltm_settings"]["active_reply"]["enable"] = False
        astrbot_config.parent.mkdir(parents=True, exist_ok=True)
        astrbot_config.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        (RUNTIME / "astrbot-login.txt").write_text("AstrBot 管理面板：http://127.0.0.1:6185\n用户名：astrbot\n初始密码："+password+"\n首次登录后请改密码。\n", encoding="utf-8")
    else:
        settings = json.loads(astrbot_config.read_text(encoding="utf-8-sig"))
        platform = next((p for p in settings.get("platform", []) if p.get("id") == "amadeus_qq"), {})
        expected = {"type":"aiocqhttp", "ws_reverse_host":"127.0.0.1", "ws_reverse_port":6199, "ws_reverse_token":config["astrbot_token"]}
        if settings.get("dashboard", {}).get("host") != "127.0.0.1" or any(platform.get(k) != v for k,v in expected.items()):
            raise RuntimeError("已有 AstrBot 配置与本机网关不一致，请核对 Dashboard 监听地址及 amadeus_qq 适配器；未覆盖已有配置。")
    # Seed the standalone agent once from the already-selected QQ model. Keep
    # user-created providers and future edits entirely under AstrBot's control.
    model = json.loads(model_path.read_text(encoding="utf-8"))
    if not settings.get("provider") and not settings.get("provider_sources") and model.get("apiKey"):
        from .astrbot_model import apply_model
        try:
            apply_model(settings, model)
        except ValueError:
            print("此模型端点需在 AstrBot 面板手动配置。")
        else:
            astrbot_config.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME / "panel-link.txt").write_text("http://127.0.0.1:"+str(config["control_port"])+"/#token="+config["control_token"]+"\n", encoding="utf-8")
    # Template is local because it contains the actual OneBot credential.
    napcat = {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [],
        "websocketClients": [], "websocketServers": [{"name": "AmadeusGateway", "enable": True,
            "host": "127.0.0.1", "port": 3001, "messagePostFormat": "array", "reportSelfMessage": False,
            "token": config["napcat_token"], "enableForcePushEvent": True, "debug": False,
            "heartInterval": 30000}]}, "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}
    (RUNTIME / "onebot11.template.json").write_text(json.dumps(napcat, ensure_ascii=False, indent=2), encoding="utf-8")
    napcat_default = RUNTIME / "napcat" / "config" / "onebot11.json"
    napcat_default.parent.mkdir(parents=True, exist_ok=True)
    if not napcat_default.exists():
        napcat_default.write_text(json.dumps(napcat, ensure_ascii=False, indent=2), encoding="utf-8")
    (RUNTIME / "paths.json").write_text(json.dumps({"source": str(source), "dashboard": str(ASSETS/"astrbot-dashboard"/"dist"),
        "python": sys.executable}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("配置已准备："+str(RUNTIME))
    print("初始模式：暂停。登录资料与面板链接已保存在本机文件中，不写入 Git。")


if __name__ == "__main__":
    prepare()
