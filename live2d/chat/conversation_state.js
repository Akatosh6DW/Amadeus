// 会话状态控制器唯一持有本地持久化、历史恢复合并和恢复代次；页面继续拥有实际消息数组与会话转场。
// 这样服务端迟到的历史不会越过当前会话或等待状态，聊天/主动控制器仍通过窄 port 读取生效状态。
(function attachConversationStateController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusConversationState = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createConversationStateApi() {
  "use strict";

  function createConversationStateController(options = {}) {
    const storage = options.storage || globalThis.localStorage;
    const storageKey = String(options.storageKey || "amadeus-live2d-chat-v2");
    const legacyStorageKey = String(options.legacyStorageKey || "");
    const maxMessages = Math.max(1, Number(options.maxMessages) || 240);
    const maxMemories = Math.max(1, Number(options.maxMemories) || 512);
    const defaultMemories = Array.isArray(options.defaultMemories)
      ? options.defaultMemories.slice()
      : [];
    const getState = typeof options.getState === "function" ? options.getState : () => null;
    const normalizeProactiveControl = typeof options.normalizeProactiveControl === "function"
      ? options.normalizeProactiveControl
      : value => (value && typeof value === "object" ? { ...value } : {});
    const visibleMessageContent = typeof options.visibleMessageContent === "function"
      ? options.visibleMessageContent
      : (_role, value) => String(value || "");
    const stripVisibleStageDirections = typeof options.stripVisibleStageDirections === "function"
      ? options.stripVisibleStageDirections
      : value => String(value || "");
    const clamp = typeof options.clamp === "function"
      ? options.clamp
      : (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
    const toNumber = typeof options.toNumber === "function"
      ? options.toNumber
      : (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
    const staleFallbackReplies = options.staleFallbackReplies instanceof Set
      ? options.staleFallbackReplies
      : new Set();
    const requestHistory = typeof options.requestHistory === "function"
      ? options.requestHistory
      : (...args) => globalThis.fetch(...args);
    const renderMessages = typeof options.renderMessages === "function"
      ? options.renderMessages
      : () => {};
    const hydrateContext = typeof options.hydrateContext === "function"
      ? options.hydrateContext
      : () => {};
    const notify = typeof options.notify === "function" ? options.notify : () => {};
    const warn = typeof options.warn === "function" ? options.warn : (...args) => console.warn(...args);
    const getWaitingForUser = typeof options.getWaitingForUser === "function"
      ? options.getWaitingForUser
      : () => Boolean(getState()?.waitingForUser);

    let hydrateGeneration = 0;

    function fallbackState() {
      return {
        messages: [],
        memories: defaultMemories.slice(),
        memoryRecords: [],
        contextEpoch: 0,
        conversationId: "conv_0",
        chatWindowHeight: 330,
        proactiveControl: normalizeProactiveControl(null),
        waitingForUser: false,
      };
    }

    function normalizeLoadedMessage(message) {
      const role = message?.role === "assistant" ? "assistant" : message?.role;
      return {
        ...message,
        content: visibleMessageContent(role, message?.content),
        meta: message?.meta && role === "assistant"
          ? { ...message.meta, spokenTextJa: stripVisibleStageDirections(message.meta.spokenTextJa) }
          : message?.meta,
      };
    }

    function load() {
      try {
        const raw = storage.getItem(storageKey)
          || (legacyStorageKey ? storage.getItem(legacyStorageKey) : null);
        if (raw) {
          const parsed = JSON.parse(raw);
          const contextEpoch = Math.max(0, Number(parsed.contextEpoch) || 0);
          return {
            messages: Array.isArray(parsed.messages)
              ? parsed.messages.map(normalizeLoadedMessage)
              : [],
            memories: Array.isArray(parsed.memories) ? parsed.memories : defaultMemories.slice(),
            memoryRecords: [],
            contextEpoch,
            conversationId: String(parsed.conversationId || `conv_${contextEpoch}`),
            chatWindowHeight: clamp(toNumber(parsed.chatWindowHeight, 330), 220, 1200),
            proactiveControl: normalizeProactiveControl(parsed.proactiveControl),
            waitingForUser: parsed.waitingForUser === true
              || parsed.proactiveControl?.reason === "unanswered_question_no_nudge",
          };
        }
      } catch {
        // Rebuild damaged local state below.
      }
      return fallbackState();
    }

    function messageHistorySignature(message) {
      const role = message?.role === "assistant" ? "assistant" : "user";
      const messageId = String(message?.meta?.messageId || "").trim();
      if (messageId) return `id\u0000${messageId}`;
      return `${role}\u0000${visibleMessageContent(role, message?.content).trim()}`;
    }

    function invalidateHydration() {
      hydrateGeneration += 1;
      return hydrateGeneration;
    }

    async function hydrate() {
      const generation = ++hydrateGeneration;
      try {
        const response = await requestHistory("/api/conversation/history", { cache: "no-store" });
        const data = await response.json();
        if (generation !== hydrateGeneration) return false;
        if (!response.ok || data.ok === false) {
          if (String(data.code || "").startsWith("context_")) {
            notify(data.error || "旧记录恢复失败，原文件已保留");
          }
          return false;
        }
        if (data.compatibilityRecovery?.status === "recovered") {
          notify(data.compatibilityRecovery.notice || "旧格式记录已恢复，原文件备份已保留");
        }
        if (data.recoveryRequired) {
          notify("检测到会话索引不一致；已从只读归档恢复显示，原始文件没有被覆盖");
          warn("conversation recovery is active:", data.storageHealth || {});
        }
        const state = getState();
        if (!state || generation !== hydrateGeneration) return false;
        const previousConversationId = state.conversationId;
        state.contextEpoch = Math.max(
          0,
          Number(data.conversationEpoch ?? state.contextEpoch) || 0,
        );
        state.conversationId = String(data.conversationId || `conv_${state.contextEpoch}`);
        hydrateContext(data, previousConversationId);
        state.messages = (Array.isArray(state.messages) ? state.messages : []).filter(message => {
          const messageConversationId = String(message?.meta?.conversationId || "");
          return messageConversationId
            ? messageConversationId === state.conversationId
            : Math.max(0, Number(message?.meta?.contextEpoch) || 0) === state.contextEpoch;
        });
        if (!Array.isArray(data.messages) || !data.messages.length) {
          renderMessages();
          persist();
          return true;
        }
        const localMessages = Array.isArray(state.messages) ? state.messages : [];
        // 以服务端历史为恢复基线，保存可按签名匹配的本地元数据，供下方逐条合并。
        const localBySignature = new Map();
        localMessages.forEach(message => {
          const key = messageHistorySignature(message);
          if (!localBySignature.has(key)) localBySignature.set(key, []);
          localBySignature.get(key).push(message);
        });
        const restored = data.messages.map(message => {
          const role = message?.role === "assistant" ? "assistant" : "user";
          const content = visibleMessageContent(role, message?.content);
          const key = messageHistorySignature({ role, content });
          const richerLocal = localBySignature.get(key)?.shift();
          return {
            role,
            content,
            meta: {
              ...(message?.meta || {}),
              ...(richerLocal?.meta || {}),
              actionReceipt: message?.meta?.actionReceipt || richerLocal?.meta?.actionReceipt || null,
              todoContext: message?.meta?.todoContext || richerLocal?.meta?.todoContext || null,
              toolUsed: message?.meta?.toolUsed || richerLocal?.meta?.toolUsed || "",
              toolStatus: message?.meta?.toolStatus || richerLocal?.meta?.toolStatus || "",
              contextEpoch: Math.max(
                0,
                Number(message?.meta?.contextEpoch ?? richerLocal?.meta?.contextEpoch ?? 0) || 0,
              ),
              restored: true,
            },
          };
        });
        let sharedPrefix = 0;
        const prefixLimit = Math.min(restored.length, localMessages.length);
        while (
          sharedPrefix < prefixLimit
          && messageHistorySignature(restored[sharedPrefix]) === messageHistorySignature(localMessages[sharedPrefix])
        ) sharedPrefix += 1;
        // 只有两端历史共享完整前缀时才接回本地尾部，避免把分叉历史混入当前会话。
        const localOnlyTail = sharedPrefix === prefixLimit ? localMessages.slice(sharedPrefix) : [];
        state.messages = [...restored, ...localOnlyTail].slice(-maxMessages);
        persist();
        return true;
      } catch (error) {
        if (generation === hydrateGeneration) warn("conversation history restore failed:", error);
        return false;
      }
    }

    function cleanHistoryForModel() {
      const state = getState() || fallbackState();
      const currentEpoch = Math.max(0, Number(state.contextEpoch) || 0);
      const currentConversationId = String(state.conversationId || "");
      return (Array.isArray(state.messages) ? state.messages : []).filter(message => {
        const messageConversationId = String(message.meta?.conversationId || "");
        const belongsToConversation = messageConversationId
          ? messageConversationId === currentConversationId
          : Math.max(0, Number(message.meta?.contextEpoch) || 0) === currentEpoch;
        return belongsToConversation
          && message.meta?.source !== "connection-error"
          && !String(message.content || "").startsWith("接口没有正常回应。")
          && !staleFallbackReplies.has(String(message.content || "").trim());
      });
    }

    function pruneStaleFallbacks() {
      const state = getState();
      if (!state) return false;
      const messages = Array.isArray(state.messages) ? state.messages : [];
      const cleaned = messages.filter(message =>
        !staleFallbackReplies.has(String(message.content || "").trim())
      );
      if (cleaned.length === messages.length) return false;
      state.messages = cleaned.slice(-maxMessages);
      persist();
      return true;
    }

    function persist() {
      const state = getState();
      if (!state) return;
      storage.setItem(storageKey, JSON.stringify({
        messages: (Array.isArray(state.messages) ? state.messages : []).slice(-maxMessages),
        memories: (Array.isArray(state.memories) ? state.memories : []).slice(0, maxMemories),
        contextEpoch: Math.max(0, Number(state.contextEpoch) || 0),
        conversationId: String(state.conversationId || `conv_${Math.max(0, Number(state.contextEpoch) || 0)}`),
        chatWindowHeight: clamp(toNumber(state.chatWindowHeight, 330), 220, 1200),
        proactiveControl: normalizeProactiveControl(state.proactiveControl),
        waitingForUser: getWaitingForUser(),
      }));
    }

    return Object.freeze({
      load,
      persist,
      hydrate,
      invalidateHydration,
      cleanHistoryForModel,
      pruneStaleFallbacks,
      messageHistorySignature,
      getState: () => ({ hydrateGeneration }),
    });
  }

  return Object.freeze({ createConversationStateController });
}));
