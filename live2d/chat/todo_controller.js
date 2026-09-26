// 待办控制器唯一持有列表、编辑器版本、加载重入和已查看 revision；页面只负责 DOM 渲染与窄 port。
// 提醒轮询仍由 reminder_polling.js 持有，接受提醒后仅调用 refreshTodos port，不复制待办状态。
(function attachTodoController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusTodoController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createTodoControllerApi() {
  "use strict";

  function numberOrZero(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function createTodoController(options = {}) {
    const elements = options.elements || {};
    const storage = options.storage || {};
    const transport = options.transport || {};
    const view = options.view || {};
    const feedback = options.feedback || {};
    const clock = options.clock || {};
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("todo transport is unavailable");
          return fetch(url, init);
        };
    const queueMicrotaskFn = typeof clock.queueMicrotask === "function"
      ? clock.queueMicrotask
      : callback => {
          if (typeof queueMicrotask === "function") queueMicrotask(callback);
          else Promise.resolve().then(callback);
        };

    let bound = false;
    let items = [];
    let activeCount = 0;
    let latestRevision = 0;
    let seenRevision = Math.max(0, numberOrZero(storage.readSeenRevision?.()));
    let unseenCount = 0;
    let loadInFlight = false;
    let loadPending = false;
    let markSeenPending = false;
    let editorTaskId = "";
    let editorVersion = 0;

    function snapshot() {
      return {
        items: items.slice(),
        activeCount,
        latestRevision,
        seenRevision,
        unseenCount,
        loadInFlight,
        loadPending,
        markSeenPending,
        editorTaskId,
        editorVersion,
      };
    }

    function renderBadge() {
      view.renderBadge?.(snapshot());
    }

    function render() {
      const state = snapshot();
      view.renderBadge?.(state);
      view.renderItems?.(state.items, state);
    }

    function markSeen(revision = latestRevision) {
      seenRevision = Math.max(seenRevision, Math.max(0, numberOrZero(revision)));
      unseenCount = 0;
      storage.writeSeenRevision?.(seenRevision);
      renderBadge();
      return seenRevision;
    }

    function isDrawerOpen() {
      if (typeof view.isDrawerOpen === "function") return Boolean(view.isDrawerOpen());
      return Boolean(elements.drawer?.classList?.contains?.("open"));
    }

    function setDrawerOpen(open) {
      const nextOpen = Boolean(open);
      if (typeof view.setDrawerOpen === "function") view.setDrawerOpen(nextOpen);
      if (nextOpen) {
        markSeen(latestRevision);
        return load({ markSeen: true });
      }
      return Promise.resolve(false);
    }

    async function load(optionsForLoad = {}) {
      const markSeenOnLoad = Boolean(optionsForLoad?.markSeen);
      if (loadInFlight) {
        loadPending = true;
        markSeenPending ||= markSeenOnLoad;
        return { pending: true };
      }
      loadInFlight = true;
      try {
        const scope = String(view.getFilterValue?.() || "active");
        const response = await request(
          "/api/todos?scope=" + encodeURIComponent(scope)
            + "&sinceRevision=" + encodeURIComponent(seenRevision),
        );
        const data = await response.json();
        if (!response.ok || data.ok === false) {
          throw new Error(data.error || "todo load failed");
        }
        items = Array.isArray(data.items) ? data.items : [];
        activeCount = Math.max(0, numberOrZero(data.activeCount));
        latestRevision = Math.max(0, numberOrZero(data.revision));
        unseenCount = Math.max(0, numberOrZero(data.unseenCount));
        if (markSeenOnLoad || isDrawerOpen()) markSeen(latestRevision);
        render();
        return snapshot();
      } catch (error) {
        feedback.warn?.("todo load failed:", error);
        feedback.notify?.("待办加载失败");
        return null;
      } finally {
        loadInFlight = false;
        if (loadPending) {
          const pendingMarkSeen = markSeenPending;
          loadPending = false;
          markSeenPending = false;
          queueMicrotaskFn(() => { void load({ markSeen: pendingMarkSeen }); });
        }
      }
    }

    async function todoApi(body) {
      const response = await request("/api/todos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || ("todo request failed (" + response.status + ")"));
      }
      return data;
    }

    function itemFromEvent(event) {
      const row = event.target?.closest?.("[data-task-id]");
      return items.find(item => String(item.taskId) === String(row?.dataset.taskId || ""));
    }

    async function createQuickTodo(event) {
      event?.preventDefault?.();
      const title = String(view.getQuickTitle?.() || "").trim();
      if (!title) return false;
      try {
        await todoApi({ action: "create", title });
        view.setQuickTitle?.("");
        await load();
        feedback.notify?.("已加入待办");
        return true;
      } catch (error) {
        feedback.notify?.("添加失败：" + error.message);
        return false;
      }
    }

    async function mutate(command, successMessage = "待办已更新") {
      try {
        await todoApi(command);
        await load();
        feedback.notify?.(successMessage);
        return true;
      } catch (error) {
        await load();
        feedback.notify?.("更新失败：" + error.message);
        return false;
      }
    }

    function openEditor(item) {
      if (!item) return false;
      editorTaskId = String(item.taskId || "");
      editorVersion = numberOrZero(item.version);
      view.setEditor?.({
        ...item,
        taskId: editorTaskId,
        version: editorVersion,
      });
      return true;
    }

    function closeEditor() {
      editorTaskId = "";
      editorVersion = 0;
      view.clearEditor?.();
    }

    function readEditor() {
      const value = view.readEditor?.() || {};
      const dueValue = String(value.dueAt || "");
      return {
        action: "update",
        taskId: editorTaskId,
        expectedVersion: editorVersion,
        title: String(value.title || "").trim(),
        details: String(value.details || "").trim(),
        dueAt: dueValue ? new Date(dueValue).toISOString() : "",
        dueDate: dueValue ? "" : String(value.dueDate || ""),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
        remindBeforeMinutes: Number(value.remindBeforeMinutes || 0),
        recurrenceRule: String(value.recurrenceRule || ""),
        priority: String(value.priority || "normal"),
      };
    }

    async function saveEditor(event) {
      event?.preventDefault?.();
      if (!editorTaskId) return false;
      const command = readEditor();
      await mutate(command, "精细设置已保存");
      closeEditor();
      return true;
    }

    async function deleteEditedTodo() {
      if (!editorTaskId) return false;
      const taskId = editorTaskId;
      const expectedVersion = editorVersion;
      closeEditor();
      await mutate({ action: "delete", taskId, expectedVersion }, "待办已删除");
      return true;
    }

    function handleListClick(event) {
      const action = event.target?.closest?.("[data-todo-action]")?.dataset.todoAction;
      const item = itemFromEvent(event);
      if (!item) return;
      if (action === "edit") openEditor(item);
      if (action === "snooze") {
        void mutate({
          action: "snooze",
          taskId: item.taskId,
          expectedVersion: numberOrZero(item.version),
          snoozeMinutes: 10,
        });
      }
    }

    function handleListChange(event) {
      if (!event.target?.matches?.(".todo-item-check")) return;
      const item = itemFromEvent(event);
      if (!item) return;
      void mutate({
        action: event.target.checked ? "complete" : "reopen",
        taskId: item.taskId,
        expectedVersion: numberOrZero(item.version),
      });
    }

    function bind() {
      if (bound) return false;
      bound = true;
      elements.toggle?.addEventListener?.("click", () => {
        void setDrawerOpen(!isDrawerOpen());
      });
      elements.refresh?.addEventListener?.("click", () => { void load(); });
      elements.filter?.addEventListener?.("change", () => { void load(); });
      elements.quickForm?.addEventListener?.("submit", event => { void createQuickTodo(event); });
      elements.list?.addEventListener?.("click", handleListClick);
      elements.list?.addEventListener?.("change", handleListChange);
      elements.editor?.addEventListener?.("submit", event => { void saveEditor(event); });
      elements.editorDueAt?.addEventListener?.("change", () => {
        if (elements.editorDueAt.value) elements.editorDueDate.value = "";
      });
      elements.editorDueDate?.addEventListener?.("change", () => {
        if (elements.editorDueDate.value) elements.editorDueAt.value = "";
      });
      elements.editorCancel?.addEventListener?.("click", closeEditor);
      elements.editorDelete?.addEventListener?.("click", () => { void deleteEditedTodo(); });
      return true;
    }

    return Object.freeze({
      bind,
      setDrawerOpen,
      load,
      markSeen,
      createQuickTodo,
      mutate,
      openEditor,
      closeEditor,
      saveEditor,
      deleteEditedTodo,
      getState: snapshot,
      isLoadInFlight: () => loadInFlight,
    });
  }

  return Object.freeze({ createTodoController });
}));
