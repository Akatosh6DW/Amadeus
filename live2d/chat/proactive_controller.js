// 主动联系控制器独占启动问候、主动调度、等待门控、取消代次和主动事件持久化状态。
// 页面只提供会话、聊天、语音、场景、传输和反馈端口；服务端等待事实优先，不能由前端计时解除。
(function attachProactiveController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusProactiveController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createProactiveControllerApi() {
  "use strict";

  function normalizeProactiveControl(value) {
    const raw = value && typeof value === "object" ? value : {};
    // Older clients stored the temporary question-wait gate as a quiet order.
    const paused = String(raw.status || "").toLowerCase() === "paused"
      && raw.reason !== "unanswered_question_no_nudge";
    return {
      status: paused ? "paused" : "allowed",
      resumeMode: paused ? "model_decides" : "",
      reason: paused ? String(raw.reason || "") : "",
      updatedAt: String(raw.updatedAt || ""),
      sourceTurnId: String(raw.sourceTurnId || ""),
      sourceUserMessage: paused ? String(raw.sourceUserMessage || "") : "",
      sourceRequestStartedAtMs: Math.max(0, Number(raw.sourceRequestStartedAtMs) || 0),
    };
  }

  function createProactiveController(options = {}) {
    const logic = options.logic || {};
    const profiles = options.frequencyProfiles || {};
    const getSettings = typeof options.getSettings === "function"
      ? options.getSettings
      : () => ({});
    const clock = options.clock || {};
    const session = options.session || {};
    const transport = options.transport || {};
    const speech = options.speech || {};
    const scene = options.scene || {};
    const delivery = options.delivery || {};
    const feedback = options.feedback || {};
    const reminder = options.reminder || {};

    const defaultNow = () => (
      typeof performance !== "undefined" && typeof performance.now === "function"
        ? performance.now()
        : Date.now()
    );
    const now = typeof clock.now === "function" ? clock.now : defaultNow;
    const random = typeof clock.random === "function" ? clock.random : Math.random;
    const localHour = typeof clock.localHour === "function"
      ? clock.localHour
      : () => new Date().getHours();
    const createAbortController = typeof clock.createAbortController === "function"
      ? clock.createAbortController
      : () => new AbortController();
    const request = typeof transport.request === "function"
      ? transport.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("proactive request transport is unavailable");
          return fetch(url, init);
        };
    const getSettingsValue = () => getSettings() || {};
    const persist = () => {
      if (typeof session.persist === "function") session.persist();
    };

    let waitingForUser = Boolean(
      typeof session.getInitialContext === "function"
        ? session.getInitialContext()?.waitingForUser
        : false,
    );
    const initialControl = typeof session.getInitialContext === "function"
      ? session.getInitialContext()?.proactiveControl
      : null;
    const proactiveState = {
      lastUserActivityAt: now(),
      lastProactiveAt: 0,
      unansweredCount: 0,
      phase: "idle",
      returnContext: "",
      nextAmbientMoodAt: 0,
      silenceLimit: typeof logic.silenceLimit === "function" ? logic.silenceLimit(random()) : 3,
      activePrompt: "",
      activeSubject: "",
      afterUserTurnId: "",
      sequenceId: 1,
      control: normalizeProactiveControl(initialControl),
    };

    let startupGreetingPending = false;
    let startupGreetingPreparing = false;
    let preparedStartupGreeting = null;
    let startupGreetingGeneration = 0;
    let startupGreetingAbortController = null;
    let nextProactiveInteractionAt = 0;
    let proactiveInteractionInFlight = false;
    let proactiveRequestAbortController = null;
    let proactiveRequestGeneration = 0;
    const proactivePersistenceInFlight = new Set();

    function settings() {
      return getSettingsValue();
    }

    function setWaiting(value, { persistState = false } = {}) {
      waitingForUser = Boolean(value);
      if (typeof session.setWaitingForUser === "function") {
        session.setWaitingForUser(waitingForUser);
      }
      if (persistState) persist();
      return waitingForUser;
    }

    function setControl(value) {
      const control = normalizeProactiveControl(value);
      proactiveState.control = control;
      if (typeof session.setProactiveControl === "function") session.setProactiveControl(control);
      return control;
    }

    function proactiveOutreachPaused() {
      return proactiveState.control.status === "paused" || waitingForUser === true;
    }

    function frequencyProfile() {
      return profiles[settings().proactiveFrequency] || profiles.normal || {
        initial: [0, 0],
        followUps: [[0, 0]],
        startupChanceScale: 1,
      };
    }

    function randomInterval(range) {
      return range[0] + random() * Math.max(0, range[1] - range[0]);
    }

    function scheduleStartupInteraction() {
      if (!settings().proactiveInteractionsEnabled || proactiveOutreachPaused()) {
        nextProactiveInteractionAt = 0;
        return;
      }
      const profile = frequencyProfile();
      nextProactiveInteractionAt = now() + (startupGreetingPending
        ? (preparedStartupGreeting?.preparedTts ? 180 + random() * 320 : 700 + random() * 1300)
        : randomInterval(profile.initial));
    }

    async function prepareStartupGreeting(voiceReadyPromise = Promise.resolve(null)) {
      const greetingGeneration = ++startupGreetingGeneration;
      const messages = typeof session.getCleanHistory === "function"
        ? session.getCleanHistory().slice(-6)
        : [];
      const startupChance = (typeof session.getMessageCount === "function" && session.getMessageCount() ? 0.3 : 0.78)
        * frequencyProfile().startupChanceScale;
      startupGreetingPending = Boolean(settings().proactiveInteractionsEnabled)
        && !proactiveOutreachPaused()
        && random() < Math.max(0.08, Math.min(0.92, startupChance));
      preparedStartupGreeting = null;
      if (!startupGreetingPending || Boolean(scene.isDeveloper?.())) return null;
      startupGreetingPreparing = true;
      try {
        const response = await request("/api/startup-greeting", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages, localHour: localHour() }),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        if (data.suppressed) {
          if (data.waitingForUser === true) setWaiting(true);
          if (data.proactiveControl) applyProactiveControl(data.proactiveControl);
          startupGreetingPending = false;
          return null;
        }
        if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
        if (data.emptyReply || !String(data.reply || "").trim()) {
          throw new Error("startup greeting returned an empty reply");
        }
        preparedStartupGreeting = {
          ...data,
          source: data.source || "proactive",
          memoryUpdates: [],
        };
        if (data.startupFallbackReason) {
          preparedStartupGreeting.startupFallbackNotice = "启动问候没有取得有效的模型回复，当前显示的是本地备用问候。";
        }
        await prepareStartupGreetingSpeech(preparedStartupGreeting, voiceReadyPromise, greetingGeneration);
        return preparedStartupGreeting;
      } catch (error) {
        if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
        feedback.warn?.("startup greeting preparation failed", error);
        // A failed request may have already persisted a pause on the server.
        // Never manufacture unsolicited speech without a successful decision.
        startupGreetingPending = false;
        preparedStartupGreeting = null;
        return null;
      } finally {
        startupGreetingPreparing = false;
      }
    }

    async function prepareStartupGreetingSpeech(
      greeting,
      voiceReadyPromise = Promise.resolve(null),
      greetingGeneration = startupGreetingGeneration,
    ) {
      if (!greeting || typeof speech.shouldSynchronizeTextWithVoice === "function"
          && !speech.shouldSynchronizeTextWithVoice()) return null;
      if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
      const provider = String(settings().ttsProvider || "").trim().toLowerCase();
      if (provider !== "gpt-sovits") return null;
      startupGreetingAbortController?.abort();
      const controller = createAbortController();
      startupGreetingAbortController = controller;
      try {
        const status = await voiceReadyPromise;
        if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
        if (!status?.running) throw new Error(status?.error || "GPT-SoVITS 尚未准备完成");
        if (typeof speech.prepareStartupTts !== "function") return null;
        const prepared = await speech.prepareStartupTts(greeting, {
          signal: controller.signal,
          generation: greetingGeneration,
        });
        if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
        if (prepared) {
          greeting.preparedTts = prepared;
          speech.onStartupAudioReady?.(prepared);
        }
        return greeting.preparedTts || null;
      } catch (error) {
        if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending || error?.name === "AbortError") {
          return null;
        }
        feedback.warn?.("startup greeting speech preparation failed", error);
        speech.onStartupTtsFailure?.(error, greeting);
        return null;
      } finally {
        if (greetingGeneration === startupGreetingGeneration) startupGreetingAbortController = null;
      }
    }

    function applyProactiveControl(value, { persistState = true } = {}) {
      const wasPaused = proactiveState.control.status === "paused";
      const control = setControl(value);
      if (control.status === "paused") {
        // A terminal unanswered-sequence sign-off may finish its own sentence.
        // User-requested quiet cancels in-flight chatter, but never a due reminder.
        const playback = speech.getState?.() || {};
        if (control.reason !== "unanswered_sequence_complete"
            && playback.kind === "proactive"
            && playback.deliveryPriority !== "scheduled_reminder") {
          speech.interruptCurrentSpeech?.();
        }
        proactiveRequestGeneration += 1;
        proactiveRequestAbortController?.abort();
        proactiveRequestAbortController = null;
        proactiveInteractionInFlight = false;
        nextProactiveInteractionAt = 0;
        startupGreetingGeneration += 1;
        startupGreetingAbortController?.abort();
        startupGreetingAbortController = null;
        startupGreetingPending = false;
        preparedStartupGreeting = null;
      } else if (wasPaused && settings().proactiveInteractionsEnabled) {
        scheduleNextProactiveInteraction(now(), 0);
      }
      if (persistState) persist();
      return control;
    }

    function scheduleNextProactiveInteraction(at = now(), unansweredCount = proactiveState.unansweredCount) {
      if (!settings().proactiveInteractionsEnabled || proactiveState.phase === "sulking" || proactiveOutreachPaused()) {
        nextProactiveInteractionAt = 0;
        return;
      }
      const profile = frequencyProfile();
      const range = unansweredCount <= 0
        ? profile.initial
        : profile.followUps[Math.min(profile.followUps.length - 1, unansweredCount - 1)];
      nextProactiveInteractionAt = at + randomInterval(range);
    }

    function interactionBlocked(nowValue) {
      const speechState = speech.getState?.() || {};
      return Boolean(
        scene.isDeveloper?.()
        || scene.isBootActive?.()
        || session.isChatRequestInFlight?.()
        || session.hasTouchPending?.()
        || speechState.pending
        || speech.isSpeaking?.(nowValue)
        || !scene.hasModel?.()
        || scene.isSettingsModalOpen?.()
        || scene.isBookmarkPanelOpen?.(),
      );
    }

    async function update(nowValue = now()) {
      if (!settings().proactiveInteractionsEnabled || proactiveState.phase === "sulking" || proactiveOutreachPaused()) {
        nextProactiveInteractionAt = 0;
        return;
      }
      if (proactiveInteractionInFlight || reminder.isInFlight?.() || !nextProactiveInteractionAt || nowValue < nextProactiveInteractionAt) return;
      if (startupGreetingPending && startupGreetingPreparing) {
        nextProactiveInteractionAt = nowValue + 500;
        return;
      }
      if (interactionBlocked(nowValue)) {
        nextProactiveInteractionAt = nowValue + 15000;
        return;
      }
      if (scene.hasUserInput?.()) {
        nextProactiveInteractionAt = nowValue + 30000;
        return;
      }
      proactiveInteractionInFlight = true;
      const sequenceId = proactiveState.sequenceId;
      const requestGeneration = ++proactiveRequestGeneration;
      proactiveRequestAbortController?.abort();
      const controller = createAbortController();
      proactiveRequestAbortController = controller;
      const greeting = startupGreetingPending;
      startupGreetingPending = false;
      const transition = typeof logic.nextState === "function"
        ? logic.nextState(proactiveState.unansweredCount, proactiveState.silenceLimit)
        : { nextCount: proactiveState.unansweredCount + 1, willSulk: false, phase: "waiting" };
      const willSulk = transition.willSulk;
      try {
        const data = await proactiveEventFor(greeting, willSulk, controller.signal);
        if (!data || controller.signal.aborted || requestGeneration !== proactiveRequestGeneration
            || sequenceId !== proactiveState.sequenceId) return;
        if (data.ok === false) {
          feedback.warn?.("model-driven proactive generation failed:", data);
          feedback.toast?.(data.message || "主动聊天生成失败；没有使用本地固定题目");
          nextProactiveInteractionAt = now() + 60000;
          return;
        }
        if (data.suppressed) {
          if (data.waitingForUser === true) setWaiting(true);
          if (data.proactiveControl) applyProactiveControl(data.proactiveControl);
          if (!proactiveOutreachPaused()) scheduleNextProactiveInteraction(now(), 0);
          return;
        }
        if (greeting && data.startupFallbackReason) {
          feedback.updateConnectionStatus?.({ connectionState: "startup-greeting-fallback" });
          feedback.toast?.(data.startupFallbackNotice || "启动问候未连接模型服务，已明确切换为本地备用问候");
        }
        proactiveState.lastProactiveAt = nowValue;
        proactiveState.unansweredCount = transition.nextCount;
        proactiveState.phase = transition.phase;
        proactiveState.activePrompt = String(data.reply || "");
        proactiveState.afterUserTurnId = data.proactiveAfterUserTurnId;
        proactiveState.activeSubject = String(data.proactiveSubject || proactiveState.activeSubject || "");
        if (willSulk) {
          proactiveState.nextAmbientMoodAt = nowValue + 150000 + random() * 90000;
          nextProactiveInteractionAt = 0;
        } else {
          scheduleNextProactiveInteraction(nowValue, proactiveState.unansweredCount);
        }
        await delivery.applyAssistant?.({
          ...data,
          source: data.source || "proactive",
          interactionKind: "proactive",
          proactiveKind: data.proactiveKind || (willSulk ? "sulk" : ""),
          proactivePhase: transition.phase,
          proactiveUnansweredCount: transition.nextCount,
          proactiveSilenceLimit: proactiveState.silenceLimit,
          proactiveDirective: willSulk
            ? "pause_until_next_user_message"
            : String(data.proactiveDirective || "unchanged"),
          proactiveEventId: data.proactiveEventId
            || `proactive-${Date.now()}-${random().toString(36).slice(2, 10)}`,
          proactiveRequestGeneration: requestGeneration,
          proactiveSequenceId: sequenceId,
          memoryUpdates: [],
        }, "");
      } catch (error) {
        if (error?.name === "AbortError" || requestGeneration !== proactiveRequestGeneration) return;
        feedback.warn?.("model-driven proactive generation failed:", error);
        feedback.toast?.(`主动聊天生成失败，未使用固定题目：${error.message || error}`);
        nextProactiveInteractionAt = now() + 60000;
      } finally {
        if (requestGeneration === proactiveRequestGeneration) proactiveRequestAbortController = null;
        proactiveInteractionInFlight = false;
      }
    }

    async function proactiveEventFor(greeting = false, willSulk = false, signal = null) {
      if (greeting) {
        const prepared = preparedStartupGreeting;
        preparedStartupGreeting = null;
        return prepared || null;
      }
      const identity = session.getConversationIdentity?.() || {};
      if (willSulk) {
        // End an unanswered sequence silently; the local sign-off cannot wake a sleeping user.
        proactiveState.phase = "sulking";
        nextProactiveInteractionAt = 0;
        setWaiting(true, { persistState: true });
        try {
          await request("/api/conversation/proactive", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              eventId: `waiting-${Date.now()}`,
              reply: "",
              proactiveKind: "sulk",
              proactivePhase: "sulking",
              unansweredCount: proactiveState.silenceLimit,
              silenceLimit: proactiveState.silenceLimit,
              proactiveDirective: "pause_until_next_user_message",
              contextEpoch: identity.contextEpoch,
              ...(typeof proactiveState.afterUserTurnId === "string"
                ? { afterUserTurnId: proactiveState.afterUserTurnId } : {}),
            }),
            ...(signal ? { signal } : {}),
          });
        } catch (error) {
          feedback.warn?.("waiting state sync deferred; local state retained:", error);
        }
        return {
          ok: true,
          suppressed: true,
          waitingForUser: true,
          reason: "unanswered_sequence_complete",
          proactiveControl: proactiveState.control,
        };
      }
      const selected = Array.isArray(settings().proactiveTopicCategories)
        ? settings().proactiveTopicCategories.filter(item => (logic.CATEGORY_IDS || []).includes(item))
        : [];
      const recentProactiveTexts = session.getRecentProactiveTexts?.(18) || [];
      const response = await request("/api/proactive-generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: proactiveState.unansweredCount > 0 ? "follow_up" : "new",
          previousProactive: proactiveState.activePrompt,
          previousSubject: proactiveState.activeSubject,
          unansweredCount: proactiveState.unansweredCount,
          topicCategories: selected.length ? selected : [...(logic.CATEGORY_IDS || [])],
          recentProactiveTexts,
          messages: (session.getCleanHistory?.() || []).slice(-14),
        }),
        ...(signal ? { signal } : {}),
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        return {
          ok: false,
          error: data.error || `HTTP ${response.status}`,
          message: data.message || "主动聊天生成失败；没有使用本地固定题目",
        };
      }
      return data;
    }

    function resetProactiveSequence(at = now()) {
      proactiveRequestGeneration += 1;
      proactiveRequestAbortController?.abort();
      proactiveRequestAbortController = null;
      proactiveState.lastUserActivityAt = at;
      proactiveState.unansweredCount = 0;
      proactiveState.phase = "idle";
      proactiveState.nextAmbientMoodAt = 0;
      proactiveState.silenceLimit = typeof logic.silenceLimit === "function" ? logic.silenceLimit(random()) : 3;
      proactiveState.activePrompt = "";
      proactiveState.activeSubject = "";
      proactiveState.afterUserTurnId = "";
      proactiveState.sequenceId += 1;
      startupGreetingGeneration += 1;
      startupGreetingAbortController?.abort();
      startupGreetingAbortController = null;
      startupGreetingPending = false;
      preparedStartupGreeting = null;
      if (settings().proactiveInteractionsEnabled && !proactiveOutreachPaused()) {
        scheduleNextProactiveInteraction(at, 0);
      } else {
        nextProactiveInteractionAt = 0;
      }
    }

    function noteUserActivity() {
      setWaiting(false, { persistState: true });
      const wasWaiting = proactiveState.unansweredCount;
      const wasSulking = proactiveState.phase === "sulking";
      const at = now();
      if (wasWaiting) {
        proactiveState.returnContext = wasSulking
          ? `Amadeus System刚才连续主动联系了用户${wasWaiting}次都没有得到回应，最后已经说去做实验并独自生闷气。用户现在回来了；自然承接他的回归，保留一点别扭，但优先回答当前内容。`
          : `Amadeus System刚才主动联系了用户${wasWaiting}次并在等待回应。用户现在回来了；可以简短表现出注意到他回来了，再自然回答当前内容。`;
      }
      resetProactiveSequence(at);
      scene.schedulePositiveAmbientMood?.(at);
    }

    function noteTouchPresence() {
      const at = now();
      resetProactiveSequence(at);
      scene.schedulePositiveAmbientMood?.(at);
    }

    function updateAmbientMood(at = now()) {
      if (proactiveState.phase !== "sulking" || !proactiveState.nextAmbientMoodAt || at < proactiveState.nextAmbientMoodAt) return;
      const playback = speech.getState?.() || {};
      if (playback.pending || speech.isSpeaking?.(at) || session.isChatRequestInFlight?.() || proactiveInteractionInFlight) return;
      const emotionState = scene.getEmotionState?.() || {};
      if (emotionState.currentEmotion !== "neutral" && (scene.emotionLifecycleWeight?.(at) || 0) > 0.08) return;
      const annoyed = random() < 0.38;
      scene.setEmotion?.(annoyed ? "annoyed" : "melancholic", "idle", {
        emotionIntensity: annoyed ? 0.56 : 0.62,
        eyeContact: 0.3,
        gazeX: annoyed ? -0.28 : -0.16,
        gazeY: 0.12,
        tension: annoyed ? 0.34 : 0.16,
        holdMs: 60000,
        fadeMs: 22000,
      });
      proactiveState.nextAmbientMoodAt = at + 180000 + random() * 240000;
    }

    async function recordProactiveConversationEvent(message, mood = {}) {
      if (!message || message.role !== "assistant" || message.meta?.kind !== "proactive") return;
      const waitingSequence = proactiveState.sequenceId;
      const eventId = String(
        message.meta.proactiveEventId
        || `proactive-${Date.now()}-${random().toString(36).slice(2, 10)}`,
      );
      message.meta.proactiveEventId = eventId;
      if (message.meta.proactivePersisted || proactivePersistenceInFlight.has(eventId)) return;
      proactivePersistenceInFlight.add(eventId);
      persist();
      try {
        const identity = session.getConversationIdentity?.() || {};
        const response = await request("/api/conversation/proactive", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            eventId,
            reply: String(message.content || ""),
            emotion: String(mood.emotion || message.meta.emotion || scene.getCurrentEmotion?.() || "neutral"),
            proactiveKind: String(message.meta.proactiveKind || ""),
            proactiveSubject: String(message.meta.proactiveSubject || ""),
            proactivePhase: String(message.meta.proactivePhase || ""),
            unansweredCount: Math.max(0, Number(message.meta.proactiveUnansweredCount) || 0),
            silenceLimit: Math.max(0, Number(message.meta.proactiveSilenceLimit) || 0),
            proactiveDirective: String(message.meta.proactiveDirective || "unchanged"),
            proactiveObservation: message.meta.proactiveObservation || null,
            requiresUserResponse: message.meta.requiresUserResponse === true,
            ...(typeof message.meta.proactiveAfterUserTurnId === "string"
              ? { afterUserTurnId: message.meta.proactiveAfterUserTurnId } : {}),
            contextEpoch: Math.max(0, Number(identity.contextEpoch) || 0),
          }),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || "proactive persistence failed");
        if (data.waitingForUser === true && waitingSequence === proactiveState.sequenceId) setWaiting(true);
        if (data.proactiveControl) applyProactiveControl(data.proactiveControl, { persistState: false });
        message.meta.proactivePersisted = true;
        delete message.meta.proactiveObservation;
        persist();
      } catch (error) {
        feedback.warn?.("proactive conversation persistence failed:", error);
      } finally {
        proactivePersistenceInFlight.delete(eventId);
      }
    }

    function syncPendingProactiveConversationEvents() {
      (session.getMessages?.() || [])
        .filter(message => message?.role === "assistant"
          && message.meta?.kind === "proactive"
          && !message.meta?.proactivePersisted)
        .forEach(message => recordProactiveConversationEvent(message));
    }

    function clearPendingPersistence() {
      proactivePersistenceInFlight.clear();
    }

    function hydrateConversationContext({ waitingForUser: serverWaiting, proactiveControl, conversationId, previousConversationId } = {}) {
      if (typeof serverWaiting === "boolean") {
        setWaiting(serverWaiting || (String(conversationId || "") === String(previousConversationId || "") && waitingForUser === true));
      }
      if (proactiveControl && typeof proactiveControl === "object") {
        applyProactiveControl(proactiveControl, { persistState: false });
      }
    }

    function markAssistantRequiresUserResponse(source) {
      if (source === "touch") return;
      setWaiting(true, { persistState: true });
      nextProactiveInteractionAt = 0;
    }

    function clearReturnContext() {
      proactiveState.returnContext = "";
    }

    function clearReturnContextIf(value) {
      if (String(value || "") === proactiveState.returnContext) {
        proactiveState.returnContext = "";
        return true;
      }
      return false;
    }

    function snapshot() {
      return {
        ...proactiveState,
        control: { ...proactiveState.control },
        waitingForUser,
        startupGreetingPending,
        startupGreetingPreparing,
        preparedStartupGreeting,
        nextProactiveInteractionAt,
        proactiveInteractionInFlight,
        proactiveRequestGeneration,
        startupGreetingGeneration,
      };
    }

    return Object.freeze({
      applyProactiveControl,
      clearPendingPersistence,
      clearReturnContext,
      clearReturnContextIf,
      getReturnContext: () => proactiveState.returnContext,
      getSequenceId: () => proactiveState.sequenceId,
      getState: snapshot,
      getWaitingForUser: () => waitingForUser,
      hydrateConversationContext,
      isPaused: proactiveOutreachPaused,
      isRequestInFlight: () => proactiveInteractionInFlight,
      markAssistantRequiresUserResponse,
      noteTouchPresence,
      noteUserActivity,
      prepareStartupGreeting,
      proactiveDeliveryStillCurrent: data => {
        if (data?.source === "todo-reminder" && String(data.reminderId || "")) {
          return !session.isChatRequestInFlight?.();
        }
        const isProactive = data?.interactionKind === "proactive"
          || data?.source === "proactive"
          || data?.source === "api-proactive"
          || data?.source === "todo-reminder";
        if (!isProactive) return true;
        const generation = Number(data.proactiveRequestGeneration || 0);
        const sequenceId = Number(data.proactiveSequenceId ?? -1);
        return !proactiveOutreachPaused()
          && !session.isChatRequestInFlight?.()
          && (!generation || generation === proactiveRequestGeneration)
          && (sequenceId < 0 || sequenceId === proactiveState.sequenceId);
      },
      recordProactiveConversationEvent,
      resetProactiveSequence,
      scheduleNextProactiveInteraction,
      scheduleStartupInteraction,
      setWaitingForUser: (value, options) => setWaiting(value, options),
      syncPendingProactiveConversationEvents,
      update,
      updateAmbientMood,
    });
  }

  return Object.freeze({ createProactiveController, normalizeProactiveControl });
}));
