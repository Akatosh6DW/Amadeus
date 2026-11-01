"""Narrow owner-side access to the separately running QQ permission gateway."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
from collections.abc import Mapping

from integrations.qq.policy import policy_revision, CONVERSATION_FIELDS, conversation_policy, voice_enabled


# MediaLibrary IDs are content-addressed SHA-256 values.  Keep the adapter
# stricter than the gateway so a model cannot turn an arbitrary opaque value
# into a cross-account file lookup.
# regex-purpose: resource
_QQ_ASSET_ID = re.compile(r"[0-9a-f]{64}\Z")
_DELIVERY_PART_FIELDS = ("sentPartCount", "totalParts", "remainingPartCount")


class _QqAccountChanged(ValueError):
    pass


def _qq_commit_intent_allowed(args: Mapping) -> bool:
    """Trust the host's already-authorized QQ call when the marker is absent.

    The normal executor reaches this adapter only after permission and
    planning checks.  ``commitIntent`` remains a typed override: an explicit
    ``False`` still rejects the mutation, while an omitted marker uses that
    host boundary as the default.  Other non-boolean values are invalid
    rather than truthy authorization.
    """
    value = args.get("commitIntent")
    return value is None or value is True


def _qq_attachment_ids(value) -> list[str] | None:
    """Validate opaque main-chat attachment IDs without treating them as paths."""
    if value is None:
        return []
    if not isinstance(value, list) or not value or len(value) > 2:
        return None
    result = []
    for item in value:
        if not isinstance(item, str):
            return None
        item = item.strip()
        if (not item or len(item) > 200 or "/" in item or "\\" in item
                or any(ord(char) < 32 or ord(char) == 127 for char in item)
                or item in result):
            return None
        result.append(item)
    return result


def _qq_image_ids(value) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or not value or len(value) > 2:
        return None
    result = []
    for item in value:
        item = str(item).strip() if isinstance(item, (str, int)) and not isinstance(item, bool) else ""
        if not _QQ_ASSET_ID.fullmatch(item) or item in result:
            return None
        result.append(item)
    return result


def _delivery_part_counts(row: Mapping) -> dict:
    """Keep bounded split-delivery counters from a gateway draft receipt."""
    result = {}
    for key in _DELIVERY_PART_FIELDS:
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000:
            result[key] = value
    return result


def _same_turn_qq_draft(row: Mapping | None, draft_id: str, request_id: str) -> bool:
    """Require a later tool turn before a model can approve its own draft."""
    return (
        isinstance(row, Mapping)
        and str(row.get("id") or "") == draft_id
        and row.get("state") == "draft"
        and bool(request_id)
        and str(row.get("creation_request_id") or "") == request_id
    )


def _qq_new_turn_required_receipt(row: Mapping, *, request_id: str,
                                  target_type: str, target_id: str,
                                  text: str, image_ids: list[str]) -> dict:
    part_counts = _delivery_part_counts(row)
    return {
        "ok": False,
        "error": "qq_approval_requires_new_turn",
        "attempted": False,
        "sent": False,
        "deliveryState": row.get("state", "draft"),
        "draftId": str(row.get("id") or ""),
        "account": str(row.get("account") or ""),
        "targetType": target_type,
        "targetId": target_id,
        "text": text,
        "imageIds": image_ids,
        "messageId": str(row.get("message_id") or ""),
        "requiresApproval": True,
        "approvalBoundary": {
            "draftRequestId": str(row.get("creation_request_id") or ""),
            "approvalRequestId": request_id,
            "sameTurn": True,
            "requiresNewTurn": True,
        },
        **part_counts,
        "message": "草稿已生成，本轮不能自行批准；请等待主人在后续独立回合明确确认，或在 QQ 动态中确认发送。",
    }


def _safe_media_result(result: dict) -> dict:
    safe = {key: result[key] for key in (
        "ok", "error", "message", "account", "sent", "attempted", "pendingFinalReply",
        "selected_image_ids", "imported", "truncated",
    ) if key in result}
    # Keep one bounded metadata list, never image bytes or local paths.
    raw_items = result.get("items")
    if isinstance(raw_items, list):
        items = []
        for asset in raw_items[:20]:
            if not isinstance(asset, Mapping):
                continue
            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or not _QQ_ASSET_ID.fullmatch(asset_id):
                continue
            row = {"id": asset_id}
            for key in (
                "name", "kind", "tags", "description", "width", "height", "animated",
                "format", "mimeType", "size", "createdAt", "updatedAt",
            ):
                if key in asset:
                    row[key] = asset[key]
            if isinstance(row.get("tags"), list):
                row["tags"] = [str(value)[:60] for value in row["tags"]
                               if isinstance(value, (str, int))][:20]
            else:
                row.pop("tags", None)
            items.append(row)
        safe["items"] = items
    return safe


def execute_qq_tool(name: str, settings: dict, args: dict, *, data_root: Path, sync_model=None) -> dict:
    if not settings.get("permissionQqControl"):
        return {"ok": False, "error": "permission_denied", "permission": "permissionQqControl",
            "attempted": False, "message": "助手的 QQ 管理权限尚未开启。请在设置的 QQ 页启用；这不表示 QQ 网关未连接，本次没有查询网关。"}
    config_path = data_root / "qq-agent" / "gateway.json"
    if not config_path.exists():
        return {"ok": False, "error": "qq_not_configured", "message": "QQ 独立智能体尚未部署，请先配置 QQ 代管面板。"}
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    from backend.tools.trust_context import bound_qq_account
    configured_account = str(config.get("policy", {}).get("account_id") or "")
    host_account = str(bound_qq_account() or "")
    supplied_account = str(args.get("_expectedAccount") or args.get("expectedAccount") or "")
    # A host-bound account and an explicitly supplied account are independent
    # claims.  A stale explicit value must not be hidden by a newer context
    # binding; either mismatch is a hard account change.
    if ((host_account and host_account != configured_account)
            or (supplied_account and supplied_account != configured_account)):
        return {"ok": False, "error": "qq_account_changed", "attempted": False, "sent": False,
                "message": "当前 QQ 账号与任务保存的账号不一致，本次没有发送，请切回原账号或修改任务目标。"}
    # The configured gateway account is the host default when no task/receipt
    # binding is present.  The gateway re-checks its live login and policy;
    # explicit or stale task accounts still fail above instead of being hidden
    # by this fallback.
    expected_account = host_account or supplied_account or configured_account
    base = "http://127.0.0.1:"+str(int(config["control_port"]))
    def call(path: str, body=None):
        if (expected_account and isinstance(body, dict) and isinstance(body.get("expected_policy"), dict)
                and str(body["expected_policy"].get("account_id") or "") != expected_account):
            raise _QqAccountChanged("qq_account_changed")
        request = urllib.request.Request(base+"/api/"+path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Authorization":"Bearer "+config["control_token"], "Content-Type":"application/json"})
        # Local control must never be forwarded through a configured cloud proxy.
        timeout = 3 if body is None else 420 if path == "delegate" else 240 if path in {"send", "approve"} else 60 if path == "qzone" else 20
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
            return json.load(response)
    delivery_attempted = False
    try:
        if name == "qq_media":
            action = str(args.get("action") or "").strip()
            if action not in {"search", "get"}:
                return {"ok": False, "error": "invalid_qq_media_action", "attempted": False}
            configured_account = str(config.get("policy", {}).get("account_id") or "")
            bound_account = expected_account
            if not bound_account:
                return {"ok": False, "error": "qq_account_required", "attempted": False,
                        "message": "QQ 图片操作缺少当前账号绑定。"}
            expected = str(bound_account)
            if not expected or expected != configured_account:
                return {"ok": False, "error": "qq_account_changed", "attempted": False,
                        "message": "QQ 账号已经变化，请重新读取当前账号及图片图库。"}
            payload = {"expectedAccount": expected, "action": action}
            if action == "search":
                payload["query"] = str(args.get("query") or "")[:160]
                try:
                    payload["limit"] = max(1, min(20, int(args.get("limit", 20))))
                except (TypeError, ValueError):
                    return {"ok": False, "error": "invalid_qq_media_limit", "attempted": False}
            else:
                image_ids = _qq_image_ids(args.get("imageIds"))
                if not image_ids:
                    return {"ok": False, "error": "invalid_qq_image_ids", "attempted": False,
                            "message": "本轮最多选择两张不重复的真实 QQ 图片。"}
                # /api/media calls the formal MediaLibrary parameter assetIds;
                # imageIds remains the model-facing name used by qq_media.
                payload["assetIds"] = image_ids
            return _safe_media_result(call("media", payload))
        if name in {'qq_space', 'qq_space_write'}:
            action = args.get('action')
            writing = name == 'qq_space_write'
            if action not in ({'like', 'comment', 'publish'} if writing else {'status', 'feed', 'posts'}):
                return {'ok': False, 'error': 'invalid_qzone_action'}
            if writing and not _qq_commit_intent_allowed(args):
                return {'ok': False, 'error': 'explicit_intent_required'}
            account = str(config.get('policy', {}).get('account_id') or '')
            expected = str(expected_account or account)
            if not expected:
                return {'ok': False, 'error': 'qq_account_required', 'attempted': False,
                        'message': '尚未配置 QQ 账号，本次未提交空间操作'}
            supplied = str(args.get('expectedAccount') or '')
            if expected != account or (supplied and supplied != expected):
                return {'ok': False, 'error': 'qq_account_changed', 'attempted': False,
                        'message': '当前 QQ 账号与本次任务绑定的账号不一致，本次未提交空间操作'}
            payload = {k: args[k] for k in ('action', 'count', 'page', 'targetQQ', 'postRef', 'content', 'visibility') if k in args}
            if writing:
                request_id = str(args.get('_requestId') or '')
                if not request_id:
                    return {'ok': False, 'error': 'request_id_required'}
                payload['requestKey'] = hashlib.sha256((request_id + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode('utf-8')).hexdigest()
            delivery_attempted = writing
            return call('qzone', {'expectedAccount': expected, 'args': payload})
        if name in {"qq_identity", "qq_remember_alias"}:
            writing = name == "qq_remember_alias"
            configured_account = str(config.get("policy", {}).get("account_id") or "")
            if writing and not _qq_commit_intent_allowed(args):
                return {"ok": False, "error": "explicit_intent_required", "attempted": False,
                        "message": "保存 QQ 别名需要主人明确请求、write 授权和 commitIntent=true。"}
            expected = str(expected_account or configured_account)
            if not expected:
                return {"ok": False, "error": "qq_account_required", "attempted": False,
                        "message": "身份查询缺少当前 QQ 账号绑定，先读取 QQ 状态后重试。"}
            if expected != configured_account:
                return {'ok': False, 'error': 'qq_account_changed', 'attempted': False,
                        'message': 'QQ 账号已经变化，请重新核对当前账号及对象。'}
            if writing:
                user_id = str(args.get("userId") or "").strip()
                alias = str(args.get("alias") or "").strip()
                reason = str(args.get("reason") or args.get("_requestText") or "").strip()[:1000]
                missing = [field for field, value in (("userId", user_id), ("alias", alias), ("reason", reason)) if not value]
                if missing:
                    return {"ok": False, "error": "identity_arguments_required", "attempted": False,
                            "argumentError": {"missingFields": missing},
                            "message": "尚未保存。需要明确的 userId、alias 和主人说明。"}
            payload = {"expectedAccount": expected,
                       "action": "remember_alias" if writing else "lookup"}
            for key in ("targetType", "targetId", "userId", "query"):
                if key in args and args[key] not in (None, ""):
                    payload[key] = str(args[key])
            if writing:
                payload["alias"] = alias
                payload["reason"] = reason
            scope = args.get("_identityScope")
            # The worker supplies a gateway-signed scope. Never accept a model
            # authored scope field; local owner calls intentionally omit it.
            if isinstance(scope, Mapping):
                payload["scope"] = dict(scope)
            result = call("identity", payload)
            if writing and isinstance(result, dict):
                # Keep the write receipt stable even when an older gateway
                # omits fields that are implied by this successful request.
                result = dict(result)
                result.setdefault("account", expected)
                result.setdefault("userId", user_id)
                result.setdefault("alias", alias)
                result.setdefault("sent", False)
                if result.get("ok") is True:
                    result.setdefault("remembered", True)
            return result
        if name == "qq_person_memory":
            action = str(args.get("action") or "").strip()
            if action not in {"remember", "forget"}:
                return {"ok": False, "error": "invalid_person_memory_action", "attempted": False}
            if not _qq_commit_intent_allowed(args):
                return {"ok": False, "error": "explicit_intent_required", "attempted": False,
                        "message": "保存或删除人物认知需要主人明确请求、write 授权和 commitIntent=true。"}
            configured_account = str(config.get("policy", {}).get("account_id") or "")
            bound_account = expected_account
            if not bound_account:
                return {"ok": False, "error": "qq_account_required", "attempted": False,
                        "message": "人物认知操作缺少当前 QQ 账号绑定。"}
            expected = str(bound_account)
            if expected != configured_account:
                return {"ok": False, "error": "qq_account_changed", "attempted": False,
                        "message": "QQ 账号已经变化，请重新核对当前账号及对象。"}
            required = [("targetType", args.get("targetType")), ("targetId", args.get("targetId")),
                        ("userId", args.get("userId")), ("reason", args.get("reason") or args.get("_requestText"))]
            if action == "remember":
                required.extend([("topic", args.get("topic")), ("value", args.get("value")),
                                 ("sourceRole", args.get("sourceRole"))])
            else:
                required.append(("topic_or_memoryId", args.get("topic") or args.get("memoryId")))
            missing = [field for field, value in required if not str(value or "").strip()]
            if missing:
                return {"ok": False, "error": "person_memory_arguments_required", "attempted": False,
                        "argumentError": {"missingFields": missing},
                        "message": "人物认知操作缺少明确会话、对象、主题、内容或理由。"}
            payload = {"expectedAccount": expected, "action": "memory_" + action,
                       "targetType": str(args.get("targetType")), "targetId": str(args.get("targetId")),
                       "userId": str(args.get("userId")), "reason": str(args.get("reason") or args.get("_requestText"))[:1000]}
            for key in ("topic", "value", "memoryId", "sourceRole", "certainty"):
                if key in args and args[key] not in (None, ""):
                    payload[key] = str(args[key])
            if "shared" in args:
                payload["shared"] = args["shared"]
            scope = args.get("_identityScope")
            if isinstance(scope, Mapping):
                payload["scope"] = dict(scope)
            for hidden, wire in (("_sourceMessageId", "sourceMessageId"),
                                 ("_sourceTime", "sourceTime"),
                                 ("_sourceSenderId", "sourceSenderId")):
                if hidden in args and args[hidden] not in (None, ""):
                    payload[wire] = args[hidden]
            result = call("identity", payload)
            if not isinstance(result, dict):
                return {"ok": False, "error": "invalid_person_memory_receipt", "attempted": True}
            # Never pass a signed scope or an arbitrary gateway payload back to
            # the model.  The memory itself is already bounded by the gateway.
            safe = {key: result[key] for key in (
                "ok", "error", "message", "account", "targetType", "targetId", "userId",
                "remembered", "forgotten", "sent", "memory", "attempted", "argumentError",
            ) if key in result}
            if isinstance(safe.get("memory"), Mapping):
                safe["memory"] = {key: safe["memory"][key] for key in (
                    "memoryId", "targetType", "targetId", "userId", "topic", "value",
                    "sourceRole", "certainty", "sourceSenderId", "sourceMessageId",
                    "sourceTime", "shared", "updatedAt",
                ) if key in safe["memory"]}
            return safe
        if name == "qq_history":
            payload = {key: args[key] for key in ("targetType", "targetId", "limit", "cursor") if key in args}
            if args.get("_expectedAccount"):
                payload["expectedAccount"] = str(args["_expectedAccount"])
            return call("history", payload)
        if name in {"qq_status", "qq_activity"}:
            result = call("status")
            actions = []
            for item in result["sends"][:10]:
                row = {k: item.get(k) for k in ("id", "account", "kind", "target", "targetName", "targetRemark", "actor", "reason", "state", "message_id", "detail", "created", "updated", "imageIds", "reply_format", "speech_language", *_DELIVERY_PART_FIELDS)}
                content = "".join(str(segment.get("data", {}).get("text", "")) for segment in item.get("message", []))
                row["text"] = content if name == "qq_activity" else content[:500]
                row["textTruncated"] = len(row["text"]) < len(content)
                actions.append(row)
            return {"ok": True, "connected": result["connected"], "connectionMessage": result["error"],
                "policy": result["policy"], "policyRevision": policy_revision(result["policy"]),
                "contacts": result.get("contacts", []), "lifecycle": result.get("lifecycle", {}),
                "contactsComplete": result.get("contactsComplete", False),
                "replyAgent": "Amadeus" if result.get("reply_agent") == "amadeus" else "AstrBot",
                "activitySummary": {"scope": "最近最多100条操作记录",
                    "sent": sum(item.get("state") == "sent" and bool(item.get("message_id")) for item in result["sends"]),
                    "draft": sum(item.get("state") == "draft" for item in result["sends"]),
                    "failed": sum(item.get("state") == "failed" for item in result["sends"]),
                    "unknown": sum(item.get("state") == "unknown" or item.get("state") == "sent" and not item.get("message_id") for item in result["sends"])},
                "recentActions": actions,
                "recentEvents": result.get("events", [])[:10],
                "capabilities": {"read": ["连接状态", "授权范围", "近期草稿与发送回执"],
                    "manage": ["configure", "delegate", "send", "draft", "approve", "pause", "resume", "mute", "unmute", "stop"], "historyTool": "qq_history",
                    "configureFields": ["account_role", "owner_ids", "owner_control_enabled", "owner_control_in_groups", "owner_developer_mode", "qzone_enabled", "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous", "qzone_interval_minutes", "qzone_instructions", "mode", "private_ids", "group_ids", "reply_format", "voice_reply_percent", "speech_language", "reply_instructions", "reply_delay_seconds", "reply_delay_mode", "reply_bubble_chars", "max_sends_per_hour", "minimum_interval_seconds", "muted_private_ids", "muted_group_ids", "background_enabled", "history_messages", "compact_reply", "reply_identity", "reply_style", "group_triggers", "group_keywords"],
                    "conversationConfigure": {"target": "targetType + targetId", "fields": sorted(CONVERSATION_FIELDS), "inherit": "字段设 null 恢复默认；全局暂停优先"},
                    "modes": {"paused": "暂停", "review": "主人确认后发送", "auto": "自动回复白名单来信"},
                    "setupLocation": "设置 → QQ", "requiresOwnerIntent": True},
                "message": "QQ 支持主动发送，不必等待新来信。主人要求告诉某人一件事时使用 delegate+instruction，目标子对话会读取历史再组织回复；发送指定完整原文才用 send+text；auto 模式直接发送，review 模式生成待确认草稿，明确确认后可用 approve。draft 仅保存草稿，不会等来信自动发出。configure 需传本次 policyRevision 作为 expectedRevision；delegate/send/approve 可省略版本，适配器会读取并原子核对当前配置；仍需明确授权。只能依据 sent 且有 message_id 声称已发送，unknown 必须告知用户核对。"}
        if not _qq_commit_intent_allowed(args):
            return {"ok": False, "error": "explicit_intent_required"}
        # The trusted owner request already states the purpose. A redundant
        # model-authored audit paraphrase must not block an authorized action.
        reason = str(args.get("reason") or (args.get("instruction") if args.get("action") == "delegate" else "") or args.get("_requestText") or "").strip()[:1000]
        if not reason:
            return {"ok": False, "error": "explanation_required", "attempted": False,
                    "argumentError": {"missingFields": ["reason"]}, "requiresClarification": False,
                    "message": "尚未调用 QQ，未发送。根据本轮已经授权的请求补齐 reason，再调用同一动作；不要只承诺稍后重试。"}
        action = args.get("action")
        if "speechLanguage" in args and (action not in {"delegate", "send", "draft"} or not isinstance(args["speechLanguage"], str) or args["speechLanguage"] not in {"zh-CN", "ja-JP"}):
            return {"ok": False, "error": "invalid_qq_speech_language", "attempted": False,
                    "message": "本次语音语言仅用于 delegate/send/draft，值为 zh-CN 或 ja-JP；长期修改请用 configure 的 policy.speech_language。"}
        if action == "configure":
            patch = args.get("policy")
            expected = args.get("expectedPolicy")
            revision = args.get("expectedRevision")
            if not isinstance(patch, dict) or not patch or set(patch) - {"mode", "account_role", "owner_ids", "owner_control_enabled", "owner_control_in_groups", "owner_developer_mode", "qzone_enabled", "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous", "qzone_interval_minutes", "qzone_instructions", "private_ids", "group_ids", "reply_format", "voice_reply_percent", "speech_language", "reply_instructions", "reply_delay_seconds", "reply_delay_mode", "reply_bubble_chars", "max_sends_per_hour", "minimum_interval_seconds", "muted_private_ids", "muted_group_ids", "background_enabled", "history_messages", "compact_reply", "reply_identity", "reply_style", "group_triggers", "group_keywords"}:
                return {"ok": False, "error": "invalid_qq_policy", "message": "只允许修改回复设置、触发条件、每小时发送上限和好友、群白名单。"}
            if not isinstance(expected, dict) and not isinstance(revision, str):
                return {"ok": False, "error": "qq_policy_snapshot_required", "message": "尚未修改。先调用 qq_status，再将 policyRevision 原样作为 args.expectedRevision 重试；也可传完整 expectedPolicy。"}
            current = call("status")
            if isinstance(revision, str):
                if revision != policy_revision(current["policy"]):
                    return {"ok": False, "error": "qq_policy_changed", "message": "QQ 配置已变化，请重新读取状态后再修改。"}
                expected = current["policy"]
            if current["policy"] != expected:
                return {"ok": False, "error": "qq_policy_changed", "message": "QQ 配置已变化，请重新读取状态后再修改。"}
            from integrations.qq.policy import normalize_policy
            try:
                if 'targetType' in args or 'targetId' in args:
                    kind, target = args.get('targetType'), str(args.get('targetId', ''))
                    saved_targets = expected.get(kind + '_ids', []) if kind in {'private', 'group'} else []
                    owner_target = kind == 'private' and target in expected.get('owner_ids', [])
                    if target not in saved_targets and not owner_target:
                        raise ValueError('请先把该联系人或群加入已保存名单，或将本人 QQ 号保存为主人账号')
                    if set(patch) - CONVERSATION_FIELDS:
                        raise ValueError('单独会话只能修改回复方式、身份、要求、间隔、上下文、排版和群聊触发；暂停请用 mute')
                    overrides = dict(expected.get('conversation_overrides', {}))
                    key = f'{kind}:{target}'
                    overrides[key] = {**overrides.get(key, {}), **patch}
                    policy = normalize_policy({**expected, 'conversation_overrides': overrides})
                else:
                    policy = normalize_policy({**expected, **patch})
            except (ValueError, TypeError) as exc:
                return {"ok": False, "error": "invalid_qq_policy", "message": str(exc)}
            scope_changed = any(policy[key] != expected[key] for key in ("mode", "private_ids", "group_ids"))
            if scope_changed and policy["mode"] != "paused" and not current["connected"]:
                return {"ok": False, "error": "qq_not_connected", "message": "先在设置 QQ 页确认登录账号和连接，再启用回复。"}
            if sync_model is not None and {'reply_format', 'voice_reply_percent', 'speech_language'}.intersection(patch) and (
                    voice_enabled(policy) or any(voice_enabled(conversation_policy(policy, kind, target))
                        for kind in ('private', 'group') for target in (
                            policy[kind + '_ids'] + (policy['owner_ids'] if kind == 'private' else [])))):
                sync_model()
            result = call("config", {"policy": policy, "expected_policy": expected,
                "actor": "Amadeus", "reason": reason})
            return {"ok": True, "policy": result["policy"], "previousPolicy": expected,
                "explanation": reason, "sent": False,
                "message": "配置已保存。向主人简短总结实际修改的回复设置、发送上限或范围；详情按需展开，没有因此发送消息。"}
        if action in {"mute", "unmute"}:
            payload = {"action": action, "targetType": args.get("targetType"), "targetId": str(args.get("targetId", "")), "reason": reason}
            if args.get("_expectedAccount"):
                payload["expectedAccount"] = args["_expectedAccount"]
            return {**call("conversation", payload), "message": "只调整该会话的回复开关，已保存名单和其他会话保持不变；全局暂停时需另行恢复全局。"}
        if action in {"pause", "resume", "stop"}:
            result = call("control", {"action":action, "reason":reason})
            return {"ok":True, **result, "message": "向主人简短总结本次暂停或恢复的结果；需要时再展开账号和范围。"}
        if action == "delegate":
            revision, request_id = args.get("expectedRevision"), str(args.get("_requestId", ""))
            if not request_id:
                return {"ok": False, "error": "qq_request_id_required", "attempted": False,
                        "message": "委托请求缺少本轮标识，尚未发送。"}
            current = call("status")
            if revision and revision != policy_revision(current["policy"]):
                return {"ok": False, "error": "qq_policy_changed", "message": "QQ 配置已变化，请重新读取状态。"}
            delivery_attempted = True
            result = call("delegate", {"expected_policy": current["policy"], "request_id": request_id,
                "targetType": args.get("targetType"), "targetId": str(args.get("targetId", "")),
                "instruction": args.get("instruction"), "reason": reason,
                **({"speechLanguage": args["speechLanguage"]} if "speechLanguage" in args else {})})
            row = result["draft"]
            message = json.loads(row["message"]) if isinstance(row["message"], str) else row["message"]
            text = "".join(item.get("data", {}).get("text", "") for item in message)
            sent = row["state"] == "sent" and bool(row.get("message_id"))
            part_counts = _delivery_part_counts(row)
            partial = part_counts.get("sentPartCount", 0) > 0 and row["state"] != "sent"
            identity = result.get("replyIdentity", current["policy"].get("reply_identity", "assistant"))
            return {"ok": sent or row["state"] == "draft", "sent": sent, "deliveryState": row["state"],
                "draftId": row["id"], "messageId": row.get("message_id") or "", "text": text,
                "targetType": row["kind"], "targetId": row["target"], "account": row["account"],
                **part_counts,
                "requiresApproval": row["state"] == "draft", "historyCount": result.get("historyCount"),
                "replyIdentity": identity, "replyFormat": row.get('reply_format', 'text'), "speechLanguage": row.get('speech_language', 'inherit'), "textSpeaker": "OWNER" if identity == "owner" else "AMADEUS",
                "message": ((f"已发{part_counts['sentPartCount']}条，剩余未完成，不重复已发部分。"
                             if partial else
                             "已交给对应 QQ 子对话结合历史处理；只按实际 sent/草稿状态向主人总结，草稿可用 approve 确认。")
                            + "text 是发给 QQ 收件人的正文，不是对主人的回答；其中‘我’指 textSpeaker，‘你’指 targetId 对应的联系人。"
                            + "转回主对话汇报时依这些身份转换人称，不能把主人的安排说成对方的安排。")}
        if action not in {"draft", "send", "approve"}:
            return {"ok":False, "error":"unsupported_qq_action"}
        attachment_ids = _qq_attachment_ids(args.get("attachmentIds"))
        if attachment_ids is None:
            return {"ok": False, "error": "invalid_qq_attachment_ids", "sent": False,
                    "message": "只能选择当前主对话中最多两张真实图片附件；不能传文件路径或重复 ID。"}
        if attachment_ids and action not in {"draft", "send"}:
            return {"ok": False, "error": "qq_attachments_only_draft_send", "sent": False,
                    "message": "主对话图片附件只能在 send 或 draft 中导入；approve 只能复核已有草稿中的 imageIds。"}
        current = None
        if action in {"send", "approve"}:
            revision = args.get("expectedRevision")
            # Reading the policy and carrying its revision across the gateway
            # transaction is adapter work, not another model reasoning round.
            # An explicitly supplied snapshot remains an optimistic-lock check.
            current = call("status")
            if revision and revision != policy_revision(current["policy"]):
                return {"ok": False, "error": "qq_policy_changed", "sent": False,
                        "message": "QQ 配置已变化，请重新读取状态；本次没有发送。"}
        request_id = str(args.get("_requestId", ""))
        image_ids = _qq_image_ids(args.get("imageIds"))
        if image_ids is None:
            return {"ok": False, "error": "invalid_qq_image_ids", "sent": False,
                    "message": "本轮最多选择两张不重复的真实 QQ 图片。"}
        if attachment_ids:
            # A draft does not otherwise need a status round trip, but importing
            # a main-chat image is account-sensitive.  Verify the live policy
            # immediately before touching the account's media library.
            bound_account = str(expected_account or "")
            configured_account = str(config.get("policy", {}).get("account_id") or "")
            if not bound_account:
                return {"ok": False, "error": "qq_account_required", "sent": False,
                        "message": "导入主对话图片前需要当前 QQ 账号的 expectedAccount。"}
            if bound_account != configured_account:
                return {"ok": False, "error": "qq_account_changed", "sent": False,
                        "message": "QQ 账号已经变化，请重新读取当前账号和主对话图片后重试。"}
            if current is None:
                current = call("status")
            live_account = str((current.get("policy") or {}).get("account_id") or "") if isinstance(current, dict) else ""
            if live_account != bound_account:
                return {"ok": False, "error": "qq_account_changed", "sent": False,
                        "message": "QQ 账号已经变化，本次没有导入或发送主对话图片。"}
            try:
                from backend.tools.qq_attachments import import_chat_images
                imported_ids = import_chat_images(data_root, attachment_ids, bound_account, call)
            except (ImportError, OSError, TypeError, ValueError, RuntimeError):
                return {"ok": False, "error": "qq_attachment_import_failed", "sent": False,
                        "message": "主对话图片导入失败，本次没有发送；请重新选择实际图片附件后重试。"}
            if not isinstance(imported_ids, list) or not imported_ids:
                return {"ok": False, "error": "qq_attachment_not_found", "sent": False,
                        "message": "未能从当前主对话找到所选图片，本次没有发送。"}
            imported_ids = _qq_image_ids(imported_ids) if imported_ids else []
            if imported_ids is None:
                return {"ok": False, "error": "invalid_imported_qq_image_ids", "sent": False,
                        "message": "图片导入回执无效，本次没有发送。"}
            image_ids = list(dict.fromkeys([*image_ids, *imported_ids]))
            if len(image_ids) > 2:
                return {"ok": False, "error": "qq_image_selection_limit", "sent": False,
                        "message": "本轮合计最多发送两张图片，请减少 imageIds 或主对话附件。"}
        if action in {"draft", "send", "approve"} and not request_id:
            return {"ok": False, "error": "qq_request_id_required", "attempted": False,
                    "sent": False, "message": "请求缺少本轮标识，不能在没有独立回合边界时发送或确认。"}
        payload = {"message_type": args.get("targetType"), "message": str(args.get("text", "")), "reason": reason}
        if image_ids:
            payload["imageIds"] = image_ids
        if payload["message_type"] not in {"private", "group"}:
            return {"ok":False, "error":"invalid_target_type"}
        payload["group_id" if payload["message_type"] == "group" else "user_id"] = str(args.get("targetId", ""))
        # Rewording an explanation on a retry must not create another outgoing message.
        identity = {key: value for key, value in payload.items() if key != "reason"} if action == "send" else payload
        payload["request_id"] = hashlib.sha256((request_id+json.dumps(identity,sort_keys=True,ensure_ascii=False)).encode("utf-8")).hexdigest()
        if action in {"draft", "send"}:
            payload["creation_request_id"] = request_id
        if "speechLanguage" in args:
            payload["speechLanguage"] = args["speechLanguage"]
        if current:
            payload["expected_policy"] = current["policy"]
        if action == "approve":
            draft_id = str(args.get("draftId") or "")
            if not draft_id:
                return {"ok": False, "error": "qq_draft_required", "sent": False,
                        "message": "需要明确确认的草稿 ID、收件人和完整正文。"}
            draft_row = next((item for item in current.get("sends", [])
                              if isinstance(item, Mapping) and str(item.get("id") or "") == draft_id), None)
            if _same_turn_qq_draft(draft_row, draft_id, request_id):
                return _qq_new_turn_required_receipt(
                    draft_row, request_id=request_id,
                    target_type=str(payload["message_type"]),
                    target_id=str(args.get("targetId", "")),
                    text=payload["message"], image_ids=image_ids)
            delivery_attempted = True
            result = call("approve", {"id": draft_id, "expected_policy": current["policy"],
                "approval_request_id": request_id,
                "expected": {"targetType": payload["message_type"], "targetId": str(args.get("targetId", "")),
                              "text": payload["message"], "imageIds": image_ids}})
        else:
            delivery_attempted = action == "send"
            result = call(action, payload)
        row = result["draft"]
        state = row["state"]
        sent = state == "sent" and bool(row.get("message_id"))
        part_counts = _delivery_part_counts(row)
        partial = part_counts.get("sentPartCount", 0) > 0 and state != "sent"
        message = (f"已发{part_counts['sentPartCount']}条，剩余未完成，不重复已发部分。" if partial else
                   "QQ 接口已确认主动发送，向主人简短说明已发给谁以及内容；不必等待对方来信。" if sent else
                   "只生成了草稿，尚未发出。向主人展示简短正文；可在对话中明确确认这条草稿，或在 QQ 动态中确认发送。不会等对方新消息后自动发出。" if state == "draft" else
                   "发送结果未知，请主人到 QQ 核对；不要重试发送。" if state in {"unknown", "sending"} or state == "sent" else
                   "消息未发送成功，请按实际回执说明，不要自动重试。")
        return {"ok": state == "draft" or sent, "deliveryState":state, "draftId":row["id"], "account":row["account"],
            "targetType":row["kind"], "targetId":row["target"], "text":payload["message"],
            "imageIds": image_ids,
            "explanation":result.get("explanation") or reason, "sent":sent, "messageId":row.get("message_id") or "",
            "replyFormat":row.get("reply_format", "text"), "speechLanguage":row.get("speech_language", "inherit"), "requiresApproval":state == "draft", **part_counts, "message":message}
    except _QqAccountChanged:
        return {"ok": False, "error": "qq_account_changed", "attempted": False, "sent": False,
                "message": "QQ 登录账号已经切换，未向新账号发送原任务的消息。"}
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", "网关拒绝操作")
        except (ValueError, AttributeError):
            error = "网关拒绝操作"
        return {"ok":False, "error":"qq_gateway_rejected", "message":str(error)[:300]}
    except (OSError, ValueError):
        if delivery_attempted:
            return {"ok": False, "error": "qq_delivery_unknown", "deliveryState": "unknown", "sent": False,
                    "account": expected_account or str(config.get("policy", {}).get("account_id") or ""),
                    "draftId": str(args.get("draftId") or ""), "targetType": args.get("targetType"),
                    "targetId": str(args.get("targetId") or ""), "text": str(args.get("text") or ""),
                    "message": "发送请求的连接中断或回执无法读取，结果未知；不要重新发送。先用 qq_status 查询近期回执，并请主人在 QQ 核对，不能声称未发送或发送成功。"}
        return {"ok":False, "error":"qq_gateway_unavailable", "message":"QQ 代管网关未启动或连接失败；没有确认任何消息发送成功。"}
