"""Public structured capability state retained across tool evidence projections."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

QQ_FACT_FIELDS = ("connected", "connectionMessage", "replyAgent", "policy", "policyRevision", "previousPolicy",
                  "capabilities", "activitySummary", "recentActions", "recentEvents", "deliveryState", "draftId",
                  "account", "targetType", "targetId", "text", "imageIds", "explanation", "sent", "messageId", "requiresApproval", "sentPartCount", "totalParts", "remainingPartCount", "contacts", "contactsComplete", "lifecycle", "muted", "exiting", "historyCount", "replyIdentity", "replyFormat", "textSpeaker")
_POLICY_FIELDS = {"mode", "account_id", "account_role", "owner_ids", "private_ids", "group_ids", "reply_instructions", "reply_delay_seconds", "reply_delay_mode", "reply_bubble_chars", "minimum_interval_seconds", "max_sends_per_hour"}
_POLICY_FIELDS.update({"owner_control_enabled", "owner_control_in_groups", "owner_developer_mode", "qzone_enabled", "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous", "qzone_interval_minutes", "qzone_instructions", "muted_private_ids", "muted_group_ids", "background_enabled", "history_messages", "compact_reply", "reply_identity", "reply_format", "voice_reply_percent", "conversation_overrides", "group_triggers", "group_keywords"})
_POLICY_FIELDS.add("speech_language")
QQ_FACT_FIELDS = (*QQ_FACT_FIELDS, "speechLanguage")
_ACTION_FIELDS = {"id", "account", "kind", "target", "targetName", "targetRemark", "actor", "reason", "state", "message_id", "detail", "created", "updated", "text", "textTruncated", "imageIds", "reply_format", "speech_language", "sentPartCount", "totalParts", "remainingPartCount"}
_MEMORY_FIELDS = ("memoryId", "targetType", "targetId", "userId", "topic", "value",
                  "sourceRole", "certainty", "sourceSenderId", "sourceMessageId",
                  "sourceTime", "shared", "updatedAt")


def _safe_memories(raw: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    result = []
    if not isinstance(raw, list):
        return result
    for row in raw[:limit]:
        if not isinstance(row, Mapping):
            continue
        safe = {}
        for key in _MEMORY_FIELDS:
            if key not in row:
                continue
            value = row[key]
            if key in {"topic", "value", "sourceRole", "certainty", "sourceMessageId", "updatedAt"}:
                if not isinstance(value, str):
                    continue
                safe[key] = value[:240 if key == "value" else 120]
            elif key in {"sourceSenderId", "userId", "targetId"}:
                value = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
                if value.isascii() and value.isdigit() and value:
                    safe[key] = value[:20]
            elif key == "memoryId":
                if isinstance(value, str):
                    safe[key] = value[:80]
            elif key == "targetType":
                if value in {"private", "group"}:
                    safe[key] = value
            elif key == "sourceTime":
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    safe[key] = str(value)[:40]
            elif key == "shared" and isinstance(value, bool):
                safe[key] = value
        if safe.get("topic") and safe.get("value") and safe.get("sourceRole"):
            result.append(safe)
    return result


def _safe_image_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [value.strip()[:80] for value in raw[:2]
            if isinstance(value, str) and value.strip() and "/" not in value and "\\" not in value
            and all(32 <= ord(char) < 127 for char in value)]


def public_tool_facts(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep exact configuration snapshots, but bound conversational history.

    Consumers must not slice policy arrays: a partial snapshot could accidentally
    remove authorized contacts when the model asks to change another setting.
    """
    if result.get("name") == "qq_media" and result.get("ok"):
        facts = {key: deepcopy(result[key]) for key in (
            "account", "pendingFinalReply", "message", "sent",
        ) if key in result}
        if "imageIds" in result:
            facts["imageIds"] = _safe_image_ids(result["imageIds"])
        facts["items"] = [{key: deepcopy(item[key]) for key in (
            "id", "name", "kind", "tags", "description", "width", "height", "animated",
        ) if key in item} for item in result.get("items", [])[:20] if isinstance(item, Mapping)]
        return facts
    if result.get("name") == "voice_configure" and result.get("ok"):
        return {"scope": "desktop", "settings": {
            key: value for key, value in (result.get("settings") or {}).items()
            if key in {"speechLanguage", "voiceLocale", "gptSovitsTargetLanguage", "voiceEnabled"}
        }}
    if result.get("name") == "qq_history" and result.get("ok"):
        facts = {key: deepcopy(result[key]) for key in (
            "account", "targetType", "targetId", "targetName", "targetRemark",
            "messages", "identityContext", "nextCursor", "hasMore", "message", "truncated",
        ) if key in result}
        context = facts.get("identityContext")
        if isinstance(context, Mapping):
            safe_context = {key: deepcopy(context[key]) for key in ("accountId", "groupId") if key in context}
            people = []
            for person in context.get("people", []) if isinstance(context.get("people"), list) else []:
                if not isinstance(person, Mapping):
                    continue
                safe_person = {key: deepcopy(person[key]) for key in (
                    "userId", "nickname", "friendRemark", "groupCard", "aliases", "isFriend",
                ) if key in person}
                if isinstance(safe_person.get("aliases"), list):
                    safe_person["aliases"] = [str(value)[:120] for value in safe_person["aliases"]
                                             if isinstance(value, (str, int))][:30]
                memories = _safe_memories(person.get("memories"))
                if memories:
                    safe_person["memories"] = memories
                people.append(safe_person)
            safe_context["people"] = people
            facts["identityContext"] = safe_context
        safe_messages = []
        for row in facts.get("messages", [])[:30]:
            if not isinstance(row, Mapping):
                continue
            safe_row = {k: deepcopy(row[k]) for k in row if k in {
                "messageId", "messageSeq", "senderId", "senderName", "senderNickname", "senderCard",
                "speakerRole", "time", "text", "textTruncated", "imageRefs", "replyTo",
            }}
            mentions = row.get("mentions")
            if isinstance(mentions, list):
                safe_row["mentions"] = [
                    {key: deepcopy(item[key]) for key in ("userId", "segmentIndex") if key in item}
                    for item in mentions[:100] if isinstance(item, Mapping)
                ]
            safe_messages.append(safe_row)
        facts["messages"] = safe_messages
        return facts
    if result.get("name") == "qq_person_memory" and result.get("ok"):
        facts = {key: deepcopy(result[key]) for key in (
            "account", "targetType", "targetId", "userId", "remembered", "forgotten", "sent", "message",
        ) if key in result}
        memories = _safe_memories([result.get("memory")])
        if memories:
            facts["memory"] = memories[0]
        return facts
    if result.get("name") in {"qq_identity", "qq_remember_alias"} and result.get("ok"):
        # Identity receipts are deliberately narrower than status/history facts:
        # no signed scope, raw gateway payload, or chat content may reach the model.
        if result.get("name") == "qq_remember_alias":
            return {key: deepcopy(result[key]) for key in (
                "account", "targetType", "targetId", "userId", "alias",
                "remembered", "sent", "message",
            ) if key in result}
        facts = {key: deepcopy(result[key]) for key in (
            "account", "groupId", "ambiguous",
        ) if key in result}
        people = []
        raw_people = result.get("people")
        if isinstance(raw_people, list):
            for row in raw_people[:100]:
                if not isinstance(row, Mapping):
                    continue
                person = {key: deepcopy(row[key]) for key in (
                    "userId", "nickname", "friendRemark", "groupCard", "aliases", "isFriend",
                ) if key in row}
                if isinstance(person.get("aliases"), list):
                    person["aliases"] = [str(value)[:120] for value in person["aliases"]
                                         if isinstance(value, (str, int))][:30]
                memories = _safe_memories(row.get("memories"))
                if memories:
                    person["memories"] = memories
                people.append(person)
        facts["people"] = people
        return facts
    if result.get("name") not in {"qq_status", "qq_manage", "qq_activity"}:
        return {}
    if not result.get("ok") and result.get("deliveryState") not in {"failed", "unknown", "sending", "sent", "cancelled"}:
        return {}
    facts = {key: deepcopy(result[key]) for key in QQ_FACT_FIELDS if key in result}
    if "imageIds" in facts:
        facts["imageIds"] = _safe_image_ids(facts["imageIds"])
    for key in ("policy", "previousPolicy"):
        if isinstance(facts.get(key), Mapping):
            facts[key] = {k: v for k, v in facts[key].items() if k in _POLICY_FIELDS}
    if isinstance(facts.get("capabilities"), Mapping):
        facts["capabilities"] = {k: v for k, v in facts["capabilities"].items()
                                 if k in {"read", "manage", "historyTool", "configureFields", "conversationConfigure", "modes", "setupLocation", "requiresOwnerIntent"}}
    if isinstance(facts.get("recentActions"), list):
        facts["recentActions"] = [{k: v for k, v in row.items() if k in _ACTION_FIELDS}
                                  for row in facts["recentActions"][:3] if isinstance(row, Mapping)]
        for row in facts["recentActions"]:
            for key in ("text", "reason", "detail"):
                if isinstance(row.get(key), str):
                    if key == "text" and len(row[key]) > 500:
                        row["textTruncated"] = True
                    row[key] = row[key][:500]
    # Status/configuration events are useful; arbitrary raw event payloads aren't.
    if isinstance(facts.get("recentEvents"), list):
        events = []
        for event in facts["recentEvents"][:3]:
            if not isinstance(event, Mapping):
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, Mapping):
                continue
            safe = {k: deepcopy(v) for k, v in payload.items()
                    if k in _POLICY_FIELDS | {"actor", "reason", "action", "account", "target"}}
            if isinstance(payload.get("policy"), Mapping):
                safe["policy"] = {k: v for k, v in payload["policy"].items() if k in _POLICY_FIELDS}
            events.append({"kind": event.get("kind"), "created": event.get("created"), "payload": safe})
        facts["recentEvents"] = events
    return facts


def historical_operation_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """A working memory of an old operation, never an executable state snapshot.

    The caller retains the full receipt in the evidence journal. Only its action,
    destination, actual outcome and spoken content belong in routine later turns.
    This deliberately excludes old directory/policy/history payloads.
    """
    shortened = []

    def brief(value: Any, key: str, limit: int = 240) -> Any:
        if isinstance(value, str):
            if len(value) > limit:
                shortened.append(key)
                return value[:limit] + '…'
            return value
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, list):
            if len(value) > 6:
                shortened.append(key)
            return [brief(item, key, 120) for item in value[:6]]
        if isinstance(value, Mapping):
            if len(value) > 6:
                shortened.append(key)
            return {str(k): brief(v, key, 120) for k, v in list(value.items())[:6]}
        return None

    try:
        request = json.loads(str(result.get('_cognitiveRequestSignature') or '{}'))
    except (ValueError, TypeError):
        request = {}
    args = request.get('args') if isinstance(request, Mapping) else {}
    args = args if isinstance(args, Mapping) else {}
    action_receipt = result.get('actionReceipt')
    action_receipt = action_receipt if isinstance(action_receipt, Mapping) else {}
    summary = {'tool': result.get('name') or action_receipt.get('tool')}
    if 'ok' in result:
        summary['ok'] = result.get('ok') is True
    # These are exact status/identity fields, not an inference from ok=True.
    # In particular, successfully creating a draft does not mean it was sent.
    for key in ('attempted', 'deliveryState', 'sent', 'sentPartCount', 'totalParts', 'remainingPartCount',
                'remembered', 'requiresApproval', 'draftId', 'messageId', 'error'):
        if key in result:
            summary[key] = brief(result[key], key)
    action = args.get('action') or action_receipt.get('action') or result.get('action')
    if action:
        summary['action'] = brief(action, 'action')
    target = {}
    for key in ('account', 'targetType', 'targetId', 'targetName', 'targetRemark', 'userId', 'alias', 'path', 'url', 'taskId'):
        value = result.get(key, args.get(key, action_receipt.get(key)))
        if value is not None:
            target[key] = brief(value, key)
    image_ids = result.get('imageIds', args.get('imageIds', action_receipt.get('imageIds')))
    if image_ids is not None:
        target['imageIds'] = _safe_image_ids(image_ids)
    for key in ('requestedTarget', 'resolvedTarget'):
        if action_receipt.get(key) is not None:
            target[key] = brief(action_receipt[key], key)
    if result.get('name') == 'qq_person_memory' and isinstance(result.get('memory'), Mapping):
        target['memory'] = {key: brief(result['memory'][key], key, 240 if key == 'value' else 120)
                            for key in _MEMORY_FIELDS if key in result['memory']}
    if target:
        summary['target'] = target
    spoken = result.get('text')
    if spoken is None and result.get('name') == 'qq_manage':
        spoken = args.get('text')
    if isinstance(spoken, str) and spoken:
        summary['text'] = brief(spoken, 'text', 1200)
    # Preserve the requested purpose/result, not the raw body/OCR/page/messages.
    for key in ('explanation', 'message'):
        if isinstance(result.get(key), str) and result[key]:
            summary[key] = brief(result[key], key)
            break
    if args.get('instruction'):
        summary['instruction'] = brief(args['instruction'], 'instruction')
    before, after = result.get('previousPolicy'), result.get('policy')
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes = {}
        def diff(old, new, path=''):
            for key in dict.fromkeys([*old, *new]):
                name = f'{path}.{key}' if path else str(key)
                if old.get(key) == new.get(key):
                    continue
                if isinstance(old.get(key), Mapping) and isinstance(new.get(key), Mapping):
                    diff(old[key], new[key], name)
                elif len(changes) < 8:
                    changes[name] = brief(new.get(key), name, 300)
                else:
                    shortened.append('changes')
        diff(before, after)
        if changes:
            summary['changes'] = changes
    elif action in {'pause', 'resume', 'stop'} and isinstance(after, Mapping):
        summary['resultingMode'] = after.get('mode')
    for key in ('muted', 'exiting', 'historyCount'):
        if key in result:
            summary[key] = brief(result[key], key)
    # Activity reads may be the only evidence that automatic QQ replies happened.
    # Keep their actual words/outcomes, never the full status/configuration bundle.
    actions = result.get('recentActions')
    if isinstance(actions, list) and actions:
        summary['recentActions'] = [{key: brief(row[key], key, 500 if key == 'text' else 120)
                                    for key in ('id', 'account', 'kind', 'target', 'targetName', 'targetRemark',
                                                'state', 'message_id', 'text', 'textTruncated', 'imageIds',
                                                'sentPartCount', 'totalParts', 'remainingPartCount') if key in row}
                                   for row in actions[:3] if isinstance(row, Mapping)]
        if len(actions) > 3:
            shortened.append('recentActions')
    messages = result.get('messages')
    if isinstance(messages, list):
        summary['messagesRead'] = len(messages)
    if result.get('name') in {'web_search', 'read_web_page'}:
        if result.get('query'):
            summary['query'] = brief(result['query'], 'query')
        if isinstance(result.get('sources'), list):
            summary['sourcesRead'] = len(result['sources'])
    if shortened:
        summary['truncatedFields'] = sorted(set(shortened))
    return summary
