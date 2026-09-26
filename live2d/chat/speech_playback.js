(function (root, factory) {
  const mouthShapes = typeof module === "object" && module.exports
    ? require("../audio/mouth_shapes.js")
    : root.AmadeusMouthShapes;
  const api = factory(mouthShapes);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusSpeechPlayback = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function (mouthShapes) {
  "use strict";

  // 唯一持有播放器、时间轴、口型采样和两类取消代次。外部只取快照或调用明确动作。
  function createSpeechPlaybackController(options) {
    const { getSettings, defaultSettings, gateway, platform, clock, mood, feedback, rules } = options;
    const {
      createAnimeMouthState, animeVisemeGroupForCue, animeShapeForGroup,
      mouthShapeDistance, lerpMouthShape, mouthShapeForCue, mouthShapeForChar,
    } = mouthShapes;
    const recordVoiceDiagnostic = (...args) => feedback.diagnostic(...args);
    const syncChatSubmitButton = () => feedback.syncSubmitButton();
    const showThinkingIndicator = (...args) => feedback.showThinking(...args);
    const hideThinkingIndicator = (...args) => feedback.hideThinking(...args);
    const updateThinkingIndicatorStage = (...args) => feedback.updateThinking(...args);
    const showVoiceFailure = (...args) => feedback.showFailure(...args);
    const clearVoiceFailure = () => feedback.clearFailure();
    const setEmotion = (...args) => mood.setEmotion(...args);
    const protectCurrentExpressionThroughSpeech = (...args) => mood.protectExpression(...args);
    const delay = ms => clock.delay(ms);
    const normalizeSpeechPace = value => rules.normalizeSpeechPace(value);
    const speedForSpeechPace = value => rules.speedForSpeechPace(value);
    const normalizeVoiceIntensity = value => rules.normalizeVoiceIntensity(value);
    const normalizeSpeechLanguage = value => rules.normalizeSpeechLanguage(value);
    const normalizeSpeechProsody = value => rules.normalizeSpeechProsody(value);
    const speechLanguageProfile = value => rules.speechLanguageProfile(value);
    const chooseSpeechText = (...args) => rules.chooseSpeechText(...args);
    const resolveSpeechPace = (...args) => rules.resolveSpeechPace(...args);
    const resolveSpeechSpeed = (...args) => rules.resolveSpeechSpeed(...args);
    const resolveAssistantMood = (...args) => rules.resolveAssistantMood(...args);
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const toNumber = (value, fallback) => {
      const number = Number(value);
      return Number.isFinite(number) ? number : fallback;
    };
    let speakUntil = 0;
let speechState = {
  active: false,
  pending: false,
  startedAt: 0,
  endsAt: 0,
  text: "",
  displayText: "",
  spokenTextJa: "",
  timeline: [],
  mouthCues: [],
  alignmentSource: "",
  ttsMetrics: null,
  charIndex: 0,
  voiceStyle: "calm",
  voiceTone: "neutral",
  voiceIntensity: "normal",
  speechPace: "normal",
  speechSpeed: 1,
  pipelineSegmentIndex: 0,
  pipelineSegmentTotal: 1,
  utterance: null,
  mood: null,
  moodApplied: false,
  kind: "idle",
  generation: 0,
  systemSpeechActive: false,
  systemSpeechDeadline: 0,
  textRevealPending: false,
};
    let ttsAudio = null;
    let ttsAudioUrl = "";
    let ttsAudioGeneration = 0;
    let audioContext = null;
    let audioAnalyser = null;
    let audioSamples = null;
    let audioMouthLevel = 0;
    let animeMouthState = createAnimeMouthState();
    let speechGeneration = 0;
    let speechPipelineGeneration = 0;
    let speechPipelineAbortController = null;
    let speechRequestAbortController = null;
    let speechGapIndicatorGeneration = 0;
    let speechPipelineActive = false;
const SPEECH_EMOJI_SEQUENCE_PATTERN = /(?:[#*0-9]\uFE0F?\u20E3|[\u{1F1E6}-\u{1F1FF}]{1,2}|(?:\p{Emoji_Presentation}|\p{Extended_Pictographic}\uFE0F)(?:\p{Emoji_Modifier})?(?:\u200D(?:\p{Emoji_Presentation}|\p{Extended_Pictographic})(?:\uFE0F)?(?:\p{Emoji_Modifier})?)*)|[\u{1F3FB}-\u{1F3FF}\u{E0020}-\u{E007F}\u200D\uFE0E\uFE0F\u20E3]/gu;

function stripEmojiForSpeech(text) {
  return String(text || "")
    .replace(SPEECH_EMOJI_SEQUENCE_PATTERN, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function startSpeech(text, voiceStyle = "calm", meta = {}) {
  const cleanText = stripEmojiForSpeech(text);
  if (!cleanText) {
    recordVoiceDiagnostic("skipped-empty", "发声文本为空，未调用语音服务");
    return Promise.resolve(false);
  }
  if (!/[\p{L}\p{N}]/u.test(cleanText)) {
    recordVoiceDiagnostic("skipped-punctuation", "发声文本只有标点，未调用语音服务");
    return Promise.resolve(false);
  }
  const generation = ++speechGeneration;
  speechRequestAbortController?.abort();
  const speechRequestController = new AbortController();
  speechRequestAbortController = speechRequestController;
  stopCurrentPlayback();
  const speechPace = normalizeSpeechPace(meta.speechPace || "normal");
  const speechSpeed = clamp(toNumber(meta.speechSpeed, speedForSpeechPace(speechPace)), 0.62, 1.45);
  const duration = estimateSpeechDuration(cleanText, voiceStyle, speechSpeed);
  speechState = {
    active: false,
    pending: true,
    queuedAt: clock.now(),
    requestStartedAt: Number(meta.requestStartedAt || 0),
    startedAt: 0,
    endsAt: 0,
    text: cleanText,
    displayText: String(meta.displayText || cleanText),
    spokenTextJa: stripEmojiForSpeech(meta.spokenTextJa || cleanText),
    semanticUnits: validatedSemanticSpeechUnits(
      cleanText,
      Array.isArray(meta.semanticUnits) ? meta.semanticUnits : null,
    ),
    timeline: [],
    mouthCues: [],
    alignmentSource: "",
    ttsMetrics: null,
    charIndex: 0,
    voiceStyle,
    voiceTone: String(meta.voiceTone || "neutral"),
    voiceIntensity: normalizeVoiceIntensity(meta.voiceIntensity),
    speechPace,
    speechSpeed,
    utterance: null,
    mood: meta.mood || null,
    moodApplied: false,
    preparedTts: meta.preparedTts || null,
    originalAudio: meta.originalAudio || null,
    kind: String(meta.kind || "chat"),
    deliveryPriority: String(meta.deliveryPriority || ""),
    provider: String(getSettings().ttsProvider || defaultSettings.ttsProvider).trim().toLowerCase(),
    generation,
    pipelineSegmentIndex: Math.max(0, Number(meta.pipelineSegmentIndex) || 0),
    pipelineSegmentTotal: Math.max(1, Number(meta.pipelineSegmentTotal) || 1),
    systemSpeechActive: false,
    systemSpeechDeadline: 0,
    textRevealPending: Boolean(getSettings().voiceEnabled),
  };
  syncChatSubmitButton();
  speakUntil = 0;
  resetAnimeMouthState();
  recordVoiceDiagnostic("queued", "回复已进入语音队列", {
    generation,
    provider: getSettings().ttsProvider,
    kind: speechState.kind,
    textLength: cleanText.length,
  });

  if (!getSettings().voiceEnabled) {
    speechState.pending = false;
    speechState.textRevealPending = false;
    recordVoiceDiagnostic("skipped-disabled", "设置中的语音朗读已关闭", { generation });
    return Promise.resolve(false);
  }
  if (speechState.mood) {
    const continuesSameSegmentedMood = (
      speechState.pipelineSegmentIndex > 1
      && mood.currentEmotion() === speechState.mood.emotion
    );
    if (!continuesSameSegmentedMood) {
      setEmotion(speechState.mood.emotion, speechState.mood.gesture, speechState.mood);
    }
    speechState.moodApplied = true;
  }
  const provider = String(getSettings().ttsProvider || "").trim().toLowerCase();
  const localProviders = new Set(["gpt-sovits", "voicevox", "generic"]);
  if (localProviders.has(provider)) {
    if (provider !== "gpt-sovits" && !String(getSettings().ttsEndpoint || "").trim()) {
      return Promise.resolve(failLocalSpeech(new Error("本地 TTS Endpoint 为空"), generation, "本地语音配置错误"));
    }
    recordVoiceDiagnostic("local-request", "正在调用本地语音模型", { generation, provider });
    return startLocalTts(cleanText, voiceStyle, speechState.voiceTone, speechState.voiceIntensity, speechState.speechSpeed, speechState.speechPace, generation, speechState.semanticUnits, speechRequestController.signal).then(() => true).catch(error => {
      if (isSpeechCancellation(error) || generation !== speechGeneration) {
        recordVoiceDiagnostic("cancelled", "语音请求被更新的回复或互动取消", { generation });
        return null;
      }
      return failLocalSpeech(error, generation, "本地语音合成失败");
    });
  }

  if (provider === "system") {
    recordVoiceDiagnostic("system-request", "设置中已明确选择系统语音", { generation });
    return startSystemSpeech(cleanText, voiceStyle, duration, generation);
  }

  return Promise.resolve(failLocalSpeech(new Error(`未知 TTS 提供商：${provider || "(空)"}`), generation, "语音配置错误"));
}

function semanticSpeechUnits(text) {
  const value = stripEmojiForSpeech(text);
  if (!value || !/[\p{L}\p{N}]/u.test(value)) return [];
  // The model's speech_unit records are the only authority for visible
  // segmentation. Punctuation, including ellipses, remains inside this
  // atomic fallback unit.
  return [value];
}

function validatedSemanticSpeechUnits(text, supplied = null) {
  const value = stripEmojiForSpeech(text);
  if (!Array.isArray(supplied)) return semanticSpeechUnits(value);
  const units = supplied.map(stripEmojiForSpeech).filter(item => /[\p{L}\p{N}]/u.test(item));
  if (!units.length) {
    throw new Error("语义发声单元包含空内容");
  }
  const compact = input => String(input || "").replace(/\s+/g, "");
  if (compact(units.join("")) !== compact(value)) {
    throw new Error("语义发声单元没有完整覆盖待合成文本");
  }
  return units;
}

function speechSegmentMeta(meta, segment, index, total, preparedTts = null) {
  return {
    ...meta,
    spokenTextJa: segment,
    preparedTts,
    pipelineSegmentIndex: index + 1,
    pipelineSegmentTotal: total,
    semanticUnits: [segment],
  };
}

function buildSpeechSegmentTtsRequest(segment, voiceStyle, meta) {
  const request = buildTtsRequest(
    segment,
    voiceStyle,
    meta.voiceTone || "neutral",
    meta.voiceIntensity || "normal",
    clamp(toNumber(meta.mood?.emotionIntensity, 1), 0.2, 2.5),
    meta.speechSpeed || 1,
    meta.speechPace || "normal",
    [segment],
  );
  return {
    ...request,
    speechLanguage: normalizeSpeechLanguage(meta.speechLanguage || request.speechLanguage),
    locale: normalizeSpeechLanguage(meta.speechLanguage || request.speechLanguage),
    speechProsody: normalizeSpeechProsody(meta.speechProsody || request.speechProsody),
    requestId: String(meta.requestId || ""),
    utteranceId: String(meta.utteranceId || ""),
    segmentIndex: Math.max(1, Math.trunc(toNumber(meta.segmentIndex, 1))),
    segmentTotal: Math.max(1, Math.trunc(toNumber(meta.segmentTotal, 1))),
  };
}

// 预计 endsAt 只用于口型和看门狗预算；分段交接必须等真实 Audio 结束事件。
async function waitForSpeechSegmentEnd(generation, pipelineToken) {
  const waitStartedAt = clock.now();
  let audioBarrierReported = false;
  while (pipelineToken === speechPipelineGeneration && generation === speechGeneration) {
    const activeAudio = ttsAudioGeneration === generation ? ttsAudio : null;
    if (activeAudio && !activeAudio.ended && !activeAudio.error) {
      if (!audioBarrierReported) {
        audioBarrierReported = true;
        recordVoiceDiagnostic(
          "playback-barrier-waiting",
          "正在等待上一句真实音频播放结束，下一句不会抢播",
          { generation, currentTime: Number(activeAudio.currentTime || 0) },
        );
      }
      const durationMs = Number.isFinite(activeAudio.duration) && activeAudio.duration > 0
        ? activeAudio.duration * 1000
        : Math.max(30000, estimateSpeechDuration(
          speechState.text,
          speechState.voiceStyle,
          speechState.speechSpeed,
        ) + 20000);
      if (clock.now() - waitStartedAt > durationMs + 12000) {
        recordVoiceDiagnostic(
          "playback-barrier-watchdog",
          "上一句播放器没有发出结束事件，已先明确停止旧音频再继续",
          {
            generation,
            duration: Number.isFinite(activeAudio.duration) ? activeAudio.duration : null,
            currentTime: Number(activeAudio.currentTime || 0),
          },
        );
        stopLocalTts(true);
        speechState.pending = false;
        speechState.active = false;
        return true;
      }
      await delay(60);
      continue;
    }
    if (!speechState.pending && !isSpeaking(clock.now())) return true;
    await delay(60);
  }
  return false;
}

const SPEECH_BOUNDARY_PAUSE_MS = Object.freeze({
  continue: 0,
  micro_pause: 110,
  phrase_pause: 260,
  turn_pause: 420,
});

function normalizeSpeechBoundary(value, fallback = "continue") {
  const candidate = String(value || "").trim();
  return Object.prototype.hasOwnProperty.call(SPEECH_BOUNDARY_PAUSE_MS, candidate)
    ? candidate
    : fallback;
}

function speechBoundaryPauseMs(boundaryAfter, speechPace = "normal") {
  const boundary = normalizeSpeechBoundary(boundaryAfter);
  const base = SPEECH_BOUNDARY_PAUSE_MS[boundary] || 0;
  const pace = normalizeSpeechPace(speechPace);
  const paceFactor = {
    very_slow: 1.28,
    slow: 1.14,
    normal: 1,
    fast: 0.86,
    very_fast: 0.72,
  }[pace] || 1;
  return Math.round(base * paceFactor);
}

async function waitForSpeechBoundary(boundaryAfter, speechPace, generation, pipelineToken) {
  const pauseMs = speechBoundaryPauseMs(boundaryAfter, speechPace);
  if (pauseMs <= 0) return true;
  recordVoiceDiagnostic("pipeline-boundary-pause", `句间停顿 ${pauseMs} ms`, {
    boundaryAfter: normalizeSpeechBoundary(boundaryAfter),
    pauseMs,
    pipelineToken,
  });
  const deadline = clock.now() + pauseMs;
  while (clock.now() < deadline) {
    if (pipelineToken !== speechPipelineGeneration || generation !== speechGeneration) {
      return false;
    }
    await delay(Math.min(40, Math.max(1, deadline - clock.now())));
  }
  return pipelineToken === speechPipelineGeneration && generation === speechGeneration;
}

async function continueSegmentedSpeech(segments, voiceStyle, meta, pipelineToken) {
  try {
    for (let index = 1; index < segments.length; index += 1) {
      if (pipelineToken !== speechPipelineGeneration) return;
      const segment = segments[index];
      const controller = speechPipelineAbortController;
      const request = buildSpeechSegmentTtsRequest(segment, voiceStyle, meta);
      const preparedState = { ready: false, payload: null, error: null };
      preparedState.promise = requestPreparedTts(request, null, controller?.signal)
        .then(payload => {
          preparedState.ready = true;
          preparedState.payload = payload;
          recordVoiceDiagnostic("pipeline-prefetch-ready", `第 ${index + 1}/${segments.length} 段已在后台合成完成`, {
            pipelineToken,
            segmentIndex: index + 1,
            segmentTotal: segments.length,
          });
          return payload;
        })
        .catch(error => {
          preparedState.ready = true;
          preparedState.error = error;
          return null;
        });
      recordVoiceDiagnostic("pipeline-prefetch", `播放第 ${index}/${segments.length} 段时预合成第 ${index + 1} 段`, {
        pipelineToken,
        segmentIndex: index + 1,
        segmentTotal: segments.length,
      });

      const currentGeneration = speechState.generation;
      if (!await waitForSpeechSegmentEnd(currentGeneration, pipelineToken)) return;
      const previousBoundary = Array.isArray(meta.pipelineBoundaries)
        ? meta.pipelineBoundaries[index - 1]
        : "phrase_pause";
      if (!await waitForSpeechBoundary(
        previousBoundary,
        meta.speechPace,
        currentGeneration,
        pipelineToken,
      )) return;
      if (pipelineToken !== speechPipelineGeneration) return;
      if (!preparedState.ready) {
        speechGapIndicatorGeneration = -pipelineToken;
        showThinkingIndicator(
          speechGapIndicatorGeneration,
          "voice-wait",
          `第 ${index}/${segments.length} 段已经播完，正在完成第 ${index + 1} 段语音`,
        );
      }
      await preparedState.promise;
      if (pipelineToken !== speechPipelineGeneration) return;
      if (preparedState.error && ttsRetryWouldDuplicateWork(preparedState.error)) {
        if (speechGapIndicatorGeneration) {
          hideThinkingIndicator(speechGapIndicatorGeneration);
          speechGapIndicatorGeneration = 0;
        }
        failLocalSpeech(preparedState.error, speechGeneration, "后续分段语音合成超时");
        meta.onPipelineSegmentFailed?.(
          index,
          meta.pipelineDisplaySegments?.[index] || "",
          segment,
          segments.length,
        );
        return;
      }
      if (preparedState.error && !speechGapIndicatorGeneration) {
        speechGapIndicatorGeneration = -pipelineToken;
        showThinkingIndicator(
          speechGapIndicatorGeneration,
          "voice-wait",
          `第 ${index}/${segments.length} 段已经播完，正在重试第 ${index + 1} 段语音`,
        );
      }
      const preparedTts = preparedState.payload ? { key: preparedTtsKey(request), payload: preparedState.payload } : null;
      const started = await startSpeech(
        segment,
        voiceStyle,
        speechSegmentMeta(meta, segment, index, segments.length, preparedTts),
      );
      if (speechGapIndicatorGeneration) {
        hideThinkingIndicator(speechGapIndicatorGeneration);
        speechGapIndicatorGeneration = 0;
      }
      if (pipelineToken !== speechPipelineGeneration) return;
      if (!started) {
        meta.onPipelineSegmentFailed?.(
          index,
          meta.pipelineDisplaySegments?.[index] || "",
          segment,
          segments.length,
        );
        return;
      }
      meta.onPipelineSegmentStarted?.(
        index,
        meta.pipelineDisplaySegments?.[index] || "",
        segment,
        segments.length,
      );
      updateThinkingIndicatorStage("voice-prefetch", `正在播放第 ${index + 1}/${segments.length} 段`);
    }
  } finally {
    if (pipelineToken === speechPipelineGeneration) {
      speechPipelineActive = false;
      speechPipelineAbortController = null;
    }
  }
}

function startSegmentedSpeech(text, voiceStyle = "calm", meta = {}) {
  const provider = String(getSettings().ttsProvider || "").trim().toLowerCase();
  const suppliedSegments = Array.isArray(meta.pipelineSpeechSegments)
    ? meta.pipelineSpeechSegments.map(item => String(item || "").trim()).filter(Boolean)
    : [];
  const segments = getSettings().voiceEnabled && provider === "gpt-sovits"
    ? (suppliedSegments.length ? suppliedSegments : [String(text || "").trim()])
    : [String(text || "").trim()];
  if (segments.length <= 1) return startSpeech(text, voiceStyle, meta);
  const pipelineMeta = {
    ...meta,
    pipelineBoundaries: segments.map((_segment, index) => normalizeSpeechBoundary(
      Array.isArray(meta.pipelineBoundaries) ? meta.pipelineBoundaries[index] : "",
      index === segments.length - 1 ? "turn_pause" : "phrase_pause",
    )),
  };

  speechPipelineAbortController?.abort();
  speechPipelineAbortController = new AbortController();
  const pipelineToken = ++speechPipelineGeneration;
  speechPipelineActive = true;
  updateThinkingIndicatorStage("speech-segmenting", `已按语义切为 ${segments.length} 段`);
  recordVoiceDiagnostic("pipeline-segmented", `长回复已切为 ${segments.length} 段，首段优先`, {
    pipelineToken,
    segmentTotal: segments.length,
    segmentLengths: segments.map(segment => segment.length),
  });
  updateThinkingIndicatorStage("voice-first", `正在合成第 1/${segments.length} 段`);
  const firstStarted = startSpeech(
    segments[0],
    voiceStyle,
    speechSegmentMeta(pipelineMeta, segments[0], 0, segments.length, meta.preparedTts || null),
  );
  firstStarted.then(started => {
    if (!started || pipelineToken !== speechPipelineGeneration) {
      if (started === false && pipelineToken === speechPipelineGeneration) {
        meta.onPipelineSegmentFailed?.(
          0,
          meta.pipelineDisplaySegments?.[0] || "",
          segments[0],
          segments.length,
        );
      }
      if (pipelineToken === speechPipelineGeneration) speechPipelineActive = false;
      return;
    }
    meta.onPipelineSegmentStarted?.(
      0,
      meta.pipelineDisplaySegments?.[0] || "",
      segments[0],
      segments.length,
    );
    updateThinkingIndicatorStage("voice-playing", `第 1/${segments.length} 段已开始播放`);
    continueSegmentedSpeech(segments, voiceStyle, pipelineMeta, pipelineToken).catch(error => {
      if (isSpeechCancellation(error) || pipelineToken !== speechPipelineGeneration) return;
      recordVoiceDiagnostic("pipeline-error", `分段语音流水线中止：${error.message}`, { pipelineToken });
    });
  });
  return firstStarted;
}

function speechCancellationError() {
  const error = new Error("speech superseded");
  error.name = "SpeechCanceledError";
  return error;
}

function isSpeechCancellation(error) {
  return error?.name === "SpeechCanceledError" || error?.name === "AbortError";
}


function assertCurrentSpeech(generation) {
  if (generation !== speechGeneration || speechState.generation !== generation) {
    throw speechCancellationError();
  }
}

function stopCurrentPlayback() {
  if (platform.hasSystemSpeech()) platform.speechSynthesis().cancel();
  stopLocalTts(true);
}

function interruptCurrentSpeech() {
  recordVoiceDiagnostic("cancelled", "当前语音被新的互动中断", { kind: speechState.kind, generation: speechState.generation });
  speechGeneration += 1;
  speechPipelineGeneration += 1;
  speechPipelineAbortController?.abort();
  speechPipelineAbortController = null;
  speechRequestAbortController?.abort();
  speechRequestAbortController = null;
  speechPipelineActive = false;
  if (speechGapIndicatorGeneration) {
    hideThinkingIndicator(speechGapIndicatorGeneration);
    speechGapIndicatorGeneration = 0;
  }
  stopCurrentPlayback();
  speechState.active = false;
  speechState.pending = false;
  speechState.textRevealPending = false;
  speechState.endsAt = 0;
  speechState.kind = "idle";
  speechState.systemSpeechActive = false;
  speechState.systemSpeechDeadline = 0;
  speakUntil = 0;
  resetAnimeMouthState();
  syncChatSubmitButton();
}


function beginEstimatedSpeech(duration) {
  const now = clock.now();
  resetAnimeMouthState();
  applySpeechMood();
  speechState.active = true;
  speechState.pending = false;
  speechState.textRevealPending = false;
  speechState.startedAt = now;
  speechState.endsAt = now + duration;
  speakUntil = speechState.endsAt;
  protectCurrentExpressionThroughSpeech(speechState.endsAt);
}

function beginAudioSpeech(audio, fallbackText) {
  const now = clock.now();
  resetAnimeMouthState();
  applySpeechMood();
  const duration = Number.isFinite(audio.duration) && audio.duration > 0
    ? audio.duration * 1000
    : estimateSpeechDuration(fallbackText || speechState.text, speechState.voiceStyle, speechState.speechSpeed);
  speechState.active = true;
  speechState.pending = false;
  speechState.textRevealPending = false;
  speechState.startedAt = now - audio.currentTime * 1000;
  speechState.endsAt = speechState.startedAt + duration;
  speechState.charIndex = timelineIndexAt(audio.currentTime || 0);
  speechState.ttsMetrics = {
    ...(speechState.ttsMetrics || {}),
    audioPlayMs: Math.round(now - speechState.queuedAt),
    clientTtfvMs: speechState.requestStartedAt > 0 ? Math.round(now - speechState.requestStartedAt) : null,
  };
  speakUntil = speechState.endsAt;
  protectCurrentExpressionThroughSpeech(speechState.endsAt);
}

function applySpeechMood() {
  const mood = speechState.mood;
  if (!mood) return;
  if (!speechState.moodApplied) {
    setEmotion(mood.emotion, mood.gesture, mood);
    speechState.moodApplied = true;
  }
}

async function startSystemSpeech(cleanText, voiceStyle, duration, generation = speechGeneration) {
  if (generation !== speechGeneration) return null;
  const configuredProvider = String(getSettings().ttsProvider || "").trim().toLowerCase();
  const queuedProvider = String(speechState.provider || "").trim().toLowerCase();
  if (configuredProvider !== "system" || queuedProvider !== "system") {
    return failLocalSpeech(new Error("已阻止非显式的系统语音调用"), generation, "系统音回退已拦截");
  }
  if (!platform.hasSystemSpeech()) {
    speechState.pending = false;
    speechState.textRevealPending = false;
    recordVoiceDiagnostic("system-unavailable", "当前 WebView 没有可用的系统语音");
    return false;
  }

  let startTimeout = 0;
  let settleStarted;
  const started = new Promise(resolve => {
    let settled = false;
    settleStarted = value => {
      if (settled) return;
      settled = true;
      clearTimeout(startTimeout);
      resolve(value);
    };
  });

  const utterance = platform.createUtterance(cleanText);
  utterance.lang = getSettings().voiceLocale || "ja-JP";
  utterance.rate = clamp(getSettings().speechRate * speechState.speechSpeed * voiceRateForStyle(voiceStyle), 0.55, 1.65);
  utterance.pitch = clamp(getSettings().voicePitch * voicePitchForStyle(voiceStyle), 0.75, 1.75);
  utterance.volume = 1;
  const voice = platform.selectVoice();
  if (voice) {
    utterance.voice = voice;
    utterance.lang = voice.lang || utterance.lang;
  }
  utterance.onboundary = event => {
    if (generation !== speechGeneration) return;
    if (typeof event.charIndex === "number") {
      speechState.charIndex = clamp(event.charIndex, 0, cleanText.length - 1);
    }
  };
  utterance.onstart = () => {
    if (generation !== speechGeneration) return settleStarted(null);
    speechState.systemSpeechActive = true;
    speechState.systemSpeechDeadline = clock.now() + duration * 2 + 3000;
    beginEstimatedSpeech(duration);
    recordVoiceDiagnostic("system-playing", "系统语音已经开始播放", { generation });
    settleStarted(true);
  };
  utterance.onend = () => {
    if (generation !== speechGeneration) return;
    speechState.systemSpeechActive = false;
    speechState.active = false;
    speechState.endsAt = Math.min(speechState.endsAt, clock.now() + 180);
    speakUntil = speechState.endsAt;
    protectCurrentExpressionThroughSpeech(clock.now());
    recordVoiceDiagnostic("ended", "系统语音播放完成", { generation });
    syncChatSubmitButton();
  };
  utterance.onerror = () => {
    if (generation !== speechGeneration) return settleStarted(null);
    speechState.systemSpeechActive = false;
    speechState.pending = false;
    speechState.textRevealPending = false;
    speechState.endsAt = clock.now() + Math.min(1400, duration * 0.35);
    speakUntil = speechState.endsAt;
    recordVoiceDiagnostic("system-error", "系统语音播放失败", { generation });
    syncChatSubmitButton();
    settleStarted(false);
  };
  speechState.utterance = utterance;
  startTimeout = clock.setTimeout(() => {
    if (generation !== speechGeneration) return settleStarted(null);
    speechState.pending = false;
    speechState.textRevealPending = false;
    recordVoiceDiagnostic("system-timeout", "系统语音未能开始播放，文字已在失败确认后显示", { generation });
    syncChatSubmitButton();
    settleStarted(false);
  }, 12000);
  platform.speechSynthesis().speak(utterance);
  return started;
}

function buildTtsRequest(cleanText, voiceStyle, voiceTone = "neutral", voiceIntensity = "normal", emotionIntensity = 1, speechSpeed = 1, speechPace = "normal", semanticUnits = null) {
  const requestedText = stripEmojiForSpeech(cleanText);
  return {
    text: requestedText,
    voiceStyle: String(voiceStyle || "calm"),
    voiceTone: String(voiceTone || "neutral"),
    voiceIntensity: normalizeVoiceIntensity(voiceIntensity),
    emotionIntensity: clamp(toNumber(emotionIntensity, 1), 0.2, 2.5),
    // The settings slider is a real global speed control for local GPT-SoVITS too.
    // Keep this multiplication at the transport boundary so browser system TTS,
    // which already applies getSettings().speechRate, is not accelerated twice.
    speechSpeed: clamp(toNumber(speechSpeed, 1) * getSettings().speechRate, 0.62, 1.45),
    speechPace: normalizeSpeechPace(speechPace),
    speechLanguage: speechLanguageProfile().speechLanguage,
    speechProsody: normalizeSpeechProsody(getSettings().speechProsody),
    locale: speechLanguageProfile().locale,
    // The model decides the user-visible delivery count. GPT-SoVITS may still
    // use stable internal inference chunks, but it must join them into this
    // one requested WAV instead of exposing extra playback segments.
    segmentMode: "joined",
    // These are required speech acts, not optional acoustic hints. The TTS
    // service may split a long unit internally, but it may not merge across a
    // unit boundary or omit a failed tail.
    semanticUnits: validatedSemanticSpeechUnits(requestedText, semanticUnits),
  };
}

function preparedTtsKey(ttsRequest) {
  const {
    requestId: _requestId,
    utteranceId: _utteranceId,
    segmentIndex: _segmentIndex,
    segmentTotal: _segmentTotal,
    ...synthesisRequest
  } = ttsRequest || {};
  return JSON.stringify({
    ...synthesisRequest,
    provider: String(getSettings().ttsProvider || "").trim().toLowerCase(),
    preset: String(getSettings().gptSovitsModelPreset || ""),
    endpoint: String(getSettings().ttsEndpoint || ""),
  });
}

async function requestPreparedTts(ttsRequest, generation = null, signal = null) {
  const prepared = await gateway.fetch("/api/tts-prepare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(ttsRequest),
    ...(signal ? { signal } : {}),
  });
  if (generation !== null) assertCurrentSpeech(generation);
  if (!prepared.ok) {
    const rawError = await prepared.text();
    let errorPayload = null;
    try {
      errorPayload = JSON.parse(rawError);
    } catch {
      errorPayload = null;
    }
    const error = new Error(errorPayload?.error || rawError || `HTTP ${prepared.status}`);
    error.ttsDiagnostics = errorPayload?.diagnostics && typeof errorPayload.diagnostics === "object"
      ? errorPayload.diagnostics
      : null;
    throw error;
  }
  const payload = await prepared.json();
  if (generation !== null) assertCurrentSpeech(generation);
  if (!payload.audioBase64) throw new Error("prepared TTS returned no audio");
  return payload;
}

function ttsRetryWouldDuplicateWork(error) {
  return /timeout|timed out|超时|http\s*50[234]|service unavailable|busy|tts_incomplete_audio|"deterministic"\s*:\s*true|produced no audio after retries|(?:segment|inference chunk) \d+\/\d+ produced no audio/i.test(String(error?.message || error || ""));
}

// 本地语音可在本地接口间按原规则回退；播放失败交给调用方转文字，不偷换成系统语音。
async function startLocalTts(cleanText, voiceStyle, voiceTone = "neutral", voiceIntensity = "normal", speechSpeed = 1, speechPace = "normal", generation = speechGeneration, semanticUnits = null, signal = null) {
  let blob;
  let alignment = null;
  let alignmentId = "";
  let retryBlockedError = null;
  const emotionIntensity = clamp(toNumber(speechState.mood?.emotionIntensity, 1), 0.2, 2.5);
  const ttsRequest = buildTtsRequest(cleanText, voiceStyle, voiceTone, voiceIntensity, emotionIntensity, speechSpeed, speechPace, semanticUnits);
  const originalAudio = speechState.originalAudio;
  if (originalAudio?.audioBase64) {
    blob = base64ToBlob(originalAudio.audioBase64, originalAudio.contentType || "audio/wav");
    speechState.ttsMetrics = {
      source: "original_clip",
      originalClipId: String(originalAudio.id || ""),
      voiceId: String(originalAudio.voiceId || ""),
      audioBytes: Math.floor(String(originalAudio.audioBase64).length * 0.75),
    };
    recordVoiceDiagnostic("original-clip-ready", "本轮小概率使用与显示文字绑定的原作语音", {
      generation,
      originalClipId: speechState.ttsMetrics.originalClipId,
      voiceId: speechState.ttsMetrics.voiceId,
      audioBytes: speechState.ttsMetrics.audioBytes,
    });
  }
  if (!blob && String(getSettings().ttsProvider || "").toLowerCase() === "gpt-sovits") {
    try {
      const cached = speechState.preparedTts;
      const cacheHit = Boolean(cached?.payload?.audioBase64)
        && cached.key === preparedTtsKey(ttsRequest);
      const payload = cacheHit
        ? cached.payload
        : await requestPreparedTts(ttsRequest, generation, signal);
      speechState.preparedTts = null;
      assertCurrentSpeech(generation);
      blob = base64ToBlob(payload.audioBase64, payload.contentType || "audio/wav");
      alignmentId = String(payload.alignmentId || "");
      alignment = payload.alignment || null;
      speechState.ttsMetrics = payload.metrics || null;
      recordVoiceDiagnostic(cacheHit ? "startup-cache-hit" : "local-audio-ready", cacheHit
        ? "直接使用启动动画期间预合成的开机问候音频"
        : "本地语音与动漫预估口型已生成，Rhubarb 正在后台校正", {
        generation,
        metrics: speechState.ttsMetrics,
        alignmentId,
        alignmentSource: alignment?.source || "none",
      });
    } catch (error) {
      if (isSpeechCancellation(error)) throw error;
      if (error?.ttsDiagnostics) speechState.ttsMetrics = error.ttsDiagnostics;
      if (ttsRetryWouldDuplicateWork(error)) retryBlockedError = error;
      console.warn("Deferred TTS failed, falling back to timed TTS:", error);
      recordVoiceDiagnostic("deferred-failed", `两阶段语音接口不可用，正在回退到完整时间轴接口：${error.message}`, {
        generation,
        metrics: speechState.ttsMetrics,
      });
    }
    if (!blob && !retryBlockedError) {
      try {
        const timed = await gateway.fetch("/api/tts-timed", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(ttsRequest),
          ...(signal ? { signal } : {}),
        });
        assertCurrentSpeech(generation);
        if (!timed.ok) throw new Error(await timed.text() || `HTTP ${timed.status}`);
        const payload = await timed.json();
        assertCurrentSpeech(generation);
        if (!payload.audioBase64) throw new Error("timed TTS returned no audio");
        blob = base64ToBlob(payload.audioBase64, payload.contentType || "audio/wav");
        alignment = payload.alignment || null;
        speechState.ttsMetrics = payload.metrics || null;
        recordVoiceDiagnostic("local-ready", "本地语音与口型时间轴已生成（兼容路径）", {
          generation,
          metrics: speechState.ttsMetrics,
          alignmentSource: alignment?.source || "none",
        });
      } catch (error) {
        if (isSpeechCancellation(error)) throw error;
        if (ttsRetryWouldDuplicateWork(error)) retryBlockedError = error;
        console.warn("Timed TTS failed, falling back to raw audio:", error);
        recordVoiceDiagnostic("timed-failed", `带时间轴的语音生成失败，正在重试原始音频：${error.message}`, { generation });
      }
    }
  }
  if (!blob && retryBlockedError) {
    recordVoiceDiagnostic("retry-blocked", "上一次合成仍在运行或已明确拒绝残缺音频，已阻止重复提交相同文本", {
      generation,
      textLength: cleanText.length,
    });
    throw retryBlockedError;
  }
  if (!blob) {
    recordVoiceDiagnostic("raw-retry", "正在重新调用本地模型生成原始音频", { generation });
    const response = await gateway.fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ttsRequest),
      ...(signal ? { signal } : {}),
    });
    assertCurrentSpeech(generation);
    if (!response.ok) {
      const message = await response.text();
      throw new Error(message || `HTTP ${response.status}`);
    }
    blob = await response.blob();
    assertCurrentSpeech(generation);
    recordVoiceDiagnostic("raw-ready", "本地模型原始音频生成成功（无精细时间轴）", { generation });
  }
  assertCurrentSpeech(generation);
  speechState.timeline = Array.isArray(alignment?.items) ? alignment.items : [];
  speechState.mouthCues = Array.isArray(alignment?.mouthCues) ? alignment.mouthCues : [];
  speechState.alignmentSource = alignment?.source || "";
  const url = platform.createObjectURL(blob);
  const audio = platform.createAudio(url);
  ttsAudio = audio;
  ttsAudioUrl = url;
  ttsAudioGeneration = generation;

  audio.onloadedmetadata = () => {
    if (generation !== speechGeneration) return;
    if (speechState.active && Number.isFinite(audio.duration) && audio.duration > 0) {
      speechState.endsAt = speechState.startedAt + audio.duration * 1000;
      speakUntil = speechState.endsAt;
      protectCurrentExpressionThroughSpeech(speechState.endsAt);
    }
  };
  audio.ontimeupdate = () => {
    if (generation !== speechGeneration) return;
    if (Number.isFinite(audio.duration) && audio.duration > 0) {
      speechState.charIndex = timelineIndexAt(audio.currentTime);
    }
  };
  audio.onended = () => {
    if (generation !== speechGeneration) return;
    if (ttsAudio === audio) {
      ttsAudio = null;
      ttsAudioGeneration = 0;
    }
    releaseTtsAudioUrl(url);
    audioMouthLevel = 0;
    speechState.active = false;
    speechState.endsAt = clock.now() + 180;
    speakUntil = speechState.endsAt;
    protectCurrentExpressionThroughSpeech(clock.now());
    recordVoiceDiagnostic("ended", "本地语音播放完成", { generation });
    syncChatSubmitButton();
  };
  audio.onerror = () => {
    if (generation !== speechGeneration) return;
    if (ttsAudio === audio) {
      ttsAudio = null;
      ttsAudioGeneration = 0;
    }
    releaseTtsAudioUrl(url);
    audioMouthLevel = 0;
    failLocalSpeech(new Error("本地 WAV 已生成，但 WebView 播放失败"), generation, "本地语音播放失败");
    syncChatSubmitButton();
  };
  connectAudioAnalyser(audio);
  if (audioContext?.state === "suspended") {
    await audioContext.resume();
  }
  assertCurrentSpeech(generation);
  await audio.play();
  assertCurrentSpeech(generation);
  beginAudioSpeech(audio, cleanText);
  clearVoiceFailure();
  recordVoiceDiagnostic("local-playing", "GPT-SoVITS 已开始播放", {
    generation,
    duration: Number.isFinite(audio.duration) ? Number(audio.duration.toFixed(3)) : null,
    metrics: speechState.ttsMetrics,
  });
  if (alignmentId) {
    pollDeferredAlignment(alignmentId, generation, signal).catch(error => {
      if (isSpeechCancellation(error) || generation !== speechGeneration) return;
      recordVoiceDiagnostic("alignment-failed", `精细口型对齐失败，本轮继续使用实时音量嘴型：${error.message}`, { generation });
    });
  }
}

async function pollDeferredAlignment(alignmentId, generation, signal = null) {
  const startedAt = clock.now();
  for (let attempt = 0; attempt < 100; attempt += 1) {
    assertCurrentSpeech(generation);
    const response = await gateway.fetch("/api/tts-alignment", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alignmentId }),
      ...(signal ? { signal } : {}),
    });
    assertCurrentSpeech(generation);
    if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
    const payload = await response.json();
    assertCurrentSpeech(generation);
    if (payload.status === "ready" && payload.alignment) {
      const nextAlignment = payload.alignment;
      speechState.timeline = Array.isArray(nextAlignment.items) ? nextAlignment.items : [];
      speechState.mouthCues = Array.isArray(nextAlignment.mouthCues) ? nextAlignment.mouthCues : [];
      speechState.alignmentSource = nextAlignment.source || "deferred";
      speechState.ttsMetrics = {
        ...(speechState.ttsMetrics || {}),
        alignmentMs: Number(payload.alignmentMs || 0),
        alignmentReadyMs: Math.round(clock.now() - startedAt),
        alignmentDeferred: true,
      };
      recordVoiceDiagnostic("alignment-ready", "精细口型时间轴已接管实时音量嘴型", {
        generation,
        alignmentSource: speechState.alignmentSource,
        metrics: speechState.ttsMetrics,
      });
      return;
    }
    if (payload.status === "failed") throw new Error(payload.error || "alignment failed");
    await clock.delay(120);
  }
  throw new Error("alignment timeout");
}

function stopLocalTts(preserveSpeechState = false) {
  if (ttsAudio) {
    try {
      ttsAudio.pause();
      ttsAudio.removeAttribute("src");
      ttsAudio.load();
    } catch {
      // Audio cleanup is best effort inside WebView.
    }
  }
  ttsAudio = null;
  ttsAudioGeneration = 0;
  releaseTtsAudioUrl();
  audioMouthLevel = 0;
  if (!preserveSpeechState) speechState.pending = false;
  resetAnimeMouthState();
}

function releaseTtsAudioUrl(expectedUrl = "") {
  if (!ttsAudioUrl || (expectedUrl && expectedUrl !== ttsAudioUrl)) return;
  platform.revokeObjectURL(ttsAudioUrl);
  ttsAudioUrl = "";
}

function base64ToBlob(base64, contentType) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new Blob([bytes], { type: contentType || "audio/wav" });
}

function timelineIndexAt(seconds) {
  const timeline = Array.isArray(speechState.timeline) ? speechState.timeline : [];
  if (timeline.length) {
    const index = timeline.findIndex(item => seconds >= Number(item.start || 0) && seconds < Number(item.end || 0));
    if (index >= 0) return index;
    if (seconds >= Number(timeline[timeline.length - 1].end || 0)) return timeline.length - 1;
    return 0;
  }
  const duration = ttsAudio && Number.isFinite(ttsAudio.duration) && ttsAudio.duration > 0
    ? ttsAudio.duration
    : Math.max(0.001, (speechState.endsAt - speechState.startedAt) / 1000);
  return clamp(Math.floor((seconds / duration) * speechState.text.length), 0, Math.max(0, speechState.text.length - 1));
}

function connectAudioAnalyser(audio) {
  try {
    audioContext = audioContext || platform.createAudioContext();
    const source = audioContext.createMediaElementSource(audio);
    audioAnalyser = audioContext.createAnalyser();
    audioAnalyser.fftSize = 512;
    audioSamples = new Uint8Array(audioAnalyser.fftSize);
    source.connect(audioAnalyser);
    audioAnalyser.connect(audioContext.destination);
  } catch (error) {
    console.warn("Audio analyser unavailable:", error);
    audioAnalyser = null;
    audioSamples = null;
  }
}

function estimateSpeechDuration(text, voiceStyle = "calm", speechSpeed = 1) {
  const chars = [...String(text).replace(/\s+/g, "")];
  const punctuation = chars.filter(ch => /[，。！？、；：,.!?;:]/.test(ch)).length;
  const weighted = chars.length + punctuation * 4;
  const styleFactor = voiceStyle === "energetic" ? 0.88 : voiceStyle === "sharp" ? 0.94 : voiceStyle === "soft" ? 1.12 : 1;
  return clamp((weighted * 105 * styleFactor) / Math.max(0.55, getSettings().speechRate * speechSpeed), 1000, 16000);
}

function voiceRateForStyle(style) {
  if (style === "energetic") return 1.08;
  if (style === "sharp") return 1.04;
  if (style === "soft") return 0.9;
  if (style === "teasing") return 1.02;
  return 0.98;
}

function voicePitchForStyle(style) {
  if (style === "energetic") return 1.06;
  if (style === "sharp") return 1.02;
  if (style === "soft") return 0.96;
  if (style === "teasing") return 1.08;
  return 1;
}

function isSpeaking(now = clock.now()) {
  if (!speechState.active) return false;
  if (speechState.systemSpeechActive && now < speechState.systemSpeechDeadline) return true;
  if (speechState.systemSpeechActive) speechState.systemSpeechActive = false;
  if (now <= speechState.endsAt) return true;
  speechState.active = false;
  return now < speakUntil;
}

function updateAudioMouthLevel() {
  if (!audioAnalyser || !audioSamples || !ttsAudio || ttsAudio.paused) {
    audioMouthLevel *= 0.82;
    return;
  }
  audioAnalyser.getByteTimeDomainData(audioSamples);
  let sum = 0;
  for (const sample of audioSamples) {
    const centered = (sample - 128) / 128;
    sum += centered * centered;
  }
  const rms = Math.sqrt(sum / audioSamples.length);
  const target = clamp((rms - 0.016) * 5.6, 0, 0.74);
  audioMouthLevel = audioMouthLevel * 0.68 + target * 0.32;
}

function speechMouthOpen(now = clock.now()) {
  if (!isSpeaking(now) || !speechState.text) return 0;
  const elapsed = now - speechState.startedAt;
  const timedShape = mouthShapeAt(now);
  const char = currentSpeechChar(now);
  const shape = timedShape || mouthShapeForChar(char);
  const pause = /[。！？.!?]/.test(char) ? 0.18 : /[，、；：,;:]/.test(char) ? 0.42 : 1;
  const styleEnergy = speechState.voiceStyle === "energetic" ? 1.12 : speechState.voiceStyle === "sharp" ? 1.05 : speechState.voiceStyle === "soft" ? 0.9 : 1.02;
  const mouthScale = speechState.voiceTone === "flustered" ? 1.48 : speechState.voiceTone === "comforting" ? 1.26 : 1.4;
  const beat = Math.pow(Math.sin(elapsed * 0.033 * getSettings().speechRate), 2);
  const secondBeat = Math.pow(Math.sin(elapsed * 0.019 + 0.8), 2) * 0.045;
  const boundary = animeSpeechBoundaryEnvelope(now);
  const audioDriven = audioMouthLevel > 0.015 ? audioMouthLevel : 0;
  const hasTimedShape = Boolean(timedShape);
  const phonemePulse = hasTimedShape ? 0.98 + beat * 0.035 : 0.78 + beat * 0.2;
  const phonemeDriven = (shape.open * mouthScale * phonemePulse + (hasTimedShape ? secondBeat * 0.32 : secondBeat)) * pause;
  const audioBlend = hasTimedShape ? audioDriven * 0.5 : audioDriven * (0.94 + shape.open * 0.7);
  return clamp(Math.max(audioBlend, phonemeDriven) * styleEnergy * boundary, 0, 0.96);
}

function speechMouthForm(now = clock.now()) {
  if (!isSpeaking(now) || !speechState.text) return 0;
  const shape = mouthShapeAt(now) || mouthShapeForChar(currentSpeechChar(now));
  const elapsed = now - speechState.startedAt;
  const wobble = speechState.mouthCues.length ? 0.006 : 0.028;
  return clamp((shape.form + Math.sin(elapsed * 0.012) * wobble) * animeSpeechBoundaryEnvelope(now), -0.62, 0.62);
}


function mouthShapeAt(now = clock.now()) {
  const cues = Array.isArray(speechState.mouthCues) ? speechState.mouthCues : [];
  if (!cues.length) return null;
  if (animeMouthState.cachedShape && Math.abs(now - animeMouthState.cachedAt) < 12) {
    return animeMouthState.cachedShape;
  }
  const seconds = ttsAudio ? ttsAudio.currentTime : Math.max(0, (now - speechState.startedAt) / 1000);
  const duration = speechAudioDurationSeconds();
  if (seconds <= 0.012 || (duration > 0 && seconds >= duration - 0.035)) {
    const closed = applyMouthEnvelope(animeShapeForGroup("closed"), "closed", 0);
    animeMouthState.shape = lerpMouthShape(animeMouthState.shape, closed, 0.42);
    animeMouthState.cachedAt = now;
    animeMouthState.cachedShape = { ...animeMouthState.shape };
    return animeMouthState.cachedShape;
  }
  const candidateGroup = dominantAnimeViseme(cues, seconds);
  const minHold = animeMouthHoldSeconds();
  const currentGroup = animeMouthState.group || "closed";
  const targetGroup = shouldSwitchAnimeViseme(currentGroup, candidateGroup, seconds, minHold)
    ? candidateGroup
    : currentGroup;

  if (targetGroup !== currentGroup) {
    animeMouthState.group = targetGroup;
    animeMouthState.lastSwitchAt = seconds;
    animeMouthState.holdUntil = seconds + minHold;
  }

  const baseShape = animeShapeForGroup(animeMouthState.group);
  const envelope = animeMouthEnvelope();
  const targetShape = applyMouthEnvelope(baseShape, animeMouthState.group, envelope, animeSpeechBoundaryEnvelope(now));
  const dt = animeMouthState.lastUpdateAt ? Math.max(0.001, (now - animeMouthState.lastUpdateAt) / 1000) : 0.016;
  const factor = 1 - Math.exp(-dt * animeMouthSmoothingRate());
  animeMouthState.shape = lerpMouthShape(animeMouthState.shape, targetShape, factor);
  animeMouthState.lastUpdateAt = now;
  animeMouthState.cachedAt = now;
  animeMouthState.cachedShape = { ...animeMouthState.shape };
  return animeMouthState.cachedShape;
}

function resetAnimeMouthState() {
  animeMouthState = createAnimeMouthState();
}

function dominantAnimeViseme(cues, seconds) {
  const duration = speechAudioDurationSeconds();
  const edgeWindow = duration > 0 && (seconds < 0.16 || seconds > duration - 0.18);
  const windowSize = edgeWindow ? 0.055 : animeMouthAnalysisWindowSeconds();
  const start = Math.max(0, seconds - windowSize * 0.48);
  const end = duration > 0 ? Math.min(duration, seconds + windowSize * 0.52) : seconds + windowSize * 0.52;
  const weights = {};
  const currentCue = cues.find(item => seconds >= Number(item.start || 0) && seconds < Number(item.end || 0));
  const currentGroup = animeVisemeGroupForCue(String(currentCue?.value || "X"));
  for (const cue of cues) {
    const cueStart = Number(cue.start || 0);
    const cueEnd = Number(cue.end || cueStart);
    if (cueEnd <= start || cueStart >= end) continue;
    const overlap = Math.max(0, Math.min(cueEnd, end) - Math.max(cueStart, start));
    const group = animeVisemeGroupForCue(String(cue.value || "X"));
    weights[group] = (weights[group] || 0) + overlap;
  }
  if (currentGroup) weights[currentGroup] = (weights[currentGroup] || 0) + windowSize * 0.62;
  if (audioMouthLevel < 0.025 && !edgeWindow) return "closed";
  if (currentGroup === "closed" && audioMouthLevel < 0.11 && !edgeWindow) return "closed";
  return Object.entries(weights).sort((a, b) => b[1] - a[1])[0]?.[0] || currentGroup;
}

function shouldSwitchAnimeViseme(currentGroup, nextGroup, seconds, minHold) {
  if (currentGroup === nextGroup) return false;
  const duration = speechAudioDurationSeconds();
  const nearEdge = duration > 0 && (seconds < 0.14 || seconds > duration - 0.16);
  if (nearEdge && nextGroup === "closed") return true;
  if (nearEdge && currentGroup === "closed" && nextGroup !== "closed") return true;
  if (nextGroup === "closed") return seconds - animeMouthState.lastSwitchAt > minHold * 0.7 || audioMouthLevel < 0.065;
  if (currentGroup === "closed" && audioMouthLevel > 0.13) return true;
  const distance = mouthShapeDistance(animeShapeForGroup(currentGroup), animeShapeForGroup(nextGroup));
  if (distance < 0.22) return false;
  if (distance > 0.52 && seconds - animeMouthState.lastSwitchAt > minHold * 0.9) return true;
  return seconds >= animeMouthState.holdUntil;
}

function animeMouthHoldSeconds() {
  const pace = speechState.speechPace || "normal";
  if (pace === "very_slow") return 0.145;
  if (pace === "slow") return 0.124;
  if (pace === "fast" || pace === "very_fast") return 0.082;
  return 0.102;
}

function animeMouthAnalysisWindowSeconds() {
  const pace = speechState.speechPace || "normal";
  if (pace === "very_slow") return 0.18;
  if (pace === "slow") return 0.16;
  if (pace === "fast" || pace === "very_fast") return 0.105;
  return 0.13;
}

function animeMouthSmoothingRate() {
  const pace = speechState.speechPace || "normal";
  if (pace === "very_slow") return 9.5;
  if (pace === "slow") return 11;
  if (pace === "fast" || pace === "very_fast") return 15;
  return 12.5;
}

function speechAudioDurationSeconds() {
  if (ttsAudio && Number.isFinite(ttsAudio.duration) && ttsAudio.duration > 0) return ttsAudio.duration;
  const duration = (speechState.endsAt - speechState.startedAt) / 1000;
  return Number.isFinite(duration) && duration > 0 ? duration : 0;
}

function animeSpeechBoundaryEnvelope(now = clock.now()) {
  if (!isSpeaking(now)) return 0;
  const elapsedMs = ttsAudio ? ttsAudio.currentTime * 1000 : Math.max(0, now - speechState.startedAt);
  const durationMs = speechAudioDurationSeconds() * 1000 || Math.max(1, speechState.endsAt - speechState.startedAt);
  const start = smoothstep(elapsedMs / 96);
  const end = smoothstep((durationMs - elapsedMs) / 145);
  return clamp(start * end, 0, 1);
}

function animeMouthEnvelope() {
  return clamp(audioMouthLevel > 0.012 ? audioMouthLevel : 0, 0, 0.78);
}

function applyMouthEnvelope(shape, group, envelope, boundary = animeSpeechBoundaryEnvelope()) {
  if (group === "closed") {
    return { open: clamp(envelope * 0.12, 0.005, 0.055), form: shape.form };
  }
  const weightedOpen = shape.open * 0.66 + envelope * 0.76;
  const minimumOpen = (group === "small" || group === "pucker" ? 0.24 : 0.34) * boundary;
  return {
    open: clamp(Math.max(minimumOpen, weightedOpen) * boundary, 0.005, 0.9),
    form: shape.form,
  };
}

function smoothstep(value) {
  const x = clamp(value, 0, 1);
  return x * x * (3 - 2 * x);
}

function currentSpeechChar(now = clock.now()) {
  const timeline = Array.isArray(speechState.timeline) ? speechState.timeline : [];
  if (timeline.length) {
    const seconds = ttsAudio ? ttsAudio.currentTime : Math.max(0, (now - speechState.startedAt) / 1000);
    const item = timeline[timelineIndexAt(seconds)];
    return String(item?.text || "");
  }
  const duration = Math.max(1, speechState.endsAt - speechState.startedAt);
  const progress = clamp((now - speechState.startedAt) / duration, 0, 1);
  const estimatedIndex = Math.floor(progress * speechState.text.length);
  const index = Math.max(speechState.charIndex || 0, estimatedIndex);
  return speechState.text[Math.min(speechState.text.length - 1, index)] || "";
}


// 队列只负责预取当前/下一句、播放与取消；消息 DOM、交付回执和工具门由 chat hooks 承接。
function createChatUtterancePlayback(generation, requestStartedAt, requestId = "", hooks = {}) {
  speechPipelineAbortController?.abort();
  speechPipelineAbortController = new AbortController();
  const controller = speechPipelineAbortController;
  const pipelineToken = ++speechPipelineGeneration;
  speechPipelineActive = true;
  hooks.onStart?.(requestId);
  syncChatSubmitButton();
  const queue = [];
  let cursor = 0;
  let closed = false;
  let running = false;
  let wake = null;
  let finalResult = null;
  let turnCompleted = false;
  let textFallbackAfterVoiceFailure = false;
  let pipelineCancelled = false;
  const recordDelivery = (stage, item = null) => hooks.onDelivery?.(stage, item, finalResult);

  const signalQueue = () => {
    if (!wake) return;
    const resolve = wake;
    wake = null;
    resolve();
  };
  const waitForQueue = (pollMs = 0) => new Promise(resolve => {
    let timer = 0;
    const settle = () => {
      if (wake === settle) wake = null;
      if (timer) clock.clearTimeout(timer);
      resolve();
    };
    wake = settle;
    if (pollMs > 0) timer = clock.setTimeout(settle, pollMs);
  });
  const settlePipeline = () => {
    if (pipelineToken !== speechPipelineGeneration) return;
    speechPipelineActive = false;
    speechPipelineAbortController = null;
    hooks.onSettled?.(requestId);
    syncChatSubmitButton();
    // The request owns the thinking indicator. A stream may settle because it
    // is handing off to the complete-response fallback, so playback cleanup
    // must not make an unfinished turn appear idle.
  };
  const markDeliveryPartial = (item, failedIndex) => hooks.onPartialFailure?.(item, failedIndex);
  const reveal = (item, voiceStarted = false) => {
    if (item.revealed) return;
    item.revealed = true;
    hooks.onReveal?.(item, voiceStarted, finalResult, queue.length);
    recordDelivery("text_revealed", item);
  };
  const prepare = item => {
    if (item.preparationStarted) return item.promise;
    item.preparationStarted = true;
    const provider = String(getSettings().ttsProvider || "").trim().toLowerCase();
    if (item.textOnly) {
      item.ready = true;
      item.prepared = null;
      item.promise = Promise.resolve(null);
      return item.promise;
    }
    if (item.originalAudio?.audioBase64) {
      item.ready = true;
      item.prepared = null;
      item.promise = Promise.resolve(null);
      recordVoiceDiagnostic("original-reaction-ready", "称呼反应将直接使用已校验的原作音频", {
        requestId,
        utteranceId: item.utteranceId,
        originalClipId: String(item.originalAudio.id || ""),
      });
      return item.promise;
    }
    if (!getSettings().voiceEnabled || provider !== "gpt-sovits") {
      item.ready = true;
      item.prepared = null;
      item.promise = Promise.resolve(null);
      return;
    }
    const request = buildSpeechSegmentTtsRequest(item.speechText, item.voiceStyle, {
      voiceTone: item.voiceTone,
      voiceIntensity: item.voiceIntensity,
      speechSpeed: item.speechSpeed,
      speechPace: item.speechPace,
      speechLanguage: item.speechLanguage,
      speechProsody: item.speechProsody,
      mood: item,
      requestId,
      utteranceId: item.utteranceId,
      segmentIndex: item.displayIndex + 1,
      segmentTotal: Math.max(
        queue.length,
        item.displayIndex + 1,
        Number(item.visibleSegmentCountAfter) || 1,
      ),
    });
    item.ttsRequest = request;
    item.promise = requestPreparedTts(request, null, controller.signal)
      .then(payload => {
        item.ready = true;
        item.prepared = { key: preparedTtsKey(request), payload };
        return item.prepared;
      })
      .catch(error => {
        item.ready = true;
        if (isSpeechCancellation(error) || controller.signal.aborted) {
          item.cancelled = true;
          item.cancellationReason = String(error?.name || "pipeline_aborted");
          return null;
        }
        item.error = error;
        return null;
      });
  };
  const run = async () => {
    if (running) return;
    running = true;
    try {
      while (pipelineToken === speechPipelineGeneration && !pipelineCancelled) {
        if (cursor >= queue.length) {
          if (closed) break;
          if (
            cursor > 0
            && !speechState.pending
            && !isSpeaking(clock.now())
          ) {
            showThinkingIndicator(
              generation,
              "model-generating",
              "上一句已播完，正在生成或合成下一句",
            );
          }
          await waitForQueue(140);
          continue;
        }
        const item = queue[cursor];
        if (textFallbackAfterVoiceFailure) {
          reveal(item, false);
          recordDelivery("text_revealed_after_audio_failure", item);
          cursor += 1;
          continue;
        }
        prepare(item);
        if (cursor > 0) {
          const currentGeneration = speechState.generation;
          if (!await waitForSpeechSegmentEnd(currentGeneration, pipelineToken)) break;
          const previousItem = queue[cursor - 1];
          if (!await waitForSpeechBoundary(
            previousItem?.boundaryAfter,
            previousItem?.speechPace,
            currentGeneration,
            pipelineToken,
          )) break;
        }
        if (!item.ready) {
          showThinkingIndicator(
            generation,
            "voice-wait",
            cursor === 0 ? "第一条完整语句已生成，正在合成语音" : "上一句已播完，正在完成下一句语音",
          );
        }
        await item.promise;
        if (pipelineToken !== speechPipelineGeneration) break;
        if (item.cancelled || (pipelineCancelled && !item.error)) break;
        if (item.textOnly) {
          reveal(item, false);
          recordDelivery("text_revealed_after_tts_unavailable", item);
          cursor += 1;
          continue;
        }
        if (item.error) {
          textFallbackAfterVoiceFailure = true;
          // A confirmed failure changes the remainder of this turn to text
          // delivery.  Cancel an already-prefetched next segment so it cannot
          // keep the TTS worker and memory busy after speech has been abandoned.
          controller.abort();
          failLocalSpeech(item.error, speechGeneration, `第 ${cursor + 1} 条流式语音合成失败`);
          recordVoiceDiagnostic(
            "utterance-audio-failed-text-fallback",
            `第 ${cursor + 1} 条音频已确认失败，改为提交该段及后续文字`,
            {
              requestId,
              utteranceId: item.utteranceId,
              deterministic: ttsRetryWouldDuplicateWork(item.error),
            },
          );
          recordDelivery("audio_failed", item);
          reveal(item, false);
          recordDelivery("text_revealed_after_audio_failure", item);
          markDeliveryPartial(item, cursor);
          cursor += 1;
          continue;
        }
        const started = await startSpeech(item.speechText, item.voiceStyle, {
          displayText: item.textZh,
          spokenTextJa: item.spokenTextJa,
          speechText: item.speechText,
          speechLanguage: item.speechLanguage,
          speechPace: item.speechPace,
          speechSpeed: item.speechSpeed,
          voiceTone: item.voiceTone,
          voiceIntensity: item.voiceIntensity,
          mood: item,
          preparedTts: item.prepared,
          originalAudio: item.originalAudio,
          // Playback must consume the exact semantic-unit contract used by
          // pre-synthesis. Re-deriving sentence boundaries here changes the
          // cache identity and needlessly synthesizes the same reply again.
          semanticUnits: Array.isArray(item.ttsRequest?.semanticUnits)
            ? item.ttsRequest.semanticUnits
            : [item.speechText],
          kind: "chat",
          requestStartedAt,
          pipelineSegmentIndex: cursor,
          pipelineSegmentTotal: Math.max(queue.length, cursor + 1),
        });
        if (pipelineToken !== speechPipelineGeneration) break;
        if (started === null) {
          recordVoiceDiagnostic(
            "utterance-audio-cancelled",
            `第 ${cursor + 1} 条音频被新请求取消`,
            { requestId, utteranceId: item.utteranceId },
          );
          break;
        }
        if (started !== true) {
          textFallbackAfterVoiceFailure = true;
          controller.abort();
          recordVoiceDiagnostic(
            "utterance-audio-start-failed-text-fallback",
            `第 ${cursor + 1} 条音频未能开始，改为提交该段及后续文字`,
            { requestId, utteranceId: item.utteranceId },
          );
          recordDelivery("audio_failed", item);
          reveal(item, false);
          recordDelivery("text_revealed_after_audio_failure", item);
          markDeliveryPartial(item, cursor);
          cursor += 1;
          continue;
        }
        reveal(item, true);
        recordDelivery("audio_started", item);
        if (queue[cursor + 1]) prepare(queue[cursor + 1]);
        cursor += 1;
      }
    } finally {
      settlePipeline();
      if (turnCompleted) hideThinkingIndicator(generation);
    }
  };
  return {
    enqueue(raw) {
      if (closed || pipelineToken !== speechPipelineGeneration) return;
      const item = {
        ...raw,
        utteranceId: String(raw?.utteranceId || `u${queue.length + 1}`),
        speechUnitId: String(raw?.unitId || raw?.speechUnitId || raw?.utteranceId || `u${queue.length + 1}`),
        textZh: String(raw?.textZh || "").trim(),
        spokenTextJa: String(raw?.spokenTextJa || "").trim(),
        voiceStyle: String(raw?.voiceStyle || "calm"),
        voiceTone: String(raw?.voiceTone || "neutral"),
        voiceIntensity: normalizeVoiceIntensity(raw?.voiceIntensity || "normal"),
        speechPace: normalizeSpeechPace(raw?.speechPace || "normal"),
        speechSpeed: clamp(toNumber(raw?.speechSpeed, 1), 0.62, 1.45),
        speechLanguage: normalizeSpeechLanguage(raw?.speechLanguage || getSettings().speechLanguage),
        speechProsody: normalizeSpeechProsody(raw?.speechProsody || getSettings().speechProsody),
        emotion: String(raw?.emotion || "neutral"),
        emotionIntensity: clamp(toNumber(raw?.emotionIntensity, 1), 0.2, 2.5),
        gesture: String(raw?.gesture || "idle"),
        blushIntensity: clamp(toNumber(raw?.blushIntensity, 0), 0, 1),
        eyeContact: clamp(toNumber(raw?.eyeContact, 0.72), 0, 1),
        gazeX: clamp(toNumber(raw?.gazeX, 0), -0.6, 0.6),
        gazeY: clamp(toNumber(raw?.gazeY, 0), -0.6, 0.6),
        tension: clamp(toNumber(raw?.tension, 0), 0, 1),
        originalAudio: raw?.originalAudio && typeof raw.originalAudio === "object"
          ? { ...raw.originalAudio }
          : null,
        originalReactionId: String(raw?.originalReactionId || raw?.originalAudio?.id || ""),
        boundaryAfter: normalizeSpeechBoundary(raw?.boundaryAfter),
        displayIndex: queue.length,
        revealed: false,
        ready: false,
        prepared: null,
        error: null,
        cancelled: false,
        cancellationReason: "",
        preparationStarted: false,
        textOnly: Boolean(raw?.textOnly),
      };
      item.speechText = chooseSpeechText(item.textZh, item.spokenTextJa, item.speechLanguage);
      if (!item.textZh || (!item.speechText && !item.textOnly)) return;
      queue.push(item);
      if (item.displayIndex === cursor) prepare(item);
      signalQueue();
      run().catch(error => {
        if (!isSpeechCancellation(error)) {
          recordVoiceDiagnostic("utterance-stream-playback-error", error.message, { pipelineToken });
        }
      });
    },
    enqueueFinal(result) {
      finalResult = result;
      const reply = String(result?.replyWithoutStreamPreamble || result?.reply || "").trim();
      const spoken = chooseSpeechText(
        reply,
        String(result?.spokenTextJaWithoutStreamPreamble || result?.spokenTextJa || "").trim(),
      );
      const mood = resolveAssistantMood(result || {}, "", reply);
      const contracted = Array.isArray(result?.speechUnits)
        ? result.speechUnits.filter(unit => unit && unit.deliveryRequired !== false).map(unit => ({
          ...unit,
          textZh: String(unit.textZh || "").trim(),
          spokenTextJa: String(unit.spokenTextJa || "").trim(),
        })).filter(unit => unit.textZh && chooseSpeechText(unit.textZh, unit.spokenTextJa, result?.speechLanguage))
        : [];
      const units = contracted.length ? contracted : [{ textZh: reply, spokenTextJa: spoken }];
      units.forEach((unit, index) => this.enqueue({
        utteranceId: String(unit.unitId || `final-${index + 1}`),
        unitId: String(unit.unitId || `final-${index + 1}`),
        textZh: unit.textZh,
        spokenTextJa: unit.spokenTextJa,
        ...mood,
        ...unit,
        boundaryAfter: normalizeSpeechBoundary(
          unit.boundaryAfter,
          index === units.length - 1 ? "turn_pause" : "phrase_pause",
        ),
        speechLanguage: normalizeSpeechLanguage(result?.speechLanguage || getSettings().speechLanguage),
        speechPace: resolveSpeechPace(result || {}, ""),
        speechSpeed: resolveSpeechSpeed(result || {}, "", resolveSpeechPace(result || {}, "")),
        textOnly: Boolean(result?.textOnlyDelivery) && !chooseSpeechText(
          unit.textZh,
          unit.spokenTextJa,
          result?.speechLanguage || getSettings().speechLanguage,
        ),
      }));
    },
    setFinalResult(result) {
      finalResult = result;
      recordDelivery("turn_envelope_validated");
      hooks.onFinalResult?.(result);
    },
    close() {
      closed = true;
      turnCompleted = true;
      signalQueue();
      if (!running && !queue.length) {
        settlePipeline();
        hideThinkingIndicator(generation);
      }
    },
    abort() {
      closed = true;
      pipelineCancelled = true;
      controller.abort();
      if (pipelineToken === speechPipelineGeneration) {
        speechRequestAbortController?.abort();
        speechRequestAbortController = null;
      }
      signalQueue();
      if (!running) settlePipeline();
    },
    get message() {
      return hooks.getMessage?.() || null;
    },
    get queuedCount() {
      return queue.length;
    },
  };
}


function failLocalSpeech(error, generation = speechGeneration, title = "本地语音合成失败") {
  if (generation !== speechGeneration) return null;
  console.warn(`${title}:`, error);
  speechState.pending = false;
  speechState.active = false;
  speechState.textRevealPending = false;
  speechState.systemSpeechActive = false;
  speechState.endsAt = clock.now();
  speakUntil = speechState.endsAt;
  resetAnimeMouthState();
  const message = error instanceof Error ? error.message : String(error || "未知错误");
  recordVoiceDiagnostic("local-failed", `${title}，本次未播放系统音：${message}`, { generation });
  showVoiceFailure(title, error);
  return false;
}


function speakPulse(length) {
  const now = clock.now();
  const duration = clamp(length * 46, 900, 5200);
  resetAnimeMouthState();
  speechState = {
    active: true,
    pending: false,
    startedAt: now,
    endsAt: now + duration,
    text: "あ".repeat(clamp(Math.round(length / 3), 4, 80)),
    displayText: "",
    spokenTextJa: "",
    timeline: [],
    mouthCues: [],
    alignmentSource: "",
    ttsMetrics: null,
    charIndex: 0,
    voiceStyle: "calm",
    voiceTone: "neutral",
    voiceIntensity: "normal",
    speechPace: "normal",
    speechSpeed: 1,
    pipelineSegmentIndex: 1,
    pipelineSegmentTotal: 1,
    utterance: null,
    mood: null,
    kind: "debug",
    generation: ++speechGeneration,
    systemSpeechActive: false,
    systemSpeechDeadline: 0,
  };
  speakUntil = speechState.endsAt;
}


    return {
      get state() { return { ...speechState }; },
      get pipelineActive() { return speechPipelineActive; },
      get generation() { return speechGeneration; },
      get currentSpeechIndex() {
        return ttsAudio && speechState.timeline?.length
          ? timelineIndexAt(ttsAudio.currentTime)
          : speechState.charIndex;
      },
      isBusy(now = clock.now()) { return speechPipelineActive || speechState.pending || isSpeaking(now); },
      sampleLipSync(now = clock.now(), enabled = true) {
        // 原渲染顺序：先采音量，再取开口和嘴形；后者复用 12ms 口型缓存。
        updateAudioMouthLevel();
        if (!enabled) return { mouthOpen: 0, mouthForm: 0 };
        return { mouthOpen: speechMouthOpen(now), mouthForm: speechMouthForm(now) };
      },
      startSpeech, startSegmentedSpeech, interruptCurrentSpeech,
      createChatUtterancePlayback, isSpeaking, speechMouthOpen, speechMouthForm,
      buildTtsRequest, preparedTtsKey, requestPreparedTts,
      ttsRetryWouldDuplicateWork, semanticSpeechUnits, validatedSemanticSpeechUnits,
      stripEmojiForSpeech, normalizeSpeechBoundary, speechBoundaryPauseMs,
      waitForSpeechSegmentEnd, waitForSpeechBoundary, failLocalSpeech, speakPulse,
      startLocalTts, startSystemSpeech, stopLocalTts, estimateSpeechDuration,
      isSpeechCancellation,
      markIdle() { speechState.kind = "idle"; },
    };
  }
  return { createSpeechPlaybackController };
}));
