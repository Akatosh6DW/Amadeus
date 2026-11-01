// External delivery receipts are the only source for deciding whether a
// QQ send completed and whether the local confirmation is presentation-only.
// Keep this projection free of reply-text matching so a model sentence cannot
// make an unverified send look successful.
(function attachDeliveryProjection(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusDeliveryProjection = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createDeliveryProjectionApi() {
  "use strict";

  const QUERY_TOOLS = new Set(["qq_status", "qq_activity", "qq_history", "qq_identity"]);
  const SEND_ACTIONS = new Set(["send", "delegate", "approve"]);
  const DELIVERY_STATES = new Set(["draft", "sending", "sent", "unknown", "failed", "rejected"]);
  const SILENT_POLICY_VALUES = new Set([
    "silent", "silent_send_confirmation", "silent_confirmation", "quiet",
    "quiet_confirmation", "no_confirmation",
  ]);
  const VERBOSE_POLICY_VALUES = new Set([
    "announce", "normal", "speak", "show", "visible", "answer", "reply",
    "substantive", "substantive_answer", "full_answer",
  ]);
  const ACTION_ONLY = "execute_or_report_action";
  const GENERIC_RESPONSE = "respond_to_current_utterance";
  const TOOL_COMPLETION = "tool_completion";

  function isObject(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function asText(value) {
    return String(value == null ? "" : value).trim();
  }

  function receiptCandidates(value) {
    if (!isObject(value)) return [];
    const candidates = [];
    if (Array.isArray(value.actionReceipts)) candidates.push(...value.actionReceipts);
    if (isObject(value.actionReceipt)) candidates.push(value.actionReceipt);
    if (Array.isArray(value.deliveryReceipts)) candidates.push(...value.deliveryReceipts);
    if (isObject(value.deliveryReceipt)) candidates.push(value.deliveryReceipt);
    if (Array.isArray(value.completedToolReceipts)) candidates.push(...value.completedToolReceipts);
    if (Array.isArray(value.toolReceipts)) candidates.push(...value.toolReceipts);
    const scheduledRows = Array.isArray(value.scheduledToolReceipts)
      ? value.scheduledToolReceipts.filter(isObject)
      : [];
    if (scheduledRows.length) candidates.push(...scheduledRows);
    if (isObject(value.scheduledDelivery)) {
      const scheduled = value.scheduledDelivery;
      const status = asText(scheduled.status).toLowerCase();
      if (status && status !== "not_attempted") {
        const target = isObject(scheduled.target) ? scheduled.target : {};
        const matchingStep = scheduledRows.find(row => {
          const rowState = asText(row.deliveryState).toLowerCase();
          const rowTargetType = asText(row.targetType).toLowerCase();
          const rowTargetId = asText(row.targetId);
          const rowAccount = asText(row.account);
          const targetType = asText(target.targetType).toLowerCase();
          const targetId = asText(target.targetId);
          const account = asText(target.accountId || target.account);
          const rowMessageId = asText(row.messageId || row.message_id);
          const scheduledMessageId = asText(scheduled.messageId);
          const sameTarget = rowTargetId === targetId
            && (!targetType || !rowTargetType || rowTargetType === targetType)
            && (!account || !rowAccount || rowAccount === account);
          return asText(row.name || row.tool).toLowerCase() === "qq_manage"
            && rowState === status
            && (status !== "sent"
              ? sameTarget
              : (Boolean(scheduledMessageId && rowMessageId === scheduledMessageId)
                || Boolean(sameTarget && rowMessageId)));
        });
        candidates.push({
          tool: "qq_manage",
          operation: "send",
          outcome: status === "sent" ? "sent" : status,
          ok: status === "sent",
          sent: status === "sent",
          deliveryState: status,
          messageId: scheduled.messageId,
          account: target.accountId || target.account,
          targetType: target.targetType,
          targetId: target.targetId,
          toolCallId: matchingStep?.toolCallId || matchingStep?.callId || "",
          scheduledProjection: true,
        });
      }
    }
    if (asText(value.name) || asText(value.tool) || asText(value.deliveryState)
      || asText(value.messageId) || typeof value.sent === "boolean") {
      candidates.push(value);
    }
    return candidates.filter(isObject);
  }

  function normalizeReceipt(value) {
    if (!isObject(value)) return null;
    const nested = isObject(value.actionReceipt) ? value.actionReceipt : {};
    const merged = { ...value, ...nested };
    const tool = asText(merged.tool || merged.name);
    if (!tool) return null;
    const state = asText(merged.deliveryState).toLowerCase();
    const operation = asText(merged.operation).toLowerCase()
      || (QUERY_TOOLS.has(tool) ? "query" : "")
      || ((state || asText(merged.messageId) || typeof merged.sent === "boolean")
        ? (state === "draft" ? "draft" : "send") : "")
      || (tool === "qq_manage" && asText(merged.action).toLowerCase() === "draft" ? "draft" : "")
      || (tool === "qq_manage" && SEND_ACTIONS.has(asText(merged.action).toLowerCase()) ? "send" : "")
      || (tool === "qq_manage" ? "action" : "tool");
    const messageId = asText(merged.messageId || merged.message_id);
    let outcome = asText(merged.outcome).toLowerCase();
    if (!outcome) {
      if (operation === "query") outcome = merged.ok === true ? "completed" : merged.ok === false ? "failed" : "unknown";
      else if (state === "sent" && messageId) outcome = "sent";
      else if (state === "draft") outcome = "draft";
      else if (state === "failed" || state === "rejected" || merged.ok === false) outcome = "failed";
      else if (state === "sending" || state === "unknown" || merged.sent === true) outcome = "unknown";
      else outcome = merged.ok === true ? "completed" : "unknown";
    }
    return {
      ...merged,
      tool,
      operation,
      outcome,
      deliveryState: state,
      messageId,
      sent: merged.sent === true,
      preflightFailure: merged.preflightFailure === true,
      errorCode: asText(merged.errorCode).toLowerCase(),
    };
  }

  function receiptsFor(value) {
    const seen = new Set();
    const receipts = [];
    for (const candidate of receiptCandidates(value)) {
      const receipt = normalizeReceipt(candidate);
      if (!receipt) continue;
      const key = [receipt.tool, receipt.operation, receipt.outcome,
        receipt.messageId, asText(receipt.draftId), asText(receipt.targetId),
        asText(receipt.toolCallId || receipt.callId)].join("\u001f");
      if (seen.has(key)) continue;
      seen.add(key);
      receipts.push(receipt);
    }
    return receipts;
  }

  function isConfirmedQqSendReceipt(value) {
    const receipt = normalizeReceipt(value);
    return Boolean(receipt
      && receipt.tool === "qq_manage"
      && receipt.operation === "send"
      && receipt.outcome === "sent"
      && receipt.deliveryState === "sent"
      && receipt.messageId);
  }

  function sameTarget(left, right) {
    if (!isObject(left) || !isObject(right)) return false;
    const leftType = asText(left.targetType).toLowerCase();
    const rightType = asText(right.targetType).toLowerCase();
    const leftId = asText(left.targetId);
    const rightId = asText(right.targetId);
    if (!leftId || !rightId || leftId !== rightId) return false;
    // invalid_target_type may leave the rejected request without a type. The
    // exact target ID still binds it to a later corrected call; an explicit
    // type mismatch remains a different target.
    if (leftType && rightType && leftType !== rightType) return false;
    const leftAccount = asText(left.account);
    const rightAccount = asText(right.account);
    return !leftAccount || !rightAccount || leftAccount === rightAccount;
  }

  function isPreflightQqSendReceipt(value) {
    const receipt = normalizeReceipt(value);
    return Boolean(receipt
      && receipt.tool === "qq_manage"
      && receipt.operation === "send"
      && receipt.preflightFailure === true
      && receipt.outcome === "failed"
      && !receipt.deliveryState
      && !receipt.messageId
      && receipt.sent !== true);
  }

  function requiredActsFor(value) {
    if (!isObject(value?.finalTurnEnvelope)) return null;
    const envelope = value.finalTurnEnvelope;
    const acts = envelope.required_acts ?? envelope.requiredActs;
    return Array.isArray(acts) ? acts.map(asText).filter(Boolean) : null;
  }

  function explicitPresentationPolicy(value) {
    const fields = [
      value?.deliveryPresentation,
      value?.delivery_presentation,
      value?.deliveryNotification,
      value?.notificationPolicy,
      value?.uiDelivery?.notificationPolicy,
      value?.uiDelivery?.presentation,
      value?.presentation?.notificationPolicy,
      value?.presentation?.mode,
      value?.presentationContract?.notificationPolicy,
      value?.presentationContract?.mode,
    ];
    return fields.map(item => {
      if (isObject(item)) {
        return asText(item.notificationPolicy || item.mode || item.presentation || item.kind).toLowerCase();
      }
      return asText(item).toLowerCase();
    }).find(Boolean) || "";
  }

  function outcomeFor(value) {
    if (!isObject(value)) return null;
    const candidates = [
      value.outcome,
      value._cognitiveOutcome,
      value.cognitive?.outcome,
    ];
    return candidates.find(isObject) || null;
  }

  function outcomeOnlyToolCompletion(value) {
    const outcome = outcomeFor(value);
    const claims = outcome?.claims;
    // The model's outcome is only a presentation hint after the host has
    // verified the real receipt.  Require a non-empty, fully typed success
    // claim list so a missing/partial outcome cannot hide an answer.
    return Array.isArray(claims) && claims.length > 0 && claims.every(claim => (
      isObject(claim)
      && asText(claim.kind).toLowerCase() === TOOL_COMPLETION
      && asText(claim.resultStatus).toLowerCase() === "success"
    ));
  }

  function isPureQqSendConfirmation(value) {
    if (!isObject(value)) return false;
    const receipts = receiptsFor(value);
    const queries = receipts.filter(receipt => QUERY_TOOLS.has(receipt.tool));
    if (queries.some(receipt => receipt.outcome !== "completed")) return false;
    const actions = receipts.filter(receipt => !QUERY_TOOLS.has(receipt.tool));
    // Target lookup is part of the send workflow and may appear beside the
    // final send receipt. It does not itself require a spoken confirmation.
    const confirmed = actions.filter(isConfirmedQqSendReceipt);
    if (!confirmed.length) return false;
    if (!actions.every(receipt => (
      isConfirmedQqSendReceipt(receipt)
      || (isPreflightQqSendReceipt(receipt)
        && confirmed.some(send => sameTarget(receipt, send)))
    ))) return false;
    const requiredActs = requiredActsFor(value);
    // A host-declared answer, task completion, or other substantive act must
    // remain visible even when the QQ side effects all succeeded. The generic
    // response act is the fallback emitted for an action turn; it is benign
    // only with an outcome made entirely of successful tool completions.
    if (requiredActs && requiredActs.some(act => (
      act !== ACTION_ONLY && act !== GENERIC_RESPONSE
    ))) return false;
    if (requiredActs?.includes(GENERIC_RESPONSE) && !outcomeOnlyToolCompletion(value)) {
      return false;
    }
    const explicitPolicy = explicitPresentationPolicy(value);
    if (VERBOSE_POLICY_VALUES.has(explicitPolicy)) return false;
    if (SILENT_POLICY_VALUES.has(explicitPolicy)) return true;
    // A verified send plus a model outcome containing only successful
    // tool_completion claims is an action acknowledgement.  This deliberately
    // does not infer from reply text or from the required-acts label, which may
    // be answer_current_question for an elliptical send request.
    // A turn carrying a substantive required act was rejected above; repeated
    // external-action acts may still use this receipt-only acknowledgement.
    if (outcomeOnlyToolCompletion(value)
      && (!requiredActs || requiredActs.every(act => (
        act === ACTION_ONLY || act === GENERIC_RESPONSE
      )))) return true;
    // The final envelope is host-owned. One or more external-action acts are
    // the structured distinction between a pure send acknowledgement and a
    // turn that also owes the user a substantive answer.
    if (!requiredActs || !requiredActs.length) return false;
    return requiredActs.every(act => act === ACTION_ONLY || act === GENERIC_RESPONSE);
  }

  return Object.freeze({
    normalizeReceipt,
    receiptsFor,
    isConfirmedQqSendReceipt,
    isPreflightQqSendReceipt,
    sameTarget,
    isPureQqSendConfirmation,
    outcomeFor,
    outcomeOnlyToolCompletion,
    requiredActsFor,
  });
}));
