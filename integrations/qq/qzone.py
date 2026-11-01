"""Bounded QQ Space access through the currently verified NapCat account.

The gateway owns the OneBot connection and passes a generation-scoped ``call``
function to :meth:`QzoneService.execute`::

    service = QzoneService(runtime_path)
    result = await service.execute(args, policy, scoped_call)

Only the small action set in this module is exposed.  QZone feed identifiers
are kept in an account-scoped observation file and represented to callers by
opaque ``postRef`` values.  This prevents a model from inventing a QQ number,
tid, or QZone key for an interaction.  Cookies are used only in memory while
an action runs; they are never returned, logged, or persisted.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

try:  # qzone-sdk is an optional QQ component dependency.
    from qzone_sdk import ManualCookieProvider, QZoneClient
except Exception:  # pragma: no cover - exercised when the optional package is absent.
    ManualCookieProvider = None  # type: ignore[assignment,misc]
    QZoneClient = None  # type: ignore[assignment,misc]


OneBotCall = Callable[[str, dict | None], Awaitable[Any]]
_ACCOUNT = re.compile(r"^[0-9]{5,20}$")
_ACTIONS = frozenset({"status", "feed", "posts", "publish", "like", "comment"})
_VISIBILITY = {"public": 1, "friends": 4, "private": 64}
_MAX_COUNT = 20
_MAX_CONTENT = 2_000
_MAX_REF = 256
_MAX_REQUEST_KEY = 256
_ARGUMENTS = {
    "status": {"action"},
    "feed": {"action", "count", "page"},
    "posts": {"action", "targetQQ", "target_qq", "num", "count"},
    "publish": {"action", "content", "visibility", "requestKey", "request_key"},
    "like": {"action", "postRef", "post_ref", "requestKey", "request_key"},
    "comment": {"action", "postRef", "post_ref", "content", "requestKey", "request_key"},
}


class _QzoneError(ValueError):
    """An error with a safe code/message; never carries a provider response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Auth:
    account: str
    cookies: str
    qzone_cookie_ready: bool


def _account(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    return text if _ACCOUNT.fullmatch(text) else ""


def _identifier(value: Any, *, limit: int = 256) -> str:
    """Return a bounded opaque provider identifier without coercing objects."""
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).strip()
    if not text or len(text) > limit or not text.isascii():
        return ""
    return text


def _number(value: Any, *, limit: int = 20) -> str:
    text = _identifier(value, limit=limit)
    return text if text.isdigit() else ""


def _bounded_int(value: Any, default: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise _QzoneError("invalid_arguments", "数量参数必须为整数。")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise _QzoneError("invalid_arguments", "数量参数必须为整数。") from None
    if result < 1:
        raise _QzoneError("invalid_arguments", "数量参数必须为正整数。")
    return min(result, maximum)


def _response_data(response: Any, *, action: str) -> Any:
    """Unwrap a OneBot response while discarding provider error text."""
    if not isinstance(response, dict):
        return response
    retcode = response.get("retcode")
    status = response.get("status")
    if retcode not in (None, 0, "0") or status not in (None, "ok", "success"):
        raise _QzoneError("qzone_action_failed", f"NapCat {action} 未返回可用结果。")
    if "data" in response:
        return response.get("data")
    return response


def _cookie_map(cookie_string: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip()
    return values


def _cookie_uin(value: str) -> str:
    value = unquote(value).strip()
    if value.startswith(("o", "O")):
        value = value[1:]
    value = value.lstrip('0')
    return _account(value)


def _cookie_accounts(cookies: str) -> tuple[set[str], bool]:
    values = _cookie_map(cookies)
    found: set[str] = set()
    malformed = False
    for name in ("p_uin", "uin"):
        if name not in values:
            continue
        parsed = _cookie_uin(values[name])
        if parsed:
            found.add(parsed)
        else:
            malformed = True
    return found, malformed


def _safe_response_id(value: Any) -> str:
    text = _identifier(value, limit=128)
    return text


def _first(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw and raw[name] not in (None, ""):
            return raw[name]
    return None


def _text(value: Any, limit: int = _MAX_CONTENT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def preserve_feed_references(raw: str, items: list[dict]) -> list[dict]:
    """Keep observed interaction fields dropped by qzone-sdk 1.0.1.

    feeds3 HTML exposes data-curkey/data-unikey on like buttons (also used by
    onebot-qzone's parser). Match within each SDK feed fragment, never across
    posts, and never synthesize a provider key from arbitrary text.
    """
    fragments = [part for part in raw.split('{ver:')[1:] if part.strip()]
    for item, fragment in zip(items, fragments):
        decoded = html.unescape(re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m[1], 16)), fragment))
        decoded = decoded.replace('\\"', '"').replace('\\/', '/')
        for field in ('curkey', 'unikey', 'fid', 'topicId'):
            match = re.search(rf'data-{field}=["\']([^"\']+)["\']', decoded, re.I)
            if not match:
                match = re.search(rf'(?<![\w-]){field}\s*:\s*["\']([^"\']+)["\']', fragment)
            if match:
                item[field] = match[1]
    return items


class QzoneService:
    """Execute the bounded QZone actions for one selected account.

    ``sdk_factory`` is intentionally injectable for offline tests.  Production
    uses the reviewed qzone-sdk API with ``ManualCookieProvider`` and never its
    NapCat auto-provider, because the gateway has already fetched and checked
    the account-scoped cookie itself.
    """

    def __init__(self, runtime: Path, *, sdk_factory: Callable[[str, str], Any] | None = None):
        self.runtime = Path(runtime)
        self.sdk_factory = sdk_factory
        self._states: dict[str, dict[str, Any]] = {}

    # ---- account-scoped local state ---------------------------------
    def _state_path(self, account: str) -> Path:
        return self.runtime / "qzone" / f"{account}.json"

    def _blank_state(self, account: str) -> dict[str, Any]:
        return {"version": 1, "account": account, "observations": {}, "receipts": {}}

    def _load_state(self, account: str) -> dict[str, Any]:
        cached = self._states.get(account)
        if cached is not None:
            return cached
        state = self._blank_state(account)
        path = self._state_path(account)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("account") == account:
                if isinstance(raw.get("observations"), dict):
                    state["observations"] = raw["observations"]
                if isinstance(raw.get("receipts"), dict):
                    state["receipts"] = raw["receipts"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            raise _QzoneError('qzone_storage_unavailable', 'QQ 空间本地记录无法读取，已停止操作。') from None
        self._states[account] = state
        return state

    def _save_state(self, account: str, state: dict[str, Any]) -> None:
        path = self._state_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bound caches so a long-running account cannot grow unbounded local
        # state.  Dict insertion order preserves the newest observations.
        for key in ("observations", "receipts"):
            values = state.get(key)
            if isinstance(values, dict) and len(values) > 500:
                state[key] = dict(list(values.items())[-500:])
        payload = {
            "version": 1,
            "account": account,
            "observations": state.get("observations", {}),
            "receipts": state.get("receipts", {}),
        }
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise _QzoneError("qzone_storage_unavailable", "QQ 空间本地状态暂时无法保存。") from None

    # ---- OneBot authentication --------------------------------------
    @staticmethod
    async def _onebot_call(call: OneBotCall, action: str, params: dict | None = None) -> Any:
        """Accept both common fake-call forms while matching Gateway's API."""
        if params is not None:
            return await call(action, params)
        try:
            return await call(action)
        except TypeError:
            # A few lightweight test adapters require an explicit empty params
            # object for no-argument actions; Gateway itself accepts omission.
            return await call(action, {})

    async def _authenticate(self, account: str, call: OneBotCall, *, require_p_skey: bool) -> _Auth:
        try:
            login_response = await self._onebot_call(call, "get_login_info")
            login = _response_data(login_response, action="get_login_info")
        except _QzoneError:
            raise
        except Exception:
            raise _QzoneError("qzone_connection_failed", "QQ 空间暂时无法确认当前登录账号。") from None
        if not isinstance(login, dict):
            raise _QzoneError("qzone_login_mismatch", "NapCat 未返回当前登录账号。")
        logged_in = _account(_first(login, "user_id", "uin", "self_id"))
        if logged_in != account:
            raise _QzoneError("qzone_account_mismatch", "当前登录 QQ 与选中账号不一致，已停止本次操作。")

        try:
            cookie_response = await self._onebot_call(call, "get_cookies", {"domain": "user.qzone.qq.com"})
            cookie_data = _response_data(cookie_response, action="get_cookies")
        except _QzoneError:
            raise
        except Exception:
            raise _QzoneError("qzone_connection_failed", "QQ 空间登录凭证暂时无法读取。") from None
        if not isinstance(cookie_data, dict) or not isinstance(cookie_data.get("cookies"), str):
            raise _QzoneError("qzone_cookie_missing", "NapCat 未返回 QQ 空间登录凭证。")
        cookies = cookie_data["cookies"]
        cookie_accounts, malformed = _cookie_accounts(cookies)
        if malformed or not cookie_accounts:
            raise _QzoneError("qzone_cookie_account_missing", "QQ 空间登录凭证缺少可核对的账号。")
        if cookie_accounts != {account}:
            raise _QzoneError("qzone_account_mismatch", "QQ 空间登录凭证与选中账号不一致，已停止本次操作。")
        ready = bool(_cookie_map(cookies).get("p_skey"))
        if require_p_skey and not ready:
            raise _QzoneError("qzone_cookie_missing", "QQ 空间登录凭证尚未包含 p_skey。")
        return _Auth(account=account, cookies=cookies, qzone_cookie_ready=ready)

    # ---- qzone-sdk adapter ------------------------------------------
    async def _sdk(self, auth: _Auth) -> Any:
        if self.sdk_factory is not None:
            client = self.sdk_factory(auth.cookies, auth.account)
            return await client if inspect.isawaitable(client) else client
        if ManualCookieProvider is None or QZoneClient is None:
            raise _QzoneError("qzone_dependency_missing", "QQ 空间组件缺少 qzone-sdk 依赖。")
        try:
            provider = ManualCookieProvider(cookies=auth.cookies, uin=auth.account)
            return QZoneClient(auth_provider=provider)
        except Exception:
            raise _QzoneError("qzone_dependency_failed", "QQ 空间 SDK 初始化失败。") from None

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _sdk_call(self, auth: _Auth, call: OneBotCall, method: str, *args, **kwargs):
        client = await self._sdk(auth)
        await self._onebot_call(call, 'get_login_info')
        if self.sdk_factory is not None:
            result = await self._maybe_await(getattr(client, method)(*args, **kwargs))
        else:
            # The SDK exposes async methods but uses blocking urllib internally.
            # Keep the gateway loop responsive to stop/switch commands, checking
            # its generation immediately before each actual network request.
            loop = asyncio.get_running_loop()
            original = client._http._request
            original_get = client._get
            raw_reads = []
            def capture_get(*get_args, **get_kwargs):
                raw = original_get(*get_args, **get_kwargs)
                raw_reads.append(raw)
                from qzone_sdk.utils.jsonp import parse_jsonp
                parsed = parse_jsonp(raw)
                if isinstance(parsed, dict) and parsed.get('code', 0) not in (0, '0'):
                    raise _QzoneError('qzone_read_failed', 'QQ 空间接口拒绝了读取请求。')
                return raw
            client._get = capture_get
            def guarded_request(*request_args, **request_kwargs):
                asyncio.run_coroutine_threadsafe(self._onebot_call(call, 'get_login_info'), loop).result(timeout=15)
                return original(*request_args, **request_kwargs)
            client._http._request = guarded_request
            def invoke():
                return asyncio.run(getattr(client, method)(*args, **kwargs))
            result = await asyncio.to_thread(invoke)
            if method == 'get_feed_list' and raw_reads and isinstance(result, list):
                result = preserve_feed_references(raw_reads[-1], result)
        await self._onebot_call(call, 'get_login_info')
        return result

    # ---- observation references -------------------------------------
    def _new_ref(self, account: str, source: str, raw: dict[str, Any], state: dict[str, Any]) -> str | None:
        author = _number(_first(raw, "author", "author_qq", "hostUin", "hostuin", "uin", "opuin"))
        identifier = _safe_response_id(_first(raw, "id", "fid", "tid", "key", "topicId", "topic_id"))
        if not author or not identifier:
            return None
        cur_key = _identifier(_first(raw, "curkey", "cur_key", "curKey"))
        uni_key = _identifier(_first(raw, "unikey", "uni_key", "uniKey"))
        fid = _identifier(_first(raw, "fid", "topicId", "topic_id"))
        if not fid:
            # qzone-sdk's comment() documents this topicId form for moods;
            # derive it only from identifiers returned by the SDK.
            fid = f"{author}_{identifier}__1" if source == "posts" else ''
        observations = state["observations"]
        for _ in range(5):
            reference = "qz1_" + secrets.token_urlsafe(18)
            if reference not in observations:
                observations[reference] = {
                    "account": account,
                    "source": source,
                    "author": author,
                    "id": identifier,
                    "fid": fid,
                    "curKey": cur_key,
                    "uniKey": uni_key,
                }
                return reference
        raise _QzoneError("qzone_reference_failed", "QQ 空间动态引用暂时无法建立。")

    def _resolve_ref(self, account: str, value: Any) -> dict[str, Any]:
        reference = value if isinstance(value, str) else ""
        if not reference or len(reference) > _MAX_REF:
            raise _QzoneError("invalid_post_ref", "请使用最近读取结果中的 QQ 空间 postRef。")
        observation = self._load_state(account)["observations"].get(reference)
        if not isinstance(observation, dict) or observation.get("account") != account:
            raise _QzoneError("invalid_post_ref", "该 QQ 空间 postRef 不属于当前账号或已失效。")
        return observation

    @staticmethod
    def _safe_images(raw: dict[str, Any]) -> list[str]:
        value = raw.get("images", raw.get("pic", []))
        if not isinstance(value, list):
            return []
        images: list[str] = []
        for item in value[:9]:
            if isinstance(item, dict):
                item = _first(item, "url", "url1", "smallurl")
            if not isinstance(item, str) or not item.startswith(("http://", "https://")):
                continue
            if any(secret in item.lower() for secret in ("skey=", "p_skey=", "cookie=")):
                continue
            images.append(item[:2_048])
        return images

    def _public_item(self, raw: Any, source: str, reference: str | None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {"postRef": reference, "content": "", "author": "", "createdAt": ""}
        author = _number(_first(raw, "author", "author_qq", "hostUin", "hostuin", "uin", "opuin"))
        created = _first(raw, "created_time", "createTime", "feedstime", "abstime")
        if not isinstance(created, (str, int, float)) or isinstance(created, bool):
            created = ""
        result: dict[str, Any] = {
            "postRef": reference,
            "author": author,
            "content": _text(_first(raw, "content", "text", "rt_content")),
            "createdAt": str(created)[:64] if created != "" else "",
            "canLike": bool(_first(raw, 'curkey', 'curKey') and _first(raw, 'unikey', 'uniKey')),
            "canComment": source == 'posts' or bool(_first(raw, 'fid', 'topicId', 'topic_id')),
        }
        images = self._safe_images(raw)
        if images:
            result["images"] = images
        name = _first(raw, "name", "nickname", "remark")
        if isinstance(name, str) and name.strip():
            result["authorName"] = name.strip()[:100]
        return result

    async def _read_feed(self, auth: _Auth, args: dict[str, Any], state: dict[str, Any], call) -> dict[str, Any]:
        count = _bounded_int(args.get("count"), 10, _MAX_COUNT)
        page = _bounded_int(args.get("page"), 1, 100)
        try:
            raw_items = await self._sdk_call(auth, call, 'get_feed_list', page=page, count=count)
        except _QzoneError:
            raise
        except Exception:
            raise _QzoneError("qzone_read_failed", "QQ 空间好友动态暂时无法读取。") from None
        if not isinstance(raw_items, list):
            raise _QzoneError("qzone_read_failed", "QQ 空间未返回可用的好友动态。")
        items = []
        for raw in raw_items[:count]:
            reference = self._new_ref(auth.account, "feed", raw, state) if isinstance(raw, dict) else None
            items.append(self._public_item(raw, "feed", reference))
        self._save_state(auth.account, state)
        return {"ok": True, "action": "feed", "account": auth.account, "items": items, "count": len(items)}

    async def _read_posts(self, auth: _Auth, args: dict[str, Any], state: dict[str, Any], call) -> dict[str, Any]:
        target = _number(_first(args, "targetQQ", "target_qq"))
        if not target:
            raise _QzoneError("invalid_arguments", "posts 需要合法的 targetQQ。")
        count = _bounded_int(_first(args, "num", "count"), 10, _MAX_COUNT)
        try:
            raw_items = await self._sdk_call(auth, call, 'get_friend_moods', target, num=count)
        except _QzoneError:
            raise
        except Exception:
            raise _QzoneError("qzone_read_failed", "QQ 空间说说暂时无法读取。") from None
        if not isinstance(raw_items, list):
            raise _QzoneError("qzone_read_failed", "QQ 空间未返回可用的说说。")
        items = []
        for raw in raw_items[:count]:
            reference = self._new_ref(auth.account, "posts", raw, state) if isinstance(raw, dict) else None
            items.append(self._public_item(raw, "posts", reference))
        self._save_state(auth.account, state)
        return {"ok": True, "action": "posts", "account": auth.account, "targetQQ": target,
                "items": items, "count": len(items)}

    # ---- write receipts ---------------------------------------------
    @staticmethod
    def _request_key(args: dict[str, Any]) -> str:
        first = args.get("requestKey")
        second = args.get("request_key")
        if first not in (None, "") and second not in (None, "") and first != second:
            raise _QzoneError("invalid_arguments", "requestKey 不能有两个不同值。")
        value = first if first not in (None, "") else second
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_REQUEST_KEY:
            raise _QzoneError("invalid_arguments", "写入操作需要 requestKey。")
        if any(ord(char) < 32 for char in value):
            raise _QzoneError("invalid_arguments", "requestKey 含有无效字符。")
        return value.strip()

    @staticmethod
    def _fingerprint(account: str, action: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps({"account": account, "action": action, **payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt_result(record: dict[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
        sent = record.get("state") == "sent"
        result: dict[str, Any] = {
            "ok": sent,
            "sent": sent,
            "account": record.get("account", ""),
            "action": record.get("action", ""),
            "requestKey": record.get("requestKey", ""),
            "receipt": {key: value for key, value in record.items()
                        if key in {"account", "action", "requestKey", "state", "postRef", "platformId"}},
        }
        if duplicate:
            result["duplicate"] = True
        if not sent:
            result["error"] = "qzone_write_failed" if record.get("state") == "failed" else "qzone_request_pending"
            result["message"] = "QQ 空间写入未确认成功，未报告为已发送。"
        return result

    def _existing_receipt(self, account: str, request_key: str, fingerprint: str) -> dict[str, Any] | None:
        record = self._load_state(account)["receipts"].get(request_key)
        if not isinstance(record, dict):
            return None
        if record.get("fingerprint") != fingerprint:
            raise _QzoneError("request_key_conflict", "requestKey 已绑定另一条 QQ 空间操作。")
        return record

    def _begin_receipt(self, account: str, action: str, request_key: str, fingerprint: str, post_ref: str) -> dict[str, Any]:
        state = self._load_state(account)
        existing = self._existing_receipt(account, request_key, fingerprint)
        if existing is not None:
            return existing
        record = {"account": account, "action": action, "requestKey": request_key,
                  "postRef": post_ref, "fingerprint": fingerprint, "state": "pending"}
        # Exclusive claims survive damaged caches, concurrent instances and
        # restarts. A claimed operation is never automatically issued again.
        claims = self.runtime / 'qzone' / account / 'claims'
        claims.mkdir(parents=True, exist_ok=True)
        marker = claims / (hashlib.sha256(request_key.encode()).hexdigest() + '.json')
        try:
            with marker.open('x', encoding='utf-8') as stream:
                json.dump(record, stream)
        except FileExistsError:
            raise _QzoneError('qzone_request_pending', '该操作已有持久记录，请核对结果，不要重发。') from None
        state["receipts"][request_key] = record
        self._save_state(account, state)
        return record

    def _finish_receipt(self, account: str, request_key: str, *, state_name: str, platform_id: str = "") -> dict[str, Any]:
        state = self._load_state(account)
        record = state["receipts"].get(request_key)
        if not isinstance(record, dict):
            raise _QzoneError("qzone_storage_unavailable", "QQ 空间写入回执无法保存。")
        record["state"] = state_name
        if platform_id:
            record["platformId"] = platform_id
        self._save_state(account, state)
        return record

    @staticmethod
    def _send_qzone_success(response: Any) -> tuple[bool, str]:
        if not isinstance(response, dict):
            return False, ""
        if response.get("retcode") not in (None, 0, "0") or response.get("status") not in (None, "ok", "success"):
            return False, ""
        if response.get("success") is False:
            return False, ""
        data = response.get("data", response)
        if isinstance(data, dict):
            platform_id = _safe_response_id(_first(data, "tid", "message_id", "id"))
            return bool(platform_id), platform_id
        return False, ""

    @staticmethod
    def _sdk_success(response: Any) -> bool:
        return isinstance(response, dict) and response.get("success") is True

    async def _write(self, auth: _Auth, action: str, args: dict[str, Any], policy: dict[str, Any],
                     state: dict[str, Any], call: OneBotCall) -> dict[str, Any]:
        if policy.get("qzone_enabled") is not True:
            raise _QzoneError("qzone_disabled", "QQ 空间功能尚未开启。")
        if policy.get("qzone_allow_write") is not True:
            raise _QzoneError("qzone_write_disabled", "QQ 空间写入权限尚未开启。")
        request_key = self._request_key(args)

        if action == "publish":
            content = args.get("content")
            if not isinstance(content, str) or not content.strip() or len(content) > _MAX_CONTENT:
                raise _QzoneError("invalid_arguments", "publish 需要 1–2000 字的文字。")
            visibility = args.get("visibility", "friends")
            if not isinstance(visibility, str) or visibility not in _VISIBILITY:
                raise _QzoneError("invalid_arguments", "visibility 只能是 public、friends 或 private。")
            fingerprint_payload = {"content": content, "visibility": visibility}
            post_ref = ""
        else:
            post_ref_value = args.get("postRef", args.get("post_ref"))
            observation = self._resolve_ref(auth.account, post_ref_value)
            post_ref = post_ref_value
            if action == "like" and (not observation.get("curKey") or not observation.get("uniKey")):
                raise _QzoneError("post_ref_unusable", "该动态没有可用于点赞的完整引用，请重新读取。")
            if action == "comment":
                content = args.get("content")
                if not isinstance(content, str) or not content.strip() or len(content) > _MAX_CONTENT:
                    raise _QzoneError("invalid_arguments", "comment 需要 1–2000 字的文字。")
                if not observation.get("fid") or not observation.get("author"):
                    raise _QzoneError("post_ref_unusable", "该动态没有可用于评论的完整引用，请重新读取。")
                fingerprint_payload = {"postRef": post_ref, "content": content}
            else:
                fingerprint_payload = {"postRef": post_ref}

        fingerprint = self._fingerprint(auth.account, action, fingerprint_payload)
        existing = self._existing_receipt(auth.account, request_key, fingerprint)
        if existing is not None:
            return self._receipt_result(existing, duplicate=True)
        self._begin_receipt(auth.account, action, request_key, fingerprint, post_ref)

        try:
            if action == "publish":
                visibility = _VISIBILITY[args.get("visibility", "friends")]
                # The official NapCat action owns upload/session handling.  This
                # first version intentionally sends text only.
                response = await self._onebot_call(call, "send_qzone_msg", {
                    "content": args["content"],
                    "ugc_right": visibility,
                })
                success, platform_id = self._send_qzone_success(response)
            else:
                observation = self._resolve_ref(auth.account, post_ref)
                if action == "like":
                    response = await self._sdk_call(auth, call, 'like', observation["author"], observation["id"],
                                                    observation["curKey"], observation["uniKey"])
                else:
                    response = await self._sdk_call(auth, call, 'comment', observation["author"], observation["fid"], args["content"])
                success, platform_id = self._sdk_success(response), ""
        except _QzoneError:
            self._finish_receipt(auth.account, request_key, state_name="failed")
            raise
        except Exception:
            self._finish_receipt(auth.account, request_key, state_name="unknown")
            return {"ok": False, "sent": False, "error": "qzone_write_failed",
                    "deliveryState": "unknown", "message": "QQ 空间写入结果未知，请核对空间，不能自动重发。", "account": auth.account,
                    "action": action, "requestKey": request_key}
        if not success:
            self._finish_receipt(auth.account, request_key, state_name="failed")
            return {"ok": False, "sent": False, "error": "qzone_write_failed",
                    "message": "QQ 空间写入未收到成功回执，未报告为已发送。", "account": auth.account,
                    "action": action, "requestKey": request_key}
        record = self._finish_receipt(auth.account, request_key, state_name="sent", platform_id=platform_id)
        return self._receipt_result(record)

    # ---- public entry point -----------------------------------------
    async def execute(self, args: dict, policy: dict, call: OneBotCall) -> dict:
        """Run one supported action and return a cookie-free result."""
        if not isinstance(args, dict) or not isinstance(policy, dict):
            return {"ok": False, "error": "invalid_arguments", "message": "QQ 空间参数无效。"}
        action = args.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            return {"ok": False, "error": "unsupported_action", "message": "不支持该 QQ 空间操作。"}
        unexpected = set(args) - _ARGUMENTS[action]
        if unexpected:
            return {"ok": False, "error": "unsupported_arguments", "message": "QQ 空间操作包含未开放的参数。"}
        account = _account(policy.get("account_id"))
        if not account:
            return {"ok": False, "error": "qzone_account_missing", "message": "当前 QQ 配置没有合法账号。"}

        # Every supported action verifies the selected account and obtains the
        # QZone cookie from the current scoped OneBot connection first.  The
        # policy gates below never receive or expose the cookie value.
        require_p_skey = action in {"feed", "posts", "like", "comment"}
        try:
            auth = await self._authenticate(account, call, require_p_skey=require_p_skey)
            if action != "status" and policy.get("qzone_enabled") is not True:
                raise _QzoneError("qzone_disabled", "QQ 空间功能尚未开启。")
            if action in {"publish", "like", "comment"} and policy.get("qzone_allow_write") is not True:
                raise _QzoneError("qzone_write_disabled", "QQ 空间写入权限尚未开启。")
            state = self._load_state(account)
            if action == "status":
                return {"ok": True, "action": "status", "account": account,
                        "qzoneEnabled": policy.get("qzone_enabled") is True,
                        "allowWrite": policy.get("qzone_allow_write") is True,
                        "allowPublish": policy.get("qzone_allow_publish") is True,
                        "autonomous": policy.get("qzone_autonomous") is True,
                        "cookieReady": auth.qzone_cookie_ready}
            if action == "feed":
                return await self._read_feed(auth, args, state, call)
            if action == "posts":
                return await self._read_posts(auth, args, state, call)
            return await self._write(auth, action, args, policy, state, call)
        except _QzoneError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message, "sent": False}
        except Exception:
            # Never reflect SDK/OneBot exception text: it can include cookies.
            return {"ok": False, "error": "qzone_failed", "message": "QQ 空间操作未完成。", "sent": False}


__all__ = ["QzoneService"]
