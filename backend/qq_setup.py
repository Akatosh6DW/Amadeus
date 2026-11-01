"""Owner-facing setup for the installed, local QQ gateway and NapCat runtime.

Only explicit mutations start services or change configuration. The browser sees
an allowlisted projection; transport credentials never cross this boundary.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from agent.tieba_corpus import forum_corpus_status
from integrations.qq.amadeus_runtime import MODEL_KEYS
from integrations.qq.policy import DEFAULT_POLICY, Ledger, normalize_policy
from integrations.qq.account_profiles import account_summaries, remember_account, select_account
from integrations.qq import component


class _SetupError(ValueError):
    """Contains only a deliberately authored, safe UI message."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("local service redirects are unsupported")


_PHASES = frozenset({"ready", "initializing", "offline", "generating_qrcode",
                     "waiting_qrcode", "qrcode_scanned", "reconnecting"})
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class QqSetupService:
    def __init__(self, root: Path, data_root: Path,
                 get_settings: Callable[[], Mapping[str, Any]],
                 write_log: Callable[..., None] | None = None,
                 prepare_voice_settings: Callable[[dict], dict] | None = None):
        self.root = Path(root).resolve()
        self.runtime = Path(data_root).resolve() / "qq-agent"
        self.get_settings = get_settings
        self.prepare_voice_settings = prepare_voice_settings or (lambda model: model)
        self._lock = threading.Lock()
        self._credential_lock = threading.Lock()
        self._credential: tuple[str, float, str] | None = None
        self._launches: dict[str, tuple[Any, float]] = {}
        self._reported_launch_failures: dict[str, tuple[float, int]] = {}
        self._presence_lock = threading.Lock()
        self._presence_pending: dict[str, bool] = {}
        self._presence_worker: threading.Thread | None = None
        self._write_log = write_log or (lambda *args, **kwargs: None)

    def desktop_presence(self, client_id: Any, active: bool) -> None:
        """Queue the latest desired lease; optional QQ I/O never holds desktop HTTP."""
        if not isinstance(client_id, str) or not client_id.startswith('desktop-'):
            return
        with self._presence_lock:
            self._presence_pending[client_id] = active
            if self._presence_worker is None:
                self._presence_worker = threading.Thread(target=self._drain_presence,
                    name='amadeus-qq-presence', daemon=True)
                self._presence_worker.start()

    def _drain_presence(self) -> None:
        while True:
            with self._presence_lock:
                if not self._presence_pending:
                    self._presence_worker = None
                    return
                client_id = next(iter(self._presence_pending))
                active = self._presence_pending.pop(client_id)
            try:
                self._sync_desktop_presence(client_id, active)
            except Exception as error:
                # Plugin failure must neither reject a desktop lease nor strand the queue.
                self._write_log('qq_desktop_presence_failed', active=active, errorType=type(error).__name__)

    def _sync_desktop_presence(self, client_id: str, active: bool) -> None:
        try:
            config = self._gateway_config()
            self._gateway(config, '/api/presence', {'clientId': client_id, 'active': active})
        except _SetupError:
            # A desktop lease only supervises an already-running QQ gateway.
            # Saved account/configuration is not a request to launch QQ.
            pass

    @staticmethod
    def _read_json(path: Path) -> dict:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value

    @staticmethod
    def _port(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("invalid port")
        port = int(value)
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        return port

    def _gateway_config(self) -> dict:
        try:
            config = self._read_json(self.runtime / "gateway.json")
            self._port(config["control_port"])
            if not isinstance(config.get("control_token"), str) or not config["control_token"]:
                raise ValueError("missing token")
            return config
        except (OSError, ValueError, KeyError, TypeError):
            raise _SetupError("QQ 网关尚未准备好，请先安装并准备本机 QQ 组件。") from None

    @staticmethod
    def _request(port: int, path: str, *, token: str = "", body: dict | None = None,
                 timeout: float = 1.5) -> dict:
        # Fixed loopback destinations and disabled proxies keep local credentials local.
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid local service response")
        return payload

    def _gateway(self, config: dict, path: str, body: dict | None = None) -> dict:
        try:
            result = self._request(self._port(config["control_port"]), path,
                                   token=config["control_token"], body=body,
                                   timeout=240 if path in {"/api/approve", "/api/conversation/clear"}
                                   else 60 if path == "/api/media" else 5 if path == "/api/contacts" else 1.5)
            if (path != "/api/status" and result.get("error")) or result.get("ok") is False:
                raise ValueError("gateway rejected request")
            if path == "/api/status" and not isinstance(result.get("policy"), dict):
                raise ValueError("invalid gateway status")
            return result
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise _SetupError("操作被 QQ 网关拒绝，请检查当前模式、授权范围或草稿状态；没有确认消息发送。") from None
            raise _SetupError("QQ 网关暂时无法响应，请稍候重试。") from None
        except Exception:
            if path == "/api/approve":
                raise _SetupError("发送结果尚未确认，请在 QQ 和动态记录中核对；请勿重复发送。") from None
            raise _SetupError("QQ 网关暂时无法响应，请启动组件后重试。") from None

    def _napcat_session(self) -> tuple[int, str]:
        try:
            config = self._read_json(self.runtime / "napcat" / "config" / "webui.json")
            if config.get("disableWebUI") or not config.get("token"):
                raise ValueError("webui unavailable")
            port = self._port(config.get("port", 6099))
            password = hashlib.sha256((str(config["token"]) + ".napcat").encode()).hexdigest()
            with self._credential_lock:
                fingerprint = f"{port}:{password}"
                if self._credential and self._credential[0] == fingerprint and self._credential[1] > time.monotonic():
                    return port, self._credential[2]
                response = self._request(port, "/api/auth/login", body={"hash": password})
                credential = response.get("data", {}).get("Credential")
                if response.get("code", 0) != 0 or not isinstance(credential, str) or not credential:
                    raise ValueError("authentication unavailable")
                self._credential = fingerprint, time.monotonic() + 1800, credential
                return port, credential
        except Exception:
            raise _SetupError("QQ 登录组件暂时无法连接，请启动组件后稍候重试。") from None

    def _napcat(self, session: tuple[int, str], path: str) -> dict:
        try:
            result = self._request(session[0], path, token=session[1], body={})
            if result.get("code", 0) != 0 or not isinstance(result.get("data"), dict):
                raise ValueError("NapCat rejected request")
            return result["data"]
        except Exception:
            self._credential = None
            raise _SetupError("QQ 登录状态暂时无法读取，请稍候重试。") from None

    @staticmethod
    def _empty_login() -> dict:
        return {"isLogin": False, "isOffline": False, "loginPhase": "unavailable",
                "coreReady": False, "accountId": "", "nickname": "", "qrImage": "",
                "loginError": "", "errorCode": ""}

    def _qr_failure_code(self) -> str:
        """Supplement older NapCat versions which log QR failures but leave the API at scanned.

        Only the managed launch's current QR attempt is relevant. Never return
        raw log lines: these can contain QR credentials and message contents.
        """
        try:
            path = self.runtime / "napcat.stdout.log"
            floor = max((self.runtime / "napcat-processes.json").stat().st_mtime_ns,
                        (self.runtime / "napcat/cache/qrcode.png").stat().st_mtime_ns)
            if path.stat().st_mtime_ns < floor:
                return ""
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                tail = stream.read(65536).decode("utf-8", errors="replace")
            code = ""
            for line in tail.splitlines():
                if any(marker in line for marker in ("请扫描下面的二维码", "二维码已保存到", "二维码已被扫描")):
                    code = ""
                # regex-purpose: protocol
                match = re.search(r"\[Core\]\s*\[Login\]\s*Login Error,ErrType:\s*(\d{1,6})\s+ErrCode:\s*(\d{1,6})\s*$", line)
                if match:
                    code = f"{match[1]}/{match[2]}"
            return code
        except OSError:
            return ""

    @staticmethod
    def _login_message(login: dict) -> str:
        if login.get("loginError"):
            return login["loginError"]
        return {
            "waiting_qrcode": "请用手机 QQ 扫描二维码，并在手机上确认登录。",
            "qrcode_scanned": "二维码已扫描，请在手机 QQ 上确认登录。",
            "generating_qrcode": "正在获取登录二维码，请稍候。",
            "initializing": "QQ 正在初始化登录，请稍候。",
            "reconnecting": "QQ 正在重新连接，请稍候。",
            "offline": "QQ 已掉线，请重新登录。",
        }.get(login.get("loginPhase"), "")

    def _login(self) -> tuple[dict, str]:
        login = self._empty_login()
        try:
            session = self._napcat_session()
            value = self._napcat(session, "/api/QQLogin/CheckLoginStatus")
            login.update({key: value.get(key) is True for key in ("isLogin", "isOffline", "coreReady")})
            phase = value.get("loginPhase")
            login["loginPhase"] = phase if phase in _PHASES else "initializing"
            if login["isLogin"] or login["isOffline"]:
                info = self._napcat(session, "/api/QQLogin/GetQQLoginInfo")
                account = str(info.get("uin", "")).strip()
                if account.isascii() and account.isdigit():
                    login["accountId"] = account
                login["nickname"] = str(info.get("nick", ""))[:100]
                if info.get("online") is False:
                    login.update(isLogin=False, isOffline=True, coreReady=False)
            elif login["loginPhase"] == "waiting_qrcode":
                try:
                    image_path = self.runtime / "napcat" / "cache" / "qrcode.png"
                    if image_path.stat().st_size <= 2_000_000:
                        image = image_path.read_bytes()
                        if image.startswith(_PNG_HEADER):
                            login["qrImage"] = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
                except OSError:
                    pass  # NapCat writes the image asynchronously; the next poll can read it.
            if not login["isLogin"]:
                code = (self._qr_failure_code() if login["loginPhase"] == "qrcode_scanned" else "")
                if value.get("loginError") or code:
                    login.update(loginPhase="login_failed", errorCode=code, qrImage="",
                                 loginError=(f"QQ 登录失败（错误 {code}），请重新扫码并在手机 QQ 上确认登录。" if code
                                             else "QQ 登录失败，请重新扫码并在手机 QQ 上确认登录。"))
            return login, login["loginError"]
        except _SetupError as exc:
            return login, str(exc)
        except Exception:
            return login, "QQ 登录状态暂时无法读取，请稍候重试。"

    @staticmethod
    def _policy(value: Any) -> dict:
        try:
            if not isinstance(value, dict):
                raise ValueError("invalid policy")
            policy = normalize_policy(value)
            # ``account_role`` is part of the profile policy.  Keep this
            # compatibility guard until every runtime has the shared policy
            # normalizer: older normalizers drop unknown keys, while the
            # setup endpoint must still reject non-canonical values and keep a
            # supplied role in its response/CAS snapshot.
            if "account_role" in value or "account_role" in policy:
                role = policy.get("account_role", value.get("account_role", "assistant"))
                if role not in {"assistant", "owner"}:
                    raise ValueError("账号用途必须为 assistant / owner")
                policy["account_role"] = role
            if any(not ident.isascii() for ident in [policy["account_id"], *policy["private_ids"], *policy["group_ids"]]):
                raise ValueError("QQ identities must use ASCII digits")
            return policy
        except (TypeError, ValueError, OverflowError):
            raise _SetupError("QQ 会话范围或频率配置无效，请检查联系人、群号和模式。") from None

    def _model(self) -> dict:
        settings = self.get_settings()
        selected = {key: value for key, value in settings.items() if key in MODEL_KEYS}
        endpoint = urlsplit(str(selected.get("apiEndpoint") or "").strip())
        needs_key = (endpoint.scheme == "https" and endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
                     and not (endpoint.hostname or "").endswith(".local"))
        key = str(selected.get("apiKey") or "").strip()
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or not str(selected.get("apiModel") or "").strip()
                or (needs_key and (not key or key in {"__KEEP__", "__CLEAR__"}))):
            raise _SetupError("请先在模型设置中选择并配置可用的模型，再保存 QQ 代管设置。")
        return selected

    def _installed(self) -> bool:
        paths = component.read_json(self.runtime / 'paths.json')
        if paths.get('qq_client'):
            host = paths.get('host_executable') or paths.get('python', '')
            return bool(host and Path(host).is_file() and Path(paths['qq_client']).is_file()
                        and all((component.shell_path(self.root, self.runtime) / name).is_file() for name in component.REQUIRED)
                        and (self.runtime / 'gateway.json').is_file())
        assets = self.root / ".runtime-assets" / "qq-agent"
        return all(path.is_file() for path in (
            self.root / "tools" / "qq_agent.ps1", self.root / "tools" / "qq_napcat.ps1",
            self.root / ".conda" / "qq-agent" / "python.exe",
            assets / "qqnt" / "Files" / "QQ.exe", assets / "napcat-shell" / "NapCatWinBootMain.exe",
            assets / "napcat-shell" / "napcat.mjs", self.runtime / "paths.json", self.runtime / "gateway.json"))

    def status(self) -> dict:
        config, gateway, errors = {}, {}, {}
        policy = dict(DEFAULT_POLICY)
        try:
            config = self._gateway_config()
            policy = self._policy(config.get("policy", {}))
            gateway = self._gateway(config, "/api/status")
            policy = self._policy(gateway.get("policy", policy))
            if gateway.get("error"):
                errors["gateway"] = "QQ 网关连接尚未就绪，请检查登录状态。"
        except _SetupError as exc:
            errors["gateway"] = str(exc)
        login, login_error = self._login()
        if login_error:
            errors["login"] = login_error
        try:
            self._model()
            model_configured = True
        except Exception:
            model_configured = False
        installed = self._installed()
        connected = bool(gateway.get("connected") is True and login["isLogin"] and login["coreReady"]
                         and login["accountId"] == policy["account_id"])
        needs_binding = bool(login["isLogin"] and login["accountId"] and login["accountId"] != policy["account_id"])
        starting = False
        failed = []
        for name, (process, started) in list(self._launches.items()):
            code = process.poll()
            ready = bool(gateway) if name == "qq_agent" else login["loginPhase"] != "unavailable"
            elapsed = time.monotonic() - started
            if not ready and (code is None or (code == 0 and elapsed < 30)):
                starting = True
            if not ready and (code not in {None, 0} or (code == 0 and elapsed >= 30)):
                label = "QQ 连接程序" if name == "qq_agent" else "QQ 登录程序"
                failed.append(f"{label}启动失败（退出码 {code}）" if code else f"{label}在启动命令结束后仍未响应")
                if self._reported_launch_failures.get(name) != (started, code):
                    self._reported_launch_failures[name] = (started, code)
                    self._write_log("qq_component_start_failed", component=name, exitCode=code,
                                    logFile=f"qq-agent/logs/{name}.launcher.log")
        if failed:
            errors["startup"] = "；".join(failed) + "。详情已保存到 data/qq-agent/logs，可点击 ▶ 重试。"
            if component.read_json(self.runtime / 'napcat-start-error.json').get('code') == 'QQ_ALREADY_RUNNING':
                errors['startup'] = '请先退出正在运行的 QQ，再连接代管；Amadeus 不会自动关闭你的 QQ'
        reply_agent = gateway.get("reply_agent", config.get("reply_agent", "astrbot"))
        message = ("QQ 代管已连接。" if connected else "QQ 已登录，可以保存代管设置。" if login["isLogin"]
                   else "请用手机 QQ 扫描二维码并确认登录。" if login["qrImage"]
                   else "QQ 组件正在启动，请稍候查看登录状态。" if starting
                   else "请启动 QQ 组件，随后在此扫码登录。" if installed else "本机 QQ 组件尚未安装完整。")
        # OneBot cannot connect until QQ logs in. Its generic connection error
        # must not hide the actionable login stage (or a concrete login failure).
        progress = self._login_message(login) if not connected else ""
        message = errors.get("startup") or progress or (next(iter(errors.values())) if errors else message)
        if needs_binding:
            message = "登录账号已改变，请点击「使用这个账号」；旧账号的回复名单与上下文不会沿用。"
        reply_mode = policy["mode"] if policy["mode"] != "paused" else gateway.get("resume_mode", config.get("resume_mode", "review"))
        return {"ok": True, "installed": installed, "connected": connected, "gatewayRunning": bool(gateway),
                "accountProfiles": gateway.get("accountProfiles", account_summaries(config)),
                "corpusStatus": forum_corpus_status(self.runtime.parent),
                "replyMode": reply_mode if reply_mode in {"auto", "review"} else "review",
                "replyAgent": reply_agent if reply_agent in {"amadeus", "astrbot"} else "astrbot",
                "modelConfigured": model_configured, "policy": policy, "login": login,
                "component": component.state(self.root, self.runtime),
                "needsAccountBinding": needs_binding,
                "needsConnectionSetup": bool(gateway and login["isLogin"] and login["coreReady"] and not login["isOffline"]
                                             and login["accountId"] == policy["account_id"] and not connected),
                "contacts": [] if needs_binding else gateway.get('contacts', self._cached_contact_names(policy)),
                "contactsComplete": not needs_binding and gateway.get('contactsComplete', False),
                "lifecycle": gateway.get("lifecycle", {"desktopOnline": False, "backgroundEnabled": policy["background_enabled"], "active": False}),
                "message": message, "errors": errors, "starting": starting}

    def _cached_contact_names(self, policy):
        from integrations.qq.contacts import read_contacts
        return [row for row in read_contacts(self.runtime / 'contacts-cache.json', policy['account_id'])
                if row['id'] in policy[row['kind'] + '_ids']]

    def _write_model(self, model: dict) -> None:
        # Resolve the desktop's chosen reference map only on an explicit save,
        # not on status polling. No conversation/private memory is exported.
        model = {key: value for key, value in self.prepare_voice_settings(dict(model)).items() if key in MODEL_KEYS}
        temporary = None
        try:
            self.runtime.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.runtime,
                                             prefix="amadeus-model-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(model, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.runtime / "amadeus-model.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def sync_model_settings(self) -> None:
        """Explicit owner configuration uses the current desktop model/voice."""
        self._write_model(self._model())

    def sync_model_settings_if_configured(self, changed: dict) -> bool:
        """Keep an existing QQ export current after a desktop settings save."""
        if not MODEL_KEYS.intersection(changed) or not (self.runtime / 'amadeus-model.json').is_file():
            return False
        self.sync_model_settings()
        return True

    def _save(self, body: dict) -> dict:
        config = self._gateway_config()
        gateway = self._gateway(config, "/api/status")
        policy = self._policy(gateway.get("policy", config.get("policy", {})))
        expected = body.get("expectedPolicy")
        if expected is not None and expected != policy:
            raise _SetupError("QQ 配置已通过对话或其他界面更新，请点击「撤销修改」重新读取设置后再保存。")
        snapshot = dict(policy)
        saved_reply_mode = policy["mode"] if policy["mode"] != "paused" else gateway.get("resume_mode", config.get("resume_mode", "review"))
        if "expectedReplyMode" in body and body["expectedReplyMode"] != saved_reply_mode:
            raise _SetupError("QQ 回复方式已通过对话或其他界面更新，请撤销修改后重新编辑。")
        if "account_role" in body and body["account_role"] not in {"assistant", "owner"}:
            raise _SetupError("账号用途请选择 Amadeus 的专用账号或我的个人账号。")
        reply_mode = body.get("replyMode")
        if "replyMode" in body:
            if reply_mode not in {"auto", "review"} or "mode" in body:
                raise _SetupError("请选择自动回复或先生成草稿。")
            # Saving preferences must never turn paused replies on.
            if policy["mode"] != "paused":
                policy["mode"] = reply_mode
        for key in ("mode", "account_role", "owner_ids", "private_ids", "group_ids", "reply_format", "reply_bubble_chars", "voice_reply_percent", "speech_language", "reply_instructions", "reply_delay_seconds", "reply_delay_mode", "max_sends_per_hour",
                    "muted_private_ids", "muted_group_ids", "background_enabled", "history_messages", "compact_reply", "reply_identity", "reply_style", "conversation_overrides", "owner_control_enabled", "owner_control_in_groups", "owner_developer_mode", "qzone_enabled", "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous", "qzone_interval_minutes", "qzone_instructions"):
            if key in body:
                policy[key] = body[key]
        login, _error = self._login()
        verified = login["isLogin"] and login["coreReady"] and not login["isOffline"] and login["accountId"]
        if verified:
            if str(login['accountId']) != str(policy['account_id']):
                raise _SetupError("QQ 登录账号已改变，请先点击「使用这个账号」，再为新账号选择回复对象。")
        elif policy["mode"] != "paused" and any(policy[key] != snapshot[key] for key in ("mode", "private_ids", "group_ids")):
            raise _SetupError("开启代管前，请先扫码登录 QQ 并等待登录完成。")
        policy = self._policy(policy)
        self._write_model(self._model())
        payload = {"policy": policy, "reply_agent": "amadeus", "expected_policy": snapshot}
        if reply_mode is not None:
            payload.update(resume_mode=reply_mode, expected_resume_mode=saved_reply_mode)
        result = self._gateway(config, "/api/config", payload)
        policy = self._policy(result.get("policy", policy))
        return {"ok": True, "policy": policy, "replyAgent": "amadeus", "modelConfigured": True,
                "replyMode": reply_mode or saved_reply_mode,
                "message": "设置已保存，代管仍暂停；点击顶部 ▶ 开始。" if policy["mode"] == "paused" else "设置已保存，正在使用新的回复规则。"}

    def _use_account(self, body: dict) -> dict:
        config = self._gateway_config()
        gateway = self._gateway(config, "/api/status")
        previous = self._policy(gateway.get("policy", config.get("policy", {})))
        if body.get("expectedPolicy") != previous:
            raise _SetupError("QQ 配置已更新，请刷新状态后再确认账号。")
        login, _ = self._login()
        if not (login["isLogin"] and login["coreReady"] and not login["isOffline"]
                and login["accountId"] and login["accountId"] == body.get("accountId")):
            raise _SetupError("登录账号尚未确认或已改变，请等待扫码登录完成后重试。")
        paused = self._gateway(config, "/api/control", {"action": "pause", "reason": "绑定新 QQ 账号前停止旧账号回复"})
        previous = self._policy(paused.get("policy", {**previous, "mode": "paused"}))
        # NapCat has one network configuration per QQ account. Older logins may
        # still have a different port/token; configure the current login locally.
        address = urlsplit(config.get("napcat_ws", ""))
        if address.scheme != "ws" or address.hostname not in {"127.0.0.1", "localhost"} or not address.port:
            raise _SetupError("QQ 连接地址需要是本机 WebSocket，请先检查本机连接配置。")
        onebot = {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketClients": [],
                  "websocketServers": [{"name": "AmadeusGateway", "enable": True, "host": "127.0.0.1",
                  "port": address.port, "messagePostFormat": "array", "reportSelfMessage": False,
                  "token": config["napcat_token"], "enableForcePushEvent": True, "debug": False, "heartInterval": 30000}]},
                  "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}
        try:
            port, credential = self._napcat_session()
            result = self._request(port, "/api/OB11Config/SetConfig", token=credential,
                                   body={"config": json.dumps(onebot, ensure_ascii=False)}, timeout=5)
            if result.get("code") != 0:
                raise ValueError("network configuration rejected")
        except Exception:
            raise _SetupError("新账号的 QQ 连接配置未完成，代管保持暂停，请重试「使用这个账号」。") from None
        confirmed, _ = self._login()
        if not (confirmed["isLogin"] and confirmed["coreReady"] and not confirmed["isOffline"]
                and confirmed["accountId"] == login["accountId"]):
            raise _SetupError("配置期间登录账号已改变，代管保持暂停，请刷新后重试。")
        # Restore only the verified account's own saved preferences. This also
        # finishes OneBot setup after selecting an account before its QR login.
        selected = select_account({**config, "policy": previous,
            "resume_mode": gateway.get("resume_mode", config.get("resume_mode", "review"))}, login["accountId"])
        policy = self._policy(selected["policy"])
        result = self._gateway(config, "/api/config", {"policy": policy, "expected_policy": previous,
                              "reply_agent": "amadeus", "resume_mode": selected["resume_mode"]})
        return {"ok": True, "policy": self._policy(result.get("policy", policy)), "replyAgent": "amadeus",
                "replyMode": result.get("resume_mode", selected["resume_mode"]),
                "message": "已连接这个账号，已恢复它自己的回复设置；代管保持暂停，请确认回复对象后点击 ▶ 开始。"}

    def _select_account(self, body: dict) -> dict:
        account = body.get("accountId")
        # regex-purpose: protocol
        if not isinstance(account, str) or not re.fullmatch(r"[0-9]{5,20}", account):
            raise _SetupError("请输入有效的 QQ 号。")
        config = self._gateway_config()
        # Offline configuration is allowed only when the local service refused
        # the connection; a timeout/authentication error is not permission to
        # overwrite a running gateway's state.
        try:
            # Windows may take about two seconds to return WSAECONNREFUSED
            # on localhost. The ordinary 1.5s UI poll timeout obscures it.
            gateway = self._request(self._port(config["control_port"]), "/api/status",
                                    token=config["control_token"], timeout=4)
        except URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError) and getattr(error.reason, "winerror", None) != 10061:
                raise _SetupError("QQ 网关暂时无法确认状态，请稍候再切换账号。") from None
            gateway = None
        except ConnectionRefusedError:
            gateway = None
        except Exception:
            raise _SetupError("QQ 网关暂时无法确认状态，请稍候再切换账号。") from None
        if gateway is not None and (gateway.get("ok") is False or not isinstance(gateway.get("policy"), dict)):
            raise _SetupError("QQ 网关状态不完整，请重启 QQ 连接后再切换账号。")
        previous = self._policy(gateway["policy"] if gateway else config.get("policy", {}))
        if body.get("expectedAccount") != previous["account_id"]:
            raise _SetupError("当前账号已改变，请刷新状态后再切换。")
        if gateway:
            if "accountProfiles" not in gateway:
                raise _SetupError("QQ 后台仍是旧版本，请退出 QQ 后台并重新连接后使用账号切换。")
            paused = self._gateway(config, "/api/control", {"action": "pause", "reason": "主人切换 QQ 账号配置"})
            previous = self._policy(paused.get("policy", {**previous, "mode": "paused"}))
            next_policy = previous if account == previous["account_id"] else {"account_id": account}
            result = self._gateway(config, "/api/config", {"policy": next_policy,
                "expected_policy": previous, "reply_agent": "amadeus"})
            policy = self._policy(result["policy"])
            selected = self._gateway_config()
        else:
            selected = select_account(config, account)
            selected["reply_agent"] = "amadeus"
            selected = remember_account(selected)
            ledger_path = self.runtime / "ledger.sqlite3"
            if account != previous["account_id"] and ledger_path.is_file():
                ledger = Ledger(ledger_path)
                try:
                    ledger.cancel_account_unsent(previous["account_id"])
                finally:
                    ledger.db.close()
            path = self.runtime / "gateway.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            policy = selected["policy"]
        result = {"ok": True, "policy": policy, "replyAgent": "amadeus",
                  "replyMode": selected.get("resume_mode", "review"),
                  "accountProfiles": account_summaries(selected), "starting": False,
                  "message": "账号配置已切换，已加载该账号独立的回复名单和设置；代管保持暂停。"}
        if not self._installed():
            result["message"] += "请先连接本机 QQ 组件。"
            return result
        try:
            self._write_model(self._model())
            login, _ = self._login()
            if login["isLogin"] and login["coreReady"] and login["accountId"] == account:
                if gateway:
                    return self._use_account({"accountId": account, "expectedPolicy": policy})
            else:
                self._credential = None
                self._launch("qq_napcat", "switch")
            start = self._start()
            result["starting"] = start.get("starting", False)
            result["message"] += "请使用对应的手机 QQ 完成登录，再连接此账号。"
        except _SetupError as error:
            result["message"] += " " + str(error)
        return result

    def _launch(self, name: str, action: str = "start") -> None:
        environment = dict(os.environ)
        environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if action == "switch":
            environment = {key: value for key, value in environment.items()
                           if key.upper() not in {"NAPCAT_QUICK_ACCOUNT", "NAPCAT_QUICK_PASSWORD", "NAPCAT_QUICK_PASSWORD_MD5"}}
        kwargs = {"cwd": str(self.root), "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT, "env": environment}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log_path = self.runtime / "logs" / (name + ".launcher.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as output:
            output.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {action} {name}\n".encode("utf-8"))
            try:
                command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(self.root / "tools" / (name + ".ps1"))]
                if component.read_json(self.runtime / 'paths.json').get('qq_client'):
                    command += ['-AppRoot', str(self.root), '-RuntimeRoot', str(self.runtime)]
                command += ['-Action', action]
                process = subprocess.Popen(command,
                                           stdout=output, **kwargs)
            except OSError as error:
                self._write_log("qq_component_spawn_failed", component=name, errorType=type(error).__name__)
                raise _SetupError("QQ 启动程序未能运行，请检查本机 PowerShell 是否可用。") from None
        self._launches[name] = process, time.monotonic()

    def _switch_account(self) -> dict:
        if not self._installed():
            raise _SetupError("本机 QQ 组件尚未安装完整，无法在此更换账号。")
        previous = self._launches.get("qq_napcat")
        if previous and previous[0].poll() is None:
            raise _SetupError("QQ 登录程序正在启动，请稍候再试。")
        result = self._gateway(self._gateway_config(), "/api/control",
                               {"action": "pause", "reason": "主人更换 QQ 登录账号，先停止旧账号的回复"})
        self._credential = None
        self._launch("qq_napcat", "switch")
        return {"ok": True, "policy": result.get("policy"), "starting": True,
                "message": "已暂停代管并请求重新登录，请稍候使用新 QQ 扫码。"}

    def _prepare_client(self, body: dict) -> None:
        path = body.get('clientPath', '')
        if not isinstance(path, str):
            raise _SetupError('请选择本机 QQ.exe')
        client = component.find_client(self.root, self.runtime, path)
        if not client:
            raise _SetupError('未找到兼容的 QQ NT，请自行安装或更新 QQ，或指定 QQ.exe 路径；Amadeus 不会下载 QQ 客户端。')
        try:
            frozen = bool(getattr(sys, 'frozen', False))
            desktop_python = self.root / '.conda/amadeus-desktop/python.exe'
            host = sys.executable if frozen or not desktop_python.is_file() else str(desktop_python)
            component.prepare_amadeus(self.root, self.runtime, client, python=host, frozen=frozen)
        except ValueError as exc:
            raise _SetupError(str(exc)) from None
        try:
            self._write_model(self._model())
        except _SetupError:
            pass  # QQ login is independent of configuring the conversation model.

    def _start(self) -> dict:
        if not self._installed():
            raise _SetupError("本机 QQ 组件尚未安装完整，请先安装并准备组件后再启动。")
        if self.runtime != self.root / "data" / "qq-agent" and not component.read_json(self.runtime / 'paths.json').get('qq_client'):
            raise _SetupError("当前数据目录不支持启动本机 QQ 脚本。")
        running = {"qq_agent": False, "qq_napcat": False}
        try:
            self._gateway(self._gateway_config(), "/api/status")
            running["qq_agent"] = True
        except _SetupError:
            pass
        if not running["qq_agent"]:
            # Connecting is separate from enabling replies, including after an
            # unclean exit that left an active mode on disk.
            config = self._gateway_config()
            policy = self._policy(config.get("policy", {}))
            if policy["mode"] != "paused":
                config["resume_mode"] = policy["mode"]
                config["policy"] = {**policy, "mode": "paused"}
                path = self.runtime / "gateway.json"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
        # A cached WebUI credential does not prove its process is still alive.
        login, _ = self._login()
        running["qq_napcat"] = login["loginPhase"] != "unavailable"
        launched = False
        for name, is_running in running.items():
            previous = self._launches.get(name)
            if is_running or (previous and (previous[0].poll() is None or (previous[0].poll() == 0 and time.monotonic() - previous[1] < 10))):
                continue
            self._launch(name)
            launched = True
        if not launched and all(running.values()):
            current = self.status()
            return {"ok": True, "starting": current["starting"], "connected": current["connected"],
                    "message": current["message"]}
        return {"ok": True, "starting": True, "message": "已请求启动 QQ，正在等待连接结果。"}

    def update(self, body: Any) -> dict:
        if not isinstance(body, dict):
            return {"ok": False, "message": "QQ 设置请求格式不正确。"}
        action = body.get("action")
        allowed = {"start": {"action"}, "setup": {"action", "clientPath"}, "switch_account": {"action"},
                   "use_account": {"action", "accountId", "expectedPolicy"},
                   "select_account": {"action", "accountId", "expectedAccount"}, "save": {"action", "mode", "private_ids", "group_ids", "reply_format", "reply_bubble_chars", "voice_reply_percent", "speech_language", "reply_instructions", "reply_delay_seconds", "reply_delay_mode", "max_sends_per_hour", "expectedPolicy",
                   "muted_private_ids", "muted_group_ids", "background_enabled", "history_messages", "compact_reply", "reply_identity", "reply_style", "conversation_overrides", "replyMode", "expectedReplyMode", "account_role", "owner_ids", "owner_control_enabled", "owner_control_in_groups", "owner_developer_mode", "qzone_enabled", "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous", "qzone_interval_minutes", "qzone_instructions"},
                   "pause": {"action"}, "resume": {"action"}, "stop": {"action"}, "contacts": {"action"}, "refresh_qr": {"action"},
                   "media": {"action", "mediaAction", "expectedAccount", "query", "q", "limit", "imageIds", "assetIds", "items"},
                   "mute": {"action", "targetType", "targetId", "expectedAccount"}, "unmute": {"action", "targetType", "targetId", "expectedAccount"},
                   "clear_context": {"action", "targetType", "targetId", "expectedAccount", "confirm"},
                   "approve": {"action", "id"}, "cancel": {"action", "id"}}
        if not isinstance(action, str) or action not in allowed or set(body) - allowed[action]:
            return {"ok": False, "message": "QQ 设置操作或字段不受支持。"}
        if not self._lock.acquire(timeout=0.1):
            return {"ok": False, "message": "QQ 设置正在处理中，请稍候重试。"}
        try:
            if action == 'setup':
                self._prepare_client(body)
                return self._start()
            if action == "save":
                return self._save(body)
            if action == "start":
                return self._start()
            if action == "switch_account":
                return self._switch_account()
            if action == "use_account":
                return self._use_account(body)
            if action == "select_account":
                return self._select_account(body)
            if action == "refresh_qr":
                session = self._napcat_session()
                current = self._napcat(session, "/api/QQLogin/CheckLoginStatus")
                if current.get("isLogin") is True:
                    return {"ok": True, "message": "QQ 已登录，无需刷新二维码。"}
                self._napcat(session, "/api/QQLogin/RefreshQRcode")
                return {"ok": True, "message": "已请求刷新二维码，请稍候扫码。"}
            config = self._gateway_config()
            if action == "contacts":
                return self._gateway(config, '/api/contacts')
            if action == "media":
                media_action = body.get("mediaAction")
                if media_action not in {"search", "get", "import", "delete"}:
                    raise _SetupError("QQ 图库操作无效，请刷新后重试。")
                status = self._gateway(config, "/api/status")
                current = self._policy(status.get("policy", {}))
                account = current.get("account_id", "")
                expected_account = body.get("expectedAccount")
                if (not isinstance(expected_account, str)
                        or expected_account != account
                        # regex-purpose: protocol
                        or not re.fullmatch(r"[0-9]{5,20}", account)):
                    raise _SetupError("QQ 账号已变化，请刷新图库后重试。")
                payload = {"action": media_action, "expectedAccount": account}
                for key in ("query", "q", "limit", "items"):
                    if key in body:
                        payload[key] = body[key]
                if media_action in {"get", "delete"}:
                    ids = body.get("imageIds", body.get("assetIds"))
                    payload["imageIds"] = ids
                return self._gateway(config, "/api/media", payload)
            if action == "clear_context":
                target_type = body.get("targetType")
                target_id = body.get("targetId")
                expected_account = body.get("expectedAccount")
                if (body.get("confirm") is not True or target_type not in {"private", "group"}
                        # regex-purpose: protocol
                        or isinstance(target_id, bool) or not re.fullmatch(r"[0-9]+", str(target_id or ""))
                        or isinstance(expected_account, bool)
                        # regex-purpose: protocol
                        or not re.fullmatch(r"[0-9]+", str(expected_account or ""))):
                    raise _SetupError("请明确确认当前 QQ 账号下的好友或群会话后再清理上下文。")
                # The setup action envelope is local UI protocol.  Gateway's
                # clear endpoint intentionally accepts only the authenticated
                # four-field reset contract, so do not forward ``action``.
                clear_body = {key: body[key] for key in
                              ("targetType", "targetId", "expectedAccount", "confirm")}
                result = self._gateway(config, "/api/conversation/clear", clear_body)
                cleared = result.get("cleared")
                if not isinstance(cleared, dict):
                    raise _SetupError("QQ 网关没有确认上下文已清理，请稍候重试。")
                return {"ok": True, "message": str(result.get("message") or "QQ 会话上下文已重置。"),
                        "cleared": cleared}
            if action in {"mute", "unmute"}:
                result = self._gateway(config, '/api/conversation', {**body, 'reason': '主人在 Amadeus 设置中调整该会话'})
                return {**result, 'message': '该会话已暂停回复。' if action == 'mute' else '该会话已恢复回复；仍受全局模式控制。'}
            if action in {"pause", "resume", "stop"}:
                result = self._gateway(config, "/api/control", {"action": action, "reason": "主人在 Amadeus 设置中调整 QQ 代管"})
                if action == 'stop':
                    self._launches.clear()
                return {**result, "ok": True, "message": {"pause": "已暂停所有回复，连接与设置保留；点击顶部 ▶ 可恢复。", "resume": "QQ 代管已开启，按已保存的规则处理新来信。", "stop": "QQ 后台正在退出，好友与回复设置已保留。"}[action]}
            ident = body.get("id")
            # regex-purpose: protocol
            if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
                raise _SetupError("请选择有效的 QQ 消息草稿。")
            result = self._gateway(config, "/api/" + action, {"id": ident})
            if action == "approve":
                draft = result.get("draft", {})
                state = draft.get("state")
                if state != "sent" or draft.get("message_id") in {None, ""}:
                    return {"ok": False, "draftState": state if state in {"failed", "unknown", "sending"} else "unknown",
                            "message": "QQ 接口报告发送失败。" if state == "failed" else "发送结果尚未确认，请在 QQ 和动态记录中核对；请勿重复发送。"}
                return {"ok": True, "draftState": "sent", "message": "QQ 草稿已确认发送。"}
            return {"ok": True, "message": "QQ 草稿已取消。"}
        except _SetupError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception:
            return {"ok": False, "message": "QQ 设置操作未完成，请检查组件状态后重试。"}
        finally:
            self._lock.release()
