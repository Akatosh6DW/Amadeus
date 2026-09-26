(function attachBookmarks(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusBookmarks = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createBookmarksApi() {
  "use strict";

  /**
   * 书签控制器拥有列表、编辑目标、版本和合并读取状态，并完成面板内的 CRUD 与委托事件。
   * 调用方只提供存储作用域网关、面板协调与提示；模型工具回执也只作为是否重新读取的信号，
   * 不把聊天消息或工具执行状态带入本模块。
   */
  function createBookmarksController({
    document,
    elements,
    gateway,
    beforeOpen,
    notify,
    setTimer,
    queueMicrotask,
    now,
  }) {
    const state = {
      items: [],
      editorId: "",
      editorVersion: 0,
      loadInFlight: false,
      loadPending: false,
    };
    let bound = false;

    function bookmarkHost(value) {
      try {
        return new URL(String(value || "")).host;
      } catch {
        return String(value || "");
      }
    }

    function render() {
      if (!elements.list) return;
      elements.list.replaceChildren();
      elements.empty?.classList.toggle("hidden", state.items.length > 0);
      if (elements.count) elements.count.textContent = `${state.items.length} 个`;
      for (const item of state.items) {
        const row = document.createElement("article");
        row.className = "bookmark-item";
        row.dataset.bookmarkId = String(item.bookmarkId || "");

        const main = document.createElement("div");
        main.className = "bookmark-item-main";
        const name = document.createElement("div");
        name.className = "bookmark-item-name";
        name.textContent = String(item.name || "未命名收藏");
        const host = document.createElement("div");
        host.className = "bookmark-item-host";
        host.textContent = bookmarkHost(item.url);
        host.title = String(item.url || "");
        main.append(name, host);

        const actions = document.createElement("div");
        actions.className = "bookmark-item-actions";
        const open = document.createElement("button");
        open.type = "button";
        open.dataset.bookmarkAction = "open";
        open.textContent = "打开";
        open.setAttribute("aria-label", `打开收藏：${String(item.name || "未命名收藏")}`);
        const edit = document.createElement("button");
        edit.type = "button";
        edit.dataset.bookmarkAction = "edit";
        edit.textContent = "⚙";
        edit.title = "编辑收藏";
        edit.setAttribute("aria-label", `编辑收藏：${String(item.name || "未命名收藏")}`);
        actions.append(open, edit);
        row.append(main, actions);
        elements.list.append(row);
      }
    }

    async function load() {
      if (!elements.list) return;
      if (state.loadInFlight) {
        state.loadPending = true;
        return;
      }
      state.loadInFlight = true;
      try {
        const data = await gateway.listBookmarks();
        state.items = Array.isArray(data.items) ? data.items : [];
        render();
      } catch (error) {
        console.warn("bookmark load failed:", error);
        notify("收藏夹加载失败");
      } finally {
        state.loadInFlight = false;
        if (state.loadPending) {
          state.loadPending = false;
          queueMicrotask(load);
        }
      }
    }

    function open() {
      beforeOpen();
      elements.panel?.classList.remove("hidden");
      elements.trigger?.setAttribute("aria-expanded", "true");
      load();
      setTimer(() => elements.quickName?.focus(), 0);
    }

    function closeEditor() {
      state.editorId = "";
      state.editorVersion = 0;
      elements.editor?.classList.add("hidden");
    }

    function close() {
      closeEditor();
      elements.panel?.classList.add("hidden");
      elements.trigger?.setAttribute("aria-expanded", "false");
    }

    function bookmarkFromEvent(event) {
      const row = event.target?.closest?.("[data-bookmark-id]");
      return state.items.find(item => (
        String(item.bookmarkId || "") === String(row?.dataset.bookmarkId || "")
      ));
    }

    async function openStoredBookmark(item) {
      try {
        await gateway.openBrowser({
          url: String(item.url || ""),
          actionId: `bookmark_open_${String(item.bookmarkId || "")}_${now()}`,
        });
      } catch (error) {
        notify("打开失败：" + error.message);
      }
    }

    function openEditor(item) {
      state.editorId = String(item.bookmarkId || "");
      state.editorVersion = Number(item.version || 0);
      elements.editorName.value = String(item.name || "");
      elements.editorUrl.value = String(item.url || "");
      elements.editor?.classList.remove("hidden");
      elements.editorName?.focus();
    }

    function handleListClick(event) {
      const action = event.target?.closest?.("[data-bookmark-action]")?.dataset.bookmarkAction;
      const item = bookmarkFromEvent(event);
      if (!item) return;
      if (action === "open") openStoredBookmark(item);
      if (action === "edit") openEditor(item);
    }

    async function createQuickBookmark(event) {
      event?.preventDefault();
      const name = String(elements.quickName?.value || "").trim();
      const url = String(elements.quickUrl?.value || "").trim();
      if (!name || !url) return;
      try {
        await gateway.mutateBookmark({ action: "create", name, url });
        elements.quickName.value = "";
        elements.quickUrl.value = "";
        await load();
        notify("网页已收藏");
      } catch (error) {
        await load();
        notify("收藏失败：" + error.message);
      }
    }

    async function saveEditor(event) {
      event?.preventDefault();
      if (!state.editorId) return;
      try {
        await gateway.mutateBookmark({
          action: "update",
          bookmarkId: state.editorId,
          expectedVersion: state.editorVersion,
          name: String(elements.editorName?.value || "").trim(),
          url: String(elements.editorUrl?.value || "").trim(),
        });
        closeEditor();
        await load();
        notify("收藏已更新");
      } catch (error) {
        await load();
        notify("保存失败：" + error.message);
      }
    }

    async function deleteEditedBookmark() {
      if (!state.editorId) return;
      const bookmarkId = state.editorId;
      const expectedVersion = state.editorVersion;
      try {
        await gateway.mutateBookmark({ action: "delete", bookmarkId, expectedVersion });
        closeEditor();
        await load();
        notify("收藏已删除");
      } catch (error) {
        await load();
        notify("删除失败：" + error.message);
      }
    }

    function acceptedBookmarkMutation(data) {
      const receipts = [
        data?.actionReceipt,
        ...(Array.isArray(data?.actionReceipts) ? data.actionReceipts : []),
      ].filter(receipt => receipt && typeof receipt === "object");
      return receipts.some(receipt => (
        String(receipt.tool || "") === "bookmark_manage"
        && String(receipt.status || "") === "accepted"
      ));
    }

    function refreshAfterAssistantAction(data) {
      if (acceptedBookmarkMutation(data)) load();
    }

    function bind() {
      if (bound) return;
      bound = true;
      elements.trigger?.addEventListener("click", () => {
        if (elements.panel?.classList.contains("hidden")) open();
        else close();
      });
      elements.closeButton?.addEventListener("click", close);
      elements.refreshButton?.addEventListener("click", load);
      elements.quickForm?.addEventListener("submit", createQuickBookmark);
      elements.list?.addEventListener("click", handleListClick);
      elements.editor?.addEventListener("submit", saveEditor);
      elements.editorCancelButton?.addEventListener("click", closeEditor);
      elements.editorDeleteButton?.addEventListener("click", deleteEditedBookmark);
    }

    return Object.freeze({
      bind,
      close,
      closeEditor,
      load,
      refreshAfterAssistantAction,
    });
  }

  return Object.freeze({ createBookmarksController });
}));
