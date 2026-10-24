// 设置控制器只拥有表单草稿、保存/加载代次与保存中的事务；生效设置仍由页面状态经 port 原子替换。
// API 验证结论、密钥占位和运行状态继续由 api_verification.js 与页面窄接口协作，避免复制完整 settings。
(function attachSettingsController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusSettingsController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createSettingsControllerApi() {
  "use strict";

  function cloneDraft(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return { ...value };
  }

  function createSettingsController({
    getSettings,
    normalizeSettings,
    replaceSettings,
    renderForm,
    applySettingsToUi,
    request,
    onLoaded,
    onSettingsSaved,
    applyWindowSettings,
    notify,
    onLoadError,
  }) {
    if (typeof getSettings !== "function") throw new TypeError("settings getSettings port is required");
    if (typeof normalizeSettings !== "function") throw new TypeError("settings normalizeSettings port is required");
    if (typeof replaceSettings !== "function") throw new TypeError("settings replaceSettings port is required");
    if (typeof request !== "function") throw new TypeError("settings request port is required");

    let bound = false;
    let formDraft = {};
    let draftRevision = 0;
    let settingsRevision = 0;
    let loadToken = 0;
    let saveToken = 0;
    let loadInFlight = 0;
    let saveInFlight = 0;
    let lastSavedRevision = 0;
    // Native window rollback is a server mutation too.  Keep later saves
    // behind an already-started rollback so its response cannot land after a
    // newer settings POST and restore the old value.
    let rollbackInFlight = null;

    function bind() {
      if (bound) return false;
      bound = true;
      return true;
    }

    function setDraft(update = {}) {
      formDraft = { ...formDraft, ...cloneDraft(update) };
      draftRevision += 1;
      return { ...formDraft };
    }

    function clearDraft() {
      formDraft = {};
      draftRevision += 1;
    }

    function getState() {
      return {
        bound,
        draft: { ...formDraft },
        draftRevision,
        settingsRevision,
        loadInFlight,
        saveInFlight,
        savedRevision: lastSavedRevision,
      };
    }

    function replaceSnapshot(nextSettings, reason) {
      const normalized = normalizeSettings(nextSettings);
      settingsRevision += 1;
      replaceSettings(normalized, { reason, revision: settingsRevision });
      return normalized;
    }

    async function waitForRollback() {
      const pending = rollbackInFlight;
      if (pending) await pending;
    }

    function beginRollback(operation, previousSettings, update) {
      if (operation !== saveToken) return Promise.resolve(false);
      const rollback = (async () => {
        // The operation owns this rollback when it starts.  Later saves wait
        // for the full server/native pair before issuing their own mutation.
        try {
          if (operation !== saveToken) return false;
          const rollbackSettings = Object.fromEntries(
            Object.keys(update || {}).map(key => [key, previousSettings[key]]),
          );
          const response = await request("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ settings: rollbackSettings }),
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          if (typeof applyWindowSettings === "function") {
            await applyWindowSettings(previousSettings);
          }
          return true;
        } catch {
          // Preserve the original native error; rollback remains best effort.
          return false;
        }
      })();
      const tracked = rollback.finally(() => {
        if (rollbackInFlight === tracked) rollbackInFlight = null;
      });
      rollbackInFlight = tracked;
      return tracked;
    }

    function acceptExternalSettings(snapshot) {
      // An activation response already contains the authoritative public settings.
      // Invalidate older GETs so they cannot restore the previous connection.
      loadToken += 1;
      const normalized = replaceSnapshot({ ...getSettings(), ...snapshot }, "external");
      clearDraft();
      renderForm?.();
      applySettingsToUi?.();
      return normalized;
    }

    async function load() {
      const token = ++loadToken;
      const saveTokenAtStart = saveToken;
      loadInFlight += 1;
      try {
        const response = await request("/api/settings");
        const data = await response.json();
        if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
        // 新的加载或保存已经开始时，旧响应不能覆盖当前页面设置。
        if (token !== loadToken || saveTokenAtStart !== saveToken) return { stale: true };
        onLoaded?.(data);
        const normalized = replaceSnapshot(data.settings, "load");
        clearDraft();
        renderForm?.();
        return normalized;
      } catch (error) {
        if (token !== loadToken || saveTokenAtStart !== saveToken) return { stale: true };
        const fallback = replaceSnapshot(getSettings(), "load-fallback");
        renderForm?.();
        onLoadError?.(error);
        return fallback;
      } finally {
        loadInFlight = Math.max(0, loadInFlight - 1);
      }
    }

    async function save(update = {}, message = "设置已保存", applyWindow = false) {
      const operation = ++saveToken;
      const previousSettings = normalizeSettings(getSettings());
      const optimisticSettings = replaceSnapshot(
        { ...previousSettings, ...cloneDraft(update) },
        "optimistic-save",
      );
      setDraft(update);
      renderForm?.();
      saveInFlight += 1;
      try {
        if (rollbackInFlight) await waitForRollback();
        if (operation !== saveToken) return getSettings();
        const response = await request("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ settings: update }),
        });
        const data = await response.json();
        if (operation !== saveToken) return getSettings();
        if (!response.ok || !data || data.ok === false) {
          throw new Error(data?.error || `HTTP ${response.status}`);
        }
        const savedSettings = replaceSnapshot(
          { ...optimisticSettings, ...(data.settings || {}) },
          "saved",
        );
        lastSavedRevision = settingsRevision;
        onSettingsSaved?.(update, previousSettings);
        applySettingsToUi?.();
        renderForm?.();
        if (applyWindow && typeof applyWindowSettings === "function") {
          try {
            const nativeResult = await applyWindowSettings(savedSettings);
            if (operation !== saveToken) return getSettings();
            if (nativeResult?.ok === false) {
              throw new Error(nativeResult.error || "原生窗口设置应用失败");
            }
          } catch (error) {
            if (operation !== saveToken) return getSettings();
            await beginRollback(operation, previousSettings, update);
            if (operation !== saveToken) return getSettings();
            replaceSnapshot(previousSettings, "native-rollback");
            clearDraft();
            renderForm?.();
            throw error;
          }
        }
        clearDraft();
        notify?.(message);
        return savedSettings;
      } catch (error) {
        if (operation !== saveToken) return getSettings();
        replaceSnapshot(previousSettings, "save-rollback");
        clearDraft();
        renderForm?.();
        throw error;
      } finally {
        saveInFlight = Math.max(0, saveInFlight - 1);
      }
    }

    return Object.freeze({
      bind,
      load,
      acceptExternalSettings,
      save,
      setDraft,
      clearDraft,
      getState,
      isSaveInFlight: () => saveInFlight > 0,
      isLoadInFlight: () => loadInFlight > 0,
    });
  }

  return Object.freeze({ createSettingsController });
}));
