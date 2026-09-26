// 逐帧参数 renderer 唯一持有通用平滑值，以及运行时瞳孔/脸红的目标和当前值。
// 表情、语音和开发者实验台只提供目标或读取快照；本模块不复制它们的生命周期状态。
(function attachParameterRenderer(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusParameterRenderer = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createParameterRendererApi() {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function createParameterRenderer(options = {}) {
    const writeParameter = typeof options.writeParameter === "function"
      ? options.writeParameter
      : () => {};
    const resolveMappedParameterIds = typeof options.resolveMappedParameterIds === "function"
      ? options.resolveMappedParameterIds
      : (_channel, fallbackId = "") => (fallbackId ? [fallbackId] : []);
    const parameterValues = new Map();
    const runtimePupilScaleTargets = { left: 1, right: 1 };
    const runtimePupilScaleValues = { left: 1, right: 1 };
    let runtimeAuthorBlushOpacityTarget = 1;
    let runtimeAuthorBlushOpacityValue = 1;

    function setSmoothParam(id, target, factor = 0.18) {
      const key = String(id || "");
      if (!key) return target;
      const current = parameterValues.has(key) ? parameterValues.get(key) : target;
      const value = current + (target - current) * factor;
      parameterValues.set(key, value);
      writeParameter(key, value);
      return value;
    }

    function setMappedSmoothParam(channel, target, factor = 0.18, fallbackId = "") {
      resolveMappedParameterIds(channel, fallbackId).forEach(id => {
        setSmoothParam(id, target, factor);
      });
    }

    function updateRuntime({
      pupilScale = 0,
      annotatedPupilScales = [1, 1],
      authorBlushOpacity = 1,
    } = {}) {
      runtimeAuthorBlushOpacityTarget = clamp(number(authorBlushOpacity, 1), 0, 1);
      runtimeAuthorBlushOpacityValue += (
        runtimeAuthorBlushOpacityTarget - runtimeAuthorBlushOpacityValue
      ) * 0.24;
      const pupilVisibilityMultiplier = clamp(1 + number(pupilScale, 0), 0.26, 1);
      runtimePupilScaleTargets.left = clamp(
        number(annotatedPupilScales?.[0], 1) * pupilVisibilityMultiplier,
        0.35,
        1.35,
      );
      runtimePupilScaleTargets.right = clamp(
        number(annotatedPupilScales?.[1], 1) * pupilVisibilityMultiplier,
        0.35,
        1.35,
      );
      runtimePupilScaleValues.left += (
        runtimePupilScaleTargets.left - runtimePupilScaleValues.left
      ) * 0.24;
      runtimePupilScaleValues.right += (
        runtimePupilScaleTargets.right - runtimePupilScaleValues.right
      ) * 0.24;
      return getState();
    }

    function reapply() {
      parameterValues.forEach((value, id) => writeParameter(id, value));
    }

    function resetParameters() {
      parameterValues.clear();
    }

    function getState() {
      return {
        parameters: Object.fromEntries(parameterValues),
        runtimePupilScaleTargets: { ...runtimePupilScaleTargets },
        runtimePupilScaleValues: { ...runtimePupilScaleValues },
        runtimeAuthorBlushOpacityTarget,
        runtimeAuthorBlushOpacityValue,
      };
    }

    return Object.freeze({
      setSmoothParam,
      setMappedSmoothParam,
      updateRuntime,
      reapply,
      resetParameters,
      getState,
    });
  }

  return { createParameterRenderer };
}));
