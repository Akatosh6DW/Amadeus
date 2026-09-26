// Salieri 面板控制器唯一持有解锁状态、刷新/轮询 timer 与请求代次。
// 页面只提供 memory 页可见性、开关 DOM 和 storageScopedFetch；发布模式由页面门控，不发起开发者请求。
(function attachSalieriPanel(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusSalieriPanel = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createSalieriPanelApi() {
  "use strict";

  function createSalieriPanelController(options = {}) {
    const elements = options.elements || {};
    const transport = options.transport || {};
    const view = options.view || {};
    const clock = options.clock || {};
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("Salieri transport is unavailable");
          return fetch(url, init);
        };
    const setIntervalFn = typeof clock.setInterval === "function"
      ? clock.setInterval
      : (callback, delay) => setInterval(callback, delay);
    const clearIntervalFn = typeof clock.clearInterval === "function"
      ? clock.clearInterval
      : timer => clearInterval(timer);

    let bound = false;
    let open = false;
    let enabled = false;
    let generation = 0;
    let refreshInFlight = 0;
    let unlockInFlight = 0;
    let refreshTimer = 0;

    function isCurrent(token) {
      return token === generation;
    }

    function setMessage(message) {
      if (typeof view.setMessage === "function") {
        view.setMessage(String(message));
      } else if (elements.message) {
        elements.message.textContent = String(message);
      }
    }

    function setOpen(nextOpen) {
      open = Boolean(nextOpen);
      if (typeof view.setOpen === "function") {
        view.setOpen(open);
        return;
      }
      elements.window?.classList?.toggle?.("hidden", !open);
      elements.toggle?.setAttribute?.("aria-expanded", String(open));
    }

    function render(payload) {
      enabled = Boolean(payload?.enabled);
      elements.unlockForm?.classList?.toggle?.("hidden", enabled);
      elements.transcriptWrap?.classList?.toggle?.("hidden", !enabled);
      if (!enabled) {
        setMessage("AWAITING IDENTIFIER");
        return;
      }
      const transcript = String(payload?.content || "");
      const transcriptElement = elements.transcript;
      if (transcriptElement) {
        const nearBottom = (
          transcriptElement.scrollHeight
          - transcriptElement.scrollTop
          - transcriptElement.clientHeight
        ) < 64;
        transcriptElement.textContent = `${payload?.truncated ? "[仅显示文件尾部]\n\n" : ""}${transcript}`;
        if (nearBottom || !transcriptElement.dataset.loaded) {
          transcriptElement.scrollTop = transcriptElement.scrollHeight;
        }
        transcriptElement.dataset.loaded = "1";
      }
      if (elements.status) {
        elements.status.textContent = payload?.updatedAt
          ? `RECORDING / ${String(payload.updatedAt).replace("T", " ").slice(0, 19)}`
          : "RECORDING";
      }
      if (elements.path) elements.path.textContent = String(payload?.filePath || "");
      setMessage("ARCHIVE IS OUTSIDE MEMORY CONTROL");
    }

    async function refresh({ quiet = false } = {}) {
      const token = ++generation;
      refreshInFlight += 1;
      try {
        const response = await request("/api/salieri", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        if (!isCurrent(token)) return { stale: true };
        render(payload);
        return payload;
      } catch (error) {
        if (isCurrent(token) && !quiet) {
          setMessage(`CHANNEL ERROR / ${String(error?.message || error)}`);
        }
        return null;
      } finally {
        refreshInFlight = Math.max(0, refreshInFlight - 1);
      }
    }

    async function unlock(event) {
      event?.preventDefault?.();
      const code = String(elements.codeInput?.value || "").trim();
      const token = ++generation;
      unlockInFlight += 1;
      try {
        const response = await request("/api/salieri/unlock", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const payload = await response.json();
        if (!isCurrent(token)) return { stale: true };
        if (!response.ok || !payload?.enabled) {
          setMessage("ACCESS DENIED");
          return null;
        }
        if (elements.codeInput) elements.codeInput.value = "";
        render(payload);
        return payload;
      } catch (error) {
        if (isCurrent(token)) {
          setMessage(`CHANNEL ERROR / ${String(error?.message || error)}`);
        }
        return null;
      } finally {
        unlockInFlight = Math.max(0, unlockInFlight - 1);
      }
    }

    function toggle() {
      if (typeof view.isMemoryPageVisible === "function" && !view.isMemoryPageVisible()) {
        return false;
      }
      const currentlyHidden = elements.window?.classList?.contains?.("hidden");
      setOpen(currentlyHidden === undefined ? !open : currentlyHidden);
      if (open) void refresh({ quiet: true });
      return true;
    }

    function close() {
      setOpen(false);
      return true;
    }

    function startPolling() {
      clearIntervalFn(refreshTimer);
      refreshTimer = setIntervalFn(() => { void refresh({ quiet: true }); }, 10000);
      return refreshTimer;
    }

    function stopPolling() {
      clearIntervalFn(refreshTimer);
      refreshTimer = 0;
    }

    function bind() {
      if (bound) return false;
      bound = true;
      elements.toggle?.addEventListener?.("click", toggle);
      elements.close?.addEventListener?.("click", close);
      elements.unlockForm?.addEventListener?.("submit", unlock);
      elements.refresh?.addEventListener?.("click", () => { void refresh(); });
      return true;
    }

    return Object.freeze({
      bind,
      close,
      getState: () => ({
        bound,
        open,
        enabled,
        generation,
        refreshInFlight,
        unlockInFlight,
        refreshTimer,
      }),
      isEnabled: () => enabled,
      refresh,
      unlock,
      startPolling,
      stopPolling,
    });
  }

  return Object.freeze({ createSalieriPanelController });
}));
