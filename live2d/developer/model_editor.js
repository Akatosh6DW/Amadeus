// 开发者模型编辑器唯一持有所选模型、JSON 草稿和刷新/保存代次；场景控制器仍持有注册表与生效 profile。
// 预览导航和覆盖请求沿用原 payload，页面只提供场景快照、网络和提示 port。
(function attachDeveloperModelEditor(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusDeveloperModelEditor = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createDeveloperModelEditorApi() {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  const ADVANCED_KEYS = Object.freeze([
    "supportsNativeExpressions",
    "interactionSummary",
    "lipSync",
    "eyeVisibilityCompensation",
    "headTurn",
    "fullScaleMultiplier",
    "displayBounds",
    "expressionMap",
    "fixedParameters",
    "hiddenParameters",
    "hiddenParts",
    "parameterAliases",
    "motionMap",
  ]);

  function createDeveloperModelEditorController(options = {}) {
    const dom = options.dom || {};
    const document = options.document || globalThis.document;
    const window = options.window || globalThis;
    const isDeveloper = typeof options.isDeveloper === "function" ? options.isDeveloper : () => false;
    const getSceneSnapshot = typeof options.getSceneSnapshot === "function"
      ? options.getSceneSnapshot
      : () => ({ profiles: [], overrides: {}, activeProfile: {} });
    const getPreviewModelId = typeof options.getPreviewModelId === "function"
      ? options.getPreviewModelId
      : () => "";
    const loadRegistry = typeof options.loadRegistry === "function" ? options.loadRegistry : async () => {};
    const request = typeof options.request === "function" ? options.request : (...args) => window.fetch(...args);
    const notify = typeof options.notify === "function" ? options.notify : () => {};
    const confirmAction = typeof options.confirm === "function"
      ? options.confirm
      : message => window.confirm(message);
    const clampValue = typeof options.clamp === "function" ? options.clamp : clamp;
    const toNumber = typeof options.toNumber === "function" ? options.toNumber : number;
    let bound = false;
    let selectedModel = "";
    let loadGeneration = 0;
    let mutationGeneration = 0;
    let mutationBusy = false;

    function scene() {
      const snapshot = getSceneSnapshot() || {};
      return {
        profiles: Array.isArray(snapshot.profiles) ? snapshot.profiles : [],
        overrides: snapshot.overrides && typeof snapshot.overrides === "object" ? snapshot.overrides : {},
        activeProfile: snapshot.activeProfile || {},
      };
    }

    function selectedModelId() {
      const snapshot = scene();
      const fromInput = String(dom.devModelProfileInput?.value || "");
      const candidate = selectedModel || fromInput || String(getPreviewModelId() || "")
        || String(snapshot.activeProfile?.id || "");
      return snapshot.profiles.some(profile => profile?.id === candidate)
        ? candidate
        : String(snapshot.activeProfile?.id || candidate);
    }

    function advancedProfile(profile) {
      const advanced = {};
      ADVANCED_KEYS.forEach(key => {
        if (Object.prototype.hasOwnProperty.call(profile || {}, key)) advanced[key] = profile[key];
      });
      return advanced;
    }

    function render() {
      if (!isDeveloper() || !dom.devModelProfileInput) return;
      const snapshot = scene();
      const profiles = snapshot.profiles;
      const candidate = selectedModelId();
      selectedModel = profiles.some(profile => profile?.id === candidate)
        ? candidate
        : String(getPreviewModelId() || snapshot.activeProfile?.id || profiles[0]?.id || "");
      dom.devModelProfileInput.replaceChildren(...profiles.map(profile => {
        const option = document.createElement("option");
        option.value = profile.id;
        option.textContent = profile.label || profile.id;
        return option;
      }));
      dom.devModelProfileInput.value = selectedModel;
      const profile = profiles.find(item => item.id === selectedModel) || snapshot.activeProfile || {};
      const override = snapshot.overrides[selectedModel] || {};
      if (dom.devModelScaleInput) dom.devModelScaleInput.value = toNumber(profile.scaleMultiplier, 1);
      if (dom.devModelOffsetXInput) dom.devModelOffsetXInput.value = toNumber(profile.offsetX, 0);
      if (dom.devModelOffsetYInput) dom.devModelOffsetYInput.value = toNumber(profile.offsetY, 0);
      if (dom.devModelOverrideJson && document.activeElement !== dom.devModelOverrideJson) {
        dom.devModelOverrideJson.value = JSON.stringify(advancedProfile(profile), null, 2);
      }
      if (dom.devModelProfileHint) {
        const active = profile.id === snapshot.activeProfile?.id ? "当前正在预览" : "尚未预览";
        const custom = Object.keys(override).length ? "已有开发覆盖" : "使用注册表默认值";
        dom.devModelProfileHint.textContent = `${profile.description || profile.label} / ${active} / ${custom}`;
      }
      if (dom.devModelOverrideStatus) {
        dom.devModelOverrideStatus.textContent = "覆盖文件：data/live2d_model_overrides.json；不会修改普通模式当前皮肤。";
      }
    }

    function preview() {
      if (!isDeveloper()) return;
      const modelId = selectedModelId();
      const url = new URL(window.location.href);
      url.searchParams.set("dev", "1");
      url.searchParams.set("skipBoot", "1");
      url.searchParams.set("modelPreview", modelId);
      url.searchParams.delete("previewOnly");
      window.location.assign(url.toString());
    }

    async function reload() {
      if (!isDeveloper()) return;
      const generation = ++loadGeneration;
      try {
        await loadRegistry();
        if (generation !== loadGeneration) return;
        render();
        notify("Live2D 皮肤覆盖配置已重载");
      } catch (error) {
        if (generation === loadGeneration) notify(`模型配置重载失败：${error.message}`);
      }
    }

    function readOverride() {
      let override;
      try {
        override = JSON.parse(dom.devModelOverrideJson?.value.trim() || "{}");
        if (!override || Array.isArray(override) || typeof override !== "object") {
          throw new Error("高级覆盖必须是 JSON 对象");
        }
      } catch (error) {
        notify(`模型覆盖 JSON 无效：${error.message}`);
        return null;
      }
      override.scaleMultiplier = clampValue(toNumber(dom.devModelScaleInput?.value, 1), 0.5, 2.5);
      override.offsetX = clampValue(toNumber(dom.devModelOffsetXInput?.value, 0), -1200, 1200);
      override.offsetY = clampValue(toNumber(dom.devModelOffsetYInput?.value, 0), -1200, 1200);
      return override;
    }

    async function mutate({ reset = false } = {}) {
      if (!isDeveloper() || mutationBusy) return;
      const modelId = selectedModelId();
      if (reset && !confirmAction("恢复这套皮肤的注册表默认值吗？")) return;
      const override = reset ? null : readOverride();
      if (!reset && !override) return;
      const generation = ++mutationGeneration;
      mutationBusy = true;
      if (dom.devSaveModelOverrideBtn) dom.devSaveModelOverrideBtn.disabled = true;
      if (dom.devResetModelOverrideBtn) dom.devResetModelOverrideBtn.disabled = true;
      try {
        const response = await request("/api/developer/live2d-models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(reset ? { modelId, reset: true } : { modelId, override }),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        if (generation !== mutationGeneration) return;
        notify(reset ? "皮肤覆盖已恢复默认，正在重载预览" : "皮肤覆盖已保存，正在重载预览");
        preview();
      } catch (error) {
        if (generation === mutationGeneration) notify(`${reset ? "恢复默认失败" : "模型覆盖保存失败"}：${error.message}`);
      } finally {
        if (generation === mutationGeneration) {
          mutationBusy = false;
          if (dom.devSaveModelOverrideBtn) dom.devSaveModelOverrideBtn.disabled = false;
          if (dom.devResetModelOverrideBtn) dom.devResetModelOverrideBtn.disabled = false;
        }
      }
    }

    function bind() {
      if (bound) return false;
      bound = true;
      dom.devModelProfileInput?.addEventListener("change", () => {
        selectedModel = String(dom.devModelProfileInput.value || "");
        render();
      });
      dom.devPreviewModelBtn?.addEventListener("click", preview);
      dom.devReloadModelsBtn?.addEventListener("click", reload);
      dom.devSaveModelOverrideBtn?.addEventListener("click", () => mutate());
      dom.devResetModelOverrideBtn?.addEventListener("click", () => mutate({ reset: true }));
      return true;
    }

    return Object.freeze({
      bind,
      render,
      preview,
      reload,
      save: () => mutate(),
      reset: () => mutate({ reset: true }),
      selectedModelId,
      getState: () => ({
        bound,
        selectedModel,
        loadGeneration,
        mutationGeneration,
        mutationBusy,
      }),
    });
  }

  return Object.freeze({ createDeveloperModelEditorController });
}));
