// 语音诊断控制器唯一持有诊断快照、事件环和语音状态展示 DOM。
// 播放器、设置、运行时轮询和语音请求仍归页面/播放控制器所有，只经由只读 speech/settings port 提供数据。
(function attachVoiceDiagnostics(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusVoiceDiagnostics = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createVoiceDiagnosticsApi() {
  "use strict";

  function formatJson(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value ?? "-");
    }
  }

  function formatStopDiagnostics(metrics) {
    const attempts = Array.isArray(metrics?.attempts) ? metrics.attempts : [];
    if (!attempts.length) return "尚无停止过程数据";
    const reasonLabels = {
      eos: "EOS：模型选择结束",
      max_duration: "达到本次解码 token 上限",
      decoder_limit: "达到解码器硬上限",
      decode_error: "语义解码异常",
      mixed: "一次推理包含多种停止原因",
      unobserved: "旧运行时或未捕获",
    };
    return formatJson({
      route: metrics.gptModelRoute || metrics.primaryGptModelRoute || "default",
      model: metrics.gptModel || metrics.primaryGptModel || "",
      attempts: attempts.map((attempt, index) => {
        const decodeDetails = Array.isArray(attempt.semanticDecodes)
          ? attempt.semanticDecodes
          : [];
        const estimatedAudioMs = decodeDetails.reduce(
          (total, item) => total + Number(item?.semanticDurationEstimateMs || 0),
          0,
        );
        const configuredLimitMs = decodeDetails.reduce(
          (total, item) => total + Number(item?.decoderConfiguredLimitMs || 0),
          0,
        );
        const effectiveAudioLimitEstimateMs = decodeDetails.reduce(
          (total, item) => total + Number(item?.decoderEffectiveAudioLimitEstimateMs || 0),
          0,
        );
        return {
          attempt: Number(attempt.attemptIndex || index + 1),
          reference: attempt.referenceAudio || "",
          seed: attempt.segmentSeed ?? null,
          route: attempt.gptModelRoute || "",
          model: attempt.gptModel || "",
          semantic: {
            decodeCount: Number(attempt.semanticDecodeCount || 0),
            tokenCount: Number(attempt.semanticTokenCount || 0),
            tokenBudget: Number(attempt.semanticTokenBudget || 0),
            conditionedPhonemes: Number(attempt.conditionedPhonemeCount || 0),
            semanticFrameRateHz: Number(decodeDetails[0]?.semanticFrameRateHz || 0),
            estimatedAudioMs,
            configuredLimitMs,
            effectiveAudioLimitEstimateMs,
            stopReason: reasonLabels[attempt.semanticStopReason]
              || attempt.semanticStopReason
              || "未捕获",
            eosObserved: Boolean(attempt.semanticEosObserved),
            details: decodeDetails,
          },
          waveform: {
            rawMs: Number(attempt.rawAudioMs || 0),
            compactedMs: Number(attempt.compactedAudioMs || 0),
            activeSpeechMs: Number(attempt.activeSpeechMs || 0),
            trimmedEdgeMs: Number(attempt.trimmedEdgeMs || 0),
            removedInternalSilenceMs: Number(attempt.compactedSilenceMs || 0),
          },
          result: {
            qualityTier: attempt.qualityTier || "",
            qualityConfidence: attempt.qualityConfidence ?? null,
            failureType: attempt.failureType || "",
          },
        };
      }),
    });
  }

  function createVoiceDiagnosticsController(options = {}) {
    const elements = options.elements || {};
    const clock = options.clock || {};
    const speech = options.speech || {};
    const settings = options.settings || {};
    const documentObject = options.document
      || (typeof document !== "undefined" ? document : null);
    const isDeveloper = typeof options.isDeveloper === "function"
      ? options.isDeveloper
      : () => false;
    const now = typeof clock.now === "function" ? clock.now : () => Date.now();
    const getSpeechState = typeof speech.getState === "function"
      ? speech.getState
      : () => ({});
    const getCurrentSpeechIndex = typeof speech.getCurrentIndex === "function"
      ? speech.getCurrentIndex
      : () => 0;
    const isSpeaking = typeof speech.isSpeaking === "function"
      ? speech.isSpeaking
      : () => false;
    const getVoiceSettings = typeof settings.get === "function"
      ? settings.get
      : () => ({});

    let bound = false;
    let diagnostics = {
      stage: "idle",
      message: "尚无语音请求",
      updatedAt: now(),
    };
    let events = [];

    function snapshot() {
      return {
        diagnostics: { ...diagnostics },
        events: events.map(item => ({ ...item })),
      };
    }

    function span(text, className) {
      if (!documentObject?.createElement) return null;
      const element = documentObject.createElement("span");
      element.className = className;
      element.textContent = text;
      return element;
    }

    function renderHighlightedSpeech(target, text, state) {
      if (!target) return;
      const timeline = Array.isArray(state?.timeline) ? state.timeline : [];
      const content = timeline.length
        ? timeline.map(item => String(item.text || "")).join("")
        : String(text || "");
      if (!content) {
        target.textContent = "-";
        return;
      }
      const liveIndex = getCurrentSpeechIndex();
      const index = Math.max(0, Math.min(content.length - 1, Math.floor(liveIndex || 0)));
      const windowSize = Math.max(1, Math.min(8, Math.ceil(content.length / 24)));
      const start = Math.max(0, index - Math.floor(windowSize / 2));
      const end = Math.min(content.length, start + windowSize);
      const spans = [
        span(content.slice(0, start), "dev-done"),
        span(content.slice(start, end), "dev-current"),
        span(content.slice(end), "dev-next"),
      ].filter(Boolean);
      if (typeof target.replaceChildren === "function" && spans.length) {
        target.replaceChildren(...spans);
      } else {
        target.textContent = content;
      }
    }

    function render() {
      if (!isDeveloper() || !elements.developerPanel) return false;
      const state = getSpeechState() || {};
      const voiceSettings = getVoiceSettings() || {};
      const speaking = Boolean(isSpeaking(now()));
      if (elements.voiceStatus) {
        const timing = state.ttsMetrics?.totalMs
          ? ` / TTS ${state.ttsMetrics.totalMs}MS`
          : "";
        const segment = state.pipelineSegmentTotal > 1
          ? ` / SEG ${state.pipelineSegmentIndex}/${state.pipelineSegmentTotal}`
          : "";
        elements.voiceStatus.textContent = speaking
          ? `${voiceSettings.ttsProvider || "gpt-sovits"} / ${state.alignmentSource || "NO_TIMELINE"} / ${state.voiceStyle || "calm"} / ${state.voiceTone || "neutral"}:${state.voiceIntensity || "normal"} / ${state.speechPace || "normal"}${segment} / ${state.charIndex}${timing}`
          : state.pending ? "PENDING" : "IDLE";
      }
      if (elements.replyZh && state.displayText && !state.textRevealPending) {
        elements.replyZh.textContent = state.displayText;
      }
      if (elements.spokenJa && state.spokenTextJa) {
        elements.spokenJa.textContent = state.spokenTextJa;
      }
      if (elements.speakingJa) {
        renderHighlightedSpeech(
          elements.speakingJa,
          state.spokenTextJa || state.text || "",
          state,
        );
      }
      if (elements.diagnostic) elements.diagnostic.textContent = formatJson(diagnostics);
      if (elements.stopDiagnostic) {
        elements.stopDiagnostic.textContent = formatStopDiagnostics(state.ttsMetrics);
      }
      if (elements.events) {
        elements.events.textContent = events.length
          ? events.slice(-16).reverse()
            .map(item => `[${item.time}] ${item.stage} - ${item.message}`)
            .join("\n")
          : "-";
      }
      return true;
    }

    function record(stage, message, details = {}) {
      const event = {
        time: new Date(now()).toLocaleTimeString("zh-CN", { hour12: false }),
        stage: String(stage || "unknown"),
        message: String(message || ""),
        ...details,
      };
      diagnostics = { ...event, updatedAt: now() };
      events = [...events, event].slice(-36);
      render();
      return { ...event };
    }

    function bind() {
      if (bound) return false;
      bound = true;
      return true;
    }

    return Object.freeze({
      bind,
      formatStopDiagnostics,
      getSnapshot: snapshot,
      getState: () => ({ bound, ...snapshot() }),
      record,
      render,
    });
  }

  return Object.freeze({ createVoiceDiagnosticsController, formatStopDiagnostics });
}));
