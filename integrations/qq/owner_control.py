"""Loopback client for the desktop owner's QQ control endpoint.

The QQ gateway is deliberately only a transport here.  The desktop process
owns the real assistant context and tools; this client forwards a validated
owner event to that process over its loopback HTTP endpoint.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
import re
from pathlib import Path
from typing import Any, Callable, Mapping

import aiohttp


class OwnerControlError(RuntimeError):
    """Safe, typed failure for the loopback owner-control protocol.

    ``code`` and ``stage`` are deliberately bounded protocol fields.  The
    underlying exception, endpoint, headers, and response body never cross
    the QQ boundary.  ``outcome`` describes the side-effect boundary rather
    than whether the HTTP request itself succeeded:

    * ``not_started`` means no owner request was dispatched;
    * ``unknown`` means the POST may have reached the desktop process;
    * ``applied`` means the response contained a safe applied receipt.
    """

    _DEFAULT_CODE = "owner_backend_failed"
    _DEFAULT_STAGE = "owner_control"
    _DEFAULT_OUTCOME = "unknown"

    def __init__(self, *, code: str | None = None, stage: str | None = None,
                 outcome: str | None = None, request_id: str = "",
                 http_status: int | None = None) -> None:
        # Do not pass an implementation exception to RuntimeError: str(exc)
        # is consumed by a few old callers and must stay private-safe.
        super().__init__("")
        self.code = _safe_protocol_token(code or self._DEFAULT_CODE, self._DEFAULT_CODE)
        self.stage = _safe_protocol_token(stage or self._DEFAULT_STAGE, self._DEFAULT_STAGE)
        self.outcome = _safe_outcome(outcome or self._DEFAULT_OUTCOME)
        self.request_id = _safe_request_id(request_id)
        self.http_status = _safe_http_status(http_status)

    def to_public(self) -> dict[str, Any]:
        """Return only fields safe for a gateway audit event or reply."""

        payload: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "outcome": self.outcome,
        }
        if self.request_id:
            payload["requestId"] = self.request_id
        if self.http_status is not None:
            payload["httpStatus"] = self.http_status
        return payload


class OwnerControlUnavailable(OwnerControlError):
    """The endpoint/session could not be reached before the owner POST."""

    _DEFAULT_CODE = "owner_backend_unavailable"
    _DEFAULT_STAGE = "endpoint"
    _DEFAULT_OUTCOME = "not_started"


class OwnerControlBackendFailure(OwnerControlError):
    """The desktop process replied with a safe, non-success failure."""

    _DEFAULT_CODE = "owner_backend_failed"
    _DEFAULT_STAGE = "owner_chat"
    _DEFAULT_OUTCOME = "unknown"


class OwnerControlOutcomeUnknown(OwnerControlError):
    """The owner POST may have run, but no definitive response was read."""

    _DEFAULT_CODE = "owner_outcome_unknown"
    _DEFAULT_STAGE = "owner_chat_transport"
    _DEFAULT_OUTCOME = "unknown"


_SAFE_PROTOCOL_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:"
)
_SAFE_OUTCOMES = frozenset({
    "not_started", "unknown", "applied", "side_effect_applied",
    "side_effect_unknown", "backend_failed", "unavailable",
})


def _safe_protocol_token(value: Any, default: str, *, limit: int = 80) -> str:
    token = str(value or "").strip()
    if not token or len(token) > limit or any(char not in _SAFE_PROTOCOL_TOKEN_CHARS for char in token):
        return default
    return token


def _safe_outcome(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in _SAFE_OUTCOMES else "unknown"


def _safe_request_id(value: Any) -> str:
    return _safe_protocol_token(value, "", limit=180)


def _safe_http_status(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status if 100 <= status <= 599 else None


def _safe_delivery_state(value: Any) -> str:
    token = str(value or "").strip().lower()
    allowed = {"draft", "sending", "sent", "unknown", "failed", "rejected"}
    return token if token in allowed else ""


def _safe_id(value: Any, *, limit: int = 180) -> str:
    token = str(value or "").strip()
    if not token or len(token) > limit or not token.isascii():
        return ""
    return token


OWNER_RECEIPT_STATES = frozenset({"accepted", "sent", "unknown", "failed"})


def owner_receipt_key(account: str, sender: str, kind: str, target: str,
                      message_id: str) -> str:
    """Return the durable idempotency key for one owner event.

    The values are canonicalized to strings to match the HTTP envelope and
    are hashed before storage, so the receipt database contains no message
    text or credential material.
    """

    values = [str(account), str(sender), str(kind), str(target), str(message_id)]
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class OwnerReceiptStore:
    """Durable atomic claim and delivery receipt store for owner events.

    A first ``claim`` inserts a row in the conservative ``unknown`` state and
    returns ``True``.  Every later claim for the same five-tuple returns
    ``False`` (including after a process restart), so a request whose side
    effect may have happened is never replayed automatically.  The parent
    gateway advances the row to ``accepted``, ``sent`` or ``failed`` when it
    has a definitive result, and to ``unknown`` when the outcome is uncertain.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS owner_receipts (
        receipt_key TEXT PRIMARY KEY,
        account TEXT NOT NULL,
        sender TEXT NOT NULL,
        kind TEXT NOT NULL,
        target TEXT NOT NULL,
        message_id TEXT NOT NULL,
        state TEXT NOT NULL,
        request_id TEXT NOT NULL DEFAULT '',
        reply TEXT NOT NULL DEFAULT '',
        send_message_id TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL,
        updated REAL NOT NULL
    )
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None,
            check_same_thread=False,
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(self._SCHEMA)

    @staticmethod
    def key(account: str, sender: str, kind: str, target: str,
            message_id: str) -> str:
        return owner_receipt_key(account, sender, kind, target, message_id)

    def claim(self, account: str, sender: str, kind: str, target: str,
              message_id: str) -> bool:
        """Atomically claim an event; return true only for the first claim."""

        values = tuple(str(value) for value in
                       (account, sender, kind, target, message_id))
        receipt = owner_receipt_key(*values)
        now = time.time()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO owner_receipts
                       (receipt_key, account, sender, kind, target, message_id,
                        state, created, updated)
                       VALUES (?, ?, ?, ?, ?, ?, 'unknown', ?, ?)""",
                    (receipt, *values, now, now),
                )
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return cursor.rowcount == 1

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    def get_by_key(self, receipt: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM owner_receipts WHERE receipt_key=?", (str(receipt),)
            ).fetchone()
        return self._row(row)

    def get(self, account: str, sender: str, kind: str, target: str,
            message_id: str) -> dict[str, Any] | None:
        return self.get_by_key(owner_receipt_key(account, sender, kind, target, message_id))

    def set_state(self, receipt: str, state: str, *,
                  request_id: str | None = None, reply: str | None = None,
                  send_message_id: str | None = None) -> bool:
        """Persist a definitive/uncertain state and optional safe receipt data."""

        if state not in OWNER_RECEIPT_STATES:
            raise ValueError("invalid owner receipt state")
        assignments = ["state=?", "updated=?"]
        values: list[Any] = [state, time.time()]
        if request_id is not None:
            assignments.append("request_id=?")
            values.append(str(request_id))
        if reply is not None:
            assignments.append("reply=?")
            values.append(str(reply))
        if send_message_id is not None:
            assignments.append("send_message_id=?")
            values.append(str(send_message_id))
        values.append(str(receipt))
        with self._lock:
            cursor = self.db.execute(
                f"UPDATE owner_receipts SET {', '.join(assignments)} WHERE receipt_key=?",
                values,
            )
        return cursor.rowcount == 1

    def mark_state(self, account: str, sender: str, kind: str, target: str,
                   message_id: str, state: str, *,
                   request_id: str | None = None, reply: str | None = None,
                   send_message_id: str | None = None) -> bool:
        """Convenience form of :meth:`set_state` using the event tuple."""

        return self.set_state(
            owner_receipt_key(account, sender, kind, target, message_id), state,
            request_id=request_id, reply=reply,
            send_message_id=send_message_id,
        )

    def close(self) -> None:
        with self._lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    def __enter__(self) -> "OwnerReceiptStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def owner_message_text(event: dict[str, Any]) -> str:
    """Return current owner text and typed @ IDs, without quoted instructions.

    In particular, this does not use ``raw_message`` or conversation history:
     quoted messages and other segment descriptions must not
    become instructions for the desktop owner context.
    """

    message = event.get("message")
    if not isinstance(message, list):
        return ""
    from .identity import mention_text
    from .media import segment_description
    parts: list[str] = []
    for segment in message:
        if isinstance(segment, dict) and segment.get('type') == 'at':
            parts.append(mention_text(segment))
            continue
        if not isinstance(segment, dict):
            continue
        if segment.get("type") in {"image", "mface", "face"}:
            # Keep a bounded, non-URL description in the owner正文.  The
            # gateway sends decoded pixels separately; this marker means an
            # image-only or mention-only event is still a valid owner turn.
            description = segment_description(segment)
            if description:
                parts.append(description)
            continue
        if segment.get("type") != "text":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        text = data.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()


def exact_account_mention(event: dict[str, Any], account_id: str) -> bool:
    """Whether the event has an exact OneBot ``at`` segment for this account."""

    message = event.get("message")
    if not isinstance(message, list):
        return False
    expected = str(account_id)
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "at":
            continue
        data = segment.get("data")
        if isinstance(data, dict) and str(data.get("qq", "")) == expected:
            return True
    return False


def event_sender_id(event: dict[str, Any]) -> str | None:
    """Resolve the real sender ID, rejecting conflicting OneBot fields."""

    top = event.get("user_id")
    sender = event.get("sender")
    nested = sender.get("user_id") if isinstance(sender, dict) else None

    values = [value for value in (top, nested) if value is not None]
    if not values:
        return None
    normalized = [str(value) for value in values]
    if any(not value.isascii() or not value.isdigit() or not value for value in normalized):
        return None
    if len(set(normalized)) != 1:
        return None
    return normalized[0]


class OwnerControlClient:
    """Forward owner commands to the already-running desktop process."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        timeout: float = 240.0,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.timeout = float(timeout)
        self.session_factory = session_factory or aiohttp.ClientSession

    def _endpoint(self) -> str:
        path = self.runtime_dir / "owner-backend.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise OwnerControlUnavailable(stage="endpoint_announcement") from None
        if not isinstance(raw, dict):
            raise OwnerControlUnavailable(stage="endpoint_announcement")

        # The file is an endpoint announcement, but never trust a host value
        # to escape the loopback boundary.  The expected writer only stores
        # port/pid; accepting the explicit loopback spelling keeps the check
        # unambiguous if a future writer includes host metadata.
        host = raw.get("host", "127.0.0.1")
        if host != "127.0.0.1":
            raise OwnerControlUnavailable(stage="endpoint_host")
        port = raw.get("port")
        pid = raw.get("pid")
        if isinstance(port, bool) or isinstance(pid, bool):
            raise OwnerControlUnavailable(stage="endpoint_identity")
        try:
            port_number = int(port)
            pid_number = int(pid)
        except (TypeError, ValueError):
            raise OwnerControlUnavailable(stage="endpoint_identity") from None
        if not 1 <= port_number <= 65535 or pid_number <= 0:
            raise OwnerControlUnavailable(stage="endpoint_identity")
        return f"http://127.0.0.1:{port_number}"

    @staticmethod
    def _safe_response(payload: Any) -> dict[str, Any]:
        """Keep the bounded owner result contract and discard error details.

        The owner bridge needs enough information to distinguish an applied
        QQ receipt from a local persistence warning.  Only identifiers and
        enumerated status fields survive this boundary; arbitrary error text,
        stack traces, headers, paths, and payloads do not.
        """

        if not isinstance(payload, dict):
            raise OwnerControlBackendFailure(stage="owner_chat_response")
        reply = payload.get("reply")
        request_id = payload.get("requestId")
        route = payload.get("route")
        result: dict[str, Any] = {
            "ok": payload.get("ok") is True,
            "reply": reply if isinstance(reply, str) else "",
            "requestId": _safe_request_id(request_id),
            "route": route if route in {"group_chat", "group_control", "owner_task"} else "",
        }
        ids = payload.get('imageIds')
        account = str(payload.get('galleryAccount') or '')
        if (route not in {'group_chat', 'group_control'} and isinstance(ids, list)
                and 1 <= len(ids) <= 2 and re.fullmatch(r'[0-9]{5,20}', account)
                and all(isinstance(item, str) and re.fullmatch(r'[a-f0-9]{64}', item) for item in ids)):
            result.update(imageIds=list(ids), galleryAccount=account)
        if route == 'group_control':
            from .owner_delivery import control_ack_text
            if control_ack_text(payload.get('controlAck')):
                result['controlAck'] = payload['controlAck']
            else:
                raise OwnerControlBackendFailure(stage='owner_control_receipt')
        outcome = _safe_outcome(payload.get("outcome")) if payload.get("outcome") else ""
        if outcome:
            result["outcome"] = outcome
        delivery_state = _safe_delivery_state(payload.get("deliveryState"))
        if delivery_state:
            result["deliveryState"] = delivery_state
        message_id = _safe_id(payload.get("messageId"))
        if message_id:
            result["messageId"] = message_id
        for key in ("errorCode", "errorStage"):
            token = _safe_protocol_token(payload.get(key), "")
            if token:
                result[key] = token
        if payload.get("persistenceWarning") is True:
            result["persistenceWarning"] = True
        if payload.get("attempted") is True:
            result["attempted"] = True

        # Preserve a compact delivery receipt for the gateway audit path.  A
        # malformed or oversized object is ignored rather than forwarded.
        receipt = payload.get("deliveryReceipt")
        if isinstance(receipt, Mapping):
            safe_receipt: dict[str, Any] = {}
            for key in (
                "tool", "action", "status", "deliveryState", "messageId",
                "draftId", "targetType", "targetId", "account", "sent",
                "sentPartCount", "totalParts", "remainingPartCount",
            ):
                if key not in receipt:
                    continue
                value = receipt.get(key)
                if key in {"status", "deliveryState"}:
                    value = _safe_delivery_state(value) or _safe_protocol_token(value, "")
                elif key in {"tool", "action", "targetType"}:
                    value = _safe_protocol_token(value, "")
                elif key in {"messageId", "draftId", "targetId", "account"}:
                    value = _safe_id(value)
                elif key == "sent":
                    value = value is True
                elif key in {"sentPartCount", "totalParts", "remainingPartCount"}:
                    value = value if isinstance(value, int) and not isinstance(value, bool) else None
                if value not in (None, ""):
                    safe_receipt[key] = value
            if safe_receipt:
                result["deliveryReceipt"] = safe_receipt
        return result

    async def chat(
        self,
        *,
        control_token: str,
        account: str,
        sender: str,
        kind: str,
        target: str,
        message_id: str,
        text: str,
        identity_context: dict | None = None,
        message_context: dict | None = None,
        images: list[dict] | None = None,
        media_notes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Perform one GET-session plus one non-retried owner POST.

        A POST can run tools or change local settings, so this method does not
        retry it.  Endpoint/session transport failures are distinct from
        server-side pipeline failures and from a POST whose outcome cannot be
        read.  The exception fields are safe protocol codes only.
        """

        if not isinstance(control_token, str) or not control_token:
            raise OwnerControlBackendFailure(code="owner_gateway_token_missing", stage="request_validation",
                                             outcome="not_started")
        base = self._endpoint()
        body = {
            "account": str(account),
            "sender": str(sender),
            "kind": str(kind),
            "target": str(target),
            "messageId": str(message_id),
            "text": str(text),
        }
        if identity_context is not None:
            body['identityContext'] = identity_context
        if message_context is not None:
            body['messageContext'] = message_context
        if images is not None:
            body['images'] = images
        if media_notes is not None:
            body['mediaNotes'] = media_notes
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        post_started = False
        try:
            async with self.session_factory(timeout=timeout) as session:
                try:
                    async with session.get(
                        f"{base}/api/local-session",
                        headers={"X-Amadeus-Client": "desktop"},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise OwnerControlBackendFailure(
                                code="owner_session_rejected", stage="session_http",
                                outcome="not_started", http_status=response.status,
                            )
                        try:
                            session_payload = await response.json(content_type=None)
                        except Exception:
                            raise OwnerControlBackendFailure(
                                code="owner_session_invalid", stage="session_response",
                                outcome="not_started", http_status=response.status,
                            ) from None
                        if not isinstance(session_payload, dict):
                            raise OwnerControlBackendFailure(
                                code="owner_session_invalid", stage="session_response",
                                outcome="not_started", http_status=response.status,
                            )
                        session_token = session_payload.get("token")
                        if not isinstance(session_token, str) or not session_token:
                            raise OwnerControlBackendFailure(
                                code="owner_session_invalid", stage="session_response",
                                outcome="not_started", http_status=response.status,
                            )
                except OwnerControlError:
                    raise
                except Exception:
                    raise OwnerControlUnavailable(stage="session_transport") from None

                post_started = True
                try:
                    async with session.post(
                        f"{base}/api/qq/owner-chat",
                        headers={
                            "Content-Type": "application/json",
                            "X-Amadeus-Session": session_token,
                            "X-Amadeus-Qq-Gateway": control_token,
                        },
                        json=body,
                        allow_redirects=False,
                    ) as response:
                        if response.status < 200 or response.status >= 300:
                            raise await self._post_failure(response)
                        try:
                            result_payload = await response.json(content_type=None)
                        except Exception:
                            raise OwnerControlOutcomeUnknown(
                                code="owner_response_invalid", stage="owner_chat_response",
                                http_status=response.status,
                            ) from None
                except OwnerControlError:
                    raise
                except Exception:
                    # A timeout/reset after POST dispatch cannot establish
                    # whether the desktop executed a QQ side effect.
                    raise OwnerControlOutcomeUnknown from None
        except OwnerControlUnavailable:
            raise
        except OwnerControlError:
            raise
        except Exception:
            # Session construction failures are transport failures; once the
            # POST was entered, conservatively report an unknown outcome.
            if post_started:
                raise OwnerControlOutcomeUnknown from None
            raise OwnerControlUnavailable(stage="session_transport") from None
        return self._safe_response(result_payload)

    @classmethod
    async def _post_failure(cls, response: Any) -> OwnerControlError:
        """Map a non-2xx response to a safe typed owner-control error."""

        payload: Mapping[str, Any] = {}
        try:
            raw = await response.json(content_type=None)
            if isinstance(raw, Mapping):
                payload = raw
        except Exception:
            payload = {}

        code = _safe_protocol_token(
            payload.get("errorCode") or payload.get("code"),
            "owner_backend_failed",
        )
        stage = _safe_protocol_token(
            payload.get("errorStage") or payload.get("stage"),
            "owner_chat_http",
        )
        request_id = _safe_request_id(payload.get("requestId"))
        outcome = _safe_outcome(payload.get("outcome"))
        attempted = payload.get("attempted") is True
        delivery_state = _safe_delivery_state(payload.get("deliveryState"))
        if (
            outcome == "applied"
            or attempted
            or delivery_state in {"sending", "sent", "unknown"}
            or int(response.status) >= 500
        ):
            return OwnerControlOutcomeUnknown(
                code=code if code != "owner_backend_failed" else "owner_outcome_unknown",
                stage=stage,
                outcome="unknown" if outcome != "applied" else "applied",
                request_id=request_id,
                http_status=response.status,
            )
        return OwnerControlBackendFailure(
            code=code,
            stage=stage,
            outcome="not_started" if outcome == "not_started" else "unknown",
            request_id=request_id,
            http_status=response.status,
        )
