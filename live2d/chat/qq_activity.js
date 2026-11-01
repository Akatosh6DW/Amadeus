(function attach(root, factory) {
  const api = factory(root.AmadeusDeliveryProjection);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusQqActivity = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function (deliveryProjection) {
  "use strict";
  const states = {draft: "待你确认", sending: "正在发送", sent: "已确认发送",
    failed: "发送失败", unknown: "发送结果未知，请到 QQ 核对", cancelled: "已取消"};
  const eventNames = {amadeus_thinking: "正在替你处理来信", amadeus_error: "本条处理失败，未确认发送", amadeus_busy: "当前繁忙，这条来信未处理",
    qzone_visit: "QQ 空间自主检查结果", qzone_action: "QQ 空间操作回执", owner_control_result: "主人 QQ 指令处理记录",
    voice_preparing: "正在合成 QQ 语音", voice_failed: "语音合成失败，未发送",
    policy_changed: "QQ 配置已更新", amadeus_control: "QQ 代管状态已调整"};
  function mutedCount(policy) {
    return (Array.isArray(policy?.muted_private_ids) ? policy.muted_private_ids.length : 0)
      + (Array.isArray(policy?.muted_group_ids) ? policy.muted_group_ids.length : 0);
  }
  function lifecycleText(data) {
    const lifecycle = data.lifecycle || {};
    if (lifecycle.active === false && lifecycle.backgroundEnabled === false) return "QQ 等待 Amadeus 连接";
    if (lifecycle.active === false && lifecycle.backgroundEnabled === true) return "QQ 后台已授权，等待连接";
    if (lifecycle.desktopOnline === false && !lifecycle.backgroundEnabled) return "等待 Amadeus 连接";
    return "";
  }
  function targetText(kind, id, name = "", remark = "") {
    const label = remark || name;
    const nickname = remark && name && remark !== name ? `（昵称：${name}）` : "";
    return `${kind === "group" ? "群" : "好友"} ${label ? label + nickname + " · " : ""}${id}`;
  }
  function contactText(data, kind, id, account = data.policy?.account_id) {
    const contact = account === data.policy?.account_id
      ? data.contacts?.find(item => item.kind === kind && String(item.id) === String(id)) : null;
    return targetText(kind, id, contact?.name, contact?.remark);
  }
  function actionText(row) {
    const state = row.state === "sent" && !row.message_id ? "unknown" : row.state;
    return `${row.actor || "智能体"} · ${row.account} → ${targetText(row.kind, row.target, row.targetName, row.targetRemark)}：${states[state] || state}`;
  }
  function confirmedSend(row) {
    return deliveryProjection?.isConfirmedQqSendReceipt?.({
      tool: "qq_manage",
      operation: "send",
      outcome: row?.state === "sent" && row?.message_id ? "sent" : String(row?.state || ""),
      deliveryState: row?.state,
      messageId: row?.message_id,
      sent: row?.state === "sent" && Boolean(row?.message_id),
    }) === true;
  }
  function confirmedSendEvent(event) {
    const payload = event?.payload || {};
    const ids = Array.isArray(payload.message_ids) ? payload.message_ids : [];
    return ids.some(messageId => deliveryProjection?.isConfirmedQqSendReceipt?.({
      tool: "qq_manage",
      operation: "send",
      outcome: "sent",
      deliveryState: payload.deliveryState || payload.delivery_state || "sent",
      messageId,
      sent: true,
    }) === true) || deliveryProjection?.isConfirmedQqSendReceipt?.({
      tool: "qq_manage",
      operation: "send",
      outcome: payload.outcome === "side_effect_applied" ? "sent" : payload.outcome,
      deliveryState: payload.deliveryState || payload.delivery_state,
      messageId: payload.messageId || payload.message_id,
      sent: payload.sent,
    }) === true;
  }
  function summary(data) {
    if (!data.ok) return data.message || "QQ 代管暂不可用";
    const paused = data.policy?.mode === "paused";
    const state = lifecycleText(data) || (paused ? "QQ 已暂停" : !data.connected ? "QQ 连接未就绪"
      : data.policy?.mode === "review" ? "QQ 草稿模式已开启" : "QQ 自动回复已开启");
    const row = data.recentActions?.[0];
    const event = data.recentEvents?.find(item => eventNames[item.kind]);
    if (event && event.created > (row?.updated || 0)) return `${state} · ${eventNames[event.kind]}`;
    if (row) {
      const actions = data.recentActions || [];
      const sent = data.activitySummary?.sent ?? actions.filter(item => item.state === "sent" && item.message_id).length;
      const drafts = data.activitySummary?.draft ?? actions.filter(item => item.state === "draft").length;
      const issues = data.activitySummary ? data.activitySummary.failed + data.activitySummary.unknown : actions.filter(item => ["failed", "unknown"].includes(item.state) || item.state === "sent" && !item.message_id).length;
      return `${state} · 最近` + [sent && `已回复 ${sent} 条`, drafts && `${drafts} 条草稿待确认`, issues && `${issues} 条需核对`].filter(Boolean).join("，")
        + (sent || drafts || issues ? "" : states[row.state] || "处理中");
    }
    const muted = mutedCount(data.policy);
    const mutedText = muted ? `；${muted} 个会话已暂停` : "";
    return state + mutedText + " · 展开查看记录";
  }
  function createController({element, request, isEnabled, isVisible = isEnabled, notify,
    eventTarget = globalThis, setInterval: setTimer = setInterval, clearInterval: clearTimer = clearInterval}) {
    let timer, inFlight = false, fingerprint = "", lastReport = "", acting = false, localSyncListening = false;
    function render(data) {
      const signature = JSON.stringify(data);
      if (signature === fingerprint) return;
      fingerprint = signature;
      element.querySelector("summary").textContent = summary(data);
      const list = element.querySelector(".qq-activity-list");
      list.replaceChildren();
      const add = (parent, tag, content) => {
        const child = element.ownerDocument.createElement(tag);
        child.textContent = content;
        parent.append(child);
        return child;
      };
      for (const row of data.recentActions || []) {
        const card = add(list, "article", "");
        add(card, "strong", actionText(row));
        add(card, "p", row.reason || "按你的授权处理 QQ 消息");
        add(card, "pre", (row.reply_format === "voice" ? "[语音内容] " : "") + (row.text || ""));
        add(card, "small", row.detail || (row.message_id ? `平台回执：${row.message_id}` : "尚无发送成功回执"));
        if (row.state === "draft") {
          const controls = add(card, "div", "");
          controls.className = "button-row";
          for (const [action, label] of [["approve", "确认发送以上内容"], ["cancel", "取消草稿"]]) {
            const button = add(controls, "button", label);
            button.type = "button";
            button.disabled = acting || (action === "approve" && data.policy?.mode === "paused");
            if (action === "approve" && data.policy?.mode === "paused") button.title = "代管已暂停，请先在设置 QQ 页恢复审核模式";
            button.addEventListener("click", async () => {
              if (acting || !isEnabled() || !isVisible()) return;
              acting = true;
              button.disabled = true;
              try {
                const response = await request("/api/qq/setup", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action, id: row.id})});
                const result = await response.json();
                if (!response.ok || !result.ok) throw new Error(result.message || "操作失败");
                notify?.(result.message || "操作已处理，请查看最新回执");
              } catch (error) { notify?.(error.message); }
              finally { acting = false; fingerprint = ""; await poll(); }
            });
          }
        }
      }
      for (const event of (data.recentEvents || []).filter(item => eventNames[item.kind]).slice(0, 3)) {
        const p = event.payload || {};
        const policy = p.policy || p;
        const mode = {auto: "自动回复", review: "先审核", paused: "暂停"}[policy.mode];
        add(list, "p", mode
          ? `${p.actor || "Amadeus"}：${eventNames[event.kind]}。${mode}；${(policy.private_ids || []).map(id => contactText(data, "private", id, policy.account_id)).join("、") || "无好友"}；${(policy.group_ids || []).map(id => contactText(data, "group", id, policy.account_id)).join("、") || "无群"}。${p.reason || ""}`
          : `${eventNames[event.kind]} · ${p.account || ""} → ${p.target ? contactText(data, p.kind, p.target, p.account) : ""}。${p.reason || ""}`);
      }
      if (!list.childNodes.length) add(list, "p", data.message || "还没有代管记录。");
      const newestAction = data.recentActions?.[0];
      const newestEvent = data.recentEvents?.find(item => eventNames[item.kind]);
      const report = JSON.stringify([newestAction?.id, newestAction?.state, newestAction?.message_id, newestEvent?.id]);
      if ((newestAction || newestEvent) && report !== lastReport) {
        const onlyConfirmedSend = confirmedSend(newestAction)
          && (!newestEvent || newestEvent.created <= (newestAction?.updated || 0) || confirmedSendEvent(newestEvent));
        if (lastReport && !onlyConfirmedSend) notify?.(summary(data));
        lastReport = report;
      }
    }
    async function poll() {
      if (!element) return;
      element.hidden = !isVisible();
      if (element.hidden) {
        fingerprint = "";
        lastReport = "";
        element.open = false;
        element.querySelector(".qq-activity-list").replaceChildren();
        return;
      }
      if (!isEnabled()) {
        render({ok: false, message: "QQ 插件尚未授权 · 请打开 QQ 设置"});
        return;
      }
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await request("/api/qq/activity", {cache: "no-store"});
        const data = await response.json();
        // A permission can be revoked while the read request is in flight.
        if (isEnabled() && isVisible()) render(data);
        else { element.hidden = true; element.querySelector(".qq-activity-list").replaceChildren(); }
      } catch (_) {
        if (isEnabled() && isVisible()) render({ok: false, message: "QQ 动态暂时无法读取；请打开设置 → QQ 查看连接。"});
      } finally { inFlight = false; }
    }
    function onLocalSync() {
      if (isEnabled() && isVisible()) void poll();
    }
    function listenLocalSync() {
      if (localSyncListening || !eventTarget?.addEventListener) return;
      eventTarget.addEventListener("amadeus:local-sync", onLocalSync);
      localSyncListening = true;
    }
    function unlistenLocalSync() {
      if (!localSyncListening || !eventTarget?.removeEventListener) return;
      eventTarget.removeEventListener("amadeus:local-sync", onLocalSync);
      localSyncListening = false;
    }
    listenLocalSync();
    function start() { listenLocalSync(); clearTimer(timer); poll(); timer = setTimer(poll, 5000); }
    function stop() {
      clearTimer(timer); timer = undefined;
      unlistenLocalSync();
    }
    return {start, stop, poll};
  }
  return {summary, actionText, createController};
}));
