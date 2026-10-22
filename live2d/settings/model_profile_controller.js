// Saved model connection profiles.  The browser receives metadata only; the
// local API owns the private credentials and applies them during activation.
(function attachModelProfileController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusModelProfileController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createModelProfileControllerApi() {
  "use strict";

  function createModelProfileController({
    elements,
    request,
    savedApiKeyMask = "••••••••••••",
    getDraft,
    onActivated,
    notify = () => {},
    confirm = () => true,
  }) {
    if (!elements || typeof request !== "function") throw new TypeError("model profile ports are required");
    if (typeof getDraft !== "function") throw new TypeError("model profile draft port is required");
    let profiles = [];
    let activeProfileId = null;
    let bound = false;
    let selectedProfileId = "";

    function setStatus(message, error = false) {
      if (!elements.status) return;
      elements.status.textContent = String(message || "");
      elements.status.classList.toggle("error", Boolean(error));
    }

    function selectedProfile() {
      return profiles.find(item => item.id === selectedProfileId) || null;
    }

    function render(payload = {}) {
      if (Array.isArray(payload.profiles)) profiles = payload.profiles.filter(item => item && item.id);
      if (Object.hasOwn(payload, "activeProfileId")) activeProfileId = payload.activeProfileId || null;
      if (!profiles.some(item => item.id === selectedProfileId)) {
        selectedProfileId = activeProfileId || profiles[0]?.id || "";
      }
      if (elements.select) {
        elements.select.replaceChildren(...profiles.map(profile => {
          const option = document.createElement("option");
          option.value = profile.id;
          const keyLabel = profile.apiKeySet ? " · Key 已保存" : " · 未保存 Key";
          option.textContent = `${profile.name || profile.id} · ${profile.apiModel || "未命名模型"}${keyLabel}`;
          return option;
        }));
        elements.select.value = selectedProfileId;
        elements.select.disabled = profiles.length === 0;
      }
      const hasSelection = Boolean(selectedProfile());
      if (elements.activate) elements.activate.disabled = !hasSelection;
      if (elements.remove) elements.remove.disabled = !hasSelection;
      if (elements.active) {
        const active = profiles.find(item => item.id === activeProfileId);
        elements.active.textContent = active ? `当前：${active.name}` : "当前：未绑定配置档";
      }
      return { profiles: profiles.slice(), activeProfileId };
    }

    async function read(url, init) {
      const response = await request(url, init);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    }

    function keyValue() {
      if (elements.clearKey?.checked) return "__CLEAR__";
      const value = String(elements.apiKey?.value || "").trim();
      return !value || value === savedApiKeyMask ? "__KEEP__" : value;
    }

    function draftPayload() {
      const draft = getDraft() || {};
      return {
        id: elements.overwrite?.checked ? (elements.select?.value || undefined) : undefined,
        name: String(elements.name?.value || "").trim(),
        apiEndpoint: String(elements.endpoint?.value || draft.apiEndpoint || "").trim(),
        apiModel: String(elements.model?.value || draft.apiModel || "").trim(),
        cognitiveRepairModel: String(elements.cognitive?.value || draft.cognitiveRepairModel || "follow"),
        apiKey: keyValue(),
        apiNetworkMode: String(elements.networkMode?.value || draft.apiNetworkMode || "auto"),
        apiProxyUrl: String(elements.proxy?.value || draft.apiProxyUrl || "").trim(),
        useResponseFormat: Boolean(elements.responseFormat?.checked ?? draft.useResponseFormat),
      };
    }

    async function refresh() {
      try {
        const payload = await read("/api/model-profiles", { cache: "no-store" });
        setStatus(payload.profiles?.length ? "已加载本机配置档" : "还没有保存的配置档");
        return render(payload);
      } catch (error) {
        setStatus(`配置档读取失败：${error.message}`, true);
        throw error;
      }
    }

    async function save() {
      try {
        const payload = await read("/api/model-profiles/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(draftPayload()),
        });
        if (elements.clearKey) elements.clearKey.checked = false;
        if (elements.overwrite) elements.overwrite.checked = false;
        setStatus("配置档已保存");
        notify("模型连接配置档已保存");
        return render(payload);
      } catch (error) {
        setStatus(`配置档保存失败：${error.message}`, true);
        notify(`配置档保存失败：${error.message}`);
        return { ok: false, error: error.message };
      }
    }

    async function activate() {
      const profile = selectedProfile();
      if (!profile) return { ok: false, code: "profile_not_selected" };
      try {
        const payload = await read("/api/model-profiles/activate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: profile.id }),
        });
        render(payload);
        setStatus(`已切换到：${profile.name}`);
        await onActivated?.(payload);
        notify(`已启用模型配置档：${profile.name}`);
        return payload;
      } catch (error) {
        setStatus(`配置档切换失败：${error.message}`, true);
        notify(`配置档切换失败：${error.message}`);
        return { ok: false, error: error.message };
      }
    }

    async function remove() {
      const profile = selectedProfile();
      if (!profile) return { ok: false, code: "profile_not_selected" };
      if (!confirm(`删除配置档“${profile.name}”？当前已生效的模型设置不会改变。`)) {
        return { ok: false, cancelled: true };
      }
      try {
        const payload = await read("/api/model-profiles/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: profile.id }),
        });
        selectedProfileId = "";
        setStatus("配置档已删除；当前模型设置未改变");
        notify("模型连接配置档已删除");
        return render(payload);
      } catch (error) {
        setStatus(`配置档删除失败：${error.message}`, true);
        notify(`配置档删除失败：${error.message}`);
        return { ok: false, error: error.message };
      }
    }

    function bind() {
      if (bound) return false;
      bound = true;
      elements.select?.addEventListener("change", () => {
        selectedProfileId = elements.select.value;
        render();
      });
      elements.save?.addEventListener("click", () => { save(); });
      elements.activate?.addEventListener("click", () => { activate(); });
      elements.remove?.addEventListener("click", () => { remove(); });
      return true;
    }

    return Object.freeze({ bind, refresh, render, save, activate, remove, getState: () => ({
      profiles: profiles.slice(), activeProfileId, selectedProfileId,
    }) });
  }

  return Object.freeze({ createModelProfileController });
}));
