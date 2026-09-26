// 提醒轮询控制器独占提醒 timer、in-flight 状态和 poll/ack 生命周期。
// Todo 列表、聊天交付与主动等待门控通过窄端口协作；quiet/waiting 不会阻止已授权的定时提醒。
(function attachReminderPolling(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusReminderPolling = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createReminderPollingApi() {
  "use strict";

  function createReminderPollingController(options = {}) {
    const clock = options.clock || {};
    const session = options.session || {};
    const transport = options.transport || {};
    const delivery = options.delivery || {};
    const feedback = options.feedback || {};
    const setTimeoutFn = typeof clock.setTimeout === "function"
      ? clock.setTimeout
      : (callback, delay) => setTimeout(callback, delay);
    const clearTimeoutFn = typeof clock.clearTimeout === "function"
      ? clock.clearTimeout
      : timer => clearTimeout(timer);
    const setIntervalFn = typeof clock.setInterval === "function"
      ? clock.setInterval
      : (callback, delay) => setInterval(callback, delay);
    const clearIntervalFn = typeof clock.clearInterval === "function"
      ? clock.clearInterval
      : timer => clearInterval(timer);
    const now = typeof clock.now === "function" ? clock.now : () => Date.now();
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("reminder transport is unavailable");
          return fetch(url, init);
        };

    let reminderInFlight = false;
    let initialTimer = 0;
    let reminderTimer = 0;
    let refreshTimer = 0;

    function blocked() {
      const speech = session.getSpeechState?.() || {};
      return Boolean(
        reminderInFlight
        || session.isBootActive?.()
        || session.isChatRequestInFlight?.()
        || session.isProactiveRequestInFlight?.()
        || session.isTouchRequestInFlight?.()
        || speech.pending
        || session.isSpeaking?.(now()),
      );
    }

    async function poll() {
      if (blocked()) return false;
      reminderInFlight = true;
      let reminderId = "";
      try {
        const response = await request("/api/todos/reminders/poll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || "reminder poll failed");
        if (data.suppressed) {
          if (data.proactiveControl) delivery.applyProactiveControl?.(data.proactiveControl);
          return false;
        }
        reminderId = String(data.reminderId || "");
        if (!reminderId) return false;
        const delivered = await delivery.applyAssistant?.(data);
        await request("/api/todos/reminders/ack", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reminderId, delivered: Boolean(delivered) }),
        });
        await delivery.refreshTodos?.();
        return Boolean(delivered);
      } catch (error) {
        feedback.warn?.("todo reminder failed:", error);
        if (reminderId) {
          await request("/api/todos/reminders/ack", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              reminderId,
              delivered: false,
              error: String(error.message || error),
            }),
          }).catch(() => {});
        }
        return false;
      } finally {
        reminderInFlight = false;
      }
    }

    function start() {
      clearIntervalFn(reminderTimer);
      clearIntervalFn(refreshTimer);
      clearTimeoutFn(initialTimer);
      initialTimer = setTimeoutFn(() => poll(), 2500);
      reminderTimer = setIntervalFn(() => poll(), 10000);
      refreshTimer = setIntervalFn(() => delivery.refreshTodos?.(), 30000);
    }

    function stop() {
      clearTimeoutFn(initialTimer);
      clearIntervalFn(reminderTimer);
      clearIntervalFn(refreshTimer);
      initialTimer = 0;
      reminderTimer = 0;
      refreshTimer = 0;
    }

    return Object.freeze({
      getState: () => ({
        reminderInFlight,
        initialTimer,
        reminderTimer,
        refreshTimer,
      }),
      isInFlight: () => reminderInFlight,
      poll,
      start,
      stop,
    });
  }

  return Object.freeze({ createReminderPollingController });
}));
