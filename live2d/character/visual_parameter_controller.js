// 视觉参数帧控制器唯一持有窄眼补偿、运行时瞳孔/脸红派生和逐帧参数写入。
// 页面只提交同一帧快照与窄写入端口；情绪、语音、场景和开发者覆盖仍由各自 owner 持有。
(function attachVisualParameterController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusVisualParameterController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createVisualParameterApi() {
  "use strict";

  const DEFAULT_EYE_VISIBILITY_COMPENSATION = Object.freeze({
    sustainedDelayMs: 240,
    minimumVisibleOpening: 0.42,
    closeThreshold: 0.32,
    families: Object.freeze({
      default: Object.freeze([
        Object.freeze([0, 0, 0, 0]),
        Object.freeze([0.18, 0.08, 0, 0]),
        Object.freeze([0.42, 0.54, -0.08, -0.12]),
        Object.freeze([0.62, 1, 0, 0]),
      ]),
    }),
  });

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function toNumber(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function smoothstep(value) {
    const amount = clamp(value, 0, 1);
    return amount * amount * (3 - 2 * amount);
  }

  function easeOutCubic(value) {
    return 1 - Math.pow(1 - value, 3);
  }

  function createVisualParameterController(options = {}) {
    const getActiveProfile = typeof options.getActiveProfile === "function"
      ? options.getActiveProfile
      : () => ({});
    const getVisualEmotion = typeof options.getVisualEmotion === "function"
      ? options.getVisualEmotion
      : () => "neutral";
    const visualEmotionIntensityWeight = typeof options.visualEmotionIntensityWeight === "function"
      ? options.visualEmotionIntensityWeight
      : value => clamp(toNumber(value, 1), 0, 1.55);
    const emotionLifecycleWeight = typeof options.emotionLifecycleWeight === "function"
      ? options.emotionLifecycleWeight
      : () => 1;
    const writeSmooth = typeof options.setSmoothParam === "function"
      ? options.setSmoothParam
      : () => undefined;
    const writeMappedSmooth = typeof options.setMappedSmoothParam === "function"
      ? options.setMappedSmoothParam
      : () => undefined;
    const updateRuntime = typeof options.updateRuntime === "function"
      ? options.updateRuntime
      : () => undefined;
    const renderMouth = typeof options.renderMouth === "function"
      ? options.renderMouth
      : () => undefined;
    const applyAnnotatedFaceLayers = typeof options.applyAnnotatedFaceLayers === "function"
      ? options.applyAnnotatedFaceLayers
      : () => undefined;
    const modelArmPoseTarget = typeof options.modelArmPoseTarget === "function"
      ? options.modelArmPoseTarget
      : value => value;
    const state = { narrowEyeExpressionSince: 0 };

    function eyeVisibilityFamily(visualEmotion) {
      if (visualEmotion === "furious") return "coldGlance";
      if (["amused", "laughing", "teasing", "affectionate"].includes(visualEmotion)) return "smile";
      return "default";
    }

    function activeEyeVisibilityCompensation(visualEmotion) {
      const configured = getActiveProfile()?.eyeVisibilityCompensation;
      const profile = configured && typeof configured === "object"
        ? configured
        : DEFAULT_EYE_VISIBILITY_COMPENSATION;
      const families = profile.families && typeof profile.families === "object"
        ? profile.families
        : DEFAULT_EYE_VISIBILITY_COMPENSATION.families;
      const requested = families[eyeVisibilityFamily(visualEmotion)];
      const fallback = families.default || DEFAULT_EYE_VISIBILITY_COMPENSATION.families.default;
      return {
        sustainedDelayMs: clamp(toNumber(profile.sustainedDelayMs, 240), 80, 1200),
        minimumVisibleOpening: clamp(toNumber(profile.minimumVisibleOpening, 0.42), 0.32, 0.62),
        closeThreshold: clamp(toNumber(profile.closeThreshold, 0.32), 0.16, 0.46),
        points: Array.isArray(requested) && requested.length >= 2 ? requested : fallback,
      };
    }

    function sampleEyeVisibilityCurve(points, eyeOpening) {
      const valid = (Array.isArray(points) ? points : [])
        .filter(point => Array.isArray(point) && point.length >= 3)
        .map(point => [
          clamp(toNumber(point[0], 0), 0, 1),
          clamp(toNumber(point[1], 1), 0, 1),
          clamp(toNumber(point[2], 0), -0.35, 0.35),
          clamp(toNumber(point[3], 0), -0.25, 0.1),
        ])
        .sort((left, right) => left[0] - right[0]);
      if (!valid.length) return { gazeGain: 1, offsetY: 0, pupilScale: 0 };
      if (eyeOpening <= valid[0][0]) {
        return { gazeGain: valid[0][1], offsetY: valid[0][2], pupilScale: valid[0][3] };
      }
      for (let index = 1; index < valid.length; index += 1) {
        const left = valid[index - 1];
        const right = valid[index];
        if (eyeOpening > right[0]) continue;
        const amount = smoothstep((eyeOpening - left[0]) / Math.max(0.001, right[0] - left[0]));
        return {
          gazeGain: left[1] + (right[1] - left[1]) * amount,
          offsetY: left[2] + (right[2] - left[2]) * amount,
          pupilScale: left[3] + (right[3] - left[3]) * amount,
        };
      }
      const last = valid[valid.length - 1];
      return { gazeGain: last[1], offsetY: last[2], pupilScale: last[3] };
    }

    function stableExpressionEyeOpening(rawOpening, profile) {
      const opening = clamp(toNumber(rawOpening, 1), 0, 1);
      const closeThreshold = Math.min(
        profile.minimumVisibleOpening - 0.04,
        profile.closeThreshold,
      );
      if (opening <= closeThreshold) return 0;
      return Math.max(profile.minimumVisibleOpening, opening);
    }

    function startledPupilScale({ now, visualEmotion, expression }) {
      if (visualEmotion !== "startled") return 0;
      const age = Math.max(0, now - toNumber(expression?.emotionEnteredAt, now));
      const snapIn = easeOutCubic(clamp(age / 180, 0, 1));
      const visualWeight = visualEmotionIntensityWeight(expression?.currentEmotionWeight);
      const intensity = smoothstep(clamp((visualWeight - 0.5) / 1.25, 0, 1));
      // Param8 already carries the author's surprise-eye deformation. This
      // virtual channel is only a secondary accent; a second full contraction
      // made the iris collapse into a dot at ordinary intensity.
      return -(0.08 + intensity * 0.30) * snapIn * emotionLifecycleWeight(now);
    }

    function renderHair(context = {}) {
      const frame = context.frame || {};
      const motion = context.motion || {};
      const hair = Math.sin(toNumber(frame.tickerTime, 0) * 1.25) * 0.28
        + toNumber(frame.headLook?.x, 0) * 0.14
        + (motion.hair || 0);
      writeSmooth("ParamHairFront", clamp(hair, -1, 1), 0.12);
      writeSmooth("ParamHairSide", clamp(hair * 0.82, -1, 1), 0.12);
      writeSmooth("ParamHairBack", clamp(hair * 0.64, -1, 1), 0.12);
      writeSmooth("Param13", clamp(hair * 0.42, -1, 1), 0.12);
      writeSmooth("Param18", clamp(hair * 0.36, -1, 1), 0.12);
      return hair;
    }

    function render(context = {}) {
      const now = toNumber(context.now, 0);
      const frame = context.frame || {};
      const expression = context.expression || {};
      const visualEmotion = String(context.visualEmotion || getVisualEmotion() || "neutral");
      const emotion = context.emotion || {};
      const motion = context.motion || {};
      const speaking = Boolean(context.speaking);
      const eyeOpen = clamp(toNumber(frame.eyeOpen, 1), 0, 1);
      const annotatedExpressionPlan = context.annotatedExpressionPlan || null;
      const eyeData = context.eyeData || {};
      const eyeVisibility = activeEyeVisibilityCompensation(visualEmotion);
      const stableExpressionEyeLOpen = stableExpressionEyeOpening(
        eyeData.expressionEyeLOpen,
        eyeVisibility,
      );
      const stableExpressionEyeROpen = stableExpressionEyeOpening(
        eyeData.expressionEyeROpen,
        eyeVisibility,
      );
      const persistentEyeOpening = Math.min(stableExpressionEyeLOpen, stableExpressionEyeROpen);
      if (persistentEyeOpening < 0.68) {
        if (!state.narrowEyeExpressionSince) state.narrowEyeExpressionSince = now;
      } else {
        state.narrowEyeExpressionSince = 0;
      }
      const sustainedNarrowEye = state.narrowEyeExpressionSince > 0
        && now - state.narrowEyeExpressionSince >= eyeVisibility.sustainedDelayMs;
      const curve = sampleEyeVisibilityCurve(eyeVisibility.points, persistentEyeOpening);
      // A normal blink crosses the ugly half-open interval too quickly to justify
      // visible Y movement. Sustained expression squints use the model-specific
      // curve; momentary blinks only recenter gaze.
      const blinkGazeGain = 0.12 + eyeOpen * 0.88;
      const gazeGain = sustainedNarrowEye ? curve.gazeGain : blinkGazeGain;
      const eyeBallX = clamp(toNumber(eyeData.rawEyeBallX, 0) * gazeGain, -1, 1);
      const eyeBallY = clamp(
        toNumber(eyeData.rawEyeBallY, 0) * gazeGain + (sustainedNarrowEye ? curve.offsetY : 0),
        -1,
        1,
      );
      const visibilityPupilScale = sustainedNarrowEye ? curve.pupilScale : 0;
      const shockPupilScale = startledPupilScale({ now, visualEmotion, expression });
      // Both channels use negative values for a smaller iris/pupil. Keep the
      // stronger contraction instead of allowing the eye-visibility guard to
      // overwrite the shock response.
      const pupilScale = Math.min(visibilityPupilScale, shockPupilScale);
      updateRuntime({
        pupilScale,
        annotatedPupilScales: annotatedExpressionPlan?.pupilScales || [1, 1],
        authorBlushOpacity: annotatedExpressionPlan?.authorBlushOpacity,
      });

      writeSmooth("ParamAngleX", context.angleX, visualEmotion === "furious" ? 0.28 : 0.2);
      writeSmooth("ParamAngleY", context.angleY, 0.18);
      writeSmooth("ParamAngleZ", context.angleZ, 0.18);
      writeSmooth("Param14", context.param14, visualEmotion === "furious" ? 0.28 : 0.16);
      writeSmooth("Param15", context.param15, 0.16);
      writeSmooth("ParamBodyAngleX", context.bodyX, 0.16);
      writeSmooth("ParamBodyAngleY", context.bodyY, 0.16);
      writeSmooth("ParamBodyAngleZ", context.bodyZ, 0.16);
      writeSmooth("Param17", context.param17, 0.14);
      writeSmooth("ParamEyeBallX", eyeBallX, 0.22);
      writeSmooth("ParamEyeBallY", eyeBallY, 0.22);
      writeMappedSmooth("pupilScale", pupilScale, 0.24);
      writeSmooth("ParamEyeLOpen", clamp(stableExpressionEyeLOpen * eyeOpen, 0, 1), 0.42);
      writeSmooth("ParamEyeROpen", clamp(stableExpressionEyeROpen * eyeOpen, 0, 1), 0.42);
      writeMappedSmooth(
        "eyeSmile",
        clamp((emotion.ParamEyeSmile || 0) + (motion.eyeSmile || 0), 0, 1),
        0.42,
        "ParamEyeLSmile",
      );
      renderMouth({
        sample: context.lipSample,
        visualEmotion,
        emotionMouthForm: emotion.ParamMouthForm,
        emotionMouthOpenY: emotion.ParamMouthOpenY,
        motionMouthSmile: motion.mouthSmile,
        expressionPlan: annotatedExpressionPlan,
      });
      writeMappedSmooth("browLY", clamp(emotion.ParamBrowLY || 0, -1, 1), 0.42, "ParamBrowLY");
      writeMappedSmooth("browRY", clamp(emotion.ParamBrowRY || 0, -1, 1), 0.42, "ParamBrowRY");
      writeMappedSmooth("browLForm", clamp(emotion.ParamBrowLForm || 0, -1, 1), 0.42, "ParamBrowLForm");
      writeMappedSmooth("browRForm", clamp(emotion.ParamBrowRForm || 0, -1, 1), 0.42, "ParamBrowRForm");
      applyAnnotatedFaceLayers(annotatedExpressionPlan);
      writeSmooth("Param16", modelArmPoseTarget((emotion.Param16 ?? 0.22) + (motion.arms || 0), now), 1);
      writeSmooth("Param11", clamp((emotion.Param11 || 0) + toNumber(context.tension, 0) * 0.12, -1, 1), 0.34);
      writeSmooth("Param12", clamp((emotion.Param12 || 0) + toNumber(context.tension, 0) * 0.1, -1, 1), 0.34);
      const blushIntensity = clamp(
        toNumber(expression.currentExpressionDetail?.blushIntensity, emotion.ParamCheek || 0),
        0,
        1,
      );
      const shyCheekPulse = toNumber(context.shyCheekPulse, 0);
      const blushLifecycle = toNumber(context.blushLifecycle, 1);
      const dynamicBlush = blushIntensity > 0
        ? blushIntensity * blushLifecycle
          + shyCheekPulse * Math.max(0.35, blushLifecycle)
          + (speaking ? 0.025 * blushLifecycle : 0)
        : 0;
      writeMappedSmooth(
        "blush",
        clamp(dynamicBlush, 0, 1),
        ["shy", "flustered", "affectionate"].includes(visualEmotion) ? 0.12 : 0.14,
        "ParamCheek",
      );
      writeSmooth("ParamBreath", clamp(toNumber(frame.breath, 0.5) + (motion.breath || 0), 0, 1), 0.16);
      return {
        pupilScale,
        eyeBallX,
        eyeBallY,
        leftEyeOpen: clamp(stableExpressionEyeLOpen * eyeOpen, 0, 1),
        rightEyeOpen: clamp(stableExpressionEyeROpen * eyeOpen, 0, 1),
        dynamicBlush: clamp(dynamicBlush, 0, 1),
        sustainedNarrowEye,
      };
    }

    return Object.freeze({ render, renderHair });
  }

  return Object.freeze({ createVisualParameterController });
}));
