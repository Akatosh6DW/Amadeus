(function attachConversationPicker(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusConversationPicker = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createConversationPickerApi() {
  "use strict";

  /**
   * 会话列表控制器拥有列表数据、读取请求及其取消状态，并负责面板内的渲染和操作。
   * 页面提供存储作用域请求、当前会话标识与一次会话转场；聊天消息、回复锚点和播放
   * 取消仍归页面所有。这样删除、创建或切换时先后执行的导航规则不会分散到列表 UI 中。
   */
  function createConversationPickerController({
    document,
    elements,
    request,
    session,
    beforeOpen,
    notify,
    confirm,
    createAbortController,
  }) {
    const state = {
      controller: null,
      conversations: [],
      loading: false,
    };
    let bound = false;

    function formatConversationTimestamp(value) {
      const parsed = new Date(String(value || ""));
      if (!Number.isFinite(parsed.getTime())) return "时间未知";
      return new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(parsed);
    }

    function setActionButtonsDisabled(disabled) {
      if (elements.trigger) elements.trigger.disabled = disabled;
      if (elements.createButton) elements.createButton.disabled = disabled;
    }

    function render() {
      if (!elements.list) return;
      elements.list.replaceChildren();
      if (elements.count) elements.count.textContent = `${state.conversations.length} 个`;
      if (!state.conversations.length) {
        const empty = document.createElement("div");
        empty.className = "chat-history-search-empty";
        empty.textContent = "还没有历史对话";
        elements.list.appendChild(empty);
        return;
      }
      state.conversations.forEach(conversation => {
        const conversationId = String(conversation.conversationId || "");
        const active = conversationId === String(session.getCurrentConversationId() || "");
        const row = document.createElement("div");
        row.className = `conversation-picker-row${active ? " active" : ""}`;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "conversation-picker-item";
        item.dataset.conversationId = conversationId;
        if (active) item.setAttribute("aria-current", "true");
        const head = document.createElement("span");
        head.className = "conversation-picker-item-head";
        const title = document.createElement("strong");
        title.className = "conversation-picker-item-title";
        title.textContent = String(conversation.title || "新对话");
        const at = document.createElement("span");
        at.className = "conversation-picker-item-time";
        at.textContent = formatConversationTimestamp(conversation.updatedAt || conversation.createdAt);
        head.append(title, at);
        const preview = document.createElement("span");
        preview.className = "conversation-picker-item-preview";
        preview.textContent = String(conversation.preview || "还没有消息");
        item.append(head, preview);
        item.addEventListener("click", () => switchToConversation(conversationId));
        const deleteButton = document.createElement("button");
        deleteButton.type = "button";
        deleteButton.className = "conversation-picker-delete-btn";
        deleteButton.textContent = "×";
        deleteButton.title = `删除对话：${String(conversation.title || "新对话")}`;
        deleteButton.setAttribute("aria-label", deleteButton.title);
        deleteButton.addEventListener("click", () => deleteConversation(conversation));
        row.append(item, deleteButton);
        elements.list.appendChild(row);
      });
    }

    async function load() {
      state.controller?.abort();
      const controller = createAbortController();
      state.controller = controller;
      state.loading = true;
      if (elements.status) elements.status.textContent = "正在读取对话…";
      try {
        const response = await request("/api/conversations", {
          cache: "no-store",
          signal: controller.signal,
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        state.conversations = Array.isArray(data.conversations) ? data.conversations : [];
        render();
        if (elements.status) elements.status.textContent = "选择一条旧对话继续，或新建对话";
      } catch (error) {
        if (error?.name === "AbortError") return;
        if (elements.status) elements.status.textContent = "对话列表读取失败";
        notify(`对话列表读取失败：${error.message}`);
      } finally {
        if (state.controller === controller) {
          state.controller = null;
          state.loading = false;
        }
      }
    }

    function open() {
      beforeOpen();
      elements.panel?.classList.remove("hidden");
      elements.trigger?.setAttribute("aria-expanded", "true");
      load();
    }

    function close() {
      state.controller?.abort();
      state.controller = null;
      state.loading = false;
      elements.panel?.classList.add("hidden");
      elements.trigger?.setAttribute("aria-expanded", "false");
    }

    async function startNewConversation() {
      setActionButtonsDisabled(true);
      try {
        session.prepareForNavigation();
        const response = await request("/api/conversation/new", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        await session.applyTransition({ kind: "new", data });
        close();
        elements.composer?.focus();
        notify("已开始新对话；长期记忆和关系仍然保留");
        return true;
      } catch (error) {
        notify(`新对话创建失败：${error.message}`);
        return false;
      } finally {
        setActionButtonsDisabled(false);
      }
    }

    async function deleteConversation(conversation) {
      const requestedId = String(conversation?.conversationId || "").trim();
      if (!requestedId) return;
      const title = String(conversation?.title || "新对话").trim() || "新对话";
      if (!confirm(`确定删除对话“${title}”吗？\n删除后无法恢复。`)) return;
      const active = requestedId === String(session.getCurrentConversationId() || "");
      if (elements.status) elements.status.textContent = "正在删除对话…";
      try {
        if (active) session.prepareForNavigation();
        const response = await request("/api/conversation/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversationId: requestedId }),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        if (data.activeDeleted) await session.applyTransition({ kind: "active-deleted", data });
        state.conversations = Array.isArray(data.conversations) ? data.conversations : [];
        render();
        if (elements.status) {
          elements.status.textContent = data.activeDeleted
            ? "当前对话已删除，已进入新的空白对话"
            : "对话已删除";
        }
        notify(data.activeDeleted ? "当前对话已删除，已开始新对话" : "对话已删除");
      } catch (error) {
        if (elements.status) elements.status.textContent = "删除失败，请重试";
        notify(`删除对话失败：${error.message}`);
      }
    }

    async function switchToConversation(conversationId) {
      const requestedId = String(conversationId || "").trim();
      if (!requestedId) return;
      if (requestedId === String(session.getCurrentConversationId() || "")) {
        close();
        elements.composer?.focus();
        return;
      }
      setActionButtonsDisabled(true);
      if (elements.status) elements.status.textContent = "正在打开对话…";
      try {
        session.prepareForNavigation();
        const response = await request("/api/conversation/switch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conversationId: requestedId }),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        await session.applyTransition({ kind: "switch", data, requestedId });
        close();
        elements.composer?.focus();
        notify("已打开历史对话，可以接着聊");
      } catch (error) {
        notify(`打开对话失败：${error.message}`);
        if (elements.status) elements.status.textContent = "打开失败，请重试";
      } finally {
        setActionButtonsDisabled(false);
      }
    }

    function bind() {
      if (bound) return;
      bound = true;
      elements.trigger?.addEventListener("click", () => {
        if (elements.panel?.classList.contains("hidden")) open();
        else close();
      });
      elements.closeButton?.addEventListener("click", close);
      elements.createButton?.addEventListener("click", startNewConversation);
    }

    return Object.freeze({
      bind,
      close,
      deleteConversation,
      load,
      open,
      startNewConversation,
      switchToConversation,
    });
  }

  return Object.freeze({ createConversationPickerController });
}));
