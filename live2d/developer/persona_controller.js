// 人格控制器唯一持有人格读取/覆盖请求代次和两处面板 DOM。
// 设置对象、对话人格快照和 persona skill monitor 仍归页面/后端所有，请求通过注入的 storageScopedFetch。
(function attachPersonaController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusPersonaController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createPersonaControllerApi() {
  "use strict";

  function createPersonaController(options = {}) {
    const elements = options.elements || {};
    const transport = options.transport || {};
    const feedback = options.feedback || {};
    const view = options.view || {};
    const documentObject = options.document
      || (typeof document !== "undefined" ? document : null);
    const isDeveloper = typeof options.isDeveloper === "function"
      ? options.isDeveloper
      : () => false;
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("persona transport is unavailable");
          return fetch(url, init);
        };

    let bound = false;
    let generation = 0;
    let loadInFlight = 0;
    let saveInFlight = 0;
    let currentPersona = null;
    const saveButtonOwners = new WeakMap();

    function isCurrent(token) {
      return token === generation;
    }

    function setStatus(message) {
      if (elements.devStatus) elements.devStatus.textContent = String(message);
      if (elements.settingsStatus) elements.settingsStatus.textContent = String(message);
    }

    function activeElement(element) {
      return Boolean(documentObject && documentObject.activeElement === element);
    }

    function render(persona = {}) {
      currentPersona = persona && typeof persona === "object" ? { ...persona } : {};
      const summary = currentPersona.summary || {};
      const generatedRules = currentPersona.generatedRules || {};
      const emotionalSceneAnchors = currentPersona.emotionalSceneAnchors || {};
      const emotionalAnchors = Array.isArray(emotionalSceneAnchors.anchors)
        ? emotionalSceneAnchors.anchors
        : [];
      const global = generatedRules.global || {};
      if (elements.devStatus) {
        elements.devStatus.textContent = [
          `DeepSeek: ${summary.deepseekReady ? "READY" : "UNKNOWN"}`,
          `蒸馏来源: ${summary.distillationSource || generatedRules.source || "unknown"}`,
          `抽象标注: ${summary.distilledRows || generatedRules.rowCount || 0}`,
          `补丁: ${currentPersona.override ? "已启用" : "未设置"}`,
        ].join(" / ");
      }
      if (elements.settingsStatus) {
        elements.settingsStatus.textContent = currentPersona.override
          ? "补丁已启用"
          : "当前未设置临时补丁";
        elements.settingsStatus.title = currentPersona.overridePath || "";
      }
      if (elements.summary) {
        const lines = [
          summary.agentPositioning || "",
          summary.integration || "",
          `情绪标签: ${(global.emotion || []).join("、") || "-"}`,
          `动作标签: ${(global.gesture || []).join("、") || "-"}`,
          `语气标签: ${(global.voiceTone || []).join("、") || "-"}`,
          `名场面情绪锚点 (${emotionalAnchors.length}): ${emotionalAnchors.map(item => item.title || item.id).join("、") || "-"}`,
          `限制: ${(summary.limits || []).join("；") || "-"}`,
          `人格补丁文件: ${currentPersona.overridePath || "-"}`,
          `名场面配置: ${currentPersona.emotionalSceneAnchorsPath || "-"}`,
        ].filter(Boolean);
        elements.summary.textContent = lines.join("\n");
      }
      if (elements.devOverride && !activeElement(elements.devOverride)) {
        elements.devOverride.value = currentPersona.override || "";
      }
      if (elements.settingsOverride && !activeElement(elements.settingsOverride)) {
        elements.settingsOverride.value = currentPersona.override || "";
      }
      if (elements.generatedSkill) {
        elements.generatedSkill.value = currentPersona.generatedSkill || "";
      }
      return currentPersona;
    }

    async function load() {
      const token = ++generation;
      loadInFlight += 1;
      try {
        const response = await request("/api/persona");
        const data = await response.json();
        if (!isCurrent(token)) return { stale: true };
        if (!response.ok || data.ok === false) {
          throw new Error(data.error || `HTTP ${response.status}`);
        }
        const persona = render(data.persona || {});
        if (isDeveloper() || view.isSettingsVisible?.()) {
          feedback.notify?.("人格配置已加载");
        }
        return persona;
      } catch (error) {
        if (!isCurrent(token)) return { stale: true };
        setStatus(`加载失败：${error.message}`);
        feedback.notify?.(`人格加载失败：${error.message}`);
        return null;
      } finally {
        loadInFlight = Math.max(0, loadInFlight - 1);
      }
    }

    async function save(sourceInput = elements.devOverride, saveButton = elements.devSave) {
      const token = ++generation;
      const oldText = saveButton?.textContent || "";
      if (saveButton) {
        saveButtonOwners.set(saveButton, token);
        saveButton.disabled = true;
        saveButton.textContent = "保存中";
      }
      saveInFlight += 1;
      try {
        const response = await request("/api/persona", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ override: sourceInput?.value || "" }),
        });
        const data = await response.json();
        if (!isCurrent(token)) return { stale: true };
        if (!response.ok || data.ok === false) {
          throw new Error(data.error || `HTTP ${response.status}`);
        }
        const persona = render(data.persona || {});
        feedback.notify?.("人格补丁已保存，下一轮对话生效");
        return persona;
      } catch (error) {
        if (isCurrent(token)) feedback.notify?.(`人格补丁保存失败：${error.message}`);
        return null;
      } finally {
        if (saveButton && saveButtonOwners.get(saveButton) === token) {
          saveButton.disabled = false;
          saveButton.textContent = oldText || "保存人格补丁";
          saveButtonOwners.delete(saveButton);
        }
        saveInFlight = Math.max(0, saveInFlight - 1);
      }
    }

    function bind() {
      if (bound) return false;
      bound = true;
      elements.reloadDev?.addEventListener?.("click", () => { void load(); });
      elements.reloadSettings?.addEventListener?.("click", () => { void load(); });
      elements.devSave?.addEventListener?.("click", () => {
        void save(elements.devOverride, elements.devSave);
      });
      elements.settingsSave?.addEventListener?.("click", () => {
        void save(elements.settingsOverride, elements.settingsSave);
      });
      return true;
    }

    return Object.freeze({
      bind,
      getState: () => ({
        bound,
        generation,
        loadInFlight,
        saveInFlight,
        persona: currentPersona ? { ...currentPersona } : null,
      }),
      load,
      render,
      save,
    });
  }

  return Object.freeze({ createPersonaController });
}));
