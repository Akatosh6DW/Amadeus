// 触摸控制器独占命中配置、冷却/合并计时、请求取消代次与延后回复。
// 页面提供角色场景、会话和反馈接口；消息/语音状态继续由原页面服务持有。
(function attachTouchInteraction(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusTouchInteraction = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createTouchInteractionApi() {
  "use strict";

  const TOUCH_REGION_CONFIG_URL = "/configs/live2d_touch_regions.json";
  const TOUCH_ZONE_LABELS = Object.freeze({
    head: "头部 / 摸头",
    face: "脸部 / 轻触脸颊",
    mouth: "嘴部 / 亲吻判定",
    hand: "手部 / 牵手",
    chest: "胸部 / 越界触摸",
    body: "普通躯干（不发送事件）",
  });
  const DEFAULT_TOUCH_REGION_CONFIG = {
    touchBounds: [0, 0, 1, 1],
    priority: ["mouth", "face", "head", "hand", "chest"],
    zones: {
      mouth: [[[0.43, 0.35], [0.57, 0.35], [0.585, 0.39], [0.54, 0.415], [0.47, 0.41], [0.425, 0.385]]],
      face: [[[0.35, 0.22], [0.605, 0.22], [0.645, 0.29], [0.62, 0.38], [0.55, 0.445], [0.46, 0.445], [0.385, 0.395], [0.345, 0.31]]],
      head: [[[0.265, 0.01], [0.645, 0.01], [0.71, 0.085], [0.735, 0.2], [0.69, 0.39], [0.625, 0.515], [0.32, 0.53], [0.25, 0.4], [0.225, 0.17]]],
      hand: [
        [[0.105, 0.755], [0.255, 0.75], [0.335, 0.825], [0.31, 0.91], [0.215, 0.945], [0.09, 0.88]],
        [[0.63, 0.755], [0.82, 0.76], [0.9, 0.82], [0.91, 0.91], [0.79, 0.945], [0.655, 0.91], [0.6, 0.83]],
      ],
      chest: [[[0.315, 0.43], [0.65, 0.43], [0.735, 0.55], [0.75, 0.72], [0.64, 0.79], [0.27, 0.79], [0.18, 0.67], [0.2, 0.53]]],
    },
  };

  const NON_CHEST_TOUCH_REPLY_TARGET = 10;
  const TOUCH_BURST_THRESHOLD = 3;
  const TOUCH_IGNORE_THRESHOLD = 7;
  const TOUCH_SINGLE_SETTLE_MS = 360;
  const TOUCH_BURST_SETTLE_MS = 950;
  const TOUCH_BURST_REPLY_COOLDOWN_MS = 8000;
  const NON_CHEST_TOUCH_TIME_CREDIT_MS = 30000;

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function toNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function createTouchInteractionController(options = {}) {
    const {
      clock = {},
      config = {},
      scene = {},
      session = {},
      transport = {},
      feedback = {},
    } = options;
    const defaultNow = () => (
      typeof performance !== "undefined" && typeof performance.now === "function"
        ? performance.now()
        : Date.now()
    );
    const now = typeof clock.now === "function" ? clock.now : defaultNow;
    const setTimer = typeof clock.setTimeout === "function"
      ? clock.setTimeout
      : (callback, delay) => setTimeout(callback, delay);
    const clearTimer = typeof clock.clearTimeout === "function"
      ? clock.clearTimeout
      : timer => clearTimeout(timer);
    const createAbortController = typeof clock.createAbortController === "function"
      ? clock.createAbortController
      : () => new AbortController();
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("touch request transport is unavailable");
          return fetch(url, init);
        };
    const configRequest = typeof config.request === "function"
      ? config.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("touch config transport is unavailable");
          return fetch(url, init);
        };
    const warn = typeof feedback.warn === "function"
      ? feedback.warn
      : (...args) => console.warn(...args);
    const interactionUrl = String(transport.interactionUrl || "/api/interaction");
    const configUrl = String(config.url || TOUCH_REGION_CONFIG_URL);

    let activeProfileId = "";
    let touchRegionConfig = DEFAULT_TOUCH_REGION_CONFIG;
    let touchRequestAbortController = null;
    let touchRequestGeneration = 0;
    let touchDebounceTimer = 0;
    let lastDeveloperTouchAt = 0;
    let deferredTouchAssistant = null;
    const touchState = {
      recentTouches: [],
      recentReplies: [],
      sequence: [],
      zoneCounts: {},
      sessionCount: 0,
      consecutiveZone: "",
      consecutiveCount: 0,
      elapsedSinceSameZoneMs: 3600000,
      requestInFlight: false,
      pendingZone: "",
      pendingAfterChat: false,
      lastTouchAt: 0,
      nonChestTouchesSinceReply: NON_CHEST_TOUCH_REPLY_TARGET - 1,
      lastNonChestReplyRequestedAt: 0,
      replyCooldownUntil: 0,
    };

    function isDeveloper() {
      return Boolean(typeof scene.isDeveloper === "function" && scene.isDeveloper());
    }

    function activeModelId() {
      const modelId = typeof scene.getModelId === "function" ? scene.getModelId() : "";
      return String(modelId || activeProfileId || "");
    }

    function activeTouchRegionProfile() {
      const profiles = touchRegionConfig?.profiles;
      if (!profiles || typeof profiles !== "object") return touchRegionConfig || DEFAULT_TOUCH_REGION_CONFIG;
      return profiles[activeModelId()]
        || profiles[touchRegionConfig.defaultProfile]
        || DEFAULT_TOUCH_REGION_CONFIG;
    }

    function normalizedTouchBounds(profile = activeTouchRegionProfile()) {
      const values = Array.isArray(profile?.touchBounds)
        ? profile.touchBounds.map(value => toNumber(value, 0))
        : [0, 0, 1, 1];
      const [x, y, width, height] = values.length === 4 ? values : [0, 0, 1, 1];
      return {
        x: clamp(x, 0, 1),
        y: clamp(y, 0, 1),
        width: clamp(width, 0.001, 1),
        height: clamp(height, 0.001, 1),
      };
    }

    function pointInPolygon(x, y, polygon) {
      let inside = false;
      for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const [xi, yi] = polygon[i];
        const [xj, yj] = polygon[j];
        const crosses = (yi > y) !== (yj > y)
          && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-9) + xi;
        if (crosses) inside = !inside;
      }
      return inside;
    }

    function modelTouchInfo(event) {
      const globalPoint = event?.global || event?.data?.global;
      const currentModel = typeof scene.getModel === "function" ? scene.getModel() : null;
      if (!currentModel || !globalPoint) return { zone: "body", x: 0, y: 0 };
      let bounds;
      try {
        bounds = currentModel.getBounds();
      } catch {
        bounds = null;
      }
      if (!bounds || bounds.width <= 0 || bounds.height <= 0) {
        return { zone: "body", x: 0, y: 0 };
      }
      const rawX = (globalPoint.x - bounds.x) / bounds.width;
      const rawY = (globalPoint.y - bounds.y) / bounds.height;
      const profile = activeTouchRegionProfile();
      const touchBounds = normalizedTouchBounds(profile);
      const x = (rawX - touchBounds.x) / touchBounds.width;
      const y = (rawY - touchBounds.y) / touchBounds.height;
      let zone = "body";
      if (x >= 0 && x <= 1 && y >= 0 && y <= 1) {
        for (const candidate of profile.priority || []) {
          const polygons = profile.zones?.[candidate] || [];
          if (polygons.some(polygon => pointInPolygon(x, y, polygon))) {
            zone = candidate;
            break;
          }
        }
      }
      return {
        zone,
        x,
        y,
        rawX,
        rawY,
        modelId: activeModelId(),
        screenX: globalPoint.x,
        screenY: globalPoint.y,
      };
    }

    function renderRegions() {
      feedback.renderRegions?.(activeTouchRegionProfile(), TOUCH_ZONE_LABELS);
    }

    async function loadConfig() {
      try {
        const response = await configRequest(configUrl, { cache: "no-store" });
        const value = await response.json();
        const hasLegacyProfile = value?.zones && Array.isArray(value.priority);
        const hasModelProfiles = value?.profiles && typeof value.profiles === "object";
        if (!response.ok || (!hasLegacyProfile && !hasModelProfiles)) {
          throw new Error("invalid touch region config");
        }
        touchRegionConfig = value;
      } catch (error) {
        warn("touch region calibration unavailable; using embedded fallback", error);
        touchRegionConfig = DEFAULT_TOUCH_REGION_CONFIG;
      }
      renderRegions();
      return activeTouchRegionProfile();
    }

    function setModelProfile(profileOrId) {
      activeProfileId = typeof profileOrId === "string"
        ? profileOrId
        : String(profileOrId?.id || "");
      renderRegions();
      return activeTouchRegionProfile();
    }

    function sceneEmotionSnapshot() {
      const snapshot = typeof scene.getEmotion === "function" ? scene.getEmotion() : {};
      return {
        emotion: String(snapshot?.emotion || "neutral"),
        weight: toNumber(snapshot?.weight, 1),
        holdMs: toNumber(snapshot?.holdMs, 0),
        fadeMs: toNumber(snapshot?.fadeMs, 0),
      };
    }

    function applyImmediateTouchFeedback(zone) {
      if (scene.preserveColdShoulderPose?.()) return;
      const relationshipTone = String(scene.getRelationshipTone?.() || "");
      const allowedKiss = ["committed_partner", "life_partner", "old_married"].includes(relationshipTone);
      const lowAffection = relationshipTone === "close_companion";
      const headVariants = [
        { emotion: "amused", gesture: "smile", delta: 0.1 },
        { emotion: "affectionate", gesture: "soften", delta: 0.11 },
        { emotion: "amused", gesture: "smile", delta: 0.1 },
        { emotion: "thinking", gesture: "explain", delta: 0.07 },
        { emotion: "amused", gesture: "smile", delta: 0.1 },
        { emotion: "affectionate", gesture: "soften", delta: 0.11 },
      ];
      const feedbackData = {
        head: headVariants[Math.max(0, (touchState.zoneCounts.head || 1) - 1) % headVariants.length],
        face: {
          emotion: "shy",
          gesture: "lean",
          delta: 0.02,
          emotionIntensity: 0.84,
          blushIntensity: 0.3,
          tension: 0.22,
          accumulateExpression: false,
          smoothEmotionTransition: false,
          restart: true,
        },
        mouth: {
          emotion: allowedKiss ? "affectionate" : "shy",
          gesture: allowedKiss ? "lean" : "recoil",
          delta: 0.15,
        },
        chest: lowAffection
          ? { emotion: "annoyed", gesture: "recoil", delta: 0.26, blushIntensity: 0.72, tension: 0.78 }
          : { emotion: "flustered", gesture: "recoil", delta: 0.24, blushIntensity: 0.62, tension: 0.58 },
        hand: { emotion: "affectionate", gesture: "soften", delta: 0.12 },
      }[zone];
      if (!feedbackData) return;
      const emotion = sceneEmotionSnapshot();
      scene.applyImmediateTouchFeedback?.({
        ...feedbackData,
        emotionIntensity: feedbackData.emotionIntensity ?? emotion.weight,
        holdMs: Math.max(30000, emotion.holdMs),
        fadeMs: Math.max(9000, emotion.fadeMs),
      }, zone);
    }

    function shouldRequestTouchReply(zone, time = now()) {
      if (zone === "chest") return true;
      touchState.nonChestTouchesSinceReply += 1;
      const elapsedMs = touchState.lastNonChestReplyRequestedAt > 0
        ? Math.max(0, time - touchState.lastNonChestReplyRequestedAt)
        : 0;
      const timeCredit = Math.min(
        NON_CHEST_TOUCH_REPLY_TARGET - 1,
        elapsedMs / NON_CHEST_TOUCH_TIME_CREDIT_MS,
      );
      if (touchState.nonChestTouchesSinceReply + timeCredit < NON_CHEST_TOUCH_REPLY_TARGET) return false;
      touchState.nonChestTouchesSinceReply = 0;
      touchState.lastNonChestReplyRequestedAt = time;
      return true;
    }

    function pipelineHasPriority(time = now()) {
      return Boolean(session.isPipelineBusy?.(time));
    }

    function touchSpeechIsActive(time = now()) {
      return Boolean(session.isTouchSpeechActive?.(time));
    }

    function boundaryFor(burstCount) {
      return burstCount >= TOUCH_IGNORE_THRESHOLD
        ? "ignore"
        : burstCount >= TOUCH_BURST_THRESHOLD ? "interrupt" : "normal";
    }

    function applyBodyTap() {
      const emotion = sceneEmotionSnapshot();
      scene.applyBodyTap?.({
        emotion: emotion.emotion,
        gesture: "tap",
        emotionIntensity: emotion.weight,
        restart: false,
        smoothEmotionTransition: true,
        holdMs: Math.max(8000, emotion.holdMs),
      });
    }

    function handleModelTouch(event) {
      const hit = modelTouchInfo(event);
      const zone = hit.zone;
      const time = now();
      if (isDeveloper()) {
        lastDeveloperTouchAt = time;
        feedback.renderHit?.(hit, time, TOUCH_ZONE_LABELS);
      }
      if (scene.preserveColdShoulderPose?.()) return;
      if (zone === "body") {
        applyBodyTap();
        return;
      }
      session.noteTouchPresence?.();
      if (touchState.replyCooldownUntil > time) {
        applyImmediateTouchFeedback(zone);
        return;
      }

      if (time - touchState.lastTouchAt > 12000) touchState.sequence = [];
      touchState.recentTouches = touchState.recentTouches.filter(item => time - item.time < 180000);
      touchState.recentReplies = touchState.recentReplies.filter(item => time - item.time < 600000);
      const previousSameZone = [...touchState.recentTouches].reverse().find(item => item.zone === zone);
      touchState.elapsedSinceSameZoneMs = previousSameZone
        ? Math.round(time - previousSameZone.time)
        : 3600000;
      if (touchState.consecutiveZone === zone && time - touchState.lastTouchAt <= 15000) {
        touchState.consecutiveCount += 1;
      } else {
        touchState.consecutiveZone = zone;
        touchState.consecutiveCount = 1;
      }
      touchState.lastTouchAt = time;
      touchState.recentTouches.push({ zone, time });
      touchState.sequence.push(zone);
      touchState.sequence = touchState.sequence.slice(-20);
      touchState.zoneCounts[zone] = (touchState.zoneCounts[zone] || 0) + 1;
      touchState.sessionCount += 1;
      touchState.pendingZone = zone;
      applyImmediateTouchFeedback(zone);

      const aggregateAlreadyPending = Boolean(touchDebounceTimer) || touchState.requestInFlight;
      if (!aggregateAlreadyPending && !shouldRequestTouchReply(zone, time)) return;

      const burstCount = touchState.recentTouches.filter(item => time - item.time < 4500).length;
      const boundaryLevel = boundaryFor(burstCount);
      if (pipelineHasPriority(time)) {
        touchState.pendingAfterChat = true;
        return;
      }
      if (touchState.requestInFlight) {
        scheduleTouchInteraction(boundaryLevel === "normal" ? TOUCH_SINGLE_SETTLE_MS : TOUCH_BURST_SETTLE_MS);
        return;
      }
      scheduleTouchInteraction(boundaryLevel === "normal" ? TOUCH_SINGLE_SETTLE_MS : TOUCH_BURST_SETTLE_MS);
    }

    function scheduleTouchInteraction(delayMs) {
      if (touchDebounceTimer) clearTimer(touchDebounceTimer);
      touchDebounceTimer = setTimer(() => {
        touchDebounceTimer = 0;
        void requestTouchInteraction();
      }, delayMs);
      if (touchRequestAbortController) {
        touchRequestAbortController.abort();
        touchRequestAbortController = null;
        touchState.requestInFlight = false;
        touchRequestGeneration += 1;
      }
    }

    function conversationIdentity() {
      const identity = session.getConversationIdentity?.() || {};
      return {
        conversationId: String(identity.conversationId || ""),
        contextEpoch: Number(identity.contextEpoch) || 0,
      };
    }

    function requestId() {
      const supplied = session.createRequestId?.();
      if (supplied) return `touch_${String(supplied)}`;
      return `touch_${Date.now()}_${Math.random().toString(36).slice(2, 12)}`;
    }

    async function requestTouchInteraction() {
      if (scene.preserveColdShoulderPose?.()) {
        cancelForColdShoulder();
        return;
      }
      const latestZone = touchState.pendingZone;
      if (!latestZone || latestZone === "body") return;
      const time = now();
      if (pipelineHasPriority(time)) {
        touchState.pendingAfterChat = true;
        return;
      }
      touchState.pendingAfterChat = false;
      const burstTouches = touchState.recentTouches.filter(item => time - item.time < 4500);
      const burstZoneCounts = burstTouches.reduce((counts, item) => {
        counts[item.zone] = (counts[item.zone] || 0) + 1;
        return counts;
      }, {});
      const zone = (burstZoneCounts.chest || 0) > 0 ? "chest" : latestZone;
      const burstCount = burstTouches.length;
      const burstSequence = burstTouches.map(item => item.zone).slice(-20);
      const shortWindowCount = touchState.recentTouches.filter(item => item.zone === zone && time - item.time < 30000).length;
      const mediumWindowCount = touchState.recentTouches.filter(item => item.zone === zone && time - item.time < 180000).length;
      const boundaryLevel = boundaryFor(burstCount);
      const snapshotLastTouchAt = touchState.lastTouchAt;
      const interruptedPreviousTouch = touchSpeechIsActive(time);
      const generation = ++touchRequestGeneration;
      const thinkingGeneration = -generation;
      const controller = createAbortController();
      touchRequestAbortController = controller;
      touchState.requestInFlight = true;
      feedback.showThinking?.(thinkingGeneration, "model-generating", "正在组织触摸互动的文字回应");
      const history = session.getHistory?.() || [];
      const identity = conversationIdentity();
      const emotion = sceneEmotionSnapshot();
      const relationshipTone = String(scene.getRelationshipTone?.() || "");
      try {
        const response = await request(interactionUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: controller.signal,
          body: JSON.stringify({
            kind: zone,
            eventType: "touch",
            requestId: requestId(),
            conversationId: identity.conversationId,
            contextEpoch: identity.contextEpoch,
            burstCount,
            boundaryLevel,
            sequence: burstSequence.length ? burstSequence : touchState.sequence.slice(-20),
            burstZoneCounts,
            aggregatedBurst: burstCount >= TOUCH_BURST_THRESHOLD,
            zoneCount: touchState.zoneCounts[zone] || 1,
            shortWindowCount,
            mediumWindowCount,
            consecutiveCount: touchState.consecutiveZone === zone ? touchState.consecutiveCount : 1,
            sessionCount: touchState.sessionCount,
            elapsedSinceSameZoneMs: touchState.elapsedSinceSameZoneMs,
            recentTouchReplies: touchState.recentReplies.map(item => item.text).slice(-12),
            recentTouchAngles: touchState.recentReplies.map(item => item.angle).filter(Boolean).slice(-8),
            recentOriginalClipIds: touchState.recentReplies.map(item => item.originalClipId).filter(Boolean).slice(-4),
            interruptedPreviousTouch,
            interruptedPreviousSpeech: false,
            currentEmotion: emotion.emotion,
            currentEmotionWeight: emotion.weight,
            relationshipTone,
            developerDebug: isDeveloper(),
            messages: history,
          }),
        });
        const data = await response.json();
        if (generation !== touchRequestGeneration) return;
        const currentIdentity = conversationIdentity();
        if (identity.conversationId !== currentIdentity.conversationId
            || identity.contextEpoch !== currentIdentity.contextEpoch) return;
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        if (boundaryLevel === "ignore") {
          touchState.replyCooldownUntil = now() + TOUCH_BURST_REPLY_COOLDOWN_MS;
        }
        feedback.renderDebug?.(data);
        if (String(data.reply || "").trim()) {
          touchState.recentReplies.push({
            text: String(data.reply).trim(),
            angle: String(data.touchMetrics?.responseAngle || ""),
            originalClipId: String(data.originalAudio?.id || data.touchMetrics?.originalClipId || ""),
            time: now(),
          });
          touchState.recentReplies = touchState.recentReplies.slice(-12);
        }
        if (interruptedPreviousTouch) session.markTouchInterrupted?.();
        const assistantData = {
          ...data,
          source: "touch",
          touchZone: zone,
          touchBoundaryLevel: boundaryLevel,
          touchExpressionBand: zone === "face" && boundaryLevel === "normal" ? "middle" : "",
          memoryUpdates: [],
          accumulateExpression: true,
          smoothEmotionTransition: true,
          clientChatGeneration: thinkingGeneration,
        };
        if (pipelineHasPriority(now())) {
          deferredTouchAssistant = assistantData;
          return;
        }
        await session.applyAssistant?.(assistantData, "");
      } catch (error) {
        if (controller.signal.aborted || error?.name === "AbortError" || generation !== touchRequestGeneration) return;
        warn("touch response failed:", error);
      } finally {
        if (generation === touchRequestGeneration) {
          feedback.hideThinking?.(thinkingGeneration);
          touchState.requestInFlight = false;
          touchRequestAbortController = null;
          if (isDeveloper()) feedback.reloadRelationship?.();
          if (touchState.lastTouchAt <= snapshotLastTouchAt) touchState.pendingZone = "";
        }
      }
    }

    function cancelForColdShoulder() {
      if (touchDebounceTimer) {
        clearTimer(touchDebounceTimer);
        touchDebounceTimer = 0;
      }
      if (touchRequestAbortController) {
        touchRequestAbortController.abort();
        touchRequestAbortController = null;
      }
      touchRequestGeneration += 1;
      touchState.requestInFlight = false;
      touchState.pendingAfterChat = false;
      touchState.pendingZone = "";
      deferredTouchAssistant = null;
    }

    function onChatStarted() {
      // 用户开始聊天时暂停触摸请求，但保留待处理区域，空闲后按原规则恢复。
      if (touchDebounceTimer) {
        clearTimer(touchDebounceTimer);
        touchDebounceTimer = 0;
        touchState.pendingAfterChat = Boolean(touchState.pendingZone);
      }
      if (touchRequestAbortController) {
        const interruptedTouchGeneration = touchRequestGeneration;
        touchRequestAbortController.abort();
        touchRequestAbortController = null;
        touchRequestGeneration += 1;
        touchState.requestInFlight = false;
        touchState.pendingAfterChat = Boolean(touchState.pendingZone);
        feedback.hideThinking?.(-interruptedTouchGeneration);
      }
    }

    function invalidateForContextChange({ clearDeferred = true } = {}) {
      // 上下文切换只使在途结果失效；不同清空入口决定是否保留已延后的回复。
      if (touchRequestAbortController) touchRequestAbortController.abort();
      touchRequestAbortController = null;
      touchRequestGeneration += 1;
      touchState.requestInFlight = false;
      if (clearDeferred) deferredTouchAssistant = null;
    }

    function clearDeferredResponse() {
      deferredTouchAssistant = null;
    }

    function onInteractionIdle() {
      if (deferredTouchAssistant) {
        const pending = deferredTouchAssistant;
        deferredTouchAssistant = null;
        touchState.pendingAfterChat = false;
        void session.applyAssistant?.(pending, "");
        return true;
      }
      if (touchState.pendingAfterChat && !touchState.requestInFlight && !touchDebounceTimer) {
        touchState.pendingAfterChat = false;
        scheduleTouchInteraction(120);
        return true;
      }
      return false;
    }

    function updateDeveloperOverlay() {
      if (!isDeveloper()) return;
      const currentModel = typeof scene.getModel === "function" ? scene.getModel() : null;
      let bounds;
      try {
        bounds = currentModel?.getBounds?.();
      } catch {
        bounds = null;
      }
      if (!bounds || bounds.width <= 0 || bounds.height <= 0) return;
      const time = now();
      const stale = Boolean(lastDeveloperTouchAt && time - lastDeveloperTouchAt > 1100);
      feedback.renderOverlay?.({
        bounds,
        touchBounds: normalizedTouchBounds(),
        stale,
        now: time,
      });
      if (stale) lastDeveloperTouchAt = 0;
    }

    function hasPendingWork() {
      return Boolean(touchDebounceTimer) || touchState.requestInFlight;
    }

    function status() {
      return Object.freeze({
        requestInFlight: touchState.requestInFlight,
        debounceActive: Boolean(touchDebounceTimer),
        pendingAfterChat: touchState.pendingAfterChat,
        pendingZone: touchState.pendingZone,
        deferredAssistant: Boolean(deferredTouchAssistant),
      });
    }

    return Object.freeze({
      loadConfig,
      setModelProfile,
      activeTouchRegionProfile,
      normalizedTouchBounds,
      modelTouchInfo,
      handleModelTouch,
      updateDeveloperOverlay,
      renderRegions,
      cancelForColdShoulder,
      onChatStarted,
      invalidateForContextChange,
      clearDeferredResponse,
      onInteractionIdle,
      isRequestInFlight: () => touchState.requestInFlight,
      hasPendingWork,
      status,
    });
  }

  return Object.freeze({
    createTouchInteractionController,
    DEFAULT_TOUCH_REGION_CONFIG,
    TOUCH_ZONE_LABELS,
  });
}));
