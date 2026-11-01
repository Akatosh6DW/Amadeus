(function attach(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusQqSetup = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function parseIds(value) {
    // regex-purpose: format
    const ids = [...new Set(String(value).split(/[\s,，;；]+/u).filter(Boolean))];
    if (ids.some(id => !/^\d+$/.test(id))) throw new Error("好友和群号只能填写数字，多个号码用逗号或换行分隔。");
    return ids;
  }

  function statusText(data) {
    return runState(data).label;
  }

  function gatewayRunning(data) {
    return data?.gatewayRunning ?? Boolean(data?.connected || data?.lifecycle?.active);
  }

  function replyMode(data) {
    return data?.policy?.mode !== "paused" && data?.policy?.mode
      ? data.policy.mode : data?.replyMode || "review";
  }

  function replyFormat(value) {
    return value === "voice" ? "voice" : "text";
  }

  function replyFormatLabel(value) {
    return replyFormat(value) === "voice" ? "语音回复" : "文字回复";
  }

  function replyIdentityLabel(value) {
    return value === "owner" ? "代我用第一人称回复" : "以 Amadeus 自己的身份说话";
  }

  function speechLanguageLabel(value) {
    return {"zh-CN":"中文", "ja-JP":"日文"}[value] || "跟随桌面";
  }

  function corpusStatusText(data, selectedStyle = "") {
    const status = data?.corpusStatus;
    if (!status || typeof status !== "object") return "";
    const entries = ["tieba", "kangba"].map(style => ({style, item: status[style]}))
      .filter(({item}) => item && typeof item === "object");
    if (!entries.length) return "";
    const describe = ({item}) => {
      const label = String(item.label || item.fileName || "论坛语料");
      const fileName = String(item.fileName || "语料索引");
      const count = Number(item.recordCount);
      if (item.installed !== true) return `${label}：未安装语料（${fileName}）`;
      if (!Number.isSafeInteger(count) || count <= 0 || item.usable === false)
        return `${label}：已安装但没有可用记录（${fileName}）`;
      return `${label}：已安装，${count} 条记录`;
    };
    const selected = entries.find(entry => entry.style === selectedStyle);
    const selectedLine = selected
      ? `当前选择「${String(selected.item.label || selected.style)}」：${describe(selected)}`
      : selectedStyle === "game" ? "当前选择「红莉栖人格」：不依赖论坛语料" : "";
    const detail = `论坛语料状态：${entries.map(describe).join("；")}。`;
    const missing = entries.some(({item}) => item.installed !== true || item.usable !== true);
    const instruction = missing
      ? "未安装时可将自己的 tieba.sqlite3 或 kangba.sqlite3 放入 Amadeus 用户资料目录的 persona 文件夹；不会自动下载或替换。"
      : "";
    return [selectedLine, detail, instruction].filter(Boolean).join(" ");
  }

  function needsBinding(data) {
    return Boolean(data?.login?.isLogin && data.login.accountId && data.login.accountId !== data.policy?.account_id);
  }

  // regex-purpose: format
  const ACCOUNT_ID_PATTERN = /^[0-9]{5,20}$/;

  function needsConnectionSetup(data) {
    return Boolean(data?.needsConnectionSetup);
  }

  function loginFailureNote(login) {
    // regex-purpose: format
    const code = /^[0-9/]{1,20}$/.test(String(login?.errorCode || "")) ? `（错误码 ${String(login.errorCode)}）` : "";
    return `扫码登录未完成${code}，请重新扫码并在手机 QQ 上确认`;
  }

  function runState(data) {
    if (!data) return {label: "正在读取状态…", icon: "…", note: "读取完成后，这里会显示下一步操作"};
    if (data.unavailable) return {label: "状态暂不可用", icon: "…", note: "展开「连接与后台」，点击刷新状态重试"};
    // Losing login must not remove the owner's ability to pause queued replies.
    const pending = state => gatewayRunning(data) && data.policy?.mode && data.policy.mode !== "paused"
      ? {...state, action: "pause", icon: "⏸", buttonText: undefined, title: "暂停 QQ 回复"} : state;
    const phase = data.connected ? "" : String(data.login?.loginPhase || "").trim().toLowerCase();
    if (phase === "login_failed") return pending({label: "QQ 登录失败", icon: "↻", buttonText: "重新扫码", tone: "waiting", action: "refresh_qr",
      title: "刷新登录二维码", note: loginFailureNote(data.login)});
    if (phase === "qrcode_scanned") return pending({label: "等待手机确认登录", icon: "…", tone: "waiting",
      title: "等待手机 QQ 确认", note: "二维码已扫描，请在手机 QQ 上确认登录"});
    if (phase === "generating_qrcode") return pending({label: "正在生成登录二维码…", icon: "…", tone: "waiting",
      note: "二维码生成后会自动显示在下方"});
    if (phase === "offline" || (!data.connected && data.login?.isOffline)) return pending({label: "QQ 登录已离线", icon: "↻",
      action: gatewayRunning(data) ? "refresh_qr" : "start", buttonText: "重新登录", tone: "waiting", title: "重新登录 QQ",
      note: "QQ 账号已离线，请重新扫码登录"});
    if (phase === "initializing") return pending({label: "正在初始化 QQ 登录…", icon: "…", tone: "waiting",
      note: "后台正在读取 QQ 登录状态，请稍候"});
    if (phase === "reconnecting") return pending({label: "正在重新连接 QQ…", icon: "…", tone: "waiting",
      note: "QQ 后台可访问，正在恢复登录连接"});
    if (gatewayRunning(data) && needsBinding(data)) return {label: "等待登录当前配置账号", icon: "…", tone: "waiting",
      note: "当前配置账号与实际登录账号不同，请点击「重新登录 QQ」并用手机 QQ 扫码；旧账号的设置不会沿用"};
    if (needsConnectionSetup(data)) return {label: "等待连接当前 QQ 账号", icon: "…", tone: "waiting",
      note: "当前配置账号已经登录，但 QQ 连接尚未完成；点击「连接这个账号」完成连接"};
    if (data.errors?.startup && (!gatewayRunning(data) || data.policy?.mode === "paused")) return {
      label: "QQ 启动失败", icon: "▶", tone: "waiting", action: "start", title: "重试连接 QQ", note: data.errors.startup};
    if (data.login?.loginPhase === "waiting_qrcode") {
      const active = gatewayRunning(data) && data.policy?.mode && data.policy.mode !== "paused";
      return {label: "等待扫码登录", icon: active ? "⏸" : "…", tone: "waiting", action: active ? "pause" : undefined,
        title: active ? "暂停 QQ 回复" : "等待手机 QQ 扫码", note: "请使用手机 QQ 扫描下方二维码"};
    }
    if (!gatewayRunning(data) && data.installed === false) return data.component?.available
      ? {label: "QQ 代管未启用", icon: "▶", action: data.component.clientFound ? "setup" : undefined,
        title: "连接本机 QQ", note: data.component.clientFound ? "已找到本机 QQ，点击连接并扫码；登录不会自动开始代管" : data.component.message}
      : {label: "QQ 接入组件不完整", icon: "…", note: "请修复或重新安装 Amadeus；程序不会下载 QQ 客户端"};
    if (!gatewayRunning(data) && !data.starting) return {label: data.login?.isLogin ? "QQ 已登录 · 后台未启动" : "后台未启动 · 等待 QQ 登录", action: "start", icon: "▶", tone: "off",
      title: "连接 QQ", note: data.login?.isLogin ? "点击 ▶ 启动 QQ 后台；登录账号仍需与当前配置一致" : "点击 ▶ 启动 QQ 后台，再用手机 QQ 登录"};
    if (gatewayRunning(data) && data.policy?.mode && data.policy.mode !== "paused") {
      const ready = data.connected && data.lifecycle?.active !== false;
      const individual = Object.keys(data.policy.conversation_overrides || {}).length > 0;
      const waiting = !data.login?.isLogin ? "等待 QQ 登录" : data.login?.isOffline ? "QQ 登录已离线" : "连接未就绪";
      return {label: ready ? (individual ? "代管已开启 · 按各会话设置回复" : data.policy.mode === "auto" ? "自动回复已开启" : "草稿模式已开启") : `代管已开启 · ${waiting}`,
        action: "pause", icon: "⏸", tone: ready ? "on" : "waiting", title: "暂停 QQ 回复",
        note: ready ? "点击 ⏸ 暂停所有回复，QQ 连接和设置都会保留" : "暂时不能处理来信；点击 ⏸ 可先暂停代管"};
    }
    if (data.starting) return {label: "正在连接 QQ…", icon: "…", tone: "waiting", note: "连接完成后会自动更新，不需要重复点击"};
    if (gatewayRunning(data) && data.modelConfigured === false) return {label: "等待模型配置", icon: "▶", note: "请先在「API」设置中配置 Amadeus 使用的模型"};
    if (data.login?.isLogin && (!data.policy?.account_id || data.login.accountId !== data.policy.account_id || data.replyAgent === "astrbot")) {
      return {label: "等待保存设置", icon: "▶", note: "选择回复对象与方式，再点击底部「保存设置」"};
    }
    if (data.connected && data.lifecycle?.active === false) return {label: "已暂停 · 等待 Amadeus 连接", icon: "▶",
      note: "当前没有桌面连接；需要后台运行时，在「连接与后台」中开启并保存"};
    if (data.connected && !data.policy?.private_ids?.length && !data.policy?.group_ids?.length) return {label: "等待选择回复对象", icon: "▶",
      note: "在下方添加好友或群并保存，再开始代管"};
    if (data.connected && data.lifecycle?.active !== false) return {label: "已暂停", action: "resume", icon: "▶", tone: "off", title: "开始 QQ 代管",
      note: Object.keys(data.policy?.conversation_overrides || {}).length ? "点击 ▶ 开始代管，各会话使用自己的回复设置" : `点击 ▶ ${replyMode(data) === "auto" ? "开始自动回复" : "开始生成草稿，确认后才发送"}`};
    return {label: data.login?.isLogin ? "QQ 已登录 · 连接未就绪" : "QQ 尚未登录", action: "start", icon: "▶", tone: "waiting",
      title: "重新连接 QQ", note: "点击 ▶ 检查并连接 QQ；当前不会回复消息"};
  }

  function clampHistory(value) {
    const number = Number(value);
    return Number.isInteger(number) && number >= 0 && number <= 30 ? number : 10;
  }

  function clampQzoneInterval(value) {
    const number = Number(value);
    return Number.isInteger(number) && number >= 5 && number <= 10080 ? number : 60;
  }

  function normalizeContact(value) {
    if (!value || typeof value !== "object") return null;
    const kind = value.kind === "group" ? "group" : value.kind === "private" ? "private" : "";
    const id = String(value.id || "").trim();
    if (!kind || !/^\d+$/.test(id)) return null;
    return {kind, id, name: String(value.name || "").slice(0, 120), remark: String(value.remark || "").slice(0, 120)};
  }

  function contactLabel(contact) {
    const title = contact.remark || contact.name || contact.id;
    const suffix = contact.remark && contact.name && contact.remark !== contact.name ? `（昵称：${contact.name}）` : "";
    return `${contact.kind === "group" ? "群" : "好友"}：${title}${suffix} · ${contact.id}`;
  }

  function createController({element, request, isVisible = () => true, isEnabled = () => false,
    enablePermission, onChanged, eventTarget = globalThis,
    setInterval: setTimer = setInterval, clearInterval: clearTimer = clearInterval}) {
    const ui = Object.fromEntries(["status", "message", "start", "refresh", "login", "qr", "refresh-qr", "account", "permission",
      "lifecycle", "background", "mode", "reply-style", "reply-format", "bubble-chars", "voice-percent", "speech-language", "identity", "friends", "groups", "owner-ids", "owner-control-enabled", "owner-control-in-groups", "owner-developer-mode", "instructions", "delay", "delay-mode", "quota", "history", "compact", "contact",
      "load-contacts", "add-contact", "mute-contact", "contacts-status", "corpus-status", "muted", "save", "toggle", "pause", "resume", "stop", "result",
      "connection", "save-state", "discard", "scope-count", "main-page", "conversation-page", "conversation-back", "switch-account", "use-account", "account-change", "account-change-note", "account-profiles", "new-account", "select-account", "account-profile-note",
      "account-role", "component-setup", "component-note", "client-picker", "client-path", "setup", "login-details", "login-note",
      "conversation-title", "conversation-summary", "conversation-state", "conversation-toggle", "conversation-form", "conversation-reset", "conversation-clear-context", "conversation-clear-confirmation", "conversation-clear-prompt", "conversation-clear-confirm", "conversation-clear-cancel", "names-status",
      "qzone-enabled", "qzone-allow-write", "qzone-allow-publish", "qzone-autonomous", "qzone-interval", "qzone-instructions", "qzone-permission-note",
      "media-note", "media-kind", "media-files", "media-tags", "media-import", "media-query", "media-search", "media-items"]
      .map(name => [name, element.querySelector(`[data-qq="${name}"]`)]));
    let timer, busy = false, loading = false, contactsLoading = false, contactsRequested = false, contactsPromise = null,
      dirty = false, loaded = false, generation = 0, accountRevision = 0, observedAccount = "", observedLoginAccount = "", accountSwitchTarget = "", pendingAccountId = "", accountProfilesSignature = "", loginPanelVisible = false, accountFlowVisible = false, mediaBusy = false, mediaItems = [];
    let savedPolicy = null;
    let currentPolicy = null;
    let currentData = null, savedReplyMode = "review", conflict = false;
    let contacts = [];
    let draftOverrides = {}, conversationFields = [], conversationSync = [], conversationKey = "";
    let rawDrafts = {}, editRevision = 0, saving = false;
    const composing = new Set();
    let selectedConversation = null;
    let contextClearConfirmation = "";
    const buttons = [ui.start, ui.refresh, ui["refresh-qr"], ui["load-contacts"], ui["add-contact"], ui["mute-contact"], ui["media-import"], ui["media-search"],
      ui.save, ui.toggle, ui.pause, ui.resume, ui.stop, ui.discard, ui["conversation-toggle"], ui["conversation-reset"], ui["conversation-clear-context"], ui["conversation-clear-confirm"], ui["conversation-clear-cancel"], ui["switch-account"], ui["use-account"], ui["select-account"], ui.setup].filter(Boolean);
    const fields = [ui.background, ui.mode, ui["reply-style"], ui["reply-format"], ui["bubble-chars"], ui["voice-percent"], ui["speech-language"], ui.identity, ui.friends, ui.groups, ui["owner-ids"], ui["owner-control-enabled"], ui["owner-control-in-groups"], ui["owner-developer-mode"], ui["account-role"], ui.instructions, ui.delay, ui["delay-mode"], ui.quota, ui.history, ui.compact,
      ui["qzone-enabled"], ui["qzone-allow-write"], ui["qzone-allow-publish"], ui["qzone-autonomous"], ui["qzone-interval"], ui["qzone-instructions"]].filter(Boolean);

    function clearChildren(node) {
      if (!node) return;
      if (typeof node.replaceChildren === "function") node.replaceChildren();
      else node.textContent = "";
    }

    function normalizeAccountId(value) {
      const accountId = String(value ?? "").trim();
      return ACCOUNT_ID_PATTERN.test(accountId) ? accountId : "";
    }

    function normalizeAccountProfile(value, selectedAccount) {
      if (!value || typeof value !== "object") return null;
      const accountId = normalizeAccountId(value.accountId);
      if (!accountId) return null;
      const count = candidate => Number.isSafeInteger(Number(candidate)) && Number(candidate) >= 0 ? Number(candidate) : 0;
      return {accountId, selected: value.selected === true || accountId === selectedAccount,
        privateCount: count(value.privateCount), groupCount: count(value.groupCount)};
    }

    function accountProfiles(data) {
      const selected = normalizeAccountId(data?.policy?.account_id);
      const rows = Array.isArray(data?.accountProfiles)
        ? data.accountProfiles.map(value => normalizeAccountProfile(value, selected)).filter(Boolean) : [];
      if (selected && !rows.some(row => row.accountId === selected))
        rows.unshift({accountId: selected, selected: true, privateCount: 0, groupCount: 0});
      return rows.map(row => ({...row, selected: row.accountId === selected || row.selected === true && !selected}));
    }

    function accountSwitchCandidate() {
      const typed = String(ui["new-account"]?.value || "").trim();
      return typed || normalizeAccountId(ui["account-profiles"]?.value || accountSwitchTarget);
    }

    function resetAccountUi() {
      accountRevision += 1;
      contacts = [];
      contactsRequested = false;
      contactsLoading = false;
      contactsPromise = null;
      dirty = false;
      loaded = false;
      conflict = false;
      savedPolicy = null;
      currentPolicy = null;
      savedReplyMode = "review";
      editRevision += 1;
      draftOverrides = {};
      rawDrafts = {};
      conversationKey = "";
      composing.clear();
      selectedConversation = null;
      contextClearConfirmation = "";
      accountSwitchTarget = "";
      accountProfilesSignature = "";
      observedAccount = "";
      observedLoginAccount = "";
      mediaItems = [];
      mediaBusy = false;
      loginPanelVisible = false;
      accountFlowVisible = false;
      if (ui["new-account"]) ui["new-account"].value = "";
      if (ui.friends) ui.friends.value = "";
      if (ui.groups) ui.groups.value = "";
      if (ui["account-role"]) ui["account-role"].value = "assistant";
      if (ui["reply-style"]) ui["reply-style"].value = "auto";
      if (ui["bubble-chars"]) ui["bubble-chars"].value = "60";
      if (ui["owner-ids"]) ui["owner-ids"].value = "";
      if (ui["owner-control-enabled"]) ui["owner-control-enabled"].checked = false;
      if (ui["owner-control-in-groups"]) ui["owner-control-in-groups"].checked = false;
      if (ui["owner-developer-mode"]) ui["owner-developer-mode"].checked = false;
      if (ui.instructions) ui.instructions.value = "";
      if (ui["qzone-enabled"]) ui["qzone-enabled"].checked = false;
      if (ui["qzone-allow-write"]) ui["qzone-allow-write"].checked = false;
      if (ui["qzone-allow-publish"]) ui["qzone-allow-publish"].checked = false;
      if (ui["qzone-autonomous"]) ui["qzone-autonomous"].checked = false;
      if (ui["qzone-interval"]) ui["qzone-interval"].value = "60";
      if (ui["qzone-instructions"]) ui["qzone-instructions"].value = "";
      if (ui["media-tags"]) ui["media-tags"].value = "";
      clearChildren(ui["conversation-form"]);
      conversationFields = [];
      conversationSync = [];
      showPage();
      renderContacts();
      renderMediaItems();
    }

    function renderAccountProfiles(data) {
      const select = ui["account-profiles"];
      const selected = normalizeAccountId(data?.policy?.account_id);
      const profiles = accountProfiles(data);
      if (select) {
        // Only an explicit dropdown edit is a pending switch target. A DOM
        // value left over from the previous account must not override the
        // profile selected by the server after adding/switching an account.
        const currentTarget = accountSwitchTarget;
        const signature = JSON.stringify(profiles.map(profile => [profile.accountId, profile.selected, profile.privateCount, profile.groupCount]));
        if (signature !== accountProfilesSignature) {
          clearChildren(select);
          const doc = element.ownerDocument;
          if (doc?.createElement) profiles.forEach(profile => {
            const option = doc.createElement("option");
            option.value = profile.accountId;
            option.textContent = `${profile.accountId}${profile.selected ? " · 当前配置" : ""} · 好友 ${profile.privateCount} · 群 ${profile.groupCount}`;
            select.append(option);
          });
          accountProfilesSignature = signature;
        }
        const target = normalizeAccountId(currentTarget) || selected || profiles.find(profile => profile.selected)?.accountId;
        if (target) select.value = target;
        if (!select.value && selected) select.value = selected;
      }
      if (ui["account-profile-note"]) {
        const actual = normalizeAccountId(data?.login?.accountId);
        const isolationNote = "每个账号独立保存回复对象、设置和聊天上下文；切换后暂停回复，需登录对应账号。";
        ui["account-profile-note"].textContent = selected && actual && selected !== actual
          ? `当前配置账号为 ${selected}；实际登录的是 ${actual}，请重新登录当前配置账号后再保存回复对象。${isolationNote}`
          : selected ? `已选择配置账号 ${selected}。${isolationNote}` : isolationNote;
      }
    }

    function setToggle(button, paused, subject) {
      if (!button) return;
      const label = `${paused ? "恢复" : "暂停"} ${subject}`;
      button.textContent = paused ? "▶" : "⏸";
      button.title = label;
      button.setAttribute?.("aria-label", label);
      button.setAttribute?.("aria-pressed", paused ? "false" : "true");
    }

    function renderToggle(data) {
      const state = runState(data);
      ui.status.textContent = state.label;
      ui.status.setAttribute?.("data-tone", state.tone || "off");
      ui.message.textContent = state.note;
      if (ui.toggle) {
        ui.toggle.hidden = data?.installed === false;
        ui.toggle.textContent = busy ? "…" : (state.buttonText || state.icon);
        ui.toggle.title = busy ? "正在处理" : state.title || state.note;
        ui.toggle.setAttribute?.("aria-label", ui.toggle.title);
        ui.toggle.setAttribute?.("aria-pressed", String(state.action === "pause"));
        ui.toggle.setAttribute?.("data-action", state.action || "");
        ui.toggle.disabled = busy || !state.action || (state.action === "start" && data?.installed === false);
      }
    }

    function feedback(message) {
      ui.result.textContent = message;
      ui.result.hidden = !message;
    }

    function updateControls() {
      const online = gatewayRunning(currentData) && !currentData?.unavailable;
      const binding = needsBinding(currentData);
      if (ui["client-path"]) ui["client-path"].disabled = busy;
      const needsSetup = online && (currentData?.replyAgent === "astrbot" || (currentData?.login?.isLogin
        && currentData.login.accountId !== currentPolicy?.account_id));
      buttons.forEach(node => { node.disabled = busy; });
      if (ui.setup) ui.setup.disabled = busy || !currentData?.component?.available;
      fields.forEach(node => { node.disabled = (busy && !saving) || !loaded; });
      conversationFields.forEach(node => { node.disabled = (busy && !saving) || !loaded; });
      const qzoneEnabled = ui["qzone-enabled"]?.checked === true;
      const qzoneWrite = ui["qzone-allow-write"]?.checked === true;
      if (ui["qzone-allow-write"]) ui["qzone-allow-write"].disabled = (busy && !saving) || !loaded || !qzoneEnabled;
      if (ui["qzone-allow-publish"]) ui["qzone-allow-publish"].disabled = (busy && !saving) || !loaded || !qzoneEnabled || !qzoneWrite;
      if (ui["qzone-autonomous"]) ui["qzone-autonomous"].disabled = (busy && !saving) || !loaded || !qzoneEnabled;
      if (ui.contact) ui.contact.disabled = busy || !online || binding;
      const connectionSetup = needsConnectionSetup(currentData)
        && String(currentData?.login?.accountId || "") === String(currentPolicy?.account_id || "");
      if (ui["use-account"]) ui["use-account"].disabled = busy || (!online && !connectionSetup) || !(binding || connectionSetup) || !currentData?.login?.coreReady;
      if (ui["switch-account"]) ui["switch-account"].disabled = busy || !online || currentData?.starting || currentData?.installed === false;
      if (ui["account-profiles"]) ui["account-profiles"].disabled = busy || !currentData || currentData?.unavailable;
      if (ui["new-account"]) ui["new-account"].disabled = busy || !currentData || currentData?.unavailable;
      if (ui["owner-control-in-groups"]) ui["owner-control-in-groups"].disabled = (busy && !saving) || !loaded || ui["owner-control-enabled"]?.checked !== true;
      if (ui["owner-developer-mode"]) ui["owner-developer-mode"].disabled = (busy && !saving) || !loaded || ui["owner-control-enabled"]?.checked !== true;
      const mediaReady = Boolean(normalizeAccountId(currentPolicy?.account_id) && online);
      if (ui["media-kind"]) ui["media-kind"].disabled = (busy && !saving) || !loaded || !mediaReady || mediaBusy;
      if (ui["media-files"]) ui["media-files"].disabled = (busy && !saving) || !loaded || !mediaReady || mediaBusy;
      if (ui["media-tags"]) ui["media-tags"].disabled = (busy && !saving) || !loaded || !mediaReady || mediaBusy;
      if (ui["media-query"]) ui["media-query"].disabled = (busy && !saving) || !loaded || !mediaReady || mediaBusy;
      if (ui["media-import"]) ui["media-import"].disabled = busy || mediaBusy || !mediaReady || !(ui["media-files"]?.files?.length);
      if (ui["media-search"]) ui["media-search"].disabled = busy || mediaBusy || !mediaReady;
      if (ui["select-account"]) {
        const candidate = accountSwitchCandidate();
        ui["select-account"].disabled = busy || !currentData || currentData?.unavailable
          || !currentPolicy?.account_id || !normalizeAccountId(candidate)
          || String(candidate) === String(currentPolicy.account_id);
      }
      if (ui.stop) ui.stop.disabled = busy || !online;
      if (ui.save) ui.save.disabled = busy || !loaded || !online || conflict || (!dirty && !needsSetup);
      if (ui.discard) ui.discard.hidden = !dirty;
      if (ui["load-contacts"]) ui["load-contacts"].disabled = busy || contactsLoading || !currentData?.connected;
      if (ui["add-contact"]) ui["add-contact"].disabled = busy || !online || binding || !ui.contact?.value;
      if (ui["save-state"]) ui["save-state"].textContent = !loaded ? "正在读取设置…" : !online
        ? (dirty ? "有未保存修改 · 连接 QQ 后保存" : "配置已保留 · 连接后可保存")
        : conflict ? "设置已在其他地方更新，请撤销后重新编辑" : dirty ? "有未保存的修改" : needsSetup ? "请保存当前账号与设置" : "设置已保存";
      renderToggle(currentData);
      updateContactToggle();
      if (selectedConversation) renderConversation();
    }

    function markDirty(redraw = true) {
      dirty = true;
      editRevision += 1;
      // A settings edit invalidates a pending destructive confirmation. The
      // next context clear must be explicitly confirmed against the current
      // page state again.
      contextClearConfirmation = "";
      if (!conflict) feedback("");
      updateControls();
      if (redraw) renderMuted({policy: currentPolicy || {}});
    }

    function bindEditor(input, onInput) {
      input.addEventListener("input", onInput);
      input.addEventListener("compositionstart", () => {
        composing.add(input);
        // IME can begin before the first input event. Reserve this draft now.
        markDirty(false);
      });
      input.addEventListener("compositionend", () => { composing.delete(input); onInput(); });
      input.addEventListener("blur", () => { if (!composing.has(input)) renderConversation(); });
    }

    function syncValue(input, value) {
      const next = String(value ?? "");
      if (String(input.value) !== next && !composing.has(input)
          && element.ownerDocument?.activeElement !== input) input.value = next;
    }

    function rememberRaw(key, field, input) {
      (rawDrafts[key] ||= {})[field] = input.value;
    }

    function renderLifecycle(data) {
      if (!ui.lifecycle) return;
      const phase = String(data.login?.loginPhase || "").trim().toLowerCase();
      ui.lifecycle.textContent = data.policy?.background_enabled === true
        ? "已允许退出 Amadeus 后继续运行；点击「退出 QQ 后台」可彻底结束"
        : "随 Amadeus 退出；下次打开 Amadeus 不会自动启动 QQ";
      if (!ui.connection) return;
      if (data.connected) ui.connection.textContent = "QQ 已登录且连接正常";
      else if (phase === "login_failed") ui.connection.textContent = loginFailureNote(data.login);
      else if (phase === "qrcode_scanned") ui.connection.textContent = "二维码已扫描，等待手机确认登录";
      else if (phase === "offline" || data.login?.isOffline) ui.connection.textContent = "QQ 登录已离线";
      else if (phase === "initializing") ui.connection.textContent = "正在初始化 QQ 登录";
      else if (phase === "reconnecting") ui.connection.textContent = "正在重新连接 QQ 登录";
      else if (!gatewayRunning(data)) ui.connection.textContent = data.installed === false
        ? (data.component?.available ? "QQ 后台未启动，请展开「账号与登录」连接" : "QQ 组件尚未安装完整")
        : data.login?.isLogin ? "QQ 已登录，但后台未启动" : "QQ 后台未启动，尚未登录 QQ";
      else if (data.login?.isLogin !== true) ui.connection.textContent = "QQ 后台可访问，但尚未登录 QQ；正在检查登录状态";
      else ui.connection.textContent = data.message || "正在检查 QQ 连接";
    }

    function renderContacts() {
      if (!ui.contact) return;
      const current = ui.contact.value;
      clearChildren(ui.contact);
      const doc = element.ownerDocument;
      if (!doc?.createElement) return;
      const placeholder = doc.createElement("option");
      placeholder.value = "";
      placeholder.textContent = contacts.length ? "选择好友或群" : "连接后可读取好友和群";
      ui.contact.append(placeholder);
      for (const contact of contacts) {
        const option = doc.createElement("option");
        option.value = `${contact.kind}:${contact.id}`;
        option.textContent = contactLabel(contact);
        ui.contact.append(option);
      }
      if (current && contacts.some(contact => `${contact.kind}:${contact.id}` === current)) ui.contact.value = current;
      else ui.contact.value = "";
      updateContactToggle();
    }

    function targetLabel(target) {
      const contact = contacts.find(item => item.kind === target.kind && item.id === target.id);
      return contact ? contactLabel(contact) : `${target.kind === "group" ? "群" : "好友"}：${target.id}（名称待同步）`;
    }

    function targetMuted(policy, target) {
      const key = target.kind === "group" ? "muted_group_ids" : "muted_private_ids";
      return Array.isArray(policy?.[key]) && policy[key].map(String).includes(String(target.id));
    }

    function renderMuted(data) {
      if (!ui.muted) return;
      const doc = element.ownerDocument;
      if (dirty && doc?.activeElement && ui.muted.contains?.(doc.activeElement)
          && ["INPUT", "TEXTAREA", "SELECT"].includes(doc.activeElement.tagName)) return;
      clearChildren(ui.muted);
      if (needsBinding(currentData)) {
        ui.muted.textContent = "确认新账号后，再选择允许回复的好友或群";
        if (ui["scope-count"]) ui["scope-count"].textContent = "";
        if (ui["names-status"]) ui["names-status"].textContent = "";
        return;
      }
      const policy = data.policy || {};
      const targets = [];
      const addTargets = (kind, values) => {
        for (const value of Array.isArray(values) ? values : []) {
          const id = String(value);
          // regex-purpose: format
          if (/^\d+$/.test(id) && !targets.some(item => item.kind === kind && item.id === id)) targets.push({kind, id});
        }
      };
      let draft = policy;
      if (dirty) {
        try { draft = {...policy, private_ids: parseIds(ui.friends.value), group_ids: parseIds(ui.groups.value)}; } catch (_) { /* Keep the last valid list while typing. */ }
      }
      addTargets("private", draft.private_ids);
      addTargets("group", draft.group_ids);
      // Keep a muted target visible until the user explicitly restores it,
      // even if a concurrent policy update removed it from the allow list.
      if (!dirty) {
        addTargets("private", policy.muted_private_ids);
        addTargets("group", policy.muted_group_ids);
      }
      const unnamed = targets.filter(target => !contacts.some(row => row.kind === target.kind && row.id === target.id && (row.name || row.remark)));
      if (ui["names-status"]) ui["names-status"].textContent = unnamed.length ? (currentData?.connected ? "正在同步昵称和群名，失败后会自动重试" : "连接后自动同步昵称和群名；已读取过的名称会保留") : "";
      if (ui["scope-count"]) ui["scope-count"].textContent = `${targets.length} 个会话`;
      if (!targets.length || !doc?.createElement) {
        ui.muted.textContent = "尚未添加好友或群；展开下方「添加好友或群」选择";
        return;
      }
      for (const target of targets) {
        const row = doc.createElement("div");
        row.className = "qq-muted-row";
        const label = doc.createElement("span");
        label.textContent = targetLabel(target);
        row.append(label);
        const pending = !(policy[target.kind + "_ids"] || []).map(String).includes(target.id) && !targetMuted(policy, target);
        const status = doc.createElement("small");
        status.textContent = pending ? "待保存" : targetMuted(policy, target) ? "已暂停" : policy.mode === "paused" ? "全局已暂停" : "允许回复";
        row.append(status);
        const button = doc.createElement("button");
        button.className = "qq-contact-toggle";
        button.type = "button";
        setToggle(button, targetMuted(policy, target), targetLabel(target));
        button.disabled = busy || pending || !gatewayRunning(currentData) || currentData?.unavailable;
        button.addEventListener("click", () => act(targetMuted(policy, target) ? "unmute" : "mute", target));
        row.append(button);
        const remove = doc.createElement("button");
        remove.type = "button";
        remove.className = "qq-subtle-button";
        remove.textContent = "移除";
        remove.setAttribute?.("aria-label", `从回复列表移除${targetLabel(target)}`);
        remove.disabled = busy;
        remove.addEventListener("click", () => {
          try {
            const field = target.kind === "group" ? ui.groups : ui.friends;
            field.value = parseIds(field.value).filter(id => id !== target.id).join("\n");
            markDirty();
            feedback(`已从待保存列表移除${targetLabel(target)}，点击「保存设置」生效`);
          } catch (error) { feedback(error.message); }
        });
        row.append(remove);
        const settings = doc.createElement("button");
        settings.type = "button"; settings.className = "qq-recipient-settings";
        settings.textContent = "设置 ›";
        settings.setAttribute?.("aria-label", `设置 ${targetLabel(target)}`);
        settings.disabled = busy;
        settings.addEventListener("click", () => openConversation(target));
        row.append(settings);
        const summary = doc.createElement("small"); summary.className = "qq-conversation-summary";
        summary.textContent = conversationSummary(target);
        row.append(summary);
        ui.muted.append(row);
      }
    }

    function renderCorpusStatus(data) {
      if (!ui["corpus-status"]) return;
      const selectedStyle = String(ui["reply-style"]?.value || "");
      const text = corpusStatusText(data, selectedStyle);
      ui["corpus-status"].textContent = text;
      ui["corpus-status"].hidden = !text;
    }

    function conversationSummary(target) {
      const override = draftOverrides[`${target.kind}:${target.id}`] || {};
      const identity = override.reply_identity || ui.identity?.value || "assistant";
      const mode = override.mode || ui.mode.value || "review";
      const format = replyFormat(override.reply_format ?? ui["reply-format"]?.value);
      const percent = override.voice_reply_percent ?? Number(ui["voice-percent"]?.value ?? 100);
      const language = override.speech_language ?? ui["speech-language"]?.value ?? "inherit";
      const delay = override.reply_delay_seconds ?? Number(ui.delay.value || 0);
      const bubbleChars = override.reply_bubble_chars ?? Number(ui["bubble-chars"]?.value ?? 60);
      const triggers = target.kind === "group" ? ` · ${triggerSummary(override.group_triggers ?? currentPolicy?.group_triggers ?? ["mention"])}` : "";
      const waitMode=override.reply_delay_mode ?? ui["delay-mode"]?.value ?? "fixed";
      const configuredStyle = override.reply_style ?? ui["reply-style"]?.value ?? currentPolicy?.reply_style ?? "auto";
      const style = {game:"红莉栖人格",tieba:"贴吧人格 · 孙吧语料",kangba:"最强攻击性语料",acg:"贴吧人格 · 孙吧语料"}[configuredStyle === "auto" ? (target.kind === "group" ? "tieba" : "game") : configuredStyle] || "红莉栖人格";
      return `${replyIdentityLabel(identity)} · ${style} · ${mode === "auto" ? "自动发送" : "先审核"} · ${replyFormatLabel(format)}${format === "voice" ? `（${percent}% · ${speechLanguageLabel(language)}）` : ""} · ${bubbleChars === 0 ? "不拆分" : `每条≤${bubbleChars}字`} · ${waitMode === "length" ? "按字数等待" : `停顿 ${delay} 秒`}${triggers}${Object.keys(override).length ? " · 单独设置" : " · 使用默认"}`;
    }

    function triggerSummary(modes) {
      const names = {mention:"@ 我", keyword:"关键词", reply:"引用回复我的消息", all:"任何新消息"};
      return modes.length ? modes.map(mode => names[mode]).join(" / ") : "尚未选择触发条件";
    }

    function addGroupTriggerFields(form, key, doc) {
      const field = doc.createElement("label"); field.className="field"; field.textContent="什么时候参与这个群的聊天";
      const scope = doc.createElement("select"); scope.setAttribute("data-qq-conversation-field", "group_trigger_scope");
      for (const [value, text] of [["default","使用默认"],["custom","为这个群单独选择"]]) {
        const option=doc.createElement("option"); option.value=value; option.textContent=text; scope.append(option);
      }
      scope.addEventListener("input", () => {
        const patch={...(draftOverrides[key] || {})};
        if (scope.value === "default") { delete patch.group_triggers; delete patch.group_keywords; }
        else { patch.group_triggers=[...(currentPolicy?.group_triggers ?? ["mention"])]; patch.group_keywords=[...(currentPolicy?.group_keywords || [])]; }
        if (rawDrafts[key]) delete rawDrafts[key].group_keywords;
        if (Object.keys(patch).length) draftOverrides[key]=patch; else delete draftOverrides[key];
        markDirty(false);
      });
      conversationFields.push(scope); field.append(scope); form.append(field);
      const custom=[];
      const box=doc.createElement("div"); box.className="qq-trigger-options";
      const checkboxes={};
      for (const [mode, text] of [["mention","有人 @ 我"],["keyword","消息包含关键词"],["reply","有人引用回复我的消息"],["all","任何新消息"]]) {
        const label=doc.createElement("label"); label.className="qq-trigger-choice";
        const input=doc.createElement("input"); input.type="checkbox";
        input.setAttribute("data-qq-conversation-field", `group_trigger_${mode}`);
        input.addEventListener("input", () => {
          if (input.checked) {
            if (mode === "all") for (const [name, control] of Object.entries(checkboxes)) { if (name !== "all") control.checked=false; }
            else checkboxes.all.checked=false;
          }
          draftOverrides[key]={...(draftOverrides[key] || {}), group_triggers:Object.keys(checkboxes).filter(name => checkboxes[name].checked)};
          markDirty(false);
        });
        checkboxes[mode]=input; conversationFields.push(input);
        const caption=doc.createElement("span"); caption.textContent=text;
        label.append(input); label.append(caption); box.append(label);
      }
      custom.push(box); form.append(box);
      const label=doc.createElement("label"); label.className="field"; label.textContent="触发关键词（逗号或换行分隔）";
      const words=doc.createElement("textarea"); words.rows=2; words.maxLength=2400;
      words.placeholder="例如：Amadeus，助手，论文";
      words.setAttribute("data-qq-conversation-field", "group_keywords");
      bindEditor(words, () => {
        rememberRaw(key, "group_keywords", words);
        // regex-purpose: format
        draftOverrides[key]={...(draftOverrides[key] || {}), group_keywords:[...new Set(words.value.split(/[,，\n]+/u).map(x=>x.trim()).filter(Boolean))]};
        markDirty(false);
      });
      conversationFields.push(words); label.append(words); custom.push(label); form.append(label);
      const note=doc.createElement("p"); note.className="qq-note";
      note.textContent="前三项可以组合，满足任意一项就触发。关键词按文字包含匹配，英文不区分大小写；引用回复指 QQ 的「回复」功能，且原消息是本账号发出的。选择「任何新消息」后，每条他人的新消息都会触发，仍受暂停和发送间隔限制。";
      custom.push(note); form.append(note);
      conversationSync.push(() => {
        const override=draftOverrides[key] || {}, defaults=currentPolicy?.group_triggers ?? ["mention"];
        const inherited=override.group_triggers == null && override.group_keywords == null;
        const label=`使用默认：${triggerSummary(defaults)}`;
        if (scope.options[0].textContent !== label) scope.options[0].textContent=label;
        syncValue(scope, inherited ? "default" : "custom");
        custom.forEach(node => { node.hidden=inherited; });
        for (const [mode, control] of Object.entries(checkboxes)) control.checked=(override.group_triggers ?? defaults).includes(mode);
        syncValue(words, rawDrafts[key]?.group_keywords ?? (override.group_keywords ?? currentPolicy?.group_keywords ?? []).join("\n"));
      });
    }

    function showPage() {
      if (ui["main-page"]) ui["main-page"].hidden = Boolean(selectedConversation);
      if (ui["conversation-page"]) ui["conversation-page"].hidden = !selectedConversation;
    }

    function openConversation(target) {
      const nextKey = `${target.kind}:${target.id}`;
      if (selectedConversation && `${selectedConversation.kind}:${selectedConversation.id}` !== nextKey) contextClearConfirmation = "";
      selectedConversation = {...target};
      showPage(); renderConversation();
      element.scrollIntoView?.({block:"start"});
    }

    function renderConversation() {
      if (!selectedConversation || !ui["conversation-form"]) return;
      const target = selectedConversation, key = `${target.kind}:${target.id}`;
      ui["conversation-title"].textContent = targetLabel(target);
      ui["conversation-summary"].textContent = conversationSummary(target);
      const muted = targetMuted(currentPolicy, target);
      ui["conversation-state"].textContent = muted ? "此会话已暂停" : currentPolicy?.mode === "paused" ? "允许回复 · 全局代管已暂停" : "此会话允许回复";
      setToggle(ui["conversation-toggle"], muted, targetLabel(target));
      ui["conversation-toggle"].disabled = busy || !gatewayRunning(currentData) || !(currentPolicy?.[target.kind+"_ids"] || []).includes(target.id);
      const pending = contextClearConfirmation === key;
      const unavailable = busy || !gatewayRunning(currentData) || currentData?.unavailable;
      if (ui["conversation-clear-context"]) {
        ui["conversation-clear-context"].hidden = pending;
        ui["conversation-clear-context"].textContent = "清理对话上下文";
        ui["conversation-clear-context"].title = "让这个对象从新对话开始";
        ui["conversation-clear-context"].setAttribute?.("aria-label", ui["conversation-clear-context"].title);
        // A paused target may still be cleared; only the gateway itself must
        // be running so the scoped archive operation can be authenticated.
        ui["conversation-clear-context"].disabled = unavailable;
      }
      if (ui["conversation-clear-confirmation"]) {
        ui["conversation-clear-confirmation"].hidden = !pending;
        if (ui["conversation-clear-prompt"]) ui["conversation-clear-prompt"].textContent = `确定让「${targetLabel(target)}」从新对话开始吗？`;
        if (ui["conversation-clear-confirm"]) ui["conversation-clear-confirm"].disabled = unavailable;
        if (ui["conversation-clear-cancel"]) ui["conversation-clear-cancel"].disabled = busy;
      }
      // Status and defaults may change every poll. Keep the editor nodes alive:
      // replacing them loses focus, IME composition and the scroll position.
      if (conversationKey === key) { conversationSync.forEach(sync => sync()); return; }
      const form = ui["conversation-form"], doc = element.ownerDocument;
      clearChildren(form); conversationFields = []; conversationSync = []; conversationKey = key;
      const resolve = value => typeof value === "function" ? value() : value;
      const add = (title, field, choices, numeric) => {
        const label = doc.createElement("label"); label.className = "field"; label.textContent = title;
        const input = doc.createElement(choices ? "select" : numeric ? "input" : "textarea");
        input.setAttribute?.("data-qq-conversation-field", field);
        if (choices) for (const [value, text] of choices) {
          const option = doc.createElement("option"); option.value = value; option.textContent = resolve(text); input.append(option);
        }
        else if (numeric) { input.type="number"; input.min=numeric[0]; input.max=numeric[1]; input.step=numeric[2]; }
        else { input.rows=4; input.maxLength=2000; input.placeholder="留空使用默认要求，例如：和他聊天随意一点，不聊工作"; }
        bindEditor(input, () => {
          rememberRaw(key, field, input);
          const value = input.value.trim(), override = {...(draftOverrides[key] || {})};
          if (!value) delete override[field];
          else override[field] = numeric ? Number(value) : field === "compact_reply" ? value === "true" : value;
          if (Object.keys(override).length) draftOverrides[key]=override; else delete draftOverrides[key];
          markDirty(false);
        });
        conversationSync.push(() => {
          if (choices) choices.forEach(([,text], index) => {
            const label=resolve(text);
            if (input.options[index].textContent !== label) input.options[index].textContent=label;
          });
          if (numeric) input.placeholder=`使用默认：${resolve(numeric[3])}`;
          syncValue(input, rawDrafts[key]?.[field] ?? (draftOverrides[key] || {})[field] ?? "");
        });
        conversationFields.push(input); label.append(input); form.append(label);
      };
      if (target.kind === "group") addGroupTriggerFields(form, key, doc);
      add("对外说话方式", "reply_identity", [["",()=>`使用默认：${replyIdentityLabel(ui.identity?.value)}`],["assistant","以 Amadeus 自己的身份说话"],["owner","代我用第一人称回复"]]);
      add("人格模式", "reply_style", [["",()=>`使用默认：${{game:"红莉栖人格",tieba:"贴吧人格 · 孙吧语料",kangba:"最强攻击性语料"}[ui["reply-style"]?.value] || (target.kind === "group" ? "贴吧人格 · 孙吧语料" : "红莉栖人格")}`],["game","红莉栖人格"],["tieba","贴吧人格 · 孙吧语料"],["kangba","最强攻击性语料"]]);
      add("回复如何发送", "mode", [["",()=>`使用默认：${ui.mode.value === "auto" ? "自动发送" : "先审核"}`],["review","生成草稿，我确认后发送"],["auto","自动发送"]]);
      add("回复形式", "reply_format", [["",()=>`使用默认：${replyFormatLabel(ui["reply-format"]?.value)}`],["text","文字回复"],["voice","语音回复"]]);
      add("每条 QQ 气泡字数上限（0 不拆分）", "reply_bubble_chars", null, [0,240,1,()=>ui["bubble-chars"]?.value ?? 60]);
      add("语音回复频率（%，仅语音模式生效）", "voice_reply_percent", null, [0,100,1,()=>ui["voice-percent"]?.value ?? 100]);
      add("语音语言", "speech_language", [["",()=>`使用默认：${speechLanguageLabel(ui["speech-language"]?.value)}`],["inherit","跟随桌面语音语言"],["zh-CN","中文"],["ja-JP","日文"]]);
      add("回复等待方式", "reply_delay_mode", [["",()=>`使用默认：${ui["delay-mode"]?.value === "length" ? "随字数增加" : "固定停顿"}`],["fixed","固定停顿"],["length","随字数增加等待时间"]]);
      add("基础停顿（秒）", "reply_delay_seconds", null, [0,30,.1,()=>ui.delay.value]);
      add("两次回复最短间隔（秒）", "minimum_interval_seconds", null, [1,3600,1,()=>currentPolicy?.minimum_interval_seconds ?? 10]);
      add("自动读取最近几条消息", "history_messages", null, [0,30,1,()=>ui.history?.value ?? 10]);
      add("回复排版", "compact_reply", [["","使用默认排版"],["true","单行回复，不在句末加句号"],["false","保留模型原来的换行和标点"]]);
      add("只对这个对象的回复要求", "reply_instructions");
      const note = doc.createElement("p"); note.className="qq-note";
      note.textContent="语音模式由主人开启；频率 0% 只发文字，100% 每条语音，中间值按每条回复的概率选择。文字模式始终只发文字，QQ 来信不能切换。语音复用桌面音色与合成设置，语言可单独选择；已生成草稿保持生成时的语言。按字数等待 = 基础停顿 + 字数 ÷ 6 秒，小幅随机浮动，最长 30 秒（生成回复的耗时另计）。最短间隔限制连续发送频率。群聊历史条数为 0 时仍会在触发后读取最近 10 条。";
      form.append(note);
      conversationSync.forEach(sync => sync());
      conversationFields.forEach(input => { input.disabled=(busy && !saving) || !loaded; });
    }

    function updateContactToggle() {
      if (!ui["mute-contact"]) return;
      let target;
      try { target = selectedContact(); } catch (_) {
        ui["mute-contact"].disabled = true;
        setToggle(ui["mute-contact"], false, "当前会话");
        ui["mute-contact"].title = "先选择好友或群";
        ui["mute-contact"].setAttribute?.("aria-label", "先选择好友或群");
        return;
      }
      const paused = targetMuted(currentPolicy, target);
      ui["mute-contact"].disabled = busy;
      setToggle(ui["mute-contact"], paused, targetLabel(target));
    }

    function renderMediaItems() {
      const container = ui["media-items"];
      if (!container) return;
      clearChildren(container);
      if (!mediaItems.length) {
        container.textContent = "当前账号还没有已读取的图库项目。";
        return;
      }
      const doc = element.ownerDocument;
      mediaItems.forEach(item => {
        const row = doc.createElement("div");
        row.className = "qq-media-item";
        const label = doc.createElement("span");
        const kind = item.kind === "meme" ? "表情包" : "图片";
        label.textContent = `${String(item.name || "未命名图片").slice(0, 80)} · ${kind}`;
        row.append(label);
        if (Array.isArray(item.tags) && item.tags.length) {
          const tags = doc.createElement("small");
          tags.textContent = ` · ${item.tags.slice(0, 5).join("、")}`;
          row.append(tags);
        }
        const assetId = String(item.assetId || item.id || "");
        // regex-purpose: resource
        if (/^[0-9a-fA-F]{64}$/.test(assetId)) {
          const remove = doc.createElement("button");
          remove.type = "button";
          remove.className = "qq-subtle-button";
          remove.textContent = "删除";
          remove.title = "删除当前账号图库中的这张图片";
          remove.setAttribute?.("aria-label", `删除图库图片 ${String(item.name || assetId).slice(0, 40)}`);
          remove.disabled = mediaBusy || busy;
          remove.addEventListener("click", () => deleteMedia(assetId));
          row.append(remove);
        }
        container.append(row);
      });
    }

    async function mediaRequest(action, extra = {}) {
      const account = normalizeAccountId(currentPolicy?.account_id);
      if (!account) throw new Error("尚未读取当前 QQ 账号，不能操作图库。");
      const revision = accountRevision;
      const data = await read("/api/qq/setup", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({action: "media", mediaAction: action, expectedAccount: account, ...extra})});
      if (revision !== accountRevision || account !== normalizeAccountId(currentPolicy?.account_id)) return null;
      return data;
    }

    function fileBase64(file) {
      return new Promise((resolve, reject) => {
        if (typeof FileReader === "undefined") { reject(new Error("当前浏览器不支持图片读取。")); return; }
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("读取图片失败。"));
        reader.onload = () => {
          const value = String(reader.result || "");
          const comma = value.indexOf(",");
          if (comma < 0) reject(new Error("图片数据格式不正确。"));
          else resolve(value.slice(comma + 1));
        };
        reader.readAsDataURL(file);
      });
    }

    async function searchMedia() {
      if (mediaBusy) return;
      mediaBusy = true; updateControls();
      try {
        const data = await mediaRequest("search", {query: String(ui["media-query"]?.value || "").trim(), limit: 20});
        if (!data) return;
        mediaItems = Array.isArray(data.items) ? data.items : Array.isArray(data.assets) ? data.assets : [];
        renderMediaItems();
        if (ui["media-note"]) ui["media-note"].textContent = mediaItems.length ? `当前账号图库：${mediaItems.length} 项。` : "当前账号还没有已读取的图库项目。";
      } catch (error) {
        if (ui["media-note"]) ui["media-note"].textContent = error.message;
      } finally { mediaBusy = false; updateControls(); }
    }

    async function importMedia() {
      if (mediaBusy) return;
      const files = [...(ui["media-files"]?.files || [])];
      if (!files.length) { if (ui["media-note"]) ui["media-note"].textContent = "请先选择图片。"; return; }
      if (files.length > 20) { if (ui["media-note"]) ui["media-note"].textContent = "一次最多导入 20 张图片。"; return; }
      mediaBusy = true; updateControls();
      const imported = [];
      try {
        const kind = ui["media-kind"]?.value === "meme" ? "meme" : "image";
        const tags = String(ui["media-tags"]?.value || "")
          // regex-purpose: format
          .split(/[,，]/).map(tag => tag.trim()).filter(Boolean).slice(0, 20);
        for (const file of files) {
          // Keep each request within the shared 10 MiB media limit. Requests
          // are sent one file at a time so one oversized image cannot reject
          // otherwise valid imports in the same selection.
          if (Number(file.size) > 10 * 1024 * 1024) throw new Error("单张图片不能超过 10 MiB。");
          const item = {name: String(file.name || "QQ 图片").slice(0, 200), base64: await fileBase64(file), kind, tags, description: ""};
          const data = await mediaRequest("import", {items: [item]});
          if (!data) return;
          const rows = Array.isArray(data.items) ? data.items : Array.isArray(data.assets) ? data.assets : [];
          imported.push(...rows.filter(row => /^[0-9a-fA-F]{64}$/.test(String(row?.assetId || row?.id || ""))));
        }
        mediaItems = imported;
        if (ui["media-files"]) ui["media-files"].value = "";
        renderMediaItems();
        if (ui["media-note"]) ui["media-note"].textContent = imported.length
          ? `已导入 ${imported.length} 张图片到当前账号图库。`
          : "没有成功导入图片。";
      } catch (error) {
        mediaItems = imported;
        renderMediaItems();
        if (ui["media-note"]) ui["media-note"].textContent = imported.length
          ? `已导入 ${imported.length} 张图片；其余未完成：${error.message}` : error.message;
      } finally { mediaBusy = false; updateControls(); }
    }

    async function deleteMedia(assetId) {
      if (mediaBusy) return;
      mediaBusy = true; updateControls();
      try {
        const data = await mediaRequest("delete", {imageIds: [assetId]});
        if (!data) return;
        mediaItems = mediaItems.filter(item => String(item.assetId || item.id || "") !== assetId);
        renderMediaItems();
        if (ui["media-note"]) ui["media-note"].textContent = "已从当前账号图库删除。";
      } catch (error) {
        if (ui["media-note"]) ui["media-note"].textContent = error.message;
      } finally { mediaBusy = false; updateControls(); }
    }

    function render(data) {
      if (pendingAccountId && data?.policy?.account_id
          && normalizeAccountId(data.policy.account_id) !== pendingAccountId) return;
      if (pendingAccountId && data?.policy?.account_id
          && normalizeAccountId(data.policy.account_id) === pendingAccountId) pendingAccountId = "";
      if (data.connected && !currentData?.connected) contactsRequested = false;
      const account = normalizeAccountId(data.policy?.account_id);
      const loginAccount = normalizeAccountId(data.login?.accountId);
      if ((observedAccount && account !== observedAccount)
          || (observedLoginAccount && loginAccount && loginAccount !== observedLoginAccount)
          || (savedPolicy && account !== normalizeAccountId(savedPolicy.account_id))) {
        accountRevision += 1;
        contacts = [];
        contactsRequested = false;
        dirty = false; conflict = false; savedPolicy = null; draftOverrides = {}; rawDrafts = {}; loaded = false;
        conversationKey = ""; composing.clear();
        contextClearConfirmation = "";
        selectedConversation = null; showPage();
        renderContacts();
        mediaItems = []; renderMediaItems();
      }
      observedAccount = account;
      if (loginAccount) observedLoginAccount = loginAccount;
      currentData = data;
      currentPolicy = data.policy || null;
      renderAccountProfiles(data);
      if (ui['component-setup']) ui['component-setup'].hidden = data.installed !== false;
      if (ui['component-note']) ui['component-note'].textContent = data.component?.available
        ? (data.component.clientFound ? `已找到 QQ：${data.component.clientPath}` : data.component.message)
        : 'QQ 接入组件不完整，请修复或重新安装 Amadeus';
      if (ui['client-picker'] && data.component?.available && !data.component.clientFound) ui['client-picker'].open = true;
      const binding = needsBinding(data);
      const connectionSetup = needsConnectionSetup(data)
        && String(loginAccount || "") === String(account || "");
      const accountFlowNeeded = data.installed === false || binding || connectionSetup;
      if (accountFlowNeeded && !accountFlowVisible && ui["account-switcher"]) ui["account-switcher"].open = true;
      accountFlowVisible = accountFlowNeeded;
      if (ui["account-change"]) ui["account-change"].hidden = !(binding || connectionSetup);
      if (ui["account-change-note"]) ui["account-change-note"].textContent = binding
        ? `当前配置账号为 ${account || "未选择"}，实际登录账号为 ${loginAccount || "未知"}。可重新扫码登录当前配置账号，或改用下方已登录账号及其独立设置。`
        : connectionSetup ? `已登录当前配置账号 ${account}，但 QQ 连接尚未完成；点击下方按钮继续。` : "";
      if (ui["use-account"]) {
        ui["use-account"].hidden = !(binding || connectionSetup);
        const useLabel = connectionSetup && !binding ? "连接这个账号" : `改用已登录账号 ${loginAccount}`;
        ui["use-account"].textContent = useLabel;
        ui["use-account"].title = connectionSetup && !binding ? "完成当前已登录账号的 QQ 连接" : `使用已登录账号 ${loginAccount}及其独立配置`;
        ui["use-account"].setAttribute?.("aria-label", ui["use-account"].title);
      }
      if (binding) {
        dirty = false; loaded = false; draftOverrides = {}; rawDrafts = {}; contacts = []; renderContacts();
        conversationKey = ""; composing.clear();
        contextClearConfirmation = "";
        selectedConversation = null; showPage();
        mediaItems = []; renderMediaItems();
        ui.friends.value = ""; ui.groups.value = ""; if (ui["owner-ids"]) ui["owner-ids"].value = "";
        if (ui["owner-control-enabled"]) ui["owner-control-enabled"].checked = false;
        if (ui["owner-control-in-groups"]) ui["owner-control-in-groups"].checked = false;
        if (ui["owner-developer-mode"]) ui["owner-developer-mode"].checked = false;
        ui.instructions.value = "";
        if (ui["reply-style"]) ui["reply-style"].value = "auto";
        if (ui["qzone-enabled"]) ui["qzone-enabled"].checked = false;
        if (ui["qzone-allow-write"]) ui["qzone-allow-write"].checked = false;
        if (ui["qzone-allow-publish"]) ui["qzone-allow-publish"].checked = false;
        if (ui["qzone-autonomous"]) ui["qzone-autonomous"].checked = false;
        if (ui["qzone-interval"]) ui["qzone-interval"].value = "60";
        if (ui["qzone-instructions"]) ui["qzone-instructions"].value = "";
      }
      if (!binding && Array.isArray(data.contacts) && data.contacts.length) {
        const merged = new Map(contacts.map(row => [`${row.kind}:${row.id}`,row]));
        data.contacts.map(normalizeContact).filter(Boolean).forEach(row => merged.set(`${row.kind}:${row.id}`,row));
        contacts = [...merged.values()]; renderContacts();
      }
      const login = data.login || {};
      if (account && loginAccount && account === loginAccount && !connectionSetup)
        ui.account.textContent = `当前配置账号：${account} · 已登录：${login.nickname || "QQ"}（${loginAccount}）`;
      else if (account && loginAccount && account === loginAccount)
        ui.account.textContent = `当前配置账号：${account} · 已登录但尚未连接：${login.nickname || "QQ"}（${loginAccount}）`;
      else if (account && loginAccount)
        ui.account.textContent = `当前配置账号：${account} · 实际登录账号：${login.nickname || "QQ"}（${loginAccount}）· 需重新登录当前配置`;
      else if (account)
        ui.account.textContent = `当前配置账号：${account} · 尚未确认登录`;
      else if (loginAccount)
        ui.account.textContent = `尚未选择配置账号 · 实际登录账号：${login.nickname || "QQ"}（${loginAccount}）`;
      else ui.account.textContent = "尚未登录 QQ";
      ui.permission.textContent = isEnabled() ? "也可以通过对话调整这些设置" : "保存后允许助手通过对话管理 QQ 设置";
      const phase = String(login.loginPhase || "").trim().toLowerCase();
      const showLoginPanel = !login.isLogin && (["generating_qrcode", "waiting_qrcode", "qrcode_scanned"].includes(phase)
        || (phase === "login_failed" && runState(data).action !== "refresh_qr"));
      const justOpenedLoginPanel = showLoginPanel && !loginPanelVisible;
      ui.login.hidden = !showLoginPanel;
      if (ui["login-details"]) {
        ui["login-details"].hidden = !showLoginPanel;
        if (justOpenedLoginPanel) ui["login-details"].open = true;
      }
      loginPanelVisible = showLoginPanel;
      if (ui["login-note"]) ui["login-note"].textContent = phase === "login_failed"
        ? loginFailureNote(login)
        : phase === "qrcode_scanned" ? "二维码已扫描，请在手机 QQ 上确认登录；需要更换二维码时点击下方刷新"
        : phase === "generating_qrcode" ? "正在生成二维码，请稍候"
        : "手机 QQ 扫码并确认登录；登录后再选择回复对象";
      if (ui["refresh-qr"]) {
        ui["refresh-qr"].title = "刷新登录二维码";
        ui["refresh-qr"].setAttribute?.("aria-label", "刷新登录二维码");
      }
      const image = showLoginPanel && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(login.qrImage || "") ? login.qrImage : "";
      ui.qr.hidden = !image;
      if (image) ui.qr.src = image;
      else ui.qr.removeAttribute("src");
      if (!dirty && !binding && data.policy) {
        const policy = data.policy;
        if (ui.background) ui.background.checked = policy.background_enabled === true;
        savedReplyMode = replyMode(data);
        ui.mode.value = savedReplyMode;
        if (ui["reply-style"]) ui["reply-style"].value = policy.reply_style === "acg" ? "tieba" : policy.reply_style || "auto";
        if (ui["reply-format"]) ui["reply-format"].value = replyFormat(policy.reply_format);
        if (ui["bubble-chars"]) ui["bubble-chars"].value = Number.isSafeInteger(policy.reply_bubble_chars) ? policy.reply_bubble_chars : 60;
        if (ui["voice-percent"]) ui["voice-percent"].value = policy.voice_reply_percent ?? 100;
        if (ui["speech-language"]) ui["speech-language"].value = policy.speech_language || "inherit";
        if (ui.identity) ui.identity.value = policy.reply_identity || "assistant";
        if (ui["account-role"]) ui["account-role"].value = policy.account_role || "assistant";
        draftOverrides = JSON.parse(JSON.stringify(policy.conversation_overrides || {}));
        ui.friends.value = (policy.private_ids || []).join("\n");
        ui.groups.value = (policy.group_ids || []).join("\n");
        if (ui["owner-ids"]) ui["owner-ids"].value = (policy.owner_ids || []).join("\n");
        if (ui["owner-control-enabled"]) ui["owner-control-enabled"].checked = policy.owner_control_enabled === true;
        if (ui["owner-control-in-groups"]) ui["owner-control-in-groups"].checked = policy.owner_control_in_groups === true;
        if (ui["owner-developer-mode"]) ui["owner-developer-mode"].checked = policy.owner_developer_mode === true;
        ui.instructions.value = policy.reply_instructions || "";
        if (ui["qzone-enabled"]) ui["qzone-enabled"].checked = policy.qzone_enabled === true;
        if (ui["qzone-allow-write"]) ui["qzone-allow-write"].checked = policy.qzone_allow_write === true;
        if (ui["qzone-allow-publish"]) ui["qzone-allow-publish"].checked = policy.qzone_allow_publish === true;
        if (ui["qzone-autonomous"]) ui["qzone-autonomous"].checked = policy.qzone_autonomous === true;
        if (ui["qzone-interval"]) ui["qzone-interval"].value = clampQzoneInterval(policy.qzone_interval_minutes);
        if (ui["qzone-instructions"]) ui["qzone-instructions"].value = policy.qzone_instructions || "";
        ui.delay.value = policy.reply_delay_seconds ?? 3;
        if (ui["delay-mode"]) ui["delay-mode"].value = policy.reply_delay_mode || "fixed";
        if (ui.quota) ui.quota.value = policy.max_sends_per_hour ?? 0;
        if (ui.history) ui.history.value = clampHistory(policy.history_messages);
        if (ui.compact) ui.compact.checked = policy.compact_reply !== false;
        savedPolicy = JSON.parse(JSON.stringify(policy));
        loaded = true;
      }
      conflict = Boolean(dirty && savedPolicy && (JSON.stringify(savedPolicy) !== JSON.stringify(data.policy) || savedReplyMode !== replyMode(data)));
      if (conflict) {
        feedback("QQ 配置已在对话或其他界面中改变。请点击「撤销修改」读取最新设置，再编辑保存。");
      }
      renderLifecycle(data);
      renderMuted(data);
      renderConversation();
      renderCorpusStatus(data);
      updateControls();
      if (!contactsRequested && data.connected) void loadContacts();
    }

    async function read(path, options) {
      const response = await request(path, options);
      if (response.status === 404) throw new Error("当前后端还未加载 QQ 管理功能，请重启 Amadeus。");
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
      return data;
    }

    async function refresh(force = false) {
      if ((!force && !isVisible()) || loading || busy) return;
      loading = true;
      const revision = generation;
      try {
        const data = await read("/api/qq/setup", {cache: "no-store"});
        if (revision === generation) render(data);
      } catch (error) {
        if (revision === generation) {
          currentData = {...currentData, unavailable: true};
          updateControls();
          feedback(error.message);
        }
      } finally {
        loading = false;
        if (revision !== generation && !busy) void refresh(true);
      }
    }

    async function loadContacts(force = false) {
      if (needsBinding(currentData)) return;
      if (contactsLoading) return contactsPromise;
      if (contactsRequested && !force) return;
      contactsRequested = true;
      contactsLoading = true;
      const account = currentData?.policy?.account_id;
      const revision = accountRevision;
      if (ui["load-contacts"]) ui["load-contacts"].disabled = true;
      if (ui["contacts-status"]) ui["contacts-status"].textContent = "正在读取好友和群…";
      contactsPromise = (async () => {
        try {
          const data = await read("/api/qq/setup", {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({action: "contacts"})});
          if (revision !== accountRevision || account !== currentData?.policy?.account_id || needsBinding(currentData)) return;
          const incoming = (Array.isArray(data.contacts) ? data.contacts : []).map(normalizeContact).filter(Boolean);
          if (data.complete === false) {
            const merged = new Map(contacts.map(row => [`${row.kind}:${row.id}`,row]));
            incoming.forEach(row => merged.set(`${row.kind}:${row.id}`,row)); contacts=[...merged.values()];
            contactsRequested = false;
          } else contacts = incoming;
          renderContacts();
          if (ui["contacts-status"]) ui["contacts-status"].textContent = contacts.length ? `已读取 ${contacts.length} 个好友或群。` : "没有可显示的好友或群。";
          renderMuted({policy: currentPolicy || {}});
        } catch (error) {
          contactsRequested = false;
          if (ui["contacts-status"]) ui["contacts-status"].textContent = error.message;
        } finally {
          if (revision === accountRevision) {
            contactsLoading = false;
            contactsPromise = null;
            updateControls();
          }
        }
      })();
      return contactsPromise;
    }

    function selectedContact() {
      const value = String(ui.contact?.value || "");
      const separator = value.indexOf(":");
      if (separator <= 0) throw new Error("请先选择一个好友或群。");
      const kind = value.slice(0, separator);
      const id = value.slice(separator + 1);
      if (!/^(private|group)$/.test(kind) || !/^\d+$/.test(id)) throw new Error("联系人选择无效。");
      return {kind, id};
    }

    function addSelectedContact() {
      try {
        const target = selectedContact();
        const field = target.kind === "private" ? ui.friends : ui.groups;
        const ids = parseIds(field.value);
        if (!ids.includes(target.id)) field.value = [...ids, target.id].join("\n");
        markDirty();
        feedback(`${targetLabel(target)} 已加入待保存列表，点击底部「保存设置」生效`);
      } catch (error) { feedback(error.message); }
    }

    function prepareContextClear() {
      if (busy || !selectedConversation) return;
      const target = selectedConversation;
      const targetType = target.kind;
      const targetId = String(target.id || "");
      try {
        if (!/^(private|group)$/.test(targetType) || !/^\d+$/.test(targetId)) throw new Error("请选择有效的好友或群。");
        if (needsBinding(currentData)) throw new Error("请先确认当前 QQ 账号，再清理对象的对话上下文。");
        if (!gatewayRunning(currentData) || currentData?.unavailable) throw new Error("请先连接 QQ，再清理这个对象的对话上下文。");
        if (!currentPolicy?.account_id) throw new Error("尚未读取当前 QQ 账号，请刷新后重试。");
        contextClearConfirmation = `${targetType}:${targetId}`;
        renderConversation();
        feedback(`请确认让「${targetLabel(target)}」从新对话开始。回复设置和 QQ 原聊天记录会保留，旧的助手上下文会在本机归档。`);
      } catch (error) { feedback(error.message); }
    }

    function cancelContextClear() {
      if (busy) return;
      contextClearConfirmation = "";
      renderConversation();
      feedback("已取消清理。");
    }

    async function act(action, extra = {}) {
      if (busy) return;
      let body = {action};
      let clearFeedbackAfterStatus = false;
      try {
        if (action === 'setup') body.clientPath = ui['client-path']?.value.trim() || '';
        if (needsBinding(currentData) && ["save", "resume", "mute", "unmute"].includes(action))
          throw new Error("请先登录当前配置 QQ 账号，再设置回复对象。");
        if (["select_account", "switch_account"].includes(action) && (dirty || composing.size))
          throw new Error("请先保存设置，或撤销修改，再切换 QQ 账号。");
        if (action === "select_account") {
          const accountId = accountSwitchCandidate();
          if (!normalizeAccountId(accountId)) throw new Error("QQ 号请输入 5–20 位数字。");
          if (!currentPolicy?.account_id) throw new Error("尚未读取当前 QQ 配置，请刷新后重试。");
          if (String(accountId) === String(currentPolicy.account_id)) throw new Error("已经是当前配置账号。");
          pendingAccountId = String(accountId);
          body = {action: "select_account", accountId: String(accountId), expectedAccount: String(currentPolicy.account_id)};
        }
        if (action === "use_account") body = {...body, accountId: currentData?.login?.accountId, expectedPolicy: currentPolicy};
        if (action === "save") {
          if (!loaded) throw new Error("请先读取当前配置，再保存回复范围。");
          if (!gatewayRunning(currentData) || currentData?.unavailable) throw new Error("请先连接 QQ，再保存设置；填写的内容会保留在这里。");
          const history = clampHistory(ui.history?.value);
          if (ui.history && String(Number(ui.history.value)) !== String(history)) throw new Error("近期上下文数量请输入 0–30 的整数。");
          const percent = Number(ui["voice-percent"]?.value ?? savedPolicy?.voice_reply_percent ?? 100);
          if (!Number.isInteger(percent) || percent < 0 || percent > 100 || ui["voice-percent"]?.value === "") throw new Error("语音回复频率请输入 0–100 的整数百分比。");
          const bubbleChars = Number(ui["bubble-chars"]?.value ?? savedPolicy?.reply_bubble_chars ?? 60);
          if (!Number.isSafeInteger(bubbleChars) || bubbleChars < 0 || bubbleChars > 240 || ui["bubble-chars"]?.value === "") throw new Error("QQ 气泡字数请输入 0–240 的整数。");
          const language = ui["speech-language"]?.value || "inherit";
          if (!["inherit","zh-CN","ja-JP"].includes(language)) throw new Error("请选择有效的语音语言。");
          const qzoneInterval = clampQzoneInterval(ui["qzone-interval"]?.value ?? savedPolicy?.qzone_interval_minutes ?? 60);
          if (ui["qzone-interval"] && String(Number(ui["qzone-interval"].value)) !== String(qzoneInterval)) throw new Error("QQ 空间检查间隔请输入 5–10080 的整数分钟。");
          const qzoneInstructions = String(ui["qzone-instructions"]?.value || "");
          if (qzoneInstructions.length > 2000) throw new Error("QQ 空间自主说明不能超过 2000 字。");
          const qzoneEnabled = Boolean(ui["qzone-enabled"]?.checked);
          const qzoneWrite = Boolean(ui["qzone-allow-write"]?.checked) && qzoneEnabled;
          const accountRole = ui["account-role"]?.value || "assistant";
          if (!["assistant", "owner"].includes(accountRole)) throw new Error("请选择有效的账号用途。");
          body = {...body, voice_reply_percent: percent, reply_bubble_chars: bubbleChars, speech_language: language, replyMode: ui.mode.value, reply_format: replyFormat(ui["reply-format"]?.value), private_ids: parseIds(ui.friends.value), group_ids: parseIds(ui.groups.value), owner_ids: parseIds(ui["owner-ids"]?.value || ""), owner_control_enabled: Boolean(ui["owner-control-enabled"]?.checked), owner_control_in_groups: Boolean(ui["owner-control-in-groups"]?.checked), owner_developer_mode: Boolean(ui["owner-developer-mode"]?.checked),
            qzone_enabled: qzoneEnabled, qzone_allow_write: qzoneWrite,
            qzone_allow_publish: Boolean(ui["qzone-allow-publish"]?.checked) && qzoneWrite,
            qzone_autonomous: Boolean(ui["qzone-autonomous"]?.checked) && qzoneEnabled,
            qzone_interval_minutes: qzoneInterval, qzone_instructions: qzoneInstructions,
            reply_instructions: ui.instructions.value, reply_delay_seconds: Number(ui.delay.value),
            reply_delay_mode: ui["delay-mode"]?.value || "fixed",
            max_sends_per_hour: Number(ui.quota?.value ?? savedPolicy?.max_sends_per_hour ?? 0),
            background_enabled: Boolean(ui.background?.checked), history_messages: history,
            compact_reply: ui.compact ? ui.compact.checked : true,
            account_role: accountRole,
            reply_style: ui["reply-style"]?.value || "auto",
            reply_identity: ui.identity?.value || "assistant",
            conversation_overrides: Object.fromEntries(Object.entries(draftOverrides).filter(([key]) => {
              const [kind, id] = key.split(":");
              const targets = kind === "private"
                ? [...parseIds(ui.friends.value), ...parseIds(ui["owner-ids"]?.value || "")]
                : parseIds(ui.groups.value);
              return targets.includes(id);
            })),
            muted_private_ids: Array.isArray(savedPolicy?.muted_private_ids) ? savedPolicy.muted_private_ids : [],
            muted_group_ids: Array.isArray(savedPolicy?.muted_group_ids) ? savedPolicy.muted_group_ids : [],
            expectedPolicy: savedPolicy, expectedReplyMode: savedReplyMode};
          if (!Number.isFinite(body.reply_delay_seconds) || body.reply_delay_seconds < 0 || body.reply_delay_seconds > 30) throw new Error("回复停顿请输入 0–30 秒。");
          if (!Number.isSafeInteger(body.max_sends_per_hour) || body.max_sends_per_hour < 0) throw new Error("每小时发送上限请输入非负整数，0 表示不限。");
          for (const [key, patch] of Object.entries(body.conversation_overrides)) {
            if (patch.voice_reply_percent != null && (!Number.isInteger(patch.voice_reply_percent) || patch.voice_reply_percent < 0 || patch.voice_reply_percent > 100)) throw new Error("会话语音回复频率请输入 0–100 的整数百分比。");
            if (patch.reply_bubble_chars != null && (!Number.isSafeInteger(patch.reply_bubble_chars) || patch.reply_bubble_chars < 0 || patch.reply_bubble_chars > 240)) throw new Error("会话 QQ 气泡字数请输入 0–240 的整数。");
            if (!key.startsWith("group:")) continue;
            const modes=patch.group_triggers ?? currentPolicy?.group_triggers ?? ["mention"];
            const words=patch.group_keywords ?? currentPolicy?.group_keywords ?? [];
            if (!modes.length) throw new Error(`群 ${key.slice(6)} 请至少选择一个触发条件；停止回复请使用暂停开关。`);
            if (modes.includes("keyword") && !words.length) throw new Error(`群 ${key.slice(6)} 已选关键词触发，请填写关键词。`);
            if (words.length > 30 || words.some(word => word.length > 80)) throw new Error("关键词最多 30 个，每个不超过 80 字。");
          }
          body.muted_private_ids = body.muted_private_ids.filter(id => body.private_ids.includes(String(id)));
          body.muted_group_ids = body.muted_group_ids.filter(id => body.group_ids.includes(String(id)));
          if (currentPolicy?.mode !== "paused" && !body.private_ids.length && !body.group_ids.length) throw new Error("请先暂停代管，再清空回复列表。");
        } else if (action === "resume") {
          if (dirty) throw new Error("请先保存设置，或撤销修改，再开始代管。");
          if (!currentData?.connected) throw new Error("QQ 尚未连接，请先完成连接和登录。");
          if (!currentPolicy?.private_ids?.length && !currentPolicy?.group_ids?.length) throw new Error("请先添加回复对象并保存设置，再开始代管。");
        } else if (action === "mute" || action === "unmute") {
          const target = extra || {};
          const targetType = target.kind || target.targetType;
          const targetId = String(target.id || target.targetId || "");
          if (!/^(private|group)$/.test(targetType) || !/^\d+$/.test(targetId)) throw new Error("请选择有效的好友或群。");
          body = {action, targetType, targetId, expectedAccount: currentPolicy?.account_id};
        } else if (action === "clear_context") {
          const target = selectedConversation || {};
          const targetType = target.kind || target.targetType;
          const targetId = String(target.id || target.targetId || "");
          if (!/^(private|group)$/.test(targetType) || !/^\d+$/.test(targetId)) throw new Error("请选择有效的好友或群。");
          if (needsBinding(currentData)) throw new Error("请先确认当前 QQ 账号，再清理对象的对话上下文。");
          if (!gatewayRunning(currentData) || currentData?.unavailable) throw new Error("请先连接 QQ，再清理这个对象的对话上下文。");
          if (!currentPolicy?.account_id) throw new Error("尚未读取当前 QQ 账号，请刷新后重试。");
          const targetKey = `${targetType}:${targetId}`;
          if (contextClearConfirmation !== targetKey) throw new Error("请先展开确认区，再确认清理这个对象的对话上下文。");
          body = {action: "clear_context", targetType, targetId, expectedAccount: currentPolicy.account_id, confirm: true};
        }
        const submittedRevision = editRevision;
        const payload = JSON.stringify(body);
        if (action === "switch_account") contextClearConfirmation = "";
        busy = true; saving = action === "save"; generation += 1;
        updateControls();
        renderMuted({policy: currentPolicy || {}});
        feedback("正在处理…");
        if (action === "save" && !isEnabled()) await enablePermission();
        const data = await read("/api/qq/setup", {method: "POST", headers: {"Content-Type": "application/json"}, body: payload});
        if (action === "select_account") {
          // The selection response is only an acknowledgement. Drop every
          // account-scoped editor/cache before the follow-up GET so a late
          // poll or contacts response cannot repopulate the previous account.
          resetAccountUi();
          currentData = null;
          currentPolicy = null;
          if (ui.account) ui.account.textContent = "正在读取当前配置账号…";
          if (data.starting === true) {
            currentData = {starting: true, installed: data.installed !== false, gatewayRunning: false,
              connected: false, login: {}, accountProfiles: Array.isArray(data.accountProfiles) ? data.accountProfiles : []};
            render(currentData);
          }
        } else if (action === "save") {
          // Acknowledge only this submitted revision. Later input remains a
          // draft against the newly saved policy, not a conflicting old base.
          if (data.policy) savedPolicy = JSON.parse(JSON.stringify(data.policy));
          savedReplyMode = data.replyMode || body.replyMode;
          dirty = editRevision !== submittedRevision || composing.size > 0;
          if (!dirty) rawDrafts = {};
        } else if ((action === "mute" || action === "unmute") && dirty && savedPolicy) {
          // A per-conversation toggle must not discard an unrelated form draft.
          // Derive the snapshot from the acknowledged target so this also works
          // with a compact mutation response that omits the full policy.
          const key = body.targetType === "group" ? "muted_group_ids" : "muted_private_ids";
          const values = new Set(Array.isArray(savedPolicy[key]) ? savedPolicy[key].map(String) : []);
          if (action === "mute") values.add(String(body.targetId)); else values.delete(String(body.targetId));
          savedPolicy = {...savedPolicy, [key]: [...values]};
        } else if (["pause", "resume", "stop"].includes(action) && dirty && savedPolicy && data.policy) {
          // Stopping replies must not throw away text being edited in the form.
          savedPolicy = {...savedPolicy, mode: data.policy.mode};
          if (action === "stop") savedPolicy.background_enabled = data.policy.background_enabled;
        } else if (["mute", "unmute"].includes(action)) dirty = false;
        else if (action === "use_account") {
          const sameConfiguredAccount = String(currentData?.login?.accountId || "")
            === String(currentPolicy?.account_id || "");
          if (!sameConfiguredAccount) dirty = false;
        }
        if (action === "clear_context") contextClearConfirmation = "";
        if (action !== "select_account" && data.policy) currentData = {...currentData, policy: data.policy, replyMode: data.replyMode || currentData?.replyMode || savedReplyMode,
          replyAgent: data.replyAgent || currentData?.replyAgent, modelConfigured: data.modelConfigured ?? currentData?.modelConfigured};
        if (action === "start" || action === "setup" || action === "switch_account") {
          contactsRequested = false;
          currentData = {...currentData, starting: data.starting === true, unavailable: false};
        }
        if (action === "stop") currentData = {...currentData, gatewayRunning: false, connected: false, starting: false};
        if (currentData) render(currentData);
        if (!conflict) feedback(data.message || "操作已完成");
        clearFeedbackAfterStatus = ["start", "setup", "refresh_qr", "switch_account", "select_account", "use_account"].includes(action);
        onChanged?.();
      } catch (error) {
        if (action === "select_account") pendingAccountId = "";
        if (action === "clear_context") contextClearConfirmation = "";
        feedback(error.message);
      }
      finally { busy = false; saving = false; updateControls(); renderMuted({policy: currentPolicy || {}}); }
      await refresh(true);
      if (clearFeedbackAfterStatus && currentData && !currentData.unavailable && !conflict) feedback("");
    }

    ui["conversation-back"]?.addEventListener("click", () => { selectedConversation=null; contextClearConfirmation=""; showPage(); renderMuted({policy:currentPolicy || {}}); element.scrollIntoView?.({block:"start"}); });
    ui["conversation-reset"]?.addEventListener("click", () => { if (selectedConversation) { const key=`${selectedConversation.kind}:${selectedConversation.id}`; delete draftOverrides[key]; delete rawDrafts[key]; markDirty(false); } });
    ui["conversation-clear-context"]?.addEventListener("click", prepareContextClear);
    ui["conversation-clear-confirm"]?.addEventListener("click", () => { if (selectedConversation) return act("clear_context"); });
    ui["conversation-clear-cancel"]?.addEventListener("click", cancelContextClear);
    ui["conversation-toggle"]?.addEventListener("click", () => { if (selectedConversation) return act(targetMuted(currentPolicy, selectedConversation) ? "unmute" : "mute", selectedConversation); });
    fields.forEach(input => bindEditor(input, () => {
      markDirty();
      if (input === ui["reply-style"]) renderCorpusStatus(currentData);
    }));
    ui["qzone-enabled"]?.addEventListener("change", () => {
      if (!ui["qzone-enabled"].checked) {
        if (ui["qzone-allow-write"]) ui["qzone-allow-write"].checked = false;
        if (ui["qzone-allow-publish"]) ui["qzone-allow-publish"].checked = false;
        if (ui["qzone-autonomous"]) ui["qzone-autonomous"].checked = false;
      }
      markDirty();
    });
    ui["qzone-allow-write"]?.addEventListener("change", () => {
      if (!ui["qzone-allow-write"].checked && ui["qzone-allow-publish"]) ui["qzone-allow-publish"].checked = false;
      markDirty();
    });
    ui.start?.addEventListener("click", () => act("start"));
    ui.setup?.addEventListener('click', () => act('setup'));
    ui.refresh?.addEventListener("click", () => refresh(true));
    ui.discard?.addEventListener("click", () => {
      generation += 1; dirty = false; rawDrafts = {}; feedback("");
      if (currentData) render(currentData);
      return refresh(true);
    });
    ui["refresh-qr"]?.addEventListener("click", () => act("refresh_qr"));
    ui["switch-account"]?.addEventListener("click", () => act("switch_account"));
    ui["use-account"]?.addEventListener("click", () => act("use_account"));
    ui["account-profiles"]?.addEventListener("change", () => {
      accountSwitchTarget = normalizeAccountId(ui["account-profiles"].value);
      updateControls();
    });
    ui["new-account"]?.addEventListener("input", () => {
      accountSwitchTarget = String(ui["new-account"].value || "").trim();
      updateControls();
    });
    ui["select-account"]?.addEventListener("click", () => act("select_account"));
    ui["load-contacts"]?.addEventListener("click", () => loadContacts(true));
    ui["add-contact"]?.addEventListener("click", () => addSelectedContact());
    ui.contact?.addEventListener("change", updateControls);
    ui["media-files"]?.addEventListener("change", updateControls);
    ui["media-import"]?.addEventListener("click", () => importMedia());
    ui["media-search"]?.addEventListener("click", () => searchMedia());
    ui["mute-contact"]?.addEventListener("click", () => {
      try {
        const target = selectedContact();
        return act(targetMuted(currentPolicy, target) ? "unmute" : "mute", target);
      } catch (error) { ui.result.textContent = error.message; }
    });
    ui.save?.addEventListener("click", () => act("save"));
    ui.toggle?.addEventListener("click", () => {
      const action = runState(currentData).action;
      if (action && !ui.toggle.disabled) return act(action);
    });
    ui.pause?.addEventListener("click", () => act("pause"));
    ui.resume?.addEventListener("click", () => act("resume"));
    ui.stop?.addEventListener("click", () => act("stop"));
    let localSyncListening = false;
    function onLocalSync() {
      if (isVisible() && isEnabled()) void refresh(false);
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
    updateControls();
    return {refresh, act, loadContacts, addSelectedContact,
      start() { listenLocalSync(); clearTimer(timer); refresh(); timer = setTimer(refresh, 5000); },
      stop() {
        clearTimer(timer);
        unlistenLocalSync();
      }};
  }

  return {parseIds, statusText, runState, corpusStatusText, createController};
}));
