// Saved profiles and the editor have separate state. Credentials stay on the server.
(function attachModelProfileController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusModelProfileController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  function createModelProfileController({ elements: el, request, getDraft,
    savedApiKeyMask = "••••••••••••", onActivated, notify = () => {}, confirm = () => true }) {
    let profiles = [], activeProfileId = null, selectedProfileId = "";
    let editing = false, editingId = null, dirty = false, busy = false, bound = false;
    let revision = 0, clearKey = false;
    const selected = () => profiles.find(p => p.id === selectedProfileId);
    function status(message, error = false) {
      el.status.textContent = message;
      el.status.classList.toggle("error", error);
    }
    function render(payload = {}) {
      if (Array.isArray(payload.profiles)) profiles = payload.profiles;
      if (Object.hasOwn(payload, "activeProfileId")) activeProfileId = payload.activeProfileId;
      if (!selected()) selectedProfileId = activeProfileId || profiles[0]?.id || "";
      el.select.replaceChildren(...profiles.map(p => {
        const option = document.createElement("option");
        option.value = p.id;
        option.textContent = `${p.name} · ${p.apiModel}${p.id === activeProfileId ? " · 使用中" : ""}${p.apiKeySet ? "" : " · 未设置 Key"}`;
        return option;
      }));
      el.select.value = selectedProfileId;
      el.select.disabled = busy || editing || !profiles.length;
      for (const name of ["activate", "edit", "remove"]) el[name].disabled = busy || editing || !selected();
      el.activate.disabled ||= selectedProfileId === activeProfileId;
      el.activate.textContent = selectedProfileId === activeProfileId ? "正在使用" : "切换到此配置";
      el.create.disabled = busy || editing;
      for (const name of ["save", "saveUse", "cancel"]) el[name].disabled = busy || !editing;
      for (const name of ["name", "endpoint", "model", "apiKey", "networkMode", "proxy", "responseFormat", "cognitive"]) el[name].disabled = busy;
      if (el.clearKey) {
        el.clearKey.disabled = busy || !editingId || !profiles.find(p => p.id === editingId)?.apiKeySet;
        el.clearKey.textContent = clearKey ? "撤销移除 Key" : "移除此配置已保存的 Key";
      }
      el.editor.classList.toggle("hidden", !editing);
      const active = profiles.find(p => p.id === activeProfileId);
      el.active.textContent = active ? `当前使用：${active.name}（${active.apiModel}）`
        : `当前使用：${getDraft()?.apiModel || "尚未配置"}（尚未绑定已保存配置）`;
      return { profiles: profiles.slice(), activeProfileId };
    }
    async function read(url, init) {
      const response = await request(url, init);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    }
    const post = (action, body) => read(`/api/model-profiles/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    async function refresh() {
      if (busy) return render();
      const token = ++revision;
      try {
        const payload = await read("/api/model-profiles", { cache: "no-store" });
        return token === revision ? render(payload) : render();
      } catch (error) {
        if (token === revision) status(`配置读取失败：${error.message}`, true);
        throw error;
      }
    }
    function beginEdit(isNew = false) {
      if (busy || editing) return false;
      const p = isNew ? {} : selected();
      if (!p) return false;
      editing = true; editingId = p.id || null; dirty = false; clearKey = false;
      el.title.textContent = isNew ? "新增配置" : `编辑配置：${p.name}`;
      el.name.value = p.name || "";
      el.endpoint.value = p.apiEndpoint || "";
      el.model.value = p.apiModel || "";
      el.apiKey.value = p.apiKeySet ? savedApiKeyMask : "";
      el.apiKey.placeholder = p.apiKeySet ? "已保存；留空保留，输入新 Key 可替换" : "填写此服务商的 API Key";
      el.networkMode.value = p.apiNetworkMode || "auto";
      el.proxy.value = p.apiProxyUrl || "";
      el.responseFormat.checked = p.useResponseFormat !== false;
      el.cognitive.value = p.cognitiveRepairModel || "follow";
      if (el.keyHint) el.keyHint.textContent = p.apiKeySet ? "这套配置已保存 Key，留空或保持圆点即可保留。" : "这套配置尚未设置 Key。";
      el.save.textContent = isNew ? "保存配置" : "保存修改";
      status(isNew ? "正在新增配置；当前使用的配置保持不变。" : `正在编辑“${p.name}”；保存后可选择是否使用。`);
      render(); el.name.focus?.(); return true;
    }
    function cancel() {
      if (busy) return false;
      if (dirty && !confirm("放弃尚未保存的配置修改？")) return false;
      editing = false; editingId = null; dirty = false; el.apiKey.value = "";
      render(); status("已结束编辑，当前使用的配置未改变。"); return true;
    }
    function draftPayload() {
      const key = el.apiKey.value.trim();
      return {
        ...(editingId ? { id: editingId } : {}), name: el.name.value.trim(),
        apiEndpoint: el.endpoint.value.trim(), apiModel: el.model.value.trim(),
        apiKey: clearKey ? "__CLEAR__" : key && key !== savedApiKeyMask ? key : editingId ? "__KEEP__" : "__CLEAR__",
        apiNetworkMode: el.networkMode.value, apiProxyUrl: el.proxy.value.trim(),
        cognitiveRepairModel: el.cognitive.value, useResponseFormat: el.responseFormat.checked,
      };
    }
    async function useProfile(id) {
      const payload = await post("activate", { id });
      render(payload);
      await onActivated?.(payload);
      status(`当前使用：${payload.profile?.name || selected()?.name || "所选配置"}`);
      notify("模型配置已切换");
      return payload;
    }
    async function save(useAfterSave = false) {
      if (busy || !editing) return { ok: false };
      const draft = draftPayload();
      if (!draft.name || !draft.apiEndpoint || !draft.apiModel) {
        status("请填写配置名称、接口地址和模型名称。", true); return { ok: false };
      }
      busy = true; revision += 1; render();
      let saved = false;
      try {
        const payload = await post("save", draft);
        selectedProfileId = payload.savedProfileId;
        editing = false; editingId = null; dirty = false; el.apiKey.value = ""; saved = true;
        render(payload);
        if (useAfterSave) return await useProfile(selectedProfileId);
        status(`已保存“${draft.name}”；当前使用的设置未改变。点击“切换到此配置”可应用。`);
        notify("配置已保存"); return payload;
      } catch (error) {
        status(`${saved ? "配置已保存，但切换失败" : "保存失败"}：${error.message}`, true);
        return { ok: false, error: error.message };
      } finally { busy = false; render(); }
    }
    async function activate() {
      if (busy || editing || !selected()) return { ok: false };
      busy = true; revision += 1; render();
      try { return await useProfile(selectedProfileId); }
      catch (error) { status(`切换失败：${error.message}`, true); return { ok: false }; }
      finally { busy = false; render(); }
    }
    async function remove() {
      if (busy || editing || !selected()) return { ok: false };
      if (!confirm(`删除配置“${selected().name}”？当前正在使用的设置会保留。`)) return { cancelled: true };
      busy = true; revision += 1; render();
      try { const payload = await post("delete", { id: selectedProfileId }); render(payload); status("配置已删除，当前使用的设置未改变。"); return payload; }
      catch (error) { status(`删除失败：${error.message}`, true); return { ok: false }; }
      finally { busy = false; render(); }
    }
    function bind() {
      if (bound) return false; bound = true;
      el.select.addEventListener("change", () => { if (!editing && !busy) selectedProfileId = el.select.value; render(); });
      el.create.addEventListener("click", () => beginEdit(true));
      el.edit.addEventListener("click", () => beginEdit(false));
      el.cancel.addEventListener("click", cancel);
      el.clearKey?.addEventListener("click", () => {
        if (busy || !editingId) return;
        clearKey = !clearKey; dirty = true;
        if (el.keyHint) el.keyHint.textContent = clearKey
          ? "保存后将移除这套配置的 Key；取消编辑可放弃。"
          : "已取消移除 Key，留空或保持圆点可保留原 Key。";
        render();
      });
      el.save.addEventListener("click", () => save(false));
      el.saveUse.addEventListener("click", () => save(true));
      el.activate.addEventListener("click", activate);
      el.remove.addEventListener("click", remove);
      for (const name of ["name", "endpoint", "model", "apiKey", "networkMode", "proxy", "responseFormat", "cognitive"]) {
        for (const event of ["input", "change"]) el[name].addEventListener(event, () => { if (editing) {
          dirty = true;
          if (name === "apiKey" && clearKey) {
            clearKey = false;
            if (el.keyHint) el.keyHint.textContent = "保存后将使用刚输入的 Key。";
            render();
          }
        } });
      }
      render(); return true;
    }
    return Object.freeze({ bind, refresh, render, beginEdit, cancel, save, activate, remove,
      getState: () => ({ profiles: profiles.slice(), activeProfileId, selectedProfileId, editing, editingId, dirty, busy }) });
  }
  return Object.freeze({ createModelProfileController });
}));
