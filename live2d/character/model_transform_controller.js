// 模型变换控制器唯一持有开发者坐标草稿与预览写入；生效 settings 仍由页面经 replaceSettings port 持有。
// 选择模型、页面渲染和 fitModel 继续由页面负责，控制器只处理 scale/offset 的窄交互。
(function attachModelTransformController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusModelTransformController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createModelTransformControllerApi() {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function normalizeTransform(value) {
    const source = value && typeof value === "object" ? value : {};
    return {
      scale: clamp(number(source.scale, 1), 0.35, 2.4),
      offsetX: clamp(number(source.offsetX, 0), -1200, 1200),
      offsetY: clamp(number(source.offsetY, 0), -1200, 1200),
    };
  }

  function loadDeveloperTransforms(storage, key) {
    try {
      const parsed = JSON.parse(storage.read?.(key) || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      return Object.fromEntries(
        Object.entries(parsed).map(([modelId, transform]) => [modelId, normalizeTransform(transform)]),
      );
    } catch {
      return {};
    }
  }

  function createModelTransformController(options = {}) {
    const storage = options.storage || {};
    const settings = options.settings || {};
    const view = options.view || {};
    const isDeveloper = typeof options.isDeveloper === "function"
      ? options.isDeveloper
      : () => false;
    const getSettings = typeof settings.get === "function" ? settings.get : () => ({});
    const replaceSettings = typeof settings.replace === "function" ? settings.replace : () => {};
    const getSelectedModelId = typeof options.getSelectedModelId === "function"
      ? options.getSelectedModelId
      : () => "";
    const getActiveModelId = typeof options.getActiveModelId === "function"
      ? options.getActiveModelId
      : getSelectedModelId;
    const fit = typeof options.fit === "function" ? options.fit : () => {};
    const notify = typeof options.notify === "function" ? options.notify : () => {};
    const storageKey = String(options.storageKey || "amadeus-dev-model-transforms-v1");
    let bound = false;
    let developerTransforms = loadDeveloperTransforms(storage, storageKey);

    function persistDeveloperTransforms() {
      storage.write?.(storageKey, JSON.stringify(developerTransforms));
    }

    function selectedModelId() {
      return String(getSelectedModelId() || getActiveModelId() || "");
    }

    function transformFor(modelId = getActiveModelId()) {
      const id = String(modelId || "");
      if (isDeveloper() && developerTransforms[id]) {
        return normalizeTransform(developerTransforms[id]);
      }
      return normalizeTransform(getSettings()?.modelTransforms?.[id]);
    }

    function syncInputs() {
      const transform = transformFor(selectedModelId());
      if (view.scaleInput) view.scaleInput.value = Math.round(transform.scale * 100);
      if (view.offsetXInput) view.offsetXInput.value = transform.offsetX;
      if (view.offsetYInput) view.offsetYInput.value = transform.offsetY;
      if (view.scaleValue) view.scaleValue.value = `${Math.round(transform.scale * 100)}%`;
      return transform;
    }

    function readInputs() {
      return normalizeTransform({
        scale: number(view.scaleInput?.value, 100) / 100,
        offsetX: view.offsetXInput?.value,
        offsetY: view.offsetYInput?.value,
      });
    }

    function preview() {
      const modelId = selectedModelId();
      const transform = readInputs();
      if (isDeveloper()) {
        developerTransforms = { ...developerTransforms, [modelId]: transform };
        persistDeveloperTransforms();
      } else {
        const current = getSettings() || {};
        replaceSettings({
          ...current,
          modelTransforms: { ...(current.modelTransforms || {}), [modelId]: transform },
        }, { reason: "model-transform-preview" });
      }
      if (view.scaleValue) view.scaleValue.value = `${Math.round(transform.scale * 100)}%`;
      if (modelId === String(getActiveModelId() || "")) fit();
      return transform;
    }

    function reset() {
      const modelId = selectedModelId();
      if (isDeveloper()) {
        const next = { ...developerTransforms };
        delete next[modelId];
        developerTransforms = next;
        persistDeveloperTransforms();
      } else {
        const current = getSettings() || {};
        const next = { ...(current.modelTransforms || {}) };
        delete next[modelId];
        replaceSettings({ ...current, modelTransforms: next }, { reason: "model-transform-reset" });
      }
      syncInputs();
      if (modelId === String(getActiveModelId() || "")) fit();
      notify(isDeveloper()
        ? "已清除开发者坐标，当前回到普通模式的模型位置；普通设置未改动"
        : "已恢复这套皮肤的默认大小和位置，保存显示设置后生效");
      return transformFor(modelId);
    }

    function bind() {
      if (bound) return false;
      bound = true;
      view.modelInput?.addEventListener?.("change", () => {
        options.onModelSelectionChanged?.();
        syncInputs();
      });
      [view.scaleInput, view.offsetXInput, view.offsetYInput].forEach(input => {
        input?.addEventListener?.("input", preview);
      });
      view.resetButton?.addEventListener?.("click", reset);
      return true;
    }

    return Object.freeze({
      bind,
      normalizeTransform,
      getTransformFor: transformFor,
      getSelectedModelId: selectedModelId,
      syncInputs,
      preview,
      reset,
      getState: () => ({
        bound,
        developerTransforms: Object.fromEntries(
          Object.entries(developerTransforms).map(([id, value]) => [id, { ...value }]),
        ),
      }),
    });
  }

  return Object.freeze({ createModelTransformController, normalizeTransform });
}));
