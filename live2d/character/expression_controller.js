// 表情控制器唯一持有当前表情、稳定表情、动作时序和语义恢复状态。
// Live2D/audio/chat 仍由页面及各自控制器拥有；这里通过窄 port 驱动原生表情、动作和上下文刷新。
(function attachExpressionController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusExpressionController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createExpressionControllerApi() {
  "use strict";

  const DEFAULT_DETAIL = Object.freeze({
    blushIntensity: 0,
    eyeContact: 0.65,
    gazeX: 0,
    gazeY: 0,
    tension: 0,
    outburst: false,
  });
  const DEFAULT_OVERLAY_STRENGTH = Object.freeze({ cognitive: 0.66, stance: 0.76 });

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function easeOutCubic(value) {
    return 1 - Math.pow(1 - value, 3);
  }

  function smoothstep(value) {
    const amount = clamp(value, 0, 1);
    return amount * amount * (3 - 2 * amount);
  }

  function createExpressionController(options = {}) {
    const catalog = options.catalog || {};
    const emotionTargets = catalog.targets || {};
    const emotionSemantics = catalog.semantics || {};
    const emotionGestureCycles = catalog.gestureCycles || {};
    const overlayStrength = catalog.overlayStrength || DEFAULT_OVERLAY_STRENGTH;
    const cognitiveBaseDetailWeight = number(catalog.cognitiveBaseDetailWeight, 0.15);
    const clock = options.clock || {};
    const now = typeof clock.now === "function" ? clock.now : () => Date.now();
    const ports = options.ports || {};
    const getDeveloperMode = typeof ports.isDeveloper === "function"
      ? ports.isDeveloper
      : () => false;
    const getDeveloperOverride = typeof ports.getDeveloperOverride === "function"
      ? ports.getDeveloperOverride
      : () => null;
    const getSpeechState = typeof ports.getSpeechState === "function"
      ? ports.getSpeechState
      : () => ({});
    const isSpeaking = typeof ports.isSpeaking === "function"
      ? ports.isSpeaking
      : () => false;
    const isPipelineActive = typeof ports.isPipelineActive === "function"
      ? ports.isPipelineActive
      : () => false;
    const requestContextEmotion = typeof ports.requestContextEmotion === "function"
      ? ports.requestContextEmotion
      : async () => null;
    const getMessages = typeof ports.getMessages === "function" ? ports.getMessages : () => [];
    const getPersistentColdShoulder = typeof ports.getPersistentColdShoulder === "function"
      ? ports.getPersistentColdShoulder
      : () => false;
    const getPointer = typeof ports.getPointer === "function" ? ports.getPointer : () => ({x: 0, y: 0});
    const getHeadTurnProfile = typeof ports.getHeadTurnProfile === "function"
      ? ports.getHeadTurnProfile
      : () => ({pointerSwitchThreshold: 0.3, springFrequency: 4.8, springDamping: 0.88});
    const normalizeGesture = typeof ports.normalizeGesture === "function"
      ? ports.normalizeGesture
      : value => String(value || "idle");
    const visualIntensity = typeof ports.visualIntensity === "function"
      ? ports.visualIntensity
      : value => clamp(number(value, 0.7), 0.15, 1.55);

    let currentEmotion = "neutral";
    let stableEmotion = "neutral";
    let emotionRecoveryTarget = "neutral";
    let cognitiveOverlayBaseEmotion = "neutral";
    let cognitiveOverlayBaseDetail = null;
    let emotionEnteredAt = now();
    let currentEmotionHoldMs = 9000;
    let currentEmotionFadeMs = 3200;
    let emotionSpeechProtectedUntil = 0;
    let activeGesture = "";
    let gestureUntil = 0;
    let gestureEnteredAt = now();
    let lookAwayDirection = 1;
    let furiousTurnDirection = 0;
    let furiousTurnTarget = 1;
    let furiousTurnVelocity = 0;
    let currentEmotionWeight = 1;
    let currentExpressionDetail = { ...DEFAULT_DETAIL };
    let nextContextEmotionAt = now() + 18000;
    let contextEmotionRequestInFlight = false;
    const emotionGestureSteps = new Map();

    function emotionSemanticsFor(emotion) {
      return emotionSemantics[emotion] || emotionSemantics.neutral || { role: "stable", family: "neutral", recovery: "neutral" };
    }

    function expressionDefaultsFor(emotion) {
      const defaults = typeof catalog.defaults === "function"
        ? catalog.defaults(emotion)
        : catalog.defaults?.[emotion];
      return defaults || catalog.defaults?.neutral || {
        emotionIntensity: 0.82,
        blushIntensity: 0,
        eyeContact: 0.68,
        gazeX: 0,
        gazeY: 0,
        tension: 0,
      };
    }

    function emotionTimingFor(emotion, detail = {}) {
      const base = typeof catalog.timings === "function"
        ? catalog.timings(emotion, {})
        : catalog.timings?.[emotion];
      const timing = base || catalog.timings?.neutral || {holdMs: 0, fadeMs: 1200};
      const requestedHoldMs = clamp(number(detail.holdMs, timing.holdMs), 0, 120000);
      const requestedFadeMs = clamp(number(detail.fadeMs, timing.fadeMs), 500, 30000);
      if (emotionSemanticsFor(emotion).role === "transient") {
        return {
          holdMs: Math.min(requestedHoldMs, timing.holdMs),
          fadeMs: Math.min(requestedFadeMs, timing.fadeMs),
        };
      }
      return {holdMs: requestedHoldMs, fadeMs: requestedFadeMs};
    }

    function expressionHoldUntil() {
      return Math.max(emotionEnteredAt + currentEmotionHoldMs, emotionSpeechProtectedUntil);
    }

    function effectiveVisualEmotion() {
      return emotionSemanticsFor(currentEmotion).role === "cognitive"
        ? cognitiveOverlayBaseEmotion
        : currentEmotion;
    }

    function recoveryEmotionFor(emotion = currentEmotion) {
      const semantics = emotionSemanticsFor(emotion);
      if (semantics.role === "boundary") return getPersistentColdShoulder() ? "furious" : "annoyed";
      if (["cognitive", "stance", "transient"].includes(semantics.role)) {
        return emotionRecoveryTarget || stableEmotion || "neutral";
      }
      return semantics.recovery || "neutral";
    }

    function planEmotionTransition(nextEmotion, previousEmotion) {
      const semantics = emotionSemanticsFor(nextEmotion);
      const previousSemantics = emotionSemanticsFor(previousEmotion);
      let recovery = semantics.recovery || stableEmotion || "neutral";
      if (semantics.role === "stable") {
        stableEmotion = nextEmotion;
        recovery = semantics.recovery || "neutral";
      } else if (semantics.role === "cognitive" || semantics.role === "stance") {
        recovery = stableEmotion || "neutral";
      } else if (nextEmotion === "startled") {
        recovery = stableEmotion || "neutral";
      } else if (nextEmotion === "excited") {
        recovery = emotionSemanticsFor(stableEmotion).family === "positive" ? stableEmotion : "amused";
      } else if (nextEmotion === "furious") {
        recovery = "annoyed";
      }
      emotionRecoveryTarget = recovery === nextEmotion ? "neutral" : recovery;
      return {
        semantics,
        sameFamily: previousSemantics.family === semantics.family,
        previousRole: previousSemantics.role,
      };
    }

    function gestureDuration(gesture) {
      if (gesture === "glitch") return 900;
      if (gesture === "nod" || gesture === "recoil") return 1100;
      if (gesture === "tilt") return 1600;
      if (gesture === "look_away") return 1900;
      if (gesture === "lean") return 1400;
      if (gesture === "soften") return 1800;
      if (gesture === "smile") return 1800;
      if (gesture === "laugh") return 2600;
      if (gesture === "explain") return 2200;
      if (gesture === "perk") return 1300;
      return 760;
    }

    function setEmotion(emotion = "neutral", gesture = "idle", detail = {}) {
      if (getDeveloperMode() && getDeveloperOverride() && !detail.developerOverride) {
        ports.updateDeveloperEmotionPanel?.();
        return false;
      }
      if (getDeveloperMode() && ports.clearDeveloperManualBlush && ports.getDeveloperManualBlush?.() !== null) {
        ports.clearDeveloperManualBlush();
      }
      const nextEmotion = Object.prototype.hasOwnProperty.call(emotionTargets, emotion) ? emotion : "neutral";
      const currentTime = now();
      const previousEmotion = currentEmotion;
      const transition = planEmotionTransition(nextEmotion, previousEmotion);
      const previousDetail = { ...currentExpressionDetail };
      const previousWeight = currentEmotionWeight;
      const accumulate = Boolean(detail.accumulateExpression);
      const composeCognitiveOverlay = transition.semantics.role === "cognitive";
      if (composeCognitiveOverlay && transition.previousRole !== "cognitive") {
        cognitiveOverlayBaseEmotion = previousEmotion;
        cognitiveOverlayBaseDetail = { ...previousDetail };
      }
      const cognitiveBaseDetail = cognitiveOverlayBaseDetail || previousDetail;
      const canReuseFamily = transition.sameFamily
        && transition.semantics.role !== "transient"
        && transition.previousRole !== "transient"
        && nextEmotion !== "furious";
      const shouldRestartLifecycle = nextEmotion !== currentEmotion || detail.restart !== false;
      const shouldRestartNativeExpression = detail.restart === true || (
        detail.restart !== false && (nextEmotion === currentEmotion || !canReuseFamily)
      );
      if (shouldRestartLifecycle) {
        emotionEnteredAt = currentTime;
        emotionSpeechProtectedUntil = 0;
      }
      if (!composeCognitiveOverlay && shouldRestartLifecycle && (
        nextEmotion !== (ports.getAnnotatedExpressionStyleEmotion?.() || "neutral")
        || detail.restart !== false
      )) {
        ports.setAnnotatedExpressionStyleEmotion?.(nextEmotion, detail);
      }
      if (shouldRestartNativeExpression) {
        ports.applyNativeExpression?.(composeCognitiveOverlay ? "neutral" : nextEmotion);
      }
      currentEmotion = nextEmotion;
      const timing = emotionTimingFor(nextEmotion, detail);
      currentEmotionHoldMs = timing.holdMs;
      currentEmotionFadeMs = timing.fadeMs;
      const defaults = expressionDefaultsFor(nextEmotion);
      const emotionDelta = clamp(number(detail.emotionDelta, 0), -0.45, 0.45);
      let requestedWeight = number(detail.emotionIntensity, defaults.emotionIntensity)
        + emotionDelta
        + (accumulate ? Math.max(0, previousWeight - 0.72) * 0.12 : 0);
      if (detail.smoothEmotionTransition) requestedWeight = clamp(requestedWeight, previousWeight - 0.16, previousWeight + 0.16);
      currentEmotionWeight = clamp(requestedWeight, 0.15, 2.5);
      const requestedBlush = number(detail.blushIntensity, defaults.blushIntensity);
      const requestedEyeContact = number(detail.eyeContact, defaults.eyeContact);
      const requestedGazeX = number(detail.gazeX, defaults.gazeX);
      const requestedGazeY = number(detail.gazeY, defaults.gazeY);
      const requestedTension = number(detail.tension, defaults.tension);
      currentExpressionDetail = {
        blushIntensity: clamp(
          composeCognitiveOverlay
            ? Math.max(requestedBlush, cognitiveBaseDetail.blushIntensity * cognitiveBaseDetailWeight)
            : requestedBlush + (accumulate ? previousDetail.blushIntensity * 0.16 : 0),
          0,
          1,
        ),
        eyeContact: clamp(
          composeCognitiveOverlay
            ? requestedEyeContact * (1 - cognitiveBaseDetailWeight)
              + cognitiveBaseDetail.eyeContact * cognitiveBaseDetailWeight
            : accumulate ? Math.min(requestedEyeContact, previousDetail.eyeContact + 0.08) : requestedEyeContact,
          0,
          1,
        ),
        gazeX: clamp(composeCognitiveOverlay ? requestedGazeX + cognitiveBaseDetail.gazeX * cognitiveBaseDetailWeight : requestedGazeX, -0.6, 0.6),
        gazeY: clamp(composeCognitiveOverlay ? requestedGazeY + cognitiveBaseDetail.gazeY * cognitiveBaseDetailWeight : requestedGazeY, -0.6, 0.6),
        tension: clamp(
          composeCognitiveOverlay
            ? Math.max(requestedTension, cognitiveBaseDetail.tension * cognitiveBaseDetailWeight)
            : requestedTension,
          0,
          1,
        ),
        outburst: Boolean(detail.outburst || detail.deliveryMode === "outburst"),
      };
      const nextGesture = gesture || "";
      const profile = getHeadTurnProfile() || {};
      const pointer = getPointer() || {};
      if (nextEmotion === "furious") {
        if (pointer.x > number(profile.pointerSwitchThreshold, 0.3)) furiousTurnTarget = -1;
        else if (pointer.x < -number(profile.pointerSwitchThreshold, 0.3)) furiousTurnTarget = 1;
        else furiousTurnTarget = lookAwayDirection || 1;
        if (previousEmotion !== "furious") {
          furiousTurnDirection = 0;
          furiousTurnVelocity = 0;
        }
      } else if (nextGesture === "look_away" && (activeGesture !== nextGesture || detail.restart !== false)) {
        lookAwayDirection *= -1;
      }
      activeGesture = nextGesture;
      gestureEnteredAt = currentTime;
      gestureUntil = currentTime + gestureDuration(activeGesture);
      nextContextEmotionAt = currentTime + Math.max(9000, currentEmotionHoldMs + currentEmotionFadeMs + 2400);
      ports.updateModelStateLabel?.();
      if (nextGesture === "glitch") ports.pulseInteraction?.("");
      ports.applyNativeMotion?.(currentEmotion, activeGesture);
      ports.updateDeveloperEmotionPanel?.();
      if (activeGesture === "glitch") ports.setGlitch?.();
      return true;
    }

    function protectExpressionThroughSpeech(speechEndsAt, postSpeechHoldMs = 8000) {
      if (currentEmotion === "neutral") return;
      const currentTime = now();
      const protectedSpeechEnd = Number.isFinite(speechEndsAt) && speechEndsAt > 0
        ? Math.max(currentTime, speechEndsAt)
        : currentTime;
      emotionSpeechProtectedUntil = Math.max(
        emotionSpeechProtectedUntil,
        protectedSpeechEnd + Math.max(0, postSpeechHoldMs),
      );
    }

    function emotionLifecycleWeight(currentTime = now()) {
      if (getDeveloperMode() && getDeveloperOverride()) return 1;
      if (currentEmotion === "neutral") return 1;
      const age = Math.max(0, currentTime - emotionEnteredAt);
      const enter = easeOutCubic(clamp(age / 520, 0, 1));
      const speech = getSpeechState() || {};
      if (speech.pending && speech.moodApplied) return enter;
      const holdUntil = expressionHoldUntil();
      if (currentTime <= holdUntil) return enter;
      const fade = 1 - smoothstep((currentTime - holdUntil) / Math.max(1, currentEmotionFadeMs));
      return clamp(enter * fade, 0, 1);
    }

    function blushLifecycleWeight(currentTime = now()) {
      const lifecycle = emotionLifecycleWeight(currentTime);
      if (!["shy", "flustered", "affectionate"].includes(currentEmotion)) return lifecycle;
      const age = Math.max(0, currentTime - emotionEnteredAt);
      const enter = smoothstep(age / 900);
      const holdUntil = expressionHoldUntil();
      if (currentTime <= holdUntil) return clamp(enter, 0, 1);
      const fade = 1 - smoothstep((currentTime - holdUntil) / Math.max(900, currentEmotionFadeMs));
      return clamp(enter * fade, 0, 1);
    }

    function emotionIntensity(emotion, currentTime = now()) {
      const directMatch = currentEmotion === emotion;
      const cognitiveBaseMatch = emotionSemanticsFor(currentEmotion).role === "cognitive"
        && cognitiveOverlayBaseEmotion === emotion;
      if (!directMatch && !cognitiveBaseMatch) return 0;
      const cognitiveBaseWeight = cognitiveBaseMatch && !directMatch ? 0.88 : 1;
      return emotionLifecycleWeight(currentTime)
        * cognitiveBaseWeight
        * visualIntensity(currentEmotionWeight);
    }

    function maybeRefreshContextEmotion(currentTime = now()) {
      if (getPersistentColdShoulder()) {
        nextContextEmotionAt = currentTime + 30000;
        return;
      }
      if (contextEmotionRequestInFlight || currentTime < nextContextEmotionAt) return;
      const speech = getSpeechState() || {};
      if (speech.pending || isSpeaking(currentTime)) {
        nextContextEmotionAt = currentTime + 3000;
        return;
      }
      const messages = getMessages();
      if (!Array.isArray(messages) || !messages.length) {
        nextContextEmotionAt = currentTime + 30000;
        return;
      }
      contextEmotionRequestInFlight = true;
      nextContextEmotionAt = currentTime + 45000;
      Promise.resolve(requestContextEmotion({
        currentEmotion,
        messages: messages.slice(-8),
      }))
        .then(data => {
          if (!data || data.ok === false) throw new Error(data?.error || "emotion context unavailable");
          const emotion = Object.prototype.hasOwnProperty.call(emotionTargets, data.emotion)
            ? data.emotion
            : "neutral";
          const gesture = normalizeGesture(data.gesture);
          if (emotion === currentEmotion) {
            nextContextEmotionAt = now() + (emotion === "neutral" ? 45000 : 12000);
            return;
          }
          const defaults = expressionDefaultsFor(emotion);
          setEmotion(emotion, gesture, {
            emotionIntensity: clamp(number(data.emotionIntensity, emotion === "neutral" ? 0.72 : 0.9), 0.15, 1.25),
            blushIntensity: clamp(number(data.blushIntensity, defaults.blushIntensity), 0, 1),
            eyeContact: clamp(number(data.eyeContact, defaults.eyeContact), 0, 1),
            gazeX: clamp(number(data.gazeX, defaults.gazeX), -0.6, 0.6),
            gazeY: clamp(number(data.gazeY, defaults.gazeY), -0.6, 0.6),
            tension: clamp(number(data.tension, defaults.tension), 0, 1),
            holdMs: number(data.holdMs, emotion === "neutral" ? 0 : 45000),
            fadeMs: number(data.fadeMs, emotion === "neutral" ? 1200 : 10000),
          });
        })
        .catch(error => {
          ports.warn?.("context emotion refresh failed:", error);
          nextContextEmotionAt = now() + 60000;
        })
        .finally(() => {
          contextEmotionRequestInFlight = false;
        });
    }

    function updateLifecycle(currentTime = now()) {
      if (getDeveloperMode() && getDeveloperOverride()) return;
      if (getPersistentColdShoulder() && currentEmotion === "furious") return;
      maybeRefreshContextEmotion(currentTime);
      const speech = getSpeechState() || {};
      if (currentEmotion === "neutral" || isPipelineActive() || speech.pipelineActive || speech.pending || isSpeaking(currentTime)) return;
      if (currentTime <= expressionHoldUntil() + currentEmotionFadeMs + 420) return;
      setEmotion(recoveryEmotionFor(currentEmotion), "idle", {
        emotionIntensity: 0.78,
        blushIntensity: 0,
        holdMs: 0,
        fadeMs: 900,
      });
    }

    function updateFuriousTurnMotion(delta = 1) {
      const profile = getHeadTurnProfile() || {};
      const pointer = getPointer() || {};
      if (effectiveVisualEmotion() === "furious") {
        if (pointer.x > number(profile.pointerSwitchThreshold, 0.3)) furiousTurnTarget = -1;
        else if (pointer.x < -number(profile.pointerSwitchThreshold, 0.3)) furiousTurnTarget = 1;
      } else {
        furiousTurnTarget = 0;
      }
      const seconds = clamp(number(delta, 1) / 60, 0.001, 0.05);
      const omega = number(profile.springFrequency, 4.8);
      const damping = number(profile.springDamping, 0.88);
      const acceleration = (furiousTurnTarget - furiousTurnDirection) * omega * omega
        - 2 * damping * omega * furiousTurnVelocity;
      furiousTurnVelocity += acceleration * seconds;
      furiousTurnDirection = clamp(furiousTurnDirection + furiousTurnVelocity * seconds, -1.05, 1.05);
      if (Math.abs(furiousTurnTarget - furiousTurnDirection) < 0.001 && Math.abs(furiousTurnVelocity) < 0.001) {
        furiousTurnDirection = furiousTurnTarget;
        furiousTurnVelocity = 0;
      }
      return {
        ...profile,
        direction: furiousTurnDirection,
        target: furiousTurnTarget,
      };
    }

    function nextGestureForEmotion(emotion, requested) {
      const normalized = requested || "idle";
      if (normalized !== "idle") return normalized;
      const cycle = emotionGestureCycles[emotion];
      if (!cycle?.length) return normalized;
      const step = emotionGestureSteps.get(emotion) || 0;
      emotionGestureSteps.set(emotion, step + 1);
      return cycle[step % cycle.length];
    }

    function scheduleContextRefresh(delayMs = 1200) {
      nextContextEmotionAt = now() + Math.max(0, number(delayMs, 1200));
    }

    function stateSnapshot() {
      return {
        currentEmotion,
        stableEmotion,
        emotionRecoveryTarget,
        cognitiveOverlayBaseEmotion,
        cognitiveOverlayBaseDetail: cognitiveOverlayBaseDetail ? { ...cognitiveOverlayBaseDetail } : null,
        emotionEnteredAt,
        currentEmotionHoldMs,
        currentEmotionFadeMs,
        emotionSpeechProtectedUntil,
        activeGesture,
        gestureUntil,
        gestureEnteredAt,
        lookAwayDirection,
        furiousTurnDirection,
        furiousTurnTarget,
        furiousTurnVelocity,
        currentEmotionWeight,
        currentExpressionDetail: { ...currentExpressionDetail },
        nextContextEmotionAt,
        contextEmotionRequestInFlight,
      };
    }

    return Object.freeze({
      expressionDefaultsFor,
      emotionIntensity,
      emotionLifecycleWeight,
      blushLifecycleWeight,
      effectiveVisualEmotion,
      emotionSemanticsFor,
      getState: stateSnapshot,
      nextGestureForEmotion,
      protectExpressionThroughSpeech,
      recoveryEmotionFor,
      refreshContextEmotion: maybeRefreshContextEmotion,
      scheduleContextRefresh,
      setEmotion,
      updateFuriousTurnMotion,
      updateLifecycle,
    });
  }

  return Object.freeze({ createExpressionController });
}));
