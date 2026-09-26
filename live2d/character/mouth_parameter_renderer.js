// 口型参数 renderer 唯一持有 ParamMouthOpenY/ParamMouthForm 的逐帧平滑值。
// 语音播放器仍拥有 speech sample；表情控制器仍拥有 emotion target，本模块只组合窄 port 后写入模型。
(function attachMouthParameterRenderer(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusMouthParameterRenderer = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createMouthParameterRendererApi() {
  "use strict";

  const MOUTH_TEMPLATE_FAMILY_BY_EMOTION = Object.freeze({
    affectionate: "smile",
    amused: "smile",
    laughing: "smile",
    excited: "smile",
    proud: "smile",
    teasing: "smile",
    thinking: "awkward",
    focused: "awkward",
    skeptical: "awkward",
    awkward: "awkward",
    shy: "awkward",
    flustered: "awkward",
    concerned: "awkward",
    annoyed: "angry",
    furious: "angry",
  });

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function hasFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function mouthCurveWithSpeech(emotionCurve, speechFormDelta) {
    const baseCurve = clamp(number(emotionCurve, 0), -1, 1);
    let result = baseCurve + clamp(number(speechFormDelta, 0), -0.7, 0.7) * 0.38;
    // 语音可以改变嘴的开合与幅度，但不能把明确的笑/皱嘴拖过零点。
    if (baseCurve >= 0.22) result = Math.max(result, baseCurve * 0.58);
    if (baseCurve <= -0.22) result = Math.min(result, baseCurve * 0.58);
    return clamp(result, -1, 1);
  }

  function mouthTemplateCurveForEmotion(emotion, fallbackCurve = 0, expressionPlan = null) {
    if (hasFiniteNumber(expressionPlan?.mouthForm)) {
      return clamp(expressionPlan.mouthForm, -1, 1);
    }
    const family = MOUTH_TEMPLATE_FAMILY_BY_EMOTION[String(emotion || "")] || "";
    if (family === "smile") return clamp(Math.max(0.82, fallbackCurve), -1, 1);
    if (family === "awkward") return 0;
    // AiPoet rig 的明确生气嘴约在 -0.34，不能把数值端点当作语义最大值。
    if (family === "angry") return clamp(fallbackCurve, -0.48, -0.24);
    return clamp(number(fallbackCurve, 0), -1, 1);
  }

  function createMouthParameterRenderer(options = {}) {
    const getProfile = typeof options.getProfile === "function" ? options.getProfile : () => ({});
    const writeParameter = typeof options.writeParameter === "function"
      ? options.writeParameter
      : () => {};
    const state = {
      openValue: null,
      formValue: null,
    };

    function lipSyncProfile() {
      const profile = getProfile() || {};
      return {
        openMultiplier: clamp(number(profile.openMultiplier, 1.2), 0.5, 2.2),
        formMultiplier: clamp(number(profile.formMultiplier, 1.12), 0.4, 1.8),
        formOffset: clamp(number(profile.formOffset, 0), -0.5, 0.5),
        openSmoothing: clamp(number(profile.openSmoothing, 0.5), 0.08, 0.8),
        formSmoothing: clamp(number(profile.formSmoothing, 0.34), 0.08, 0.8),
      };
    }

    function smooth(current, target, factor) {
      return current === null ? target : current + (target - current) * factor;
    }

    function getState() {
      return {
        openValue: state.openValue,
        formValue: state.formValue,
      };
    }

    function render(input = {}) {
      const profile = lipSyncProfile();
      const sample = input.sample && typeof input.sample === "object" ? input.sample : {};
      const expressionPlan = input.expressionPlan && typeof input.expressionPlan === "object"
        ? input.expressionPlan
        : null;
      const fallbackCurve = clamp(
        number(input.emotionMouthForm, 0)
          + number(input.motionMouthSmile, 0)
          + profile.formOffset,
        -1,
        1,
      );
      const mouthOpenTarget = clamp(Math.max(
        number(sample.mouthOpen, 0) * profile.openMultiplier,
        hasFiniteNumber(expressionPlan?.mouthOpenY)
          ? expressionPlan.mouthOpenY
          : number(input.emotionMouthOpenY, 0),
      ), 0, 1);
      const mouthFormTarget = mouthCurveWithSpeech(
        mouthTemplateCurveForEmotion(input.visualEmotion, fallbackCurve, expressionPlan),
        number(sample.mouthForm, 0) * profile.formMultiplier,
      );
      state.openValue = smooth(state.openValue, mouthOpenTarget, profile.openSmoothing);
      state.formValue = smooth(state.formValue, mouthFormTarget, profile.formSmoothing);
      writeParameter("ParamMouthOpenY", state.openValue);
      writeParameter("ParamMouthForm", state.formValue);
      return getState();
    }

    function reapply() {
      if (state.openValue !== null) writeParameter("ParamMouthOpenY", state.openValue);
      if (state.formValue !== null) writeParameter("ParamMouthForm", state.formValue);
      return getState();
    }

    function reset() {
      state.openValue = null;
      state.formValue = null;
      return getState();
    }

    return Object.freeze({
      getState,
      lipSyncProfile,
      mouthCurveWithSpeech,
      mouthTemplateCurveForEmotion,
      reapply,
      render,
      reset,
    });
  }

  return Object.freeze({
    createMouthParameterRenderer,
    mouthCurveWithSpeech,
    mouthTemplateCurveForEmotion,
  });
}));
