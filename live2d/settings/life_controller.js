// 自主生活页只管理独立的 /api/life 快照，避免把长期运行状态塞进通用 settings。
(function attachLifeController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusLifeController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createLifeControllerApi() {
  "use strict";

  const DEFAULT_CONFIG = Object.freeze({
    enabled: false,
    activityFrequency: 100,
    intervalMinutes: 60,
    dailyRunLimit: 8,
    maxSteps: 6,
    activeStartHour: 8,
    activeEndHour: 23,
    instructions: "",
    directories: [],
    sources: [],
    allowWeb: true,
    allowQqRead: false,
    allowQqChat: false,
    qqChatOwner: "",
    qqChatIntervalMinutes: 180,
    publishMode: "draft",
    publishAccount: "",
    publishVisibility: "friends",
    dailyPublishLimit: 1,
  });

  const RHYTHM_DEFAULTS = Object.freeze([
    Object.freeze({ kind: "explore", label: "阅读", intervalMinutes: 120 }),
    Object.freeze({ kind: "reflect", label: "整理想法", intervalMinutes: 360 }),
    Object.freeze({ kind: "chat", label: "主动 QQ 聊天", intervalMinutes: 180 }),
    Object.freeze({ kind: "share", label: "QQ 空间分享", intervalMinutes: 720 }),
  ]);
  const RHYTHM_KINDS = new Set(RHYTHM_DEFAULTS.map(item => item.kind));
  const ACTIVITY_KIND_LABELS = Object.freeze({
    manual: "手动试运行",
    explore: "阅读",
    reflect: "整理想法",
    chat: "主动 QQ 聊天",
    share: "QQ 空间分享",
    legacy: "历史活动",
  });

  const PUBLISH_MODES = new Set(["draft", "automatic", "off"]);
  const PUBLISH_VISIBILITIES = new Set(["friends", "private", "public"]);
  const DRAFT_STATES = new Set(["draft", "publishing", "sent", "unknown", "failed", "dismissed"]);
  const ACTIVITY_STATUS_LABELS = Object.freeze({
    completed: "已完成",
    running: "运行中",
    failed: "失败",
    budget_reached: "达到预算上限",
    deferred: "已延后",
    interrupted: "已中断",
    cancelled: "已取消",
  });
  const DRAFT_STATE_LABELS = Object.freeze({
    draft: "草稿",
    publishing: "发布中",
    sent: "已发送",
    unknown: "结果未知",
    failed: "失败",
    dismissed: "已舍弃",
  });
  const VISIBILITY_LABELS = Object.freeze({ friends: "好友可见", private: "仅自己可见", public: "公开" });
  const STEP_ACTION_LABELS = Object.freeze({
    list_files: "浏览目录",
    read_file: "阅读文件",
    read_web: "阅读网页",
    read_feed: "阅读订阅",
    public_read: "阅读公开来源",
    search_web: "搜索网页",
    qq_feed: "浏览QQ空间",
    read_owner_chat: "看看最近聊到哪儿",
    chat_owner: "主动聊聊",
    remember_thought: "记下想法",
    draft_post: "动态草稿",
    finish: "完成记录",
  });
  const RECEIPT_STATE_LABELS = Object.freeze({
    sent: "已发送",
    publishing: "发布中",
    pending: "等待回执",
    unknown: "结果未知",
    failed: "失败",
    draft: "草稿",
  });
  const RECEIPT_ERROR_LABELS = Object.freeze({
    delivery_unknown: "发布回执未收到，请核对 QQ 空间；不会自动重发",
    qzone_write_failed: "QQ 空间写入失败",
    qzone_request_pending: "QQ 空间写入仍在等待回执",
    publishing_disabled: "发布权限未开启",
  });
  const CHAT_STATE_LABELS = Object.freeze({
    sent: "已发送",
    review: "待你审核",
    review_pending: "待你审核",
    awaiting_review: "待你审核",
    draft: "待你审核",
    sending: "发送中",
    not_attempted: "未发送",
    cancelled: "已取消",
    publishing: "发送中",
    pending: "等待发送",
    unknown: "结果未知",
    delivery_unknown: "结果未知",
    failed: "发送失败",
  });
  const RUN_REASON_LABELS = Object.freeze({
    model_not_configured: "尚未配置模型",
    already_running: "已有一次运行正在进行",
    disabled: "后台运行未启用",
    outside_active_hours: "当前不在活动时段",
    not_due: "尚未到下一次计划时间",
    daily_budget_reached: "已达到今日运行上限",
  });
  const THOUGHT_STATUSES = new Set(["open", "paused", "done", "dismissed", "expired"]);
  const THOUGHT_ACTIONS = new Set(["pause", "resume", "done", "dismiss"]);
  const THOUGHT_ACTION_LABELS = Object.freeze({
    pause: "已暂时放下",
    resume: "会继续记着",
    done: "这件事结束了",
    dismiss: "已放下这件事",
  });

  function clampInteger(value, fallback, minimum, maximum) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(minimum, Math.min(maximum, Math.round(parsed)));
  }

  function stringValue(value, fallback = "") {
    return typeof value === "string" ? value : fallback;
  }

  function stringList(value) {
    if (!Array.isArray(value)) return [];
    return [...new Set(value.map(item => stringValue(item).trim()).filter(Boolean))];
  }

  function normalizeConfig(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const publishMode = PUBLISH_MODES.has(source.publishMode) ? source.publishMode : DEFAULT_CONFIG.publishMode;
    const publishVisibility = PUBLISH_VISIBILITIES.has(source.publishVisibility)
      ? source.publishVisibility
      : DEFAULT_CONFIG.publishVisibility;
    return {
      enabled: source.enabled === true,
      activityFrequency: clampInteger(source.activityFrequency, DEFAULT_CONFIG.activityFrequency, 25, 200),
      intervalMinutes: clampInteger(source.intervalMinutes, DEFAULT_CONFIG.intervalMinutes, 5, 1440),
      dailyRunLimit: clampInteger(source.dailyRunLimit, DEFAULT_CONFIG.dailyRunLimit, 1, 48),
      maxSteps: clampInteger(source.maxSteps, DEFAULT_CONFIG.maxSteps, 2, 12),
      activeStartHour: clampInteger(source.activeStartHour, DEFAULT_CONFIG.activeStartHour, 0, 23),
      activeEndHour: clampInteger(source.activeEndHour, DEFAULT_CONFIG.activeEndHour, 0, 23),
      instructions: stringValue(source.instructions).trim(),
      directories: stringList(source.directories),
      sources: stringList(source.sources),
      allowWeb: source.allowWeb !== false,
      allowQqRead: source.allowQqRead === true,
      allowQqChat: source.allowQqChat === true,
      qqChatOwner: stringValue(source.qqChatOwner).trim(),
      qqChatIntervalMinutes: clampInteger(source.qqChatIntervalMinutes,
        DEFAULT_CONFIG.qqChatIntervalMinutes, 30, 10080),
      publishMode,
      publishAccount: stringValue(source.publishAccount).trim(),
      publishVisibility,
      dailyPublishLimit: clampInteger(source.dailyPublishLimit, DEFAULT_CONFIG.dailyPublishLimit, 1, 5),
    };
  }

  function normalizeRhythm(raw) {
    const source = Array.isArray(raw) ? raw : [];
    const byKind = new Map();
    for (const item of source) {
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;
      const kind = stringValue(item.kind).trim();
      if (RHYTHM_KINDS.has(kind) && !byKind.has(kind)) byKind.set(kind, item);
    }
    return RHYTHM_DEFAULTS.map(defaultItem => {
      const item = byKind.get(defaultItem.kind) || {};
      return {
        kind: defaultItem.kind,
        label: stringValue(item.label, defaultItem.label).trim() || defaultItem.label,
        enabled: item.enabled === true,
        nextAt: stringValue(item.nextAt),
        intervalMinutes: clampInteger(item.intervalMinutes, defaultItem.intervalMinutes, 1, 10080),
        runsToday: clampInteger(item.runsToday, 0, 0, 1000000),
      };
    });
  }

  function normalizeState(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const permissions = source.permissions && typeof source.permissions === "object"
      ? source.permissions
      : {};
    const rhythm = normalizeRhythm(source.rhythm);
    const chatRhythm = rhythm.find(item => item.kind === "chat");
    const nextRhythmAt = rhythm
      .filter(item => item.enabled && item.nextAt)
      .map(item => item.nextAt)
      .sort()[0] || "";
    return {
      running: source.running === true,
      nextRunAt: stringValue(source.nextRunAt) || nextRhythmAt,
      runsToday: clampInteger(source.runsToday, 0, 0, 1000000),
      rhythm,
      note: stringValue(source.note),
      lastDecision: stringValue(source.lastDecision),
      lastError: stringValue(source.lastError),
      permissions: {
        web: permissions.web === true,
        file: permissions.file === true,
        qq: permissions.qq === true,
      },
      modelConfigured: source.modelConfigured === true,
      configuredQqAccount: stringValue(source.configuredQqAccount).trim(),
      configuredQqOwners: stringList(source.configuredQqOwners),
      nextChatAt: stringValue(source.nextChatAt) || chatRhythm?.nextAt || "",
    };
  }

  function normalizeStep(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    return {
      action: stringValue(source.action, "步骤"),
      ok: source.ok !== false,
      summary: stringValue(source.summary),
      source: source.source,
    };
  }

  function normalizeActivity(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    return {
      id: stringValue(source.id),
      kind: stringValue(source.kind, "legacy").trim() || "legacy",
      startedAt: stringValue(source.startedAt),
      finishedAt: stringValue(source.finishedAt),
      status: stringValue(source.status, "unknown"),
      summary: stringValue(source.summary),
      steps: Array.isArray(source.steps) ? source.steps.map(normalizeStep) : [],
    };
  }

  function normalizeDraft(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const state = DRAFT_STATES.has(source.state) ? source.state : "unknown";
    return {
      id: stringValue(source.id),
      content: stringValue(source.content),
      account: stringValue(source.account),
      visibility: stringValue(source.visibility, DEFAULT_CONFIG.publishVisibility),
      state,
      createdAt: stringValue(source.createdAt),
      receipt: source.receipt,
    };
  }

  function normalizeChat(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const messages = Array.isArray(source.messages)
      ? source.messages
        .map(item => stringValue(item).trim())
        .filter(Boolean)
        .slice(0, 12)
      : [];
    const receipt = source.receipt;
    const receiptState = receipt && typeof receipt === "object" && !Array.isArray(receipt)
      ? stringValue(receipt.state || receipt.deliveryState)
      : "";
    const state = stringValue(source.state || receiptState, "unknown").trim() || "unknown";
    return {
      id: stringValue(source.id),
      account: stringValue(source.account).trim(),
      owner: stringValue(source.owner).trim(),
      messages,
      state,
      createdAt: stringValue(source.createdAt),
      receipt,
    };
  }

  function normalizeExperience(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    return {
      id: stringValue(source.id),
      scope: stringValue(source.scope),
      channel: stringValue(source.channel),
      kind: stringValue(source.kind),
      status: stringValue(source.status, "observed"),
      summary: stringValue(source.summary),
      sourceRefs: stringList(source.sourceRefs),
      createdAt: stringValue(source.createdAt),
      updatedAt: stringValue(source.updatedAt),
    };
  }

  function normalizeThought(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const parsedRevision = Number(source.revision);
    const revision = Number.isFinite(parsedRevision)
      ? Math.max(0, Math.round(parsedRevision))
      : 0;
    const status = stringValue(source.status, "open").trim() || "open";
    return {
      id: stringValue(source.id),
      content: stringValue(source.content),
      kind: source.kind === "topic" ? "topic" : "interest",
      status: THOUGHT_STATUSES.has(status) ? status : "open",
      revision,
      sourceRefs: stringList(source.sourceRefs),
      dueAt: stringValue(source.dueAt),
      expiresAt: stringValue(source.expiresAt),
      lastSharedAt: stringValue(source.lastSharedAt),
      sharingState: stringValue(source.sharingState),
    };
  }

  function normalizeContinuity(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    return {
      experiences: Array.isArray(source.experiences)
        ? source.experiences.map(normalizeExperience)
        : [],
      thoughts: Array.isArray(source.thoughts)
        ? source.thoughts.map(normalizeThought)
        : [],
    };
  }

  function normalizeSnapshot(raw) {
    const source = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    const state = normalizeState(source.state);
    const config = normalizeConfig(source.config);
    const owners = state.configuredQqOwners;
    // A single configured owner is safe to preselect. Keep an explicit valid
    // value across polling; an owner that is no longer in the configured list
    // cannot be submitted by the UI.
    if (!config.qqChatOwner && owners.length === 1) {
      config.qqChatOwner = owners[0];
    } else if (config.qqChatOwner && owners.length > 0 && !owners.includes(config.qqChatOwner)) {
      config.qqChatOwner = "";
    }
    return {
      ok: source.ok !== false,
      config,
      state,
      activities: Array.isArray(source.activities) ? source.activities.map(normalizeActivity) : [],
      drafts: Array.isArray(source.drafts) ? source.drafts.map(normalizeDraft) : [],
      chats: Array.isArray(source.chats) ? source.chats.map(normalizeChat).slice(0, 20) : [],
      continuity: normalizeContinuity(source.continuity),
    };
  }

  function textLines(value) {
    return String(value || "")
      .split("\n")
      .map(item => item.trim())
      .filter(Boolean);
  }

  function safeHttpUrl(value) {
    const candidate = stringValue(value).trim();
    try {
      const parsed = new URL(candidate);
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
    } catch (_error) {
      return "";
    }
  }

  function formatTime(value) {
    if (!value) return "未安排";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  function statusLabel(value, labels) {
    const raw = stringValue(value, "unknown");
    return labels[raw] || raw;
  }

  function stepActionLabel(value) {
    const raw = stringValue(value, "步骤");
    return STEP_ACTION_LABELS[raw] || raw;
  }

  function sourceTextValue(source) {
    return typeof source === "string"
      ? source
      : source?.url || source?.href || source?.title || "";
  }

  function receiptText(draft) {
    const receipt = draft?.receipt;
    if (receipt == null || receipt === "") return "";
    if (typeof receipt === "string") return `消息：${receipt}`;
    if (typeof receipt !== "object" || Array.isArray(receipt)) return `消息：${String(receipt)}`;
    const nested = receipt.receipt && typeof receipt.receipt === "object" && !Array.isArray(receipt.receipt)
      ? receipt.receipt
      : {};
    const state = nested.state || receipt.deliveryState || receipt.state || draft.state || "unknown";
    const message = receipt.message || nested.message || RECEIPT_ERROR_LABELS[receipt.error]
      || (receipt.error ? "操作未完成" : "");
    const platformId = nested.platformId || receipt.platformId || "";
    const parts = [`状态：${statusLabel(state, RECEIPT_STATE_LABELS)}`];
    if (message) parts.push(`消息：${message}`);
    if (platformId) parts.push(`平台编号：${platformId}`);
    return parts.join(" · ");
  }

  function chatReceiptText(chat) {
    const receipt = chat?.receipt;
    if (receipt == null || receipt === "") return "";
    if (typeof receipt === "string") return `回执：${receipt}`;
    if (typeof receipt !== "object" || Array.isArray(receipt)) return `回执：${String(receipt)}`;
    const nested = receipt.receipt && typeof receipt.receipt === "object" && !Array.isArray(receipt.receipt)
      ? receipt.receipt
      : {};
    const state = nested.state || receipt.deliveryState || receipt.state || chat.state || "unknown";
    const message = ["sent", "draft"].includes(state) ? "" : receipt.message || nested.message || RECEIPT_ERROR_LABELS[receipt.error]
      || (receipt.error ? "操作未完成" : "");
    const platformId = receipt.messageId || nested.platformId || receipt.platformId || "";
    const parts = [`回执：${statusLabel(state, CHAT_STATE_LABELS)}`];
    if (message) parts.push(message);
    if (platformId) parts.push(`平台编号：${platformId}`);
    return parts.join(" · ");
  }

  function responseError(response, data) {
    return data?.error || data?.message || `HTTP ${response?.status || 0}`;
  }

  function createController({
    elements = {},
    request = (...args) => fetch(...args),
    notify,
    setInterval: setIntervalFn = (...args) => setInterval(...args),
    clearInterval: clearIntervalFn = timer => clearInterval(timer),
    pollIntervalMs = 5000,
    now = () => Date.now(),
    document: documentPort,
  } = {}) {
    if (typeof request !== "function") throw new TypeError("life request port is required");

    let bound = false;
    let active = false;
    let open = false;
    let loading = 0;
    let actionInFlight = false;
    let thoughtActionInFlight = false;
    let dirty = false;
    let rendering = false;
    let timer = null;
    let requestSequence = 0;
    let draftRenderSignature = "";
    let snapshot = normalizeSnapshot({});
    let qqChatOwnerDraft = null;
    const draftEdits = new Map();

    function getDocument() {
      return documentPort || elements.page?.ownerDocument || elements.drafts?.ownerDocument
        || (typeof document !== "undefined" ? document : null);
    }

    function lifeElement(key, id) {
      return elements[key] || getDocument()?.getElementById?.(id);
    }

    function setText(element, value) {
      if (element) element.textContent = value == null ? "" : String(value);
    }

    function setDisabled(element, disabled) {
      if (element) element.disabled = Boolean(disabled);
    }

    function setStatus(message, tone = "") {
      setText(elements.status, message);
      if (elements.status) elements.status.hidden = false;
      if (elements.status?.classList?.toggle) elements.status.classList.toggle("error", tone === "error");
      if (elements.runtimeStatus?.classList?.toggle) {
        elements.runtimeStatus.classList.toggle("error", tone === "error");
      }
    }

    function collectConfig() {
      const configuredOwners = snapshot.state.configuredQqOwners;
      const selectedOwner = elements.qqChatOwner
        ? stringValue(elements.qqChatOwner.value).trim()
        : snapshot.config.qqChatOwner;
      return normalizeConfig({
        enabled: Boolean(elements.enabled?.checked),
        activityFrequency: elements.activityFrequency?.value ?? snapshot.config.activityFrequency,
        // Keep legacy scheduling fields in the request for older servers. They
        // are intentionally sourced from the saved snapshot, never from UI
        // controls, so the page only exposes one overall rhythm setting.
        intervalMinutes: snapshot.config.intervalMinutes,
        dailyRunLimit: snapshot.config.dailyRunLimit,
        maxSteps: snapshot.config.maxSteps,
        activeStartHour: elements.activeStartHour?.value,
        activeEndHour: elements.activeEndHour?.value,
        instructions: elements.instructions?.value,
        directories: textLines(elements.directories?.value),
        sources: textLines(elements.sources?.value),
        allowWeb: Boolean(elements.allowWeb?.checked),
        allowQqRead: Boolean(elements.allowQqRead?.checked),
        allowQqChat: Boolean(elements.allowQqChat?.checked),
        qqChatOwner: elements.qqChatOwner
          ? (configuredOwners.includes(selectedOwner) ? selectedOwner : "")
          : selectedOwner,
        qqChatIntervalMinutes: snapshot.config.qqChatIntervalMinutes,
        publishMode: elements.publishMode?.value,
        publishAccount: elements.publishAccount?.value,
        publishVisibility: elements.publishVisibility?.value,
        dailyPublishLimit: elements.dailyPublishLimit?.value,
      });
    }

    function renderForm(config) {
      rendering = true;
      const fields = [
        [elements.enabled, config.enabled, "checked"],
        [elements.activityFrequency, config.activityFrequency, "value"],
        [elements.activeStartHour, config.activeStartHour, "value"],
        [elements.activeEndHour, config.activeEndHour, "value"],
        [elements.instructions, config.instructions, "value"],
        [elements.directories, config.directories.join("\n"), "value"],
        [elements.sources, config.sources.join("\n"), "value"],
        [elements.allowWeb, config.allowWeb, "checked"],
        [elements.allowQqRead, config.allowQqRead, "checked"],
        [elements.allowQqChat, config.allowQqChat, "checked"],
        [elements.qqChatOwner, config.qqChatOwner, "value"],
        [elements.publishMode, config.publishMode, "value"],
        [elements.publishAccount, config.publishAccount, "value"],
        [elements.publishVisibility, config.publishVisibility, "value"],
        [elements.dailyPublishLimit, config.dailyPublishLimit, "value"],
      ];
      for (const [element, value, property] of fields) {
        if (element) element[property] = value;
      }
      rendering = false;
      renderFrequencyPreview(config.activityFrequency);
    }

    function rhythmPreviewInterval(baseMinutes, frequency) {
      const normalizedFrequency = clampInteger(frequency, DEFAULT_CONFIG.activityFrequency, 25, 200);
      return Math.max(1, Math.round(baseMinutes * DEFAULT_CONFIG.activityFrequency / normalizedFrequency));
    }

    function renderFrequencyPreview(frequency) {
      const normalizedFrequency = clampInteger(frequency, DEFAULT_CONFIG.activityFrequency, 25, 200);
      setText(elements.activityFrequencyValue, `${normalizedFrequency}%`);
      if (!elements.activityFrequencyPreview) return;
      const parts = RHYTHM_DEFAULTS.map(item =>
        `${item.label}约每 ${rhythmPreviewInterval(item.intervalMinutes, normalizedFrequency)} 分钟考虑一次`);
      setText(elements.activityFrequencyPreview,
        `预览：${parts.join("；")}。只表示考虑频率，不代表一定安排。`);
    }

    function syncSourcePresetButton() {
      setDisabled(elements.addSource, !safeHttpUrl(elements.sourcePreset?.value));
    }

    function addSourceFromPreset() {
      const source = safeHttpUrl(elements.sourcePreset?.value);
      if (!source || !elements.sources) return false;
      const current = String(elements.sources.value || "");
      const duplicate = textLines(current).some(item => item === source || safeHttpUrl(item) === source);
      if (duplicate) {
        setStatus("该来源已在草稿中");
      } else {
        const hasContent = textLines(current).length > 0;
        const separator = hasContent && !/[\r\n]$/.test(current) ? "\n" : "";
        elements.sources.value = hasContent ? `${current}${separator}${source}` : source;
        markDirty();
        setStatus("已添加来源；保存设置后生效");
      }
      if (elements.sourcePreset) elements.sourcePreset.value = "";
      syncSourcePresetButton();
      return !duplicate;
    }

    function renderQqChatOwner() {
      const select = elements.qqChatOwner;
      if (!select) return;
      const owners = snapshot.state.configuredQqOwners;
      // Dirty form values are authoritative until save. A background refresh
      // must not turn a user's choice back into the server's older value.
      const currentValue = stringValue(select.value).trim();
      if (dirty && qqChatOwnerDraft === null) qqChatOwnerDraft = currentValue;
      const selected = dirty
        ? (owners.includes(qqChatOwnerDraft) ? qqChatOwnerDraft : "")
        : (owners.includes(snapshot.config.qqChatOwner) ? snapshot.config.qqChatOwner : "");
      select.replaceChildren?.();
      const doc = getDocument();
      if (doc?.createElement) {
        const empty = doc.createElement("option");
        empty.value = "";
        empty.textContent = owners.length ? "请选择一位主人" : "暂无可用主人";
        select.appendChild(empty);
        for (const owner of owners) {
          const option = doc.createElement("option");
          option.value = owner;
          option.textContent = `主人 QQ：${owner}`;
          select.appendChild(option);
        }
      }
      select.value = selected;
      setDisabled(select, owners.length === 0);
    }

    function appendSource(parent, source, doc) {
      const value = sourceTextValue(source);
      const url = safeHttpUrl(value);
      if (!url || !doc?.createElement) {
        const text = doc?.createTextNode?.(String(value)) || null;
        if (text) parent?.appendChild?.(text);
        else setText(parent, value);
        return;
      }
      const link = doc.createElement("a");
      link.className = "life-source-link";
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = url;
      parent?.appendChild?.(link);
    }

    function renderRhythm() {
      const list = elements.rhythm || lifeElement("rhythm", "lifeRhythmList");
      if (!list) return;
      list.replaceChildren?.();
      const doc = getDocument();
      for (const rhythm of snapshot.state.rhythm) {
        const card = doc?.createElement?.("article");
        if (!card) continue;
        card.className = "life-rhythm-item";
        card.dataset.kind = rhythm.kind;
        card.dataset.enabled = rhythm.enabled ? "true" : "false";
        const head = doc.createElement("div");
        head.className = "life-rhythm-head";
        const title = doc.createElement("strong");
        title.textContent = rhythm.label || ACTIVITY_KIND_LABELS[rhythm.kind] || rhythm.kind;
        const state = doc.createElement("span");
        state.textContent = rhythm.enabled ? "已纳入安排" : "暂未纳入";
        head.append(title, state);
        card.appendChild(head);
        const interval = doc.createElement("div");
        interval.className = "life-rhythm-meta";
        interval.textContent = `大约每 ${rhythm.intervalMinutes} 分钟考虑一次`
          + (rhythm.enabled ? ` · 今日 ${rhythm.runsToday} 次` : " · 暂未纳入安排");
        card.appendChild(interval);
        const next = doc.createElement("div");
        next.className = "life-rhythm-next";
        next.textContent = rhythm.enabled && rhythm.nextAt
          ? `下次考虑：${formatTime(rhythm.nextAt)}`
          : "下次考虑：未安排";
        card.appendChild(next);
        list.appendChild(card);
      }
    }

    function renderState() {
      const state = snapshot.state;
      const account = state.configuredQqAccount;
      setDisabled(elements.useQqAccount, !account);
      setText(elements.accountHint, account
        ? `当前 QQ 代理账号：${account}。账号留空时，保存会绑定此账号；保存后不会随代理切换而自动更换。`
        : "尚未配置 QQ 代理账号，请先到 QQ 页配置；也可在上方填写要绑定的完整 QQ 号。");
      if (elements.publishAccount) {
        elements.publishAccount.placeholder = account ? `留空时绑定 ${account}` : "请先配置 QQ 代理账号或填写完整 QQ 号";
      }
      renderQqChatOwner();
      const owners = state.configuredQqOwners;
      const currentOwner = stringValue(elements.qqChatOwner?.value).trim();
      const selectedOwner = dirty && elements.qqChatOwner
        ? (owners.includes(currentOwner) ? currentOwner : "")
        : (owners.includes(snapshot.config.qqChatOwner) ? snapshot.config.qqChatOwner : "");
      if (!owners.length) {
        setText(elements.qqChatHint, "先到 QQ 页填写主人 QQ，再回来选择要聊天的主人。");
      } else if (!selectedOwner) {
        setText(elements.qqChatHint, "请选择一位已在 QQ 页设置的主人；主动聊天会像平常一样聊兴趣和最近读到的内容。");
      } else {
        setText(elements.qqChatHint, `已选择主人 QQ：${selectedOwner}。她会自己决定什么时候想来聊天。`);
      }
      if (elements.qqChatHint?.classList?.toggle) {
        elements.qqChatHint.classList.toggle("error", Boolean(snapshot.config.allowQqChat && !selectedOwner));
      }
      const runtimeLabel = state.running
        ? "运行中"
        : snapshot.config.enabled
        ? "等待下一次考虑"
        : "已禁用";
      setText(elements.runtimeStatus, runtimeLabel);
      if (elements.runtimeStatus?.dataset) elements.runtimeStatus.dataset.running = state.running ? "true" : "false";
      setText(elements.nextRun, !snapshot.config.enabled
        ? "未启用"
        : state.nextRunAt
        ? formatTime(state.nextRunAt)
        : "未安排");
      setText(elements.nextChat, !snapshot.config.allowQqChat
        ? "未启用"
        : state.nextChatAt
        ? formatTime(state.nextChatAt)
        : "未安排");
      setText(elements.runsToday, `${state.runsToday}`);
      renderRhythm();
      setText(elements.interestNote, state.note || "还没有持续兴趣笔记。");
      const lastDecision = lifeElement("lastDecision", "lifeLastDecision");
      if (lastDecision) {
        const text = state.lastDecision.trim();
        lastDecision.hidden = !text;
        setText(lastDecision, text ? `这会儿的想法：${text}` : "");
      }
      setText(elements.lastError, state.lastError || "无");
      if (elements.lastError?.classList?.toggle) elements.lastError.classList.toggle("error", Boolean(state.lastError));
      const permissionLabels = [
        ["网页", state.permissions.web],
        ["文件", state.permissions.file],
        ["QQ", state.permissions.qq],
        ["模型", state.modelConfigured],
      ];
      if (elements.permissions) {
        elements.permissions.replaceChildren?.();
        const doc = getDocument();
        for (const [label, allowed] of permissionLabels) {
          const item = doc?.createElement?.("span");
          if (!item) continue;
          item.className = allowed ? "life-permission allowed" : "life-permission denied";
          item.textContent = `${label}：${allowed ? "已授权" : "未授权"}`;
          elements.permissions.appendChild(item);
        }
      }
    }

    function thoughtSharingLabel(thought) {
      const sharingState = stringValue(thought.sharingState).trim().toLowerCase();
      if (sharingState === "sent" || thought.lastSharedAt) return "已聊过";
      if (["draft", "sending", "pending"].includes(sharingState)) return "待发";
      if (sharingState === "unknown") return "待确认";
      return "";
    }

    function renderThoughts() {
      const list = lifeElement("thoughts", "lifeThoughtsList");
      if (!list) return;
      list.replaceChildren?.();
      const doc = getDocument();
      const thoughts = snapshot.continuity.thoughts.filter(thought => ["open", "paused"].includes(thought.status));
      if (!thoughts.length) {
        const empty = doc?.createElement?.("div");
        if (empty) {
          empty.className = "life-empty";
          empty.textContent = "现在没有挂着的事。";
          list.appendChild(empty);
        }
        return;
      }
      for (const thought of thoughts) {
        const card = doc?.createElement?.("article");
        if (!card) continue;
        card.className = "life-thought-card";
        card.dataset.status = thought.status;
        const copy = doc.createElement("div");
        copy.className = "life-thought-copy";
        const content = doc.createElement("div");
        content.className = "life-thought-content";
        content.textContent = thought.content || "还没写下具体内容。";
        copy.appendChild(content);
        const metaParts = [];
        if (thought.status === "paused") metaParts.push("暂时放着");
        const sharing = thoughtSharingLabel(thought);
        if (sharing) metaParts.push(sharing);
        if (thought.dueAt) metaParts.push(`之后想起：${formatTime(thought.dueAt)}`);
        if (metaParts.length) {
          const meta = doc.createElement("div");
          meta.className = "life-thought-meta";
          meta.textContent = metaParts.join(" · ");
          copy.appendChild(meta);
        }
        card.appendChild(copy);

        const actions = doc.createElement("div");
        actions.className = "life-thought-actions";
        const addAction = (action, label) => {
          const button = doc.createElement("button");
          button.type = "button";
          button.className = "life-thought-action";
          button.textContent = label;
          button.dataset.lifeThoughtId = thought.id;
          button.dataset.lifeThoughtRevision = String(thought.revision);
          button.dataset.lifeThoughtAction = action;
          button.disabled = actionInFlight || thoughtActionInFlight || !thought.id;
          button.setAttribute?.("aria-label", `${label}：${thought.content || "这件事"}`);
          button.addEventListener?.("click", () => actThought(thought.id, thought.revision, action).catch(() => {}));
          actions.appendChild(button);
        };
        addAction(thought.status === "paused" ? "resume" : "pause",
          thought.status === "paused" ? "继续" : "暂停");
        addAction("done", "结束");
        addAction("dismiss", "放下");
        card.appendChild(actions);
        list.appendChild(card);
      }
    }

    function renderChats() {
      const list = elements.chats;
      if (!list) return;
      list.replaceChildren?.();
      const doc = getDocument();
      if (!snapshot.chats.length) {
        const empty = doc?.createElement?.("div");
        if (empty) {
          empty.className = "life-empty";
          empty.textContent = "还没有主动找你聊天。";
          list.appendChild(empty);
        }
        return;
      }
      for (const chat of snapshot.chats) {
        const card = doc?.createElement?.("article");
        if (!card) continue;
        card.className = "life-card life-chat-card";
        card.dataset.state = chat.state;
        const head = doc.createElement("div");
        head.className = "life-card-head";
        const title = doc.createElement("strong");
        title.textContent = "和你聊了聊";
        const meta = doc.createElement("span");
        const who = chat.owner || chat.account;
        meta.textContent = `${statusLabel(chat.state, CHAT_STATE_LABELS)} · ${formatTime(chat.createdAt)}${who ? ` · ${who}` : ""}`;
        head.append(title, meta);
        card.appendChild(head);
        const messages = doc.createElement("div");
        messages.className = "life-chat-messages";
        if (chat.messages.length) {
          for (const message of chat.messages) {
            const bubble = doc.createElement("div");
            bubble.className = "life-chat-message";
            bubble.textContent = message;
            messages.appendChild(bubble);
          }
        } else {
          messages.textContent = "这次没有留下可显示的聊天内容。";
        }
        card.appendChild(messages);
        const receipt = chatReceiptText(chat);
        if (receipt) {
          const receiptNode = doc.createElement("div");
          receiptNode.className = "life-card-meta life-chat-receipt";
          receiptNode.textContent = receipt;
          card.appendChild(receiptNode);
        }
        list.appendChild(card);
      }
    }

    function renderActivities() {
      const list = elements.activities;
      if (!list) return;
      list.replaceChildren?.();
      const doc = getDocument();
      if (!snapshot.activities.length) {
        const empty = doc?.createElement?.("div");
        if (empty) {
          empty.className = "life-empty";
          empty.textContent = "还没有活动记录。";
          list.appendChild(empty);
        }
        return;
      }
      for (const activity of snapshot.activities) {
        const card = doc?.createElement?.("article");
        if (!card) continue;
        card.className = "life-card life-activity-card";
        const head = doc.createElement("div");
        head.className = "life-card-head";
        const title = doc.createElement("strong");
        title.textContent = activity.summary || activity.status || "自主生活活动";
        const meta = doc.createElement("span");
        const kindLabel = ACTIVITY_KIND_LABELS[activity.kind] || activity.kind;
        meta.textContent = `${kindLabel} · ${statusLabel(activity.status, ACTIVITY_STATUS_LABELS)} · ${formatTime(activity.startedAt)}`;
        head.append(title, meta);
        card.appendChild(head);
        if (activity.finishedAt) {
          const finished = doc.createElement("div");
          finished.className = "life-card-meta";
          finished.textContent = `结束：${formatTime(activity.finishedAt)}`;
          card.appendChild(finished);
        }
        const steps = doc.createElement("ol");
        steps.className = "life-step-list";
        for (const step of activity.steps) {
          const row = doc.createElement("li");
          row.className = step.ok ? "life-step ok" : "life-step failed";
          const copy = doc.createElement("span");
          const sourceValue = sourceTextValue(step.source);
          const summary = step.summary || (step.ok ? "完成" : "失败");
          const repeatedSource = Boolean(sourceValue && summary === sourceValue);
          copy.textContent = `${stepActionLabel(step.action)}${repeatedSource ? "" : `：${summary}`}`;
          row.appendChild(copy);
          if (step.source) appendSource(row, step.source, doc);
          steps.appendChild(row);
        }
        card.appendChild(steps);
        list.appendChild(card);
      }
    }

    function draftRenderKey() {
      return JSON.stringify({
        busy: actionInFlight,
        publishAccount: snapshot.config.publishAccount,
        drafts: snapshot.drafts.map(draft => ({
          id: draft.id,
          content: draft.content,
          account: draft.account,
          visibility: draft.visibility,
          state: draft.state,
          createdAt: draft.createdAt,
          receipt: draft.receipt,
        })),
      });
    }

    function captureDraftFocus(doc) {
      const active = doc?.activeElement;
      const id = active?.dataset?.lifeDraftId;
      if (!id) return null;
      return {
        id,
        selectionStart: typeof active.selectionStart === "number" ? active.selectionStart : null,
        selectionEnd: typeof active.selectionEnd === "number" ? active.selectionEnd : null,
        scrollTop: typeof active.scrollTop === "number" ? active.scrollTop : null,
      };
    }

    function restoreDraftFocus(list, focus) {
      if (!focus) return;
      const candidates = list.querySelectorAll?.("[data-life-draft-id]") || [];
      const target = [...candidates].find(element => element.dataset?.lifeDraftId === focus.id);
      if (!target) return;
      target.focus?.();
      if (focus.selectionStart != null && typeof target.setSelectionRange === "function") {
        target.setSelectionRange(focus.selectionStart, focus.selectionEnd ?? focus.selectionStart);
      }
      if (focus.scrollTop != null) target.scrollTop = focus.scrollTop;
    }

    function renderDrafts() {
      const list = elements.drafts;
      if (!list) return;
      const signature = draftRenderKey();
      if (signature === draftRenderSignature) return;
      draftRenderSignature = signature;
      const doc = getDocument();
      const focus = captureDraftFocus(doc);
      list.replaceChildren?.();
      if (!snapshot.drafts.length) {
        const empty = doc?.createElement?.("div");
        if (empty) {
          empty.className = "life-empty";
          empty.textContent = "还没有草稿。";
          list.appendChild(empty);
        }
        return;
      }
      for (const draft of snapshot.drafts) {
        const card = doc?.createElement?.("article");
        if (!card) continue;
        card.className = "life-card life-draft-card";
        const head = doc.createElement("div");
        head.className = "life-card-head";
        const title = doc.createElement("strong");
        title.textContent = "动态草稿";
        const meta = doc.createElement("span");
        const publishAccount = draft.account || snapshot.config.publishAccount || "";
        const account = publishAccount || "尚未绑定";
        meta.textContent = `${statusLabel(draft.state, DRAFT_STATE_LABELS)} · 发布账号：${account} · ${VISIBILITY_LABELS[draft.visibility] || draft.visibility || "好友可见"}`;
        head.append(title, meta);
        card.appendChild(head);
        const editor = doc.createElement("textarea");
        editor.className = "life-draft-editor";
        editor.value = draftEdits.has(draft.id) ? draftEdits.get(draft.id) : draft.content;
        editor.dataset.lifeDraftId = draft.id;
        editor.disabled = draft.state !== "draft";
        editor.setAttribute("aria-label", `编辑草稿 ${draft.id || "未命名"}`);
        if (draft.state === "draft") {
          editor.addEventListener?.("input", () => draftEdits.set(draft.id, editor.value));
        }
        card.appendChild(editor);
        if (draft.receipt != null) {
          const receipt = doc.createElement("div");
          receipt.className = "life-card-meta";
          receipt.textContent = receiptText(draft);
          card.appendChild(receipt);
        }
        if (draft.state === "draft") {
          const actions = doc.createElement("div");
          actions.className = "life-actions";
          const publish = doc.createElement("button");
          publish.type = "button";
          publish.textContent = "发布";
          publish.disabled = actionInFlight || !publishAccount;
          if (!publishAccount) publish.title = "先在上方填写并保存 QQ 账号";
          publish.addEventListener?.("click", () => actDraft(draft.id, "publish", editor.value));
          const dismiss = doc.createElement("button");
          dismiss.type = "button";
          dismiss.textContent = "舍弃";
          dismiss.disabled = actionInFlight;
          dismiss.addEventListener?.("click", () => actDraft(draft.id, "dismiss"));
          actions.append(publish, dismiss);
          card.appendChild(actions);
        }
        list.appendChild(card);
      }
      restoreDraftFocus(list, focus);
    }

    function render() {
      renderForm(snapshot.config);
      renderState();
      renderThoughts();
      renderActivities();
      renderChats();
      renderDrafts();
    }

    function applySnapshot(raw) {
      const next = normalizeSnapshot(raw);
      const rawConfig = raw && typeof raw === "object" && raw.config && typeof raw.config === "object"
        ? raw.config
        : {};
      const previousOwner = snapshot.config.qqChatOwner;
      if (!Object.prototype.hasOwnProperty.call(rawConfig, "qqChatOwner")
        && previousOwner && next.state.configuredQqOwners.includes(previousOwner)) {
        next.config.qqChatOwner = previousOwner;
      }
      snapshot = next;
      const ids = new Set(next.drafts.map(draft => draft.id));
      for (const id of draftEdits.keys()) if (!ids.has(id)) draftEdits.delete(id);
      if (!dirty) renderForm(next.config);
      renderState();
      renderThoughts();
      renderActivities();
      renderChats();
      renderDrafts();
      return next;
    }

    async function readSnapshot(response) {
      const data = await response.json();
      if (!response.ok || data?.ok === false) throw new Error(responseError(response, data));
      return applySnapshot(data);
    }

    async function refresh({silent = false} = {}) {
      const requestToken = ++requestSequence;
      loading += 1;
      try {
        const response = await request("/api/life", { cache: "no-store" });
        const data = await response.json();
        if (requestToken !== requestSequence) return { stale: true };
        if (!response.ok || data?.ok === false) throw new Error(responseError(response, data));
        const next = applySnapshot(data);
        if (!silent) setStatus(next.state.running ? "已刷新，后台正在运行" : "已刷新");
        return next;
      } catch (error) {
        if (requestToken === requestSequence) {
          setStatus(`读取自主生活状态失败：${error.message}`, "error");
          if (!silent) notify?.(`自主生活状态读取失败：${error.message}`);
        }
        throw error;
      } finally {
        loading = Math.max(0, loading - 1);
      }
    }

    async function postSnapshot(url, body) {
      const response = await request(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok || data?.ok === false) throw new Error(responseError(response, data));
      if (data?.config && data?.state) applySnapshot(data);
      return data;
    }

    async function save({silent = false} = {}) {
      if (actionInFlight) return snapshot;
      actionInFlight = true;
      renderDrafts();
      try {
        const config = collectConfig();
        if (config.allowQqChat && !config.qqChatOwner) {
          const message = snapshot.state.configuredQqOwners.length
            ? "请先选择一位 QQ 主人，再保存主动聊天设置"
            : "请先到 QQ 页设置主人 QQ，再保存主动聊天设置";
          setStatus(message, "error");
          notify?.(message);
          const validationError = new Error(message);
          validationError.validation = true;
          throw validationError;
        }
        const data = await postSnapshot("/api/life/config", { config });
        if (data?.config && data?.state) applySnapshot(data);
        else {
          snapshot.config = normalizeConfig(data?.config || config);
        }
        dirty = false;
        qqChatOwnerDraft = null;
        renderForm(snapshot.config);
        setStatus("自主生活设置已保存");
        if (!silent) notify?.("自主生活设置已保存");
        return snapshot;
      } catch (error) {
        if (!error?.validation) {
          setStatus(`保存自主生活设置失败：${error.message}`, "error");
          notify?.(`自主生活设置保存失败：${error.message}`);
        }
        throw error;
      } finally {
        actionInFlight = false;
        renderDrafts();
      }
    }

    async function run() {
      if (actionInFlight) return null;
      actionInFlight = true;
      try {
        if (dirty) {
          actionInFlight = false;
          await save({ silent: true });
          actionInFlight = true;
        }
        const response = await request("/api/life/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(responseError(response, data));
        if (data?.ok === false || data.started === false) {
          const reasonCode = stringValue(data.reason, "当前未启动");
          const reason = RUN_REASON_LABELS[reasonCode] || reasonCode;
          setStatus(`未开始本次运行：${reason}`, "error");
          notify?.(`自主生活未开始：${reason}`);
        } else {
          setStatus("已请求试运行，正在等待活动记录");
          notify?.("自主生活试运行已启动");
        }
        await refresh({ silent: true });
        return data;
      } catch (error) {
        setStatus(`试运行失败：${error.message}`, "error");
        notify?.(`自主生活试运行失败：${error.message}`);
        throw error;
      } finally {
        actionInFlight = false;
        renderDrafts();
      }
    }

    async function stop() {
      if (actionInFlight) return null;
      actionInFlight = true;
      try {
        const data = await postSnapshot("/api/life/stop", {});
        if (data?.config && data?.state) applySnapshot(data);
        await refresh({ silent: true });
        const message = "已停止后续自主生活动作；已发出的网络请求仍可能收到回执";
        setStatus(message);
        notify?.(message);
        return data;
      } catch (error) {
        setStatus(`停止自主生活失败：${error.message}`, "error");
        notify?.(`停止自主生活失败：${error.message}`);
        throw error;
      } finally {
        actionInFlight = false;
        renderDrafts();
      }
    }

    async function actDraft(id, action, content) {
      if (!id || actionInFlight) return null;
      actionInFlight = true;
      try {
        const body = { id, action };
        if (action === "publish") body.content = String(content || "");
        const response = await request("/api/life/draft", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(responseError(response, data));
        // QQ publish can return HTTP 200 with ok:false when the receipt says
        // the external outcome is unknown. Keep the returned receipt visible
        // through the next refresh instead of treating it as a transport error.
        draftEdits.delete(id);
        if (data?.ok === false) {
          const message = stringValue(data.message, "草稿发布结果未知，请查看回执");
          setStatus(message, "error");
          notify?.(message);
          await refresh({ silent: true });
          return data;
        }
        if (data?.config && data?.state) applySnapshot(data);
        const successMessage = action === "publish" ? "已提交草稿发布" : "草稿已舍弃";
        setStatus(successMessage);
        notify?.(successMessage);
        await refresh({ silent: true });
        return data;
      } catch (error) {
        setStatus(`草稿操作失败：${error.message}`, "error");
        notify?.(`草稿操作失败：${error.message}`);
        throw error;
      } finally {
        actionInFlight = false;
        renderDrafts();
      }
    }

    async function actThought(id, revision, action) {
      if (!id || !THOUGHT_ACTIONS.has(action) || actionInFlight || thoughtActionInFlight) return null;
      thoughtActionInFlight = true;
      renderThoughts();
      try {
        const response = await request("/api/life/thought", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, revision, action }),
        });
        const data = await response.json();
        if (!response.ok || data?.ok === false) throw new Error(responseError(response, data));
        await refresh({ silent: true });
        const message = THOUGHT_ACTION_LABELS[action] || "想法已更新";
        setStatus(message);
        notify?.(message);
        return data;
      } catch (error) {
        setStatus(`更新还惦记的事失败：${error.message}`, "error");
        notify?.(`更新还惦记的事失败：${error.message}`);
        throw error;
      } finally {
        thoughtActionInFlight = false;
        renderThoughts();
      }
    }

    function markDirty() {
      if (!rendering) dirty = true;
    }

    function stopPolling() {
      if (timer != null) {
        clearIntervalFn(timer);
        timer = null;
      }
    }

    function startPolling() {
      stopPolling();
      if (!active) return;
      timer = setIntervalFn(() => {
        if (!open || loading > 0 || actionInFlight) return;
        refresh({ silent: true }).catch(() => {});
      }, pollIntervalMs);
    }

    function setActive(next) {
      active = Boolean(next);
      if (active) {
        startPolling();
        if (open && loading === 0) refresh({ silent: true }).catch(() => {});
      } else {
        stopPolling();
      }
      return active;
    }

    function bind() {
      if (bound) return false;
      bound = true;
      const formFields = [
        elements.enabled, elements.activityFrequency,
        elements.activeStartHour, elements.activeEndHour, elements.instructions, elements.directories,
        elements.sources, elements.allowWeb, elements.allowQqRead, elements.publishMode,
        elements.allowQqChat, elements.qqChatOwner,
        elements.publishAccount, elements.publishVisibility, elements.dailyPublishLimit,
      ];
      formFields.filter(Boolean).forEach(element => {
        const onDirty = element === elements.qqChatOwner
          ? () => {
            qqChatOwnerDraft = stringValue(element.value).trim();
            markDirty();
          }
          : element === elements.activityFrequency
          ? () => {
            markDirty();
            renderFrequencyPreview(element.value);
          }
          : markDirty;
        element.addEventListener?.("input", onDirty);
        element.addEventListener?.("change", onDirty);
      });
      elements.sourcePreset?.addEventListener?.("change", syncSourcePresetButton);
      elements.addSource?.addEventListener?.("click", () => addSourceFromPreset());
      syncSourcePresetButton();
      elements.save?.addEventListener?.("click", () => save().catch(() => {}));
      elements.run?.addEventListener?.("click", () => run().catch(() => {}));
      elements.stop?.addEventListener?.("click", () => stop().catch(() => {}));
      elements.refresh?.addEventListener?.("click", () => refresh().catch(() => {}));
      elements.useQqAccount?.addEventListener?.("click", () => {
        const account = snapshot.state.configuredQqAccount;
        if (!account || actionInFlight || !elements.publishAccount) return;
        elements.publishAccount.value = account;
        markDirty();
        setStatus(`已填入当前代理账号 ${account}；保存设置或试运行后生效`);
      });
      return true;
    }

    function openPage() {
      open = true;
      if (active && loading === 0) refresh({ silent: true }).catch(() => {});
    }

    function closePage() {
      open = false;
      stopPolling();
    }

    function getState() {
      return {
        bound,
        active,
        open,
        dirty,
        loading,
        actionInFlight,
        snapshot,
      };
    }

    return Object.freeze({
      bind,
      open: openPage,
      close: closePage,
      setActive,
      refresh,
      save,
      run,
      stop,
      actDraft,
      actThought,
      collectConfig,
      getSnapshot: () => snapshot,
      getState,
    });
  }

  return Object.freeze({
    DEFAULT_CONFIG,
    RHYTHM_DEFAULTS,
    normalizeConfig,
    normalizeRhythm,
    normalizeSnapshot,
    safeHttpUrl,
    createController,
  });
}));
