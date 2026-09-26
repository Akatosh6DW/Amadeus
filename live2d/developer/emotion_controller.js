// 开发者情绪面板唯一持有手动锁定和脸红草稿；表情控制器只通过 getter 读取当前覆盖。
// 面板的 DOM 读数、默认值、应用/释放时序留在此处，页面只提供表达式与 setEmotion 窄 port。
(function attachDeveloperEmotionController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusDeveloperEmotionController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createDeveloperEmotionControllerApi() {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function createDeveloperEmotionController(options = {}) {
    const dom = options.dom || {};
    const isDeveloper = typeof options.isDeveloper === "function" ? options.isDeveloper : () => false;
    const normalizeEmotion = typeof options.normalizeEmotion === "function"
      ? options.normalizeEmotion
      : value => String(value || "neutral");
    const normalizeGesture = typeof options.normalizeGesture === "function"
      ? options.normalizeGesture
      : value => String(value || "idle");
    const expressionDefaultsFor = typeof options.expressionDefaultsFor === "function"
      ? options.expressionDefaultsFor
      : () => ({ emotionIntensity: 1, blushIntensity: 0 });
    const getExpressionSnapshot = typeof options.getExpressionSnapshot === "function"
      ? options.getExpressionSnapshot
      : () => ({
        currentEmotion: "neutral",
        stableEmotion: "neutral",
        emotionRecoveryTarget: "neutral",
        currentEmotionWeight: 0,
        currentExpressionDetail: { blushIntensity: 0 },
        activeGesture: "idle",
      });
    const getRuntimeEmotionTarget = typeof options.getRuntimeEmotionTarget === "function"
      ? options.getRuntimeEmotionTarget
      : () => ({});
    const getEmotionSemantics = typeof options.getEmotionSemantics === "function"
      ? options.getEmotionSemantics
      : () => ({ role: "ordinary", family: "neutral" });
    const getVisualIntensity = typeof options.getVisualIntensity === "function"
      ? options.getVisualIntensity
      : value => number(value, 0);
    const getAnnotatedPlan = typeof options.getAnnotatedPlan === "function"
      ? options.getAnnotatedPlan
      : () => null;
    const setEmotion = typeof options.setEmotion === "function" ? options.setEmotion : () => {};
    const scheduleContextRefresh = typeof options.scheduleContextRefresh === "function"
      ? options.scheduleContextRefresh
      : () => {};
    const notify = typeof options.notify === "function" ? options.notify : () => {};
    let developerEmotionOverride = null;
    let developerEmotionManualBlush = null;

    function updateReadouts() {
      if (dom.devEmotionIntensityValue) {
        const rawIntensity = number(dom.devEmotionIntensityInput?.value, 1);
        const visualIntensity = getVisualIntensity(rawIntensity);
        dom.devEmotionIntensityValue.textContent = `${rawIntensity.toFixed(2)} → 表情 ${visualIntensity.toFixed(2)}`;
      }
      if (dom.devEmotionBlushValue) {
        dom.devEmotionBlushValue.textContent = `${Math.round(clamp(number(dom.devEmotionBlushInput?.value, 0), 0, 100))}%`;
      }
    }

    function renderPanel({ syncInputs = false } = {}) {
      if (!isDeveloper() || !dom.devEmotionStatus) return;
      const expression = getExpressionSnapshot() || {};
      if (syncInputs) {
        if (dom.devEmotionInput) dom.devEmotionInput.value = expression.currentEmotion;
        if (dom.devGestureInput) dom.devGestureInput.value = normalizeGesture(expression.activeGesture || "idle");
        if (dom.devEmotionIntensityInput) dom.devEmotionIntensityInput.value = String(expression.currentEmotionWeight);
        if (dom.devEmotionBlushInput) {
          dom.devEmotionBlushInput.value = String(Math.round(
            clamp(number(expression.currentExpressionDetail?.blushIntensity, 0), 0, 1) * 100,
          ));
        }
        if (dom.devEmotionLockInput) dom.devEmotionLockInput.checked = Boolean(developerEmotionOverride);
      }
      updateReadouts();
      const eyeCurve = number(
        getRuntimeEmotionTarget(expression.currentEmotion)?.ParamEyeSmile,
        getRuntimeEmotionTarget("neutral")?.ParamEyeSmile || 0,
      );
      const mode = developerEmotionOverride ? "MANUAL LOCK" : "AUTO";
      const blushPercent = Math.round(clamp(
        number(expression.currentExpressionDetail?.blushIntensity, 0),
        0,
        1,
      ) * 100);
      const semantics = getEmotionSemantics(expression.currentEmotion) || { role: "ordinary", family: "neutral" };
      const visualBaseEmotion = semantics.role === "cognitive"
        ? expression.cognitiveOverlayBaseEmotion
        : expression.stableEmotion;
      const visualIntensity = getVisualIntensity(expression.currentEmotionWeight);
      const annotatedPlan = getAnnotatedPlan(visualBaseEmotion);
      const mappingStatus = annotatedPlan
        ? `MAP ${String(annotatedPlan.styleId || "").toUpperCase()} / SIDE ${annotatedPlan.mirrorSide < 0 ? "MIRROR" : "BASE"}`
        : "MAP LEGACY";
      dom.devEmotionStatus.textContent = `${mode} / ${String(expression.currentEmotion || "neutral").toUpperCase()} / RAW ${number(expression.currentEmotionWeight, 0).toFixed(2)}→VISUAL ${number(visualIntensity, 0).toFixed(2)} / ${String(semantics.role || "ordinary").toUpperCase()}:${String(semantics.family || "neutral").toUpperCase()} / BASE ${String(visualBaseEmotion || "neutral").toUpperCase()} / ${mappingStatus} / NEXT ${String(expression.emotionRecoveryTarget || "neutral").toUpperCase()} / ${normalizeGesture(expression.activeGesture || "idle").toUpperCase()} / EYE ${eyeCurve.toFixed(2)} / BLUSH ${blushPercent}%`;
    }

    function loadDefaults() {
      if (!isDeveloper() || !dom.devEmotionInput) return;
      const emotion = normalizeEmotion(dom.devEmotionInput.value);
      const defaults = expressionDefaultsFor(emotion) || {};
      if (dom.devEmotionIntensityInput) dom.devEmotionIntensityInput.value = String(defaults.emotionIntensity);
      if (dom.devEmotionBlushInput) {
        dom.devEmotionBlushInput.value = String(Math.round(clamp(number(defaults.blushIntensity, 0), 0, 1) * 100));
      }
      updateReadouts();
    }

    function apply() {
      if (!isDeveloper() || !dom.devEmotionInput) return;
      const emotion = normalizeEmotion(dom.devEmotionInput.value);
      const gesture = normalizeGesture(dom.devGestureInput?.value || "idle");
      const emotionIntensity = clamp(number(dom.devEmotionIntensityInput?.value, 1), 0.15, 2.5);
      const blushIntensity = clamp(number(dom.devEmotionBlushInput?.value, 0) / 100, 0, 1);
      const locked = Boolean(dom.devEmotionLockInput?.checked);
      developerEmotionManualBlush = blushIntensity;
      const outburst = emotionIntensity > 1.25;
      developerEmotionOverride = locked ? { emotion, emotionIntensity, blushIntensity, outburst } : null;
      setEmotion(emotion, gesture, {
        developerOverride: true,
        restart: true,
        emotionIntensity,
        blushIntensity,
        deliveryMode: outburst ? "outburst" : "normal",
        outburst,
      });
      renderPanel({ syncInputs: true });
      notify(`开发者情绪：${emotion}${locked ? "（已锁定）" : "（临时）"}`);
    }

    function release() {
      if (!isDeveloper()) return;
      developerEmotionOverride = null;
      developerEmotionManualBlush = null;
      if (dom.devEmotionLockInput) dom.devEmotionLockInput.checked = false;
      const defaults = expressionDefaultsFor("neutral") || {};
      setEmotion("neutral", "idle", {
        developerOverride: true,
        restart: true,
        emotionIntensity: defaults.emotionIntensity,
        blushIntensity: 0,
        holdMs: 0,
        fadeMs: 900,
      });
      scheduleContextRefresh(1200);
      renderPanel({ syncInputs: true });
      notify("已恢复自动情绪控制");
    }

    function bind() {
      dom.devEmotionInput?.addEventListener("change", loadDefaults);
      dom.devEmotionIntensityInput?.addEventListener("input", updateReadouts);
      dom.devEmotionBlushInput?.addEventListener("input", updateReadouts);
      dom.devApplyEmotionBtn?.addEventListener("click", apply);
      dom.devReleaseEmotionBtn?.addEventListener("click", release);
    }

    return Object.freeze({
      bind,
      getOverride: () => developerEmotionOverride,
      getManualBlush: () => developerEmotionManualBlush,
      clearManualBlush: () => { developerEmotionManualBlush = null; },
      loadDefaults,
      updateReadouts,
      renderPanel,
      apply,
      release,
    });
  }

  return { createDeveloperEmotionController };
}));
