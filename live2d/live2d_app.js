"use strict";

const MODEL_REGISTRY_URL = "/assets/live2d/models.json";
const FALLBACK_MODEL_PROFILE = {
  id: "aipoet-kurisu",
  label: "AMADEUS 经典版（当前）",
  description: "当前正在使用的半身模型，保持为默认选项。",
  modelUrl: "/assets/live2d/AiPoetKurisu/kurisu.model3.json",
  supportsNativeExpressions: true,
  hiddenParameters: [],
  hiddenParts: [],
  fixedParameters: {},
  parameterAliases: { blush: ["ParamCheek"] },
  motionMap: {},
};
const MODEL_BINARY_BACKGROUND_URL = "/assets/live2d/Amadeus/kurisu.2048/background_binary_panel.png";
const BOOT_BACKGROUND_URL = "/assets/boot/amadeus_background_frame084_outpaint_grid_v5.png";
const BACKGROUND_PRESETS = new Set([
  "binary", "boot", "grid", "solid", "custom",
]);
const BACKGROUND_PRESET_ALIASES = {
  "original-watch": "grid",
  "original-clockwork": "grid",
  "original-clock-array": "grid",
  "original-divergence": "grid",
};
const REQUESTED_DEV_MODE = new URLSearchParams(window.location.search).get("dev") === "1";
const REQUESTED_TEST_MODE = new URLSearchParams(window.location.search).get("test") === "1";
const STORAGE_KEY = REQUESTED_TEST_MODE
  ? "amadeus-live2d-chat-v2-test"
  : REQUESTED_DEV_MODE
  ? "amadeus-live2d-chat-v2-developer"
  : "amadeus-live2d-chat-v2";

function runtimeModeStateLabel(label) {
  return REQUESTED_TEST_MODE ? `TEST / ${label}` : label;
}
const DEV_PANEL_COLLAPSED_KEY = "amadeus-dev-panel-collapsed-v1";
const DEV_MODEL_TRANSFORMS_KEY = "amadeus-dev-model-transforms-v1";
const SAVED_API_KEY_MASK = "••••••••••••";
const MAX_MESSAGES = 240;
const MAX_MEMORIES = 512;
const MODEL_HISTORY_LIMIT = 36;
let DEV_MODE = REQUESTED_DEV_MODE;
let MODEL_PREVIEW_ID = DEV_MODE ? new URLSearchParams(window.location.search).get("modelPreview") || "" : "";
let MODEL_PREVIEW_ONLY = DEV_MODE && new URLSearchParams(window.location.search).get("previewOnly") === "1";
let SKIP_BOOT = DEV_MODE && new URLSearchParams(window.location.search).get("skipBoot") === "1";
const TOUCH_REGION_CONFIG_URL = "/configs/live2d_touch_regions.json";
const TOUCH_ZONE_LABELS = {
  head: "头部 / 摸头",
  face: "脸部 / 轻触脸颊",
  mouth: "嘴部 / 亲吻判定",
  hand: "手部 / 牵手",
  chest: "胸部 / 越界触摸",
  body: "普通躯干（不发送事件）",
};
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

function activeTouchRegionProfile() {
  const profiles = touchRegionConfig?.profiles;
  if (!profiles || typeof profiles !== "object") return touchRegionConfig || DEFAULT_TOUCH_REGION_CONFIG;
  return profiles[activeModelProfile?.id]
    || profiles[touchRegionConfig.defaultProfile]
    || DEFAULT_TOUCH_REGION_CONFIG;
}

function normalizedTouchBounds(profile = activeTouchRegionProfile()) {
  const values = Array.isArray(profile?.touchBounds) ? profile.touchBounds.map(value => toNumber(value, 0)) : [0, 0, 1, 1];
  const [x, y, width, height] = values.length === 4 ? values : [0, 0, 1, 1];
  return {
    x: clamp(x, 0, 1),
    y: clamp(y, 0, 1),
    width: clamp(width, 0.001, 1),
    height: clamp(height, 0.001, 1),
  };
}

const PROACTIVE_LOGIC = window.AmadeusProactiveLogic;
if (!PROACTIVE_LOGIC) throw new Error("proactive_logic.js is required");

const PROACTIVE_FREQUENCY_PROFILES = Object.freeze({
  quiet: Object.freeze({
    initial: Object.freeze([15 * 60 * 1000, 30 * 60 * 1000]),
    followUps: Object.freeze([
      Object.freeze([12 * 60 * 1000, 22 * 60 * 1000]),
      Object.freeze([16 * 60 * 1000, 26 * 60 * 1000]),
      Object.freeze([20 * 60 * 1000, 30 * 60 * 1000]),
    ]),
    startupChanceScale: 0.45,
  }),
  low: Object.freeze({
    initial: Object.freeze([6 * 60 * 1000, 15 * 60 * 1000]),
    followUps: Object.freeze([
      Object.freeze([5 * 60 * 1000, 10 * 60 * 1000]),
      Object.freeze([7 * 60 * 1000, 12 * 60 * 1000]),
      Object.freeze([9 * 60 * 1000, 15 * 60 * 1000]),
    ]),
    startupChanceScale: 0.72,
  }),
  normal: Object.freeze({
    initial: Object.freeze([90 * 1000, 240 * 1000]),
    followUps: Object.freeze([
      Object.freeze([70 * 1000, 120 * 1000]),
      Object.freeze([110 * 1000, 180 * 1000]),
      Object.freeze([150 * 1000, 240 * 1000]),
    ]),
    startupChanceScale: 1,
  }),
  high: Object.freeze({
    initial: Object.freeze([45 * 1000, 120 * 1000]),
    followUps: Object.freeze([
      Object.freeze([45 * 1000, 90 * 1000]),
      Object.freeze([70 * 1000, 120 * 1000]),
      Object.freeze([100 * 1000, 160 * 1000]),
    ]),
    startupChanceScale: 1.18,
  }),
});

const DEFAULT_SETTINGS = {
  apiEndpoint: "",
  apiModel: "",
  cognitiveRepairModel: "follow",
  cognitiveAuditModel: "follow",
  cognitiveSemanticAuditEnabled: false,
  apiKey: "",
  apiKeySet: false,
  visionEnabled: true,
  visionApiKeySet: false,
  apiNetworkMode: "auto",
  apiProxyUrl: "",
  expressionRuntime: { schemaVersion: 1, activeByModel: {} },
  useResponseFormat: true,
  memoryFile: "data/memory.json",
  memoryFileAbsolute: "",
  windowWidth: 520,
  windowHeight: 760,
  liteWindowWidth: 520,
  liteWindowHeight: 760,
  fullWindowWidth: 1040,
  fullWindowHeight: 760,
  layoutMode: "lite",
  alwaysOnTop: true,
  hudCollapsed: false,
  modelScale: 1,
  modelOffsetX: 0,
  modelOffsetY: 0,
  modelTransforms: {},
  live2dModelId: "aipoet-kurisu",
  backgroundPreset: "binary",
  customBackgroundPath: "",
  voiceEnabled: true,
  lipSyncEnabled: true,
  ttsProvider: "gpt-sovits",
  ttsEndpoint: "http://127.0.0.1:9881/tts",
  ttsSpeaker: "1",
  speechRate: 1,
  speechLanguage: "ja-JP",
  speechProsody: "natural",
  voiceLocale: "ja-JP",
  voicePitch: 1.18,
  ttsDeliveryPostProcessingEnabled: false,
  gptSovitsPort: 9881,
  gptSovitsCondaEnv: "",
  gptSovitsRoot: "",
  gptSovitsModelPreset: "emotionclarity_v7_retrained_short",
  voicePresetRevision: 9,
  gptSovitsGptModel: "",
  gptSovitsSovitsModel: "",
  gptSovitsRefAudio: "",
  gptSovitsRefText: "",
  gptSovitsStyleMap: "",
  gptSovitsTargetLanguage: "日文",
  gptSovitsStatus: null,
  alignSpokenJa: true,
  autoStartGptSovits: true,
  debugDirectSpeech: false,
  permissionReadScreen: false,
  permissionReadCamera: false,
  cameraIndex: 0,
  permissionReadSystemTime: false,
  permissionWebSearch: false,
  permissionFileRead: false,
  permissionFileWrite: false,
  permissionOpenBrowser: false,
  agentFileWorkspace: "data\\agent-workspace",
  webSearchProvider: "duckduckgo",
  webSearchApiKey: "",
  webSearchApiKeySet: false,
  webSearchMaxResults: 5,
  webReadTopPages: 2,
  webRequestTimeout: 12,
  proactiveInteractionsEnabled: true,
  proactiveFrequency: "normal",
  proactiveTopicCategories: [...PROACTIVE_LOGIC.CATEGORY_IDS],
  proactiveScreenObservationEnabled: true,
  proactiveScreenObservationChance: 0.20,
  releaseMode: false,
};

const PROACTIVE_TOPIC_CATEGORIES = new Set(PROACTIVE_LOGIC.CATEGORY_IDS);
const TODO_DRAWER_OPEN_KEY = "amadeus.todoDrawerOpen.v1";
const TODO_SEEN_REVISION_KEY = "amadeus.todoSeenRevision.v1."
  + (REQUESTED_DEV_MODE ? "developer" : "main");

const DEFAULT_MEMORIES = [];

const RENDER_RESOLUTION = Math.min(3, Math.max(2, window.devicePixelRatio || 1));

const STALE_FALLBACK_REPLIES = new Set([
  "……你这个问题，倒也不是不能回答。",
  "……你这问题，倒也不是不能回答。",
]);
const NON_CHEST_TOUCH_REPLY_TARGET = 10;
const TOUCH_BURST_THRESHOLD = 3;
const TOUCH_IGNORE_THRESHOLD = 7;
const TOUCH_SINGLE_SETTLE_MS = 360;
const TOUCH_BURST_SETTLE_MS = 950;
const TOUCH_BURST_REPLY_COOLDOWN_MS = 8000;
const EMOTION_GESTURE_CYCLES = Object.freeze({
  teasing: Object.freeze(["tilt", "look_away", "tilt", "idle"]),
  proud: Object.freeze(["look_away", "tilt", "look_away", "idle"]),
  skeptical: Object.freeze(["tilt", "idle", "look_away", "tilt"]),
  shy: Object.freeze(["look_away", "idle", "tilt", "look_away"]),
  flustered: Object.freeze(["look_away", "tilt", "look_away", "idle"]),
  annoyed: Object.freeze(["look_away", "tilt", "idle"]),
  furious: Object.freeze(["look_away"]),
  amused: Object.freeze(["tilt", "idle", "look_away", "idle"]),
});
const NON_CHEST_TOUCH_TIME_CREDIT_MS = 30000;

const dom = {};
let app;
let backgroundLayer;
let backgroundRevision = 0;
let model;
let coreModel;
let modelGarmentRig = null;
let live2dModelProfiles = [FALLBACK_MODEL_PROFILE];
let live2dModelOverrides = {};
let activeModelProfile = FALLBACK_MODEL_PROFILE;
let modelHome = { x: 0, y: 0, scale: 1 };
let modelFitReference = { width: 0, height: 0, layoutMode: "" };
let tickerTime = 0;
let blinkUntil = 0;
let nextBlink = performance.now() + 1400;
let speakUntil = 0;
let currentEmotion = "neutral";
let stableEmotion = "neutral";
let emotionRecoveryTarget = "neutral";
let cognitiveOverlayBaseEmotion = "neutral";
let cognitiveOverlayBaseDetail = null;
let emotionEnteredAt = performance.now();
let currentEmotionHoldMs = 9000;
let currentEmotionFadeMs = 3200;
const EXPRESSION_POST_SPEECH_HOLD_MS = 8000;
let emotionSpeechProtectedUntil = 0;
let activeGesture = "";
let gestureUntil = 0;
let gestureEnteredAt = performance.now();
const armMotionController = AmadeusArmMotion.createController();
let lookAwayDirection = 1;
let furiousTurnDirection = 0;
let furiousTurnTarget = 1;
let furiousTurnVelocity = 0;
let narrowEyeExpressionSince = 0;
let annotatedExpressionStyleEmotion = "neutral";
let annotatedExpressionVariantUnit = Math.random();
let annotatedExpressionMirrorSide = Math.random() < 0.5 ? -1 : 1;
const runtimePupilScaleTargets = { left: 1, right: 1 };
const runtimePupilScaleValues = { left: 1, right: 1 };
let runtimeAuthorBlushOpacityTarget = 1;
let runtimeAuthorBlushOpacityValue = 1;
const emotionGestureSteps = new Map();
let lastLive2DExpression = "";
let lastLive2DMotion = "";
let lastLive2DMotionAt = 0;
let pointer = { x: 0, y: 0 };
let headLook = { x: 0, y: 0 };
let eyeLook = { x: 0, y: 0 };
let eyeDrift = { x: 0, y: 0, nextAt: performance.now() + 900 };
let shyGazeAwayDirection = Math.random() < 0.5 ? -1 : 1;
let availableVoices = [];
let settings = { ...DEFAULT_SETTINGS };
let developerModelTransforms = loadDeveloperModelTransforms();
let state = loadState();
let connectionStatus = "LOCAL";
let relationshipTone = "close_companion";
let persistentColdShoulder = false;
let transientInteraction = "";
let transientInteractionUntil = 0;
let currentEmotionWeight = 1;
let currentExpressionDetail = {
  blushIntensity: 0,
  eyeContact: 0.65,
  gazeX: 0,
  gazeY: 0,
  tension: 0,
  outburst: false,
};
let nextContextEmotionAt = performance.now() + 18000;
let contextEmotionRequestInFlight = false;
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
let voiceDiagnostics = { stage: "idle", message: "尚无语音请求", updatedAt: Date.now() };
let voiceDiagnosticEvents = [];
let developerRuntimeTimer = 0;
let developerPersonaSkillMonitor = null;
let developerEmotionOverride = null;
let developerEmotionManualBlush = null;
let developerParameterLock = false;
let developerExpressionCalibrationActive = false;
let developerParameterShowAdvanced = false;
const developerParameterOverrides = new Map();
const developerParameterBaseline = new Map();
let developerParameterMetadata = [];
const developerParameterDisplayNames = new Map();
let developerExpressionTrainingSamples = [];
let developerExpressionRuntime = { schemaVersion: 1, activeByModel: {} };
let developerExpressionTrainingSelectedId = "";
let developerExpressionTrainingLoaded = false;
let developerExpressionTrainingDirty = false;
let developerExpressionTrainingEditorOpen = false;
let developerExpressionTrainingDraft = null;
let developerExpressionTrainingDraftIsNew = false;
let developerExpressionTrainingPreviousSelectedId = "";
let developerTrainingKind = "expression";
let developerVoiceReferences = [];
let developerVoiceReferenceCatalog = null;
let developerVoiceReferenceLoaded = false;
let developerVoiceReferenceSelectedId = "";
let developerVoiceReferenceDraft = null;
let developerVoiceReferenceDirty = false;
let developerVoiceReferenceAudioUrl = "";
let developerVoiceReferenceOriginalAudioUrl = "";
const developerVoiceReferenceAudioLoadTokens = { candidate: 0, original: 0 };
let developerAuthorBlushOpacityOverride = null;
const developerPupilScaleOverrides = { left: null, right: null };
let connectionWatchdogTimer = 0;
let latestRuntimeStatus = null;
let animeMouthState = createAnimeMouthState();
let startupGreetingPending = false;
let startupGreetingPreparing = false;
let preparedStartupGreeting = null;
let startupGreetingGeneration = 0;
let startupGreetingAbortController = null;
let nextProactiveInteractionAt = 0;
let proactiveInteractionInFlight = false;
const proactivePersistenceInFlight = new Set();
let todoItems = [];
let todoActiveCount = 0;
let todoLatestRevision = 0;
let todoSeenRevision = Math.max(
  0,
  Number(window.localStorage.getItem(TODO_SEEN_REVISION_KEY)) || 0,
);
let todoUnseenCount = 0;
let todoEditorTaskId = "";
let todoEditorVersion = 0;
let todoLoadInFlight = false;
let todoLoadPending = false;
let todoMarkSeenPending = false;
let todoReminderInFlight = false;
let bookmarkItems = [];
let bookmarkEditorId = "";
let bookmarkEditorVersion = 0;
let bookmarkLoadInFlight = false;
let bookmarkLoadPending = false;
let todoReminderTimer = 0;
let todoRefreshTimer = 0;
let salieriRefreshTimer = 0;
let salieriRecorderEnabled = false;
let nextPositiveAmbientMoodAt = performance.now() + 150000 + Math.random() * 210000;
const proactiveState = {
  lastUserActivityAt: performance.now(),
  lastProactiveAt: 0,
  unansweredCount: 0,
  phase: "idle",
  returnContext: "",
  nextAmbientMoodAt: 0,
  silenceLimit: PROACTIVE_LOGIC.silenceLimit(),
  activePrompt: "",
  activeSubject: "",
  sequenceId: 1,
  control: normalizeProactiveControl(state.proactiveControl),
};
let bootSequenceActive = true;
let speechGeneration = 0;
let chatRequestGeneration = 0;
let chatAbortController = null;
let chatRequestInFlight = false;
let thinkingIndicator = null;
let thinkingProgressTimer = 0;
let thinkingProgressRequestId = "";
let activeChatRequestId = "";
let activeAnswerRequestId = "";
let supersededChatRequestId = "";
let deferredTouchAssistant = null;
let firstRunApiSetupOpened = false;
let activeConversationGroup = "";
let activeConversationInputs = [];
let activeConversationAttachments = [];
let activeConversationReplyAnchor = null;
let activeConversationFocus = null;
let activeConversationUserMessageId = "";
let pendingReplyAnchor = null;
let pendingAttachments = [];
let attachmentSerializationInFlight = false;
let conversationWasInterrupted = false;
let activeAssistantMessage = null;
const chatHistorySearchState = {
  query: "",
  role: "all",
  offset: 0,
  limit: 30,
  total: 0,
  hasMore: false,
  timer: 0,
  controller: null,
  results: [],
};
const conversationPickerState = {
  controller: null,
  conversations: [],
  loading: false,
};
let speechPipelineGeneration = 0;
let speechPipelineAbortController = null;
let speechRequestAbortController = null;
let speechGapIndicatorGeneration = 0;
let speechPipelineActive = false;
let touchRequestAbortController = null;
let touchRequestGeneration = 0;
let proactiveRequestAbortController = null;
let proactiveRequestGeneration = 0;
let touchDebounceTimer = 0;
let lastDeveloperTouchAt = 0;
let touchRegionConfig = DEFAULT_TOUCH_REGION_CONFIG;
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
const paramValues = {};
const MODEL_UI_HIDE_PARAMS = [
  "yy", "hd", "gyzz",
  "DZH", "BB", "KLSTN", "TX",
  "y1", "y2", "y3", "y4", "y5", "y6", "y7", "y8", "y9", "y10", "y11", "y12", "y13", "out14",
  "tui_chu",
];

const emotionTargets = {
  neutral: {
    ParamEyeLOpen: 0.96,
    ParamEyeROpen: 0.96,
    ParamEyeSmile: 0.16,
    ParamBrowLForm: 0.12,
    ParamBrowRForm: 0.12,
    ParamMouthForm: 0.18,
    Param16: 0.22,
    Param17: 0,
  },
  thinking: {
    ParamAngleX: 2.2,
    ParamAngleY: -9,
    ParamAngleZ: -2.4,
    ParamEyeLOpen: 0.78,
    ParamEyeROpen: 0.8,
    ParamEyeBallY: -0.12,
    ParamBrowLY: -0.5,
    ParamBrowRY: -0.38,
    ParamBrowLForm: -0.62,
    ParamBrowRForm: -0.46,
    ParamMouthForm: -0.52,
    Param11: -0.28,
    Param12: -0.22,
    Param16: 0.45,
  },
  focused: {
    ParamAngleX: 1.4,
    ParamAngleY: -8.2,
    ParamAngleZ: -1.1,
    // Keep the concentrated brow shape, but leave enough visible eye area for
    // the iris to remain centred instead of looking vertically clipped.
    ParamEyeLOpen: 0.82,
    ParamEyeROpen: 0.84,
    ParamEyeBallY: -0.06,
    ParamBrowLY: -0.68,
    ParamBrowRY: -0.58,
    ParamBrowLForm: -0.72,
    ParamBrowRForm: -0.62,
    ParamMouthForm: -0.34,
    Param11: -0.34,
    Param12: -0.3,
    Param16: 0.62,
  },
  skeptical: {
    ParamAngleX: -4.8,
    ParamAngleY: -1.4,
    ParamAngleZ: -5.8,
    ParamEyeLOpen: 0.66,
    ParamEyeROpen: 0.78,
    ParamEyeBallX: -0.18,
    ParamBrowLY: -0.78,
    ParamBrowRY: -0.28,
    ParamBrowLForm: -0.9,
    ParamBrowRForm: -0.28,
    ParamMouthForm: -0.72,
    Param11: -0.42,
    Param12: -0.22,
    Param16: 0.68,
  },
  awkward: {
    ParamAngleX: -2.8,
    ParamAngleY: -3.2,
    ParamAngleZ: 3.6,
    ParamEyeLOpen: 0.74,
    ParamEyeROpen: 0.88,
    ParamEyeBallX: -0.12,
    ParamEyeBallY: -0.08,
    ParamBrowLY: 0.22,
    ParamBrowRY: -0.18,
    ParamBrowLForm: 0.34,
    ParamBrowRForm: -0.34,
    ParamMouthForm: -0.38,
    ParamMouthOpenY: 0.03,
    ParamCheek: 0.08,
    Param11: -0.08,
    Param12: 0.05,
    Param16: 0.32,
  },
  annoyed: {
    // Annoyed stays visibly irritated without becoming the closed-eye,
    // fully turned-away furious pose.
    ParamAngleX: -7.04,
    ParamAngleY: 1.792,
    ParamAngleZ: -5.888,
    ParamEyeLOpen: 0.83,
    ParamEyeROpen: 0.852,
    ParamEyeSmile: 0.02,
    ParamEyeBallX: -0.186,
    ParamBrowLY: -0.62,
    ParamBrowRY: -0.58,
    ParamBrowLForm: -1,
    ParamBrowRForm: -1,
    ParamMouthForm: -0.34,
    ParamCheek: 0.274,
    Param11: -0.48,
    Param12: -0.48,
    Param16: 0.76,
  },
  furious: {
    // Persistent yaw is applied at render time through a damped turn controller.
    // Both eyes stay closed except for the brief, deliberate cold glance.
    ParamAngleX: 0,
    ParamAngleY: 1.6,
    ParamAngleZ: 0,
    ParamEyeLOpen: 0,
    ParamEyeROpen: 0,
    ParamEyeSmile: 0,
    ParamEyeBallX: 0,
    ParamEyeBallY: -0.22,
    ParamBrowLY: -0.9,
    ParamBrowRY: -0.82,
    ParamBrowLForm: -1,
    ParamBrowRForm: -1,
    ParamMouthForm: -0.35,
    ParamMouthOpenY: 0,
    ParamCheek: 0.12,
    Param11: -0.72,
    Param12: -0.7,
    Param16: 0.92,
  },
  teasing: {
    ParamAngleX: 7.168,
    ParamAngleY: 1.024,
    ParamAngleZ: 6.912,
    ParamEyeLOpen: 0.744,
    ParamEyeROpen: 0.874,
    ParamEyeBallX: 0.139,
    ParamBrowLY: 0.18,
    ParamBrowRY: -0.08,
    ParamBrowLForm: 0.34,
    ParamBrowRForm: -0.18,
    // A clearly raised closed-mouth curve. Keep this visually separate from
    // the small tense mouth used by shy/startled and the open laughing mouth.
    ParamMouthForm: 1,
    ParamCheek: 0,
    Param11: 0.12,
    Param12: 0.08,
    Param16: 0.28,
  },
  amused: {
    ParamAngleX: 3,
    ParamAngleY: 2.8,
    ParamAngleZ: 4.8,
    ParamEyeLOpen: 0.72,
    ParamEyeROpen: 0.67,
    ParamEyeSmile: 0.78,
    ParamEyeBallY: -0.18,
    ParamBrowLY: 0.28,
    ParamBrowRY: 0.24,
    ParamBrowLForm: 0.3,
    ParamBrowRForm: 0.26,
    ParamMouthForm: 1,
    ParamMouthOpenY: 0,
    ParamCheek: 0.225,
    Param11: 0.18,
    Param12: 0.16,
    Param16: 0.2,
  },
  laughing: {
    ParamAngleX: 2.6,
    ParamAngleY: 5.4,
    ParamAngleZ: 2.2,
    ParamEyeLOpen: 0,
    ParamEyeROpen: 0,
    ParamEyeSmile: 1,
    ParamEyeBallY: 0.04,
    ParamBrowLY: 0.38,
    ParamBrowRY: 0.34,
    ParamBrowLForm: 0.46,
    ParamBrowRForm: 0.42,
    ParamMouthForm: 0.98,
    ParamMouthOpenY: 1,
    ParamCheek: 0.34,
    Param11: 0.24,
    Param12: 0.22,
    Param16: 0.08,
  },
  shy: {
    ParamAngleX: -5.2,
    ParamAngleY: -11.5,
    ParamAngleZ: 8.6,
    ParamEyeLOpen: 0.76,
    ParamEyeROpen: 0.78,
    // Horizontal aversion is derived from the live pointer direction.
    ParamEyeBallX: 0,
    ParamEyeBallY: -0.44,
    ParamBrowLY: 0.48,
    ParamBrowRY: 0.42,
    ParamBrowLForm: 0.66,
    ParamBrowRForm: 0.58,
    ParamMouthForm: 0.12,
    ParamMouthOpenY: 0.025,
    // The re-edited typical shy sample keeps independent cheek blush at zero;
    // semantic blush remains a separate, explicitly controlled channel.
    ParamCheek: 0,
    Param11: 0.3,
    Param12: 0.28,
    Param16: 0.18,
  },
  flustered: {
    ParamAngleX: -8,
    ParamAngleY: -13.5,
    ParamAngleZ: 10.5,
    ParamEyeLOpen: 0.68,
    ParamEyeROpen: 0.7,
    ParamEyeBallX: 0,
    ParamEyeBallY: -0.38,
    ParamBrowLY: 0.66,
    ParamBrowRY: 0.62,
    ParamBrowLForm: 0.86,
    ParamBrowRForm: 0.8,
    ParamMouthForm: 0.34,
    ParamMouthOpenY: 0.09,
    ParamCheek: 0.86,
    Param11: 0.46,
    Param12: 0.42,
    Param16: 0.12,
  },
  affectionate: {
    ParamAngleX: -0.552,
    ParamAngleY: -2.392,
    ParamAngleZ: 1.288,
    ParamEyeLOpen: 1,
    ParamEyeROpen: 1,
    ParamEyeSmile: 0.66,
    ParamEyeBallX: 0.04,
    ParamEyeBallY: -0.17,
    ParamBrowLY: 0.36,
    ParamBrowRY: 0.32,
    ParamBrowLForm: 0.48,
    ParamBrowRForm: 0.44,
    ParamMouthForm: 1,
    ParamMouthOpenY: 0.2,
    ParamCheek: 0.115,
    Param11: 0.18,
    Param12: 0.16,
    Param16: 0.18,
  },
  concerned: {
    ParamAngleX: -4.8,
    ParamAngleY: -9.2,
    ParamAngleZ: 1.6,
    ParamEyeLOpen: 0.66,
    ParamEyeROpen: 0.68,
    ParamEyeBallY: -0.58,
    ParamBrowLY: 0.52,
    ParamBrowRY: 0.5,
    ParamBrowLForm: 0.82,
    ParamBrowRForm: 0.78,
    ParamMouthForm: -0.37,
    ParamCheek: 0.1,
    Param11: -0.18,
    Param12: -0.16,
    Param16: 0.48,
  },
  protective: {
    ParamAngleX: -2.8,
    ParamAngleY: -6.8,
    ParamAngleZ: -1.2,
    ParamEyeLOpen: 0.74,
    ParamEyeROpen: 0.76,
    ParamEyeBallY: -0.08,
    ParamBrowLY: -0.28,
    ParamBrowRY: -0.22,
    ParamBrowLForm: -0.44,
    ParamBrowRForm: -0.38,
    ParamMouthForm: -0.58,
    ParamCheek: 0.12,
    Param11: -0.34,
    Param12: -0.3,
    Param16: 0.92,
  },
  excited: {
    ParamAngleX: 4.2,
    ParamAngleY: 4,
    ParamAngleZ: 3.6,
    ParamEyeLOpen: 0.83,
    ParamEyeROpen: 0.852,
    ParamEyeSmile: 0.62,
    ParamBrowLY: 0.46,
    ParamBrowRY: 0.42,
    ParamBrowLForm: 0.46,
    ParamBrowRForm: 0.42,
    ParamMouthForm: 1,
    ParamMouthOpenY: 0.22,
    ParamCheek: 0.479,
    Param11: 0.32,
    Param12: 0.32,
    Param16: 0.08,
  },
  startled: {
    ParamAngleX: -6,
    ParamAngleY: 6.8,
    ParamAngleZ: -7,
    ParamEyeLOpen: 1,
    ParamEyeROpen: 1,
    ParamEyeBallY: -0.2,
    ParamBrowLY: 0.62,
    ParamBrowRY: 0.58,
    ParamBrowLForm: 0.72,
    ParamBrowRForm: 0.68,
    // Shock uses a small, stiff mouth rather than the neutral wavy curve.
    ParamMouthForm: -0.12,
    ParamMouthOpenY: 0.1,
    ParamCheek: 0.2,
    Param11: 0.28,
    Param12: 0.26,
    Param16: 0,
  },
  proud: {
    ParamAngleX: 5.4,
    ParamAngleY: 0.4,
    ParamAngleZ: 4.8,
    ParamEyeLOpen: 0.875,
    ParamEyeROpen: 0.735,
    ParamEyeSmile: 0.56,
    ParamEyeBallX: 0.06,
    ParamEyeBallY: -0.4,
    ParamBrowLY: 0.42,
    ParamBrowRY: 0.22,
    ParamBrowLForm: 0.52,
    ParamBrowRForm: 0.28,
    ParamMouthForm: 0.82,
    ParamCheek: 0.22,
    Param11: 0.24,
    Param12: 0.22,
    Param16: 0.92,
  },
  melancholic: {
    ParamAngleX: -12,
    ParamAngleY: -12.5,
    ParamAngleZ: -1.8,
    // The training rig supplies virtual per-eye pupil scaling in addition to
    // gaze recentering, so the low-lid expression can keep both irises legible.
    ParamEyeLOpen: 0.635,
    ParamEyeROpen: 0.7,
    ParamEyeBallX: 0.16,
    ParamEyeBallY: -0.47,
    ParamBrowLY: 0.58,
    ParamBrowRY: 0.54,
    ParamBrowLForm: 0.8,
    ParamBrowRForm: 0.74,
    ParamMouthForm: -0.52,
    ParamCheek: 0.06,
    Param11: -0.12,
    Param12: -0.1,
    Param16: 0.58,
  },
};

// The author's face layers are continuously mixable on this model. User
// supervision showed that combinations such as smile + anger produce the
// intended asymmetric teasing/proud faces, so runtime composition must not
// collapse them back to a one-hot categorical switch.
const AUTHOR_FACE_LAYER_IDS = Object.freeze(["Param3", "Param4", "Param5", "Param6", "Param7", "Param8"]);
const EXPRESSION_TRAINING_EMOTION_LABELS = Object.freeze({
  neutral: "中性",
  thinking: "思考",
  focused: "专注",
  skeptical: "怀疑",
  awkward: "尴尬",
  annoyed: "恼火",
  furious: "暴怒冷处理",
  teasing: "调侃",
  amused: "开心微笑",
  laughing: "大笑",
  shy: "害羞",
  flustered: "慌乱脸红",
  affectionate: "温柔亲近",
  concerned: "担心",
  protective: "保护",
  excited: "兴奋",
  startled: "震惊、意外",
  proud: "得意",
  melancholic: "低落",
});
const EXPRESSION_TRAINING_STRENGTHS = Object.freeze([
  Object.freeze({ id: "subtle", label: "轻微", intensity: 0.45 }),
  Object.freeze({ id: "typical", label: "典型", intensity: 1 }),
  Object.freeze({ id: "strong", label: "强烈", intensity: 1.65 }),
]);
const EXPRESSION_TRAINING_PARAMETER_GROUPS = Object.freeze([
  Object.freeze({
    key: "layers",
    label: "作者分类层（标注模式：连续自由混合）",
    ids: Object.freeze(["Param3", "Param4", "Param7", "Param5", "Param6", "Param8"]),
  }),
  Object.freeze({
    key: "eyes",
    label: "已确认有效的眼睛与视线",
    ids: Object.freeze([
      "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeBallX", "ParamEyeBallY",
      "VirtualPupilLScale", "VirtualPupilRScale",
    ]),
  }),
  Object.freeze({
    key: "mouth",
    label: "嘴部与脸颊",
    ids: Object.freeze(["ParamMouthForm", "ParamMouthOpenY", "ParamCheek", "VirtualAuthorBlushOpacity"]),
  }),
  Object.freeze({
    key: "pose",
    label: "头部与身体姿态",
    ids: Object.freeze([
      "ParamAngleX", "ParamAngleY", "ParamAngleZ",
      "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ",
      "Param14", "Param15", "Param16",
    ]),
  }),
  Object.freeze({
    key: "diagnostic",
    label: "模型中存在但当前脸部未见有效绑定（仅排查）",
    ids: Object.freeze([
      "ParamEyeLSmile", "ParamEyeRSmile", "ParamEyeSmile", "ParamEyeBallForm",
      "ParamBrowLY", "ParamBrowRY", "ParamBrowLX", "ParamBrowRX",
      "ParamBrowLAngle", "ParamBrowRAngle", "ParamBrowLForm", "ParamBrowRForm",
    ]),
  }),
]);
const EXPRESSION_TRAINING_UNBOUND_IDS = new Set([
  "ParamEyeLSmile", "ParamEyeRSmile", "ParamEyeSmile",
  "ParamBrowLY", "ParamBrowRY", "ParamBrowLX", "ParamBrowRX",
  "ParamBrowLAngle", "ParamBrowRAngle", "ParamBrowLForm", "ParamBrowRForm",
]);
const EXPRESSION_TRAINING_LAYER_BY_EMOTION = Object.freeze({
  shy: "Param3",
  flustered: "Param7",
  affectionate: "Param7",
  amused: "Param4",
  laughing: "Param4",
  excited: "Param4",
  proud: "Param4",
  teasing: "Param4",
  annoyed: "Param5",
  furious: "Param5",
  protective: "Param5",
  concerned: "Param6",
  melancholic: "Param6",
  startled: "Param8",
});
const EXPRESSION_TRAINING_LAYER_WARNINGS = Object.freeze({
  Param3: "作者“害羞1”层可能把瞳孔变化、脸红和局部脸部图形画在同一组素材里；若脸红是图层自带，ParamCheek 设为 0 也无法擦掉。",
  Param4: "作者“开心”层是一套完整分类脸/嘴素材；标注模式允许与其他层连续混合，请用实际画面记录是否出现双嘴或叠线。",
  Param7: "作者“娇羞”层同时驱动眼部/嘴部和两个独立脸红网格。训练台可用“作者层脸红可见度”单独隐藏 lianhong / lianhong2，但其余眼嘴仍属于同一分类层。",
  Param5: "作者“生气”层是一套完整分类脸/嘴素材；标注模式允许直接调节强度并与其他分类层叠加。",
  Param6: "作者“难过”层是一套完整分类脸/嘴素材；半闭眼时需要特别检查虹膜裁切。",
  Param8: "作者“惊讶”层会连带改变眼部素材/瞳孔观感，它不是可独立复用的“瞳孔缩放”参数。",
});
const EXPRESSION_TRAINING_LAYER_CHANNELS = Object.freeze({
  Param3: "害羞脸、局部眼部、瞳孔观感、脸红素材",
  Param4: "开心脸、眼部轮廓、嘴部曲线",
  Param5: "生气脸、眉眼观感、嘴部曲线",
  Param6: "低落脸、眼部轮廓、嘴部曲线",
  Param7: "娇羞脸、眼部、嘴部、lianhong/lianhong2",
  Param8: "惊讶脸、眼部轮廓、瞳孔观感、嘴部",
});
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

// The model-facing emotion remains compatible with the existing JSON enum,
// while this semantic layer determines how it behaves over time and which
// visual family it belongs to.
const EMOTION_SEMANTICS = Object.freeze({
  neutral: Object.freeze({ role: "stable", family: "neutral", recovery: "neutral" }),
  thinking: Object.freeze({ role: "cognitive", family: "cognitive" }),
  // Thinking is a temporary overlay on the previous face. Focused is the
  // actual emotion of a delivered answer and must own its face; composing it
  // over a preceding annoyed state used to keep the author's anger layer
  // active even though the status label already said FOCUSED.
  focused: Object.freeze({ role: "stable", family: "focus", recovery: "neutral" }),
  skeptical: Object.freeze({ role: "stance", family: "skeptical" }),
  awkward: Object.freeze({ role: "stable", family: "awkward", recovery: "neutral" }),
  annoyed: Object.freeze({ role: "stable", family: "anger", recovery: "neutral" }),
  furious: Object.freeze({ role: "boundary", family: "cold-boundary", recovery: "annoyed" }),
  // Teasing owns a complete playful face. Treating it as a stance used to
  // blend its upward mouth with the previous stable emotion (often annoyed),
  // producing the visibly kinked/wavy line between a smile and a frown.
  teasing: Object.freeze({ role: "stable", family: "playful", recovery: "neutral" }),
  amused: Object.freeze({ role: "stable", family: "playful", recovery: "neutral" }),
  laughing: Object.freeze({ role: "transient", family: "playful", recovery: "amused" }),
  shy: Object.freeze({ role: "stable", family: "shy", recovery: "neutral" }),
  flustered: Object.freeze({ role: "transient", family: "shy", recovery: "shy" }),
  affectionate: Object.freeze({ role: "stable", family: "warm", recovery: "neutral" }),
  concerned: Object.freeze({ role: "stable", family: "concern", recovery: "neutral" }),
  protective: Object.freeze({ role: "stance", family: "concern" }),
  excited: Object.freeze({ role: "transient", family: "positive", recovery: "amused" }),
  startled: Object.freeze({ role: "transient", family: "startled" }),
  proud: Object.freeze({ role: "stable", family: "positive", recovery: "neutral" }),
  melancholic: Object.freeze({ role: "stable", family: "melancholy", recovery: "neutral" }),
});

const EMOTION_OVERLAY_STRENGTH = Object.freeze({
  cognitive: 0.66,
  stance: 0.76,
});
const COGNITIVE_BASE_RAW_INTENSITY = 0.15;
const COGNITIVE_BASE_DETAIL_WEIGHT = 0.15;

document.addEventListener("DOMContentLoaded", () => {
  bindDom();
  bindEvents();
  syncChatSubmitButton();
  boot();
});

function bindDom() {
  Object.assign(dom, {
    canvas: document.getElementById("stage"),
    modelState: document.getElementById("modelState"),
    hud: document.getElementById("hud"),
    settingsBtn: document.getElementById("settingsBtn"),
    newConversationBtn: document.getElementById("newConversationBtn"),
    conversationPicker: document.getElementById("conversationPicker"),
    conversationPickerCloseBtn: document.getElementById("conversationPickerCloseBtn"),
    conversationPickerCount: document.getElementById("conversationPickerCount"),
    conversationPickerStatus: document.getElementById("conversationPickerStatus"),
    conversationPickerList: document.getElementById("conversationPickerList"),
    conversationCreateBtn: document.getElementById("conversationCreateBtn"),
    chatHistorySearchBtn: document.getElementById("chatHistorySearchBtn"),
    bookmarkBtn: document.getElementById("bookmarkBtn"),
    pinWindowBtn: document.getElementById("pinWindowBtn"),
    developerEntryBtn: document.getElementById("developerEntryBtn"),
    toggleChatBtn: document.getElementById("toggleChatBtn"),
    closeAppBtn: document.getElementById("closeAppBtn"),
    chatWrap: document.getElementById("chatWrap"),
    chatResizeHandle: document.getElementById("chatResizeHandle"),
    chatLog: document.getElementById("chatLog"),
    chatHistorySearchPanel: document.getElementById("chatHistorySearchPanel"),
    chatHistorySearchInput: document.getElementById("chatHistorySearchInput"),
    closeChatHistorySearchBtn: document.getElementById("closeChatHistorySearchBtn"),
    chatHistorySearchRoleButtons: [...document.querySelectorAll("[data-history-role]")],
    chatHistorySearchStatus: document.getElementById("chatHistorySearchStatus"),
    chatHistorySearchResults: document.getElementById("chatHistorySearchResults"),
    chatHistorySearchPrevBtn: document.getElementById("chatHistorySearchPrevBtn"),
    chatHistorySearchNextBtn: document.getElementById("chatHistorySearchNextBtn"),
    chatHistorySearchPage: document.getElementById("chatHistorySearchPage"),
    bookmarkPanel: document.getElementById("bookmarkPanel"),
    bookmarkCloseBtn: document.getElementById("bookmarkCloseBtn"),
    bookmarkRefreshBtn: document.getElementById("bookmarkRefreshBtn"),
    bookmarkCount: document.getElementById("bookmarkCount"),
    bookmarkQuickForm: document.getElementById("bookmarkQuickForm"),
    bookmarkQuickName: document.getElementById("bookmarkQuickName"),
    bookmarkQuickUrl: document.getElementById("bookmarkQuickUrl"),
    bookmarkList: document.getElementById("bookmarkList"),
    bookmarkEmpty: document.getElementById("bookmarkEmpty"),
    bookmarkEditor: document.getElementById("bookmarkEditor"),
    bookmarkEditorName: document.getElementById("bookmarkEditorName"),
    bookmarkEditorUrl: document.getElementById("bookmarkEditorUrl"),
    bookmarkEditorCancelBtn: document.getElementById("bookmarkEditorCancelBtn"),
    bookmarkEditorDeleteBtn: document.getElementById("bookmarkEditorDeleteBtn"),
    chatForm: document.getElementById("chatForm"),
    userInput: document.getElementById("userInput"),
    chatSubmitBtn: document.getElementById("chatSubmitBtn"),
    attachmentInput: document.getElementById("attachmentInput"),
    attachmentTray: document.getElementById("attachmentTray"),
    attachFileBtn: document.getElementById("attachFileBtn"),
    replyAnchorPreview: document.getElementById("replyAnchorPreview"),
    replyAnchorLabel: document.getElementById("replyAnchorLabel"),
    replyAnchorText: document.getElementById("replyAnchorText"),
    cancelReplyAnchorBtn: document.getElementById("cancelReplyAnchorBtn"),
    chatDropHint: document.getElementById("chatDropHint"),
    salieriWindowToggle: document.getElementById("salieriWindowToggle"),
    salieriWindow: document.getElementById("salieriWindow"),
    salieriCloseBtn: document.getElementById("salieriCloseBtn"),
    salieriUnlockForm: document.getElementById("salieriUnlockForm"),
    salieriCodeInput: document.getElementById("salieriCodeInput"),
    salieriTranscriptWrap: document.getElementById("salieriTranscriptWrap"),
    salieriTranscript: document.getElementById("salieriTranscript"),
    salieriStatus: document.getElementById("salieriStatus"),
    salieriRefreshBtn: document.getElementById("salieriRefreshBtn"),
    salieriPath: document.getElementById("salieriPath"),
    salieriMessage: document.getElementById("salieriMessage"),
    todoDrawer: document.getElementById("todoDrawer"),
    todoToggleBtn: document.getElementById("todoToggleBtn"),
    todoCountBadge: document.getElementById("todoCountBadge"),
    todoRefreshBtn: document.getElementById("todoRefreshBtn"),
    todoQuickForm: document.getElementById("todoQuickForm"),
    todoQuickTitle: document.getElementById("todoQuickTitle"),
    todoFilterInput: document.getElementById("todoFilterInput"),
    todoList: document.getElementById("todoList"),
    todoEmpty: document.getElementById("todoEmpty"),
    todoEditor: document.getElementById("todoEditor"),
    todoEditorTitle: document.getElementById("todoEditorTitle"),
    todoEditorDetails: document.getElementById("todoEditorDetails"),
    todoEditorDueAt: document.getElementById("todoEditorDueAt"),
    todoEditorDueDate: document.getElementById("todoEditorDueDate"),
    todoEditorPriority: document.getElementById("todoEditorPriority"),
    todoEditorRemindBefore: document.getElementById("todoEditorRemindBefore"),
    todoEditorRecurrence: document.getElementById("todoEditorRecurrence"),
    todoEditorCancelBtn: document.getElementById("todoEditorCancelBtn"),
    todoEditorDeleteBtn: document.getElementById("todoEditorDeleteBtn"),
    bootSequence: document.getElementById("bootSequence"),
    bootLogoVideo: document.getElementById("bootLogoVideo"),
    bootLog: document.getElementById("bootLog"),
    developerPanel: document.getElementById("developerPanel"),
    devPanelCollapseBtn: document.getElementById("devPanelCollapseBtn"),
    devTouchOverlay: document.getElementById("devTouchOverlay"),
    devTouchHit: document.getElementById("devTouchHit"),
    devTouchEvent: document.getElementById("devTouchEvent"),
    devTouchPrompt: document.getElementById("devTouchPrompt"),
    devTouchResult: document.getElementById("devTouchResult"),
    devRelationshipStage: document.getElementById("devRelationshipStage"),
    devRelationshipScopeInput: document.getElementById("devRelationshipScopeInput"),
    devRelationshipScopeStatus: document.getElementById("devRelationshipScopeStatus"),
    devRelationshipRadar: document.getElementById("devRelationshipRadar"),
    devRelationshipRadarValue: document.getElementById("devRelationshipRadarValue"),
    devRelationshipRadarDots: document.getElementById("devRelationshipRadarDots"),
    devRelationshipNextTitle: document.getElementById("devRelationshipNextTitle"),
    devRelationshipReadiness: document.getElementById("devRelationshipReadiness"),
    devRelationshipReadinessValue: document.getElementById("devRelationshipReadinessValue"),
    devRelationshipReactionProfile: document.getElementById("devRelationshipReactionProfile"),
    devRelationshipStagePath: document.getElementById("devRelationshipStagePath"),
    devRelationshipMeters: document.getElementById("devRelationshipMeters"),
    devRelationshipRequirements: document.getElementById("devRelationshipRequirements"),
    devRelationshipExplanation: document.getElementById("devRelationshipExplanation"),
    devRelationshipDetail: document.getElementById("devRelationshipDetail"),
    refreshRelationshipBtn: document.getElementById("refreshRelationshipBtn"),
    devVoiceStatus: document.getElementById("devVoiceStatus"),
    devTabs: [...document.querySelectorAll("[data-dev-tab]")],
    devRuntimeSummary: document.getElementById("devRuntimeSummary"),
    devRefreshRuntimeBtn: document.getElementById("devRefreshRuntimeBtn"),
    devCopyDiagnosticsBtn: document.getElementById("devCopyDiagnosticsBtn"),
    devEmotionInput: document.getElementById("devEmotionInput"),
    devGestureInput: document.getElementById("devGestureInput"),
    devEmotionIntensityInput: document.getElementById("devEmotionIntensityInput"),
    devEmotionIntensityValue: document.getElementById("devEmotionIntensityValue"),
    devEmotionBlushInput: document.getElementById("devEmotionBlushInput"),
    devEmotionBlushValue: document.getElementById("devEmotionBlushValue"),
    devEmotionLockInput: document.getElementById("devEmotionLockInput"),
    devEmotionStatus: document.getElementById("devEmotionStatus"),
    devApplyEmotionBtn: document.getElementById("devApplyEmotionBtn"),
    devReleaseEmotionBtn: document.getElementById("devReleaseEmotionBtn"),
    devTestVoiceBtn: document.getElementById("devTestVoiceBtn"),
    devRestartVoiceBtn: document.getElementById("devRestartVoiceBtn"),
    devOpenVoiceSettingsBtn: document.getElementById("devOpenVoiceSettingsBtn"),
    devVoiceDiagnostic: document.getElementById("devVoiceDiagnostic"),
    devVoiceStopDiagnostic: document.getElementById("devVoiceStopDiagnostic"),
    devVoiceEvents: document.getElementById("devVoiceEvents"),
    devModelProfileInput: document.getElementById("devModelProfileInput"),
    devModelProfileHint: document.getElementById("devModelProfileHint"),
    devModelScaleInput: document.getElementById("devModelScaleInput"),
    devModelOffsetXInput: document.getElementById("devModelOffsetXInput"),
    devModelOffsetYInput: document.getElementById("devModelOffsetYInput"),
    devParameterLockInput: document.getElementById("devParameterLockInput"),
    devParameterAdvancedInput: document.getElementById("devParameterAdvancedInput"),
    devParameterCaptureLabelInput: document.getElementById("devParameterCaptureLabelInput"),
    devParameterFilterInput: document.getElementById("devParameterFilterInput"),
    devParameterControls: document.getElementById("devParameterControls"),
    devParameterCapabilityStatus: document.getElementById("devParameterCapabilityStatus"),
    devParameterStatus: document.getElementById("devParameterStatus"),
    devStartNeutralCalibrationBtn: document.getElementById("devStartNeutralCalibrationBtn"),
    devStartCurrentCalibrationBtn: document.getElementById("devStartCurrentCalibrationBtn"),
    devRefreshParametersBtn: document.getElementById("devRefreshParametersBtn"),
    devResetParametersBtn: document.getElementById("devResetParametersBtn"),
    devCaptureParametersBtn: document.getElementById("devCaptureParametersBtn"),
    devOpenParameterCaptureDirectoryBtn: document.getElementById("devOpenParameterCaptureDirectoryBtn"),
    devTrainingStatus: document.getElementById("devTrainingStatus"),
    devTrainingFilterInput: document.getElementById("devTrainingFilterInput"),
    devTrainingEmotionFilter: document.getElementById("devTrainingEmotionFilter"),
    devTrainingRuntimeFilter: document.getElementById("devTrainingRuntimeFilter"),
    devTrainingNewBtn: document.getElementById("devTrainingNewBtn"),
    devTrainingDuplicateBtn: document.getElementById("devTrainingDuplicateBtn"),
    devTrainingBrowser: document.getElementById("devTrainingBrowser"),
    devTrainingGallery: document.getElementById("devTrainingGallery"),
    devTrainingEditor: document.getElementById("devTrainingEditor"),
    devTrainingCloseEditorBtn: document.getElementById("devTrainingCloseEditorBtn"),
    devTrainingEditorHint: document.getElementById("devTrainingEditorHint"),
    devTrainingLabelInput: document.getElementById("devTrainingLabelInput"),
    devTrainingRecommendedLabelInput: document.getElementById("devTrainingRecommendedLabelInput"),
    devTrainingEmotionInput: document.getElementById("devTrainingEmotionInput"),
    devTrainingStrengthInput: document.getElementById("devTrainingStrengthInput"),
    devTrainingIntensityInput: document.getElementById("devTrainingIntensityInput"),
    devTrainingIntensityValue: document.getElementById("devTrainingIntensityValue"),
    devTrainingTagsInput: document.getElementById("devTrainingTagsInput"),
    devTrainingNotesInput: document.getElementById("devTrainingNotesInput"),
    devTrainingCouplingWarning: document.getElementById("devTrainingCouplingWarning"),
    devTrainingEffectiveOutputs: document.getElementById("devTrainingEffectiveOutputs"),
    devTrainingParameterControls: document.getElementById("devTrainingParameterControls"),
    devTrainingSaveBtn: document.getElementById("devTrainingSaveBtn"),
    devTrainingRestoreBtn: document.getElementById("devTrainingRestoreBtn"),
    devTrainingApplyBtn: document.getElementById("devTrainingApplyBtn"),
    devTrainingActivateBtn: document.getElementById("devTrainingActivateBtn"),
    devTrainingResetRuntimeBtn: document.getElementById("devTrainingResetRuntimeBtn"),
    devTrainingRuntimeStatus: document.getElementById("devTrainingRuntimeStatus"),
    devTrainingExpressionTab: document.getElementById("devTrainingExpressionTab"),
    devTrainingVoiceReferenceTab: document.getElementById("devTrainingVoiceReferenceTab"),
    devExpressionTrainingBrowserContent: document.getElementById("devExpressionTrainingBrowserContent"),
    devVoiceReferenceBrowserContent: document.getElementById("devVoiceReferenceBrowserContent"),
    devVoiceReferenceFilterInput: document.getElementById("devVoiceReferenceFilterInput"),
    devVoiceReferenceStatusFilter: document.getElementById("devVoiceReferenceStatusFilter"),
    devVoiceReferenceRuntimeFilter: document.getElementById("devVoiceReferenceRuntimeFilter"),
    devVoiceReferenceSystemFilter: document.getElementById("devVoiceReferenceSystemFilter"),
    devVoiceReferenceRefreshBtn: document.getElementById("devVoiceReferenceRefreshBtn"),
    devVoiceReferenceAttentionBanner: document.getElementById("devVoiceReferenceAttentionBanner"),
    devVoiceReferenceGallery: document.getElementById("devVoiceReferenceGallery"),
    devVoiceReferenceStatus: document.getElementById("devVoiceReferenceStatus"),
    devVoiceReferenceEditor: document.getElementById("devVoiceReferenceEditor"),
    devVoiceReferenceCloseEditorBtn: document.getElementById("devVoiceReferenceCloseEditorBtn"),
    devVoiceReferenceEditorHint: document.getElementById("devVoiceReferenceEditorHint"),
    devVoiceReferenceSummary: document.getElementById("devVoiceReferenceSummary"),
    devVoiceReferenceCompare: document.getElementById("devVoiceReferenceCompare"),
    devVoiceReferenceOriginalPlayer: document.getElementById("devVoiceReferenceOriginalPlayer"),
    devVoiceReferenceOriginalAudio: document.getElementById("devVoiceReferenceOriginalAudio"),
    devVoiceReferenceCandidateLabel: document.getElementById("devVoiceReferenceCandidateLabel"),
    devVoiceReferenceAudio: document.getElementById("devVoiceReferenceAudio"),
    devVoiceReferenceChangeInfo: document.getElementById("devVoiceReferenceChangeInfo"),
    devVoiceReferenceMappings: document.getElementById("devVoiceReferenceMappings"),
    devVoiceReferenceTranscript: document.getElementById("devVoiceReferenceTranscript"),
  devVoiceReferenceReviewStatusInput: document.getElementById("devVoiceReferenceReviewStatusInput"),
  devVoiceReferenceEmotionLabelInput: document.getElementById("devVoiceReferenceEmotionLabelInput"),
  devVoiceReferenceMappingFitInput: document.getElementById("devVoiceReferenceMappingFitInput"),
  devVoiceReferenceNotesInput: document.getElementById("devVoiceReferenceNotesInput"),
    devVoiceReferenceReasons: document.getElementById("devVoiceReferenceReasons"),
    devVoiceReferenceSelectCandidateBtn: document.getElementById("devVoiceReferenceSelectCandidateBtn"),
    devVoiceReferenceSaveBtn: document.getElementById("devVoiceReferenceSaveBtn"),
    devVoiceReferenceResetBtn: document.getElementById("devVoiceReferenceResetBtn"),
    devModelOverrideJson: document.getElementById("devModelOverrideJson"),
    devModelOverrideStatus: document.getElementById("devModelOverrideStatus"),
    devPreviewModelBtn: document.getElementById("devPreviewModelBtn"),
    devReloadModelsBtn: document.getElementById("devReloadModelsBtn"),
    devSaveModelOverrideBtn: document.getElementById("devSaveModelOverrideBtn"),
    devResetModelOverrideBtn: document.getElementById("devResetModelOverrideBtn"),
    devPersonaStatus: document.getElementById("devPersonaStatus"),
    devPersonaSummary: document.getElementById("devPersonaSummary"),
    devPersonaOverride: document.getElementById("devPersonaOverride"),
    devGeneratedSkill: document.getElementById("devGeneratedSkill"),
    reloadPersonaBtn: document.getElementById("reloadPersonaBtn"),
    savePersonaBtn: document.getElementById("savePersonaBtn"),
    devReplyZh: document.getElementById("devReplyZh"),
    devSpokenJa: document.getElementById("devSpokenJa"),
    devSpokenRepair: document.getElementById("devSpokenRepair"),
    devSpeakingJa: document.getElementById("devSpeakingJa"),
    voiceFailureBanner: document.getElementById("voiceFailureBanner"),
    voiceFailureTitle: document.getElementById("voiceFailureTitle"),
    voiceFailureMessage: document.getElementById("voiceFailureMessage"),
    dismissVoiceFailureBtn: document.getElementById("dismissVoiceFailureBtn"),
    toast: document.getElementById("toast"),
    settingsModal: document.getElementById("settingsModal"),
    closeSettingsBtn: document.getElementById("closeSettingsBtn"),
    settingsStatus: document.getElementById("settingsStatus"),
    settingsTabs: [...document.querySelectorAll(".settings-tabs button")],
    tabPages: [...document.querySelectorAll(".tab-page")],
    settingsPersonaStatus: document.getElementById("settingsPersonaStatus"),
    settingsPersonaOverride: document.getElementById("settingsPersonaOverride"),
    reloadSettingsPersonaBtn: document.getElementById("reloadSettingsPersonaBtn"),
    saveSettingsPersonaBtn: document.getElementById("saveSettingsPersonaBtn"),
    apiEndpointInput: document.getElementById("apiEndpointInput"),
    apiModelInput: document.getElementById("apiModelInput"),
    cognitiveRepairModelInput: document.getElementById("cognitiveRepairModelInput"),
    cognitiveAuditModelInput: document.getElementById("cognitiveAuditModelInput"),
    cognitiveSemanticAuditEnabledInput: document.getElementById("cognitiveSemanticAuditEnabledInput"),
    apiKeyInput: document.getElementById("apiKeyInput"),
    visionEnabledInput: document.getElementById("visionEnabledInput"),
    visionApiKeyInput: document.getElementById("visionApiKeyInput"),
    visionApiHint: document.getElementById("visionApiHint"),
    apiNetworkModeInput: document.getElementById("apiNetworkModeInput"),
    apiProxyUrlInput: document.getElementById("apiProxyUrlInput"),
    responseFormatInput: document.getElementById("responseFormatInput"),
    apiKeyHint: document.getElementById("apiKeyHint"),
    saveApiBtn: document.getElementById("saveApiBtn"),
    testApiBtn: document.getElementById("testApiBtn"),
    apiTestStatus: document.getElementById("apiTestStatus"),
    windowWidthInput: document.getElementById("windowWidthInput"),
    windowHeightInput: document.getElementById("windowHeightInput"),
    layoutModeInput: document.getElementById("layoutModeInput"),
    modelScaleInput: document.getElementById("modelScaleInput"),
    modelOffsetXInput: document.getElementById("modelOffsetXInput"),
    modelOffsetYInput: document.getElementById("modelOffsetYInput"),
    modelScaleValue: document.getElementById("modelScaleValue"),
    live2dModelInput: document.getElementById("live2dModelInput"),
    live2dModelHint: document.getElementById("live2dModelHint"),
    backgroundPresetInput: document.getElementById("backgroundPresetInput"),
    customBackgroundPathInput: document.getElementById("customBackgroundPathInput"),
    backgroundFileRow: document.getElementById("backgroundFileRow"),
    backgroundHint: document.getElementById("backgroundHint"),
    chooseBackgroundBtn: document.getElementById("chooseBackgroundBtn"),
    resetModelTransformBtn: document.getElementById("resetModelTransformBtn"),
    alwaysOnTopInput: document.getElementById("alwaysOnTopInput"),
    hudCollapsedInput: document.getElementById("hudCollapsedInput"),
    voiceEnabledInput: document.getElementById("voiceEnabledInput"),
    lipSyncEnabledInput: document.getElementById("lipSyncEnabledInput"),
    autoStartGptSovitsInput: document.getElementById("autoStartGptSovitsInput"),
    alignSpokenJaInput: document.getElementById("alignSpokenJaInput"),
    ttsDeliveryPostProcessingEnabledInput: document.getElementById("ttsDeliveryPostProcessingEnabledInput"),
    ttsProviderInput: document.getElementById("ttsProviderInput"),
    ttsEndpointInput: document.getElementById("ttsEndpointInput"),
    ttsSpeakerInput: document.getElementById("ttsSpeakerInput"),
    speechLanguageInput: document.getElementById("speechLanguageInput"),
    speechProsodyInput: document.getElementById("speechProsodyInput"),
    voiceLocaleInput: document.getElementById("voiceLocaleInput"),
    speechRateInput: document.getElementById("speechRateInput"),
    gptSovitsStatus: document.getElementById("gptSovitsStatus"),
    gptSovitsPresetInput: document.getElementById("gptSovitsPresetInput"),
    gptSovitsPresetHint: document.getElementById("gptSovitsPresetHint"),
    gptSovitsPortInput: document.getElementById("gptSovitsPortInput"),
    gptSovitsCondaEnvInput: document.getElementById("gptSovitsCondaEnvInput"),
    gptSovitsRootInput: document.getElementById("gptSovitsRootInput"),
    gptSovitsGptModelInput: document.getElementById("gptSovitsGptModelInput"),
    gptSovitsSovitsModelInput: document.getElementById("gptSovitsSovitsModelInput"),
    gptSovitsRefAudioInput: document.getElementById("gptSovitsRefAudioInput"),
    gptSovitsRefTextInput: document.getElementById("gptSovitsRefTextInput"),
    gptSovitsStyleMapInput: document.getElementById("gptSovitsStyleMapInput"),
    gptSovitsTargetLanguageInput: document.getElementById("gptSovitsTargetLanguageInput"),
    startGptSovitsBtn: document.getElementById("startGptSovitsBtn"),
    stopGptSovitsBtn: document.getElementById("stopGptSovitsBtn"),
    testGptSovitsBtn: document.getElementById("testGptSovitsBtn"),
    saveDisplayBtn: document.getElementById("saveDisplayBtn"),
    saveInteractionBtn: document.getElementById("saveInteractionBtn"),
    saveVoiceBtn: document.getElementById("saveVoiceBtn"),
    savePermissionsBtn: document.getElementById("savePermissionsBtn"),
    permissionReadScreenInput: document.getElementById("permissionReadScreenInput"),
    permissionReadCameraInput: document.getElementById("permissionReadCameraInput"),
    cameraIndexInput: document.getElementById("cameraIndexInput"),
    permissionReadSystemTimeInput: document.getElementById("permissionReadSystemTimeInput"),
    permissionWebSearchInput: document.getElementById("permissionWebSearchInput"),
    permissionFileReadInput: document.getElementById("permissionFileReadInput"),
    permissionFileWriteInput: document.getElementById("permissionFileWriteInput"),
    permissionOpenBrowserInput: document.getElementById("permissionOpenBrowserInput"),
    agentFileWorkspaceInput: document.getElementById("agentFileWorkspaceInput"),
    webSearchProviderInput: document.getElementById("webSearchProviderInput"),
    webSearchApiKeyInput: document.getElementById("webSearchApiKeyInput"),
    webSearchMaxResultsInput: document.getElementById("webSearchMaxResultsInput"),
    webReadTopPagesInput: document.getElementById("webReadTopPagesInput"),
    webSearchStatus: document.getElementById("webSearchStatus"),
    testWebSearchBtn: document.getElementById("testWebSearchBtn"),
    proactiveInteractionsEnabledInput: document.getElementById("proactiveInteractionsEnabledInput"),
    proactiveFrequencyInput: document.getElementById("proactiveFrequencyInput"),
    proactiveScreenObservationEnabledInput: document.getElementById("proactiveScreenObservationEnabledInput"),
    proactiveScreenObservationChanceInput: document.getElementById("proactiveScreenObservationChanceInput"),
    proactiveTopicInputs: [...document.querySelectorAll("[data-proactive-topic]")],
    proactiveTopicsStatus: document.getElementById("proactiveTopicsStatus"),
    debugDirectSpeechInput: document.getElementById("debugDirectSpeechInput"),
    memoryFileInput: document.getElementById("memoryFileInput"),
    chooseMemoryBtn: document.getElementById("chooseMemoryBtn"),
    memoryFileHint: document.getElementById("memoryFileHint"),
    screenCaptureDirectoryInput: document.getElementById("screenCaptureDirectoryInput"),
    openScreenCaptureDirectoryBtn: document.getElementById("openScreenCaptureDirectoryBtn"),
    memoryEditor: document.getElementById("memoryEditor"),
    memoryRecordDetails: document.getElementById("memoryRecordDetails"),
    reloadMemoryBtn: document.getElementById("reloadMemoryBtn"),
    saveMemoryBtn: document.getElementById("saveMemoryBtn"),
    generateMemoryBtn: document.getElementById("generateMemoryBtn"),
    clearMemoryBtn: document.getElementById("clearMemoryBtn"),
    clearShortMemoryBtn: document.getElementById("clearShortMemoryBtn"),
    clearChatBtn: document.getElementById("clearChatBtn"),
    cacheStatus: document.getElementById("cacheStatus"),
    refreshCacheStatusBtn: document.getElementById("refreshCacheStatusBtn"),
    clearUnusedCacheBtn: document.getElementById("clearUnusedCacheBtn"),
    clearAllDisposableCacheBtn: document.getElementById("clearAllDisposableCacheBtn"),
  });
  const todoDrawerOpen = window.localStorage.getItem(TODO_DRAWER_OPEN_KEY) === "1";
  dom.todoDrawer?.classList.toggle("open", todoDrawerOpen);
  dom.todoDrawer?.setAttribute("aria-expanded", String(todoDrawerOpen));
  dom.todoToggleBtn?.setAttribute("aria-label", todoDrawerOpen ? "收起待办" : "展开待办");
}

function bindEvents() {
  window.addEventListener("resize", () => {
    fitModel();
    applyChatWindowHeight(state.chatWindowHeight, { persist: false });
  });
  window.addEventListener("pointermove", event => {
    pointer.x = clamp((event.clientX / Math.max(1, window.innerWidth) - 0.5) * 2, -1, 1);
    pointer.y = clamp((event.clientY / Math.max(1, window.innerHeight) - 0.5) * 2, -1, 1);
  });
  bindWindowDragRegions();

  dom.settingsBtn.addEventListener("click", openSettings);
  dom.newConversationBtn?.addEventListener("click", () => {
    if (dom.conversationPicker?.classList.contains("hidden")) openConversationPicker();
    else closeConversationPicker();
  });
  dom.conversationPickerCloseBtn?.addEventListener("click", closeConversationPicker);
  dom.conversationCreateBtn?.addEventListener("click", startNewConversation);
  dom.salieriWindowToggle?.addEventListener("click", () => {
    const memoryPageVisible = !dom.settingsModal?.classList.contains("hidden")
      && dom.tabPages?.some(page => page.dataset.page === "memory" && page.classList.contains("active"));
    if (!memoryPageVisible) return;
    const opening = dom.salieriWindow?.classList.contains("hidden");
    dom.salieriWindow?.classList.toggle("hidden", !opening);
    dom.salieriWindowToggle?.setAttribute("aria-expanded", String(opening));
    if (opening) refreshSalieriTranscript({ quiet: true });
  });
  dom.salieriCloseBtn?.addEventListener("click", () => {
    dom.salieriWindow?.classList.add("hidden");
    dom.salieriWindowToggle?.setAttribute("aria-expanded", "false");
  });
  dom.salieriUnlockForm?.addEventListener("submit", unlockSalieriRecorder);
  dom.salieriRefreshBtn?.addEventListener("click", () => refreshSalieriTranscript());
  dom.chatHistorySearchBtn?.addEventListener("click", openChatHistorySearch);
  dom.bookmarkBtn?.addEventListener("click", () => {
    if (dom.bookmarkPanel?.classList.contains("hidden")) openBookmarkPanel();
    else closeBookmarkPanel();
  });
  dom.bookmarkCloseBtn?.addEventListener("click", closeBookmarkPanel);
  dom.bookmarkRefreshBtn?.addEventListener("click", loadBookmarks);
  dom.bookmarkQuickForm?.addEventListener("submit", createQuickBookmark);
  dom.bookmarkList?.addEventListener("click", handleBookmarkListClick);
  dom.bookmarkEditor?.addEventListener("submit", saveBookmarkEditor);
  dom.bookmarkEditorCancelBtn?.addEventListener("click", closeBookmarkEditor);
  dom.bookmarkEditorDeleteBtn?.addEventListener("click", deleteEditedBookmark);
  dom.closeChatHistorySearchBtn?.addEventListener("click", closeChatHistorySearch);
  dom.chatHistorySearchInput?.addEventListener("input", scheduleChatHistorySearch);
  dom.chatHistorySearchInput?.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      chatHistorySearchState.offset = 0;
      performChatHistorySearch();
    }
  });
  dom.chatHistorySearchRoleButtons?.forEach(button => {
    button.addEventListener("click", () => {
      chatHistorySearchState.role = String(button.dataset.historyRole || "all");
      chatHistorySearchState.offset = 0;
      updateChatHistorySearchRoleButtons();
      performChatHistorySearch();
    });
  });
  dom.chatHistorySearchPrevBtn?.addEventListener("click", () => {
    chatHistorySearchState.offset = Math.max(
      0,
      chatHistorySearchState.offset - chatHistorySearchState.limit,
    );
    performChatHistorySearch();
  });
  dom.chatHistorySearchNextBtn?.addEventListener("click", () => {
    if (!chatHistorySearchState.hasMore) return;
    chatHistorySearchState.offset += chatHistorySearchState.limit;
    performChatHistorySearch();
  });
  dom.pinWindowBtn?.addEventListener("click", toggleAlwaysOnTop);
  dom.developerEntryBtn?.addEventListener("click", openDeveloperEntry);
  dom.closeSettingsBtn.addEventListener("click", closeSettings);
  dom.dismissVoiceFailureBtn?.addEventListener("click", clearVoiceFailure);
  dom.settingsModal.addEventListener("click", event => {
    if (event.target === dom.settingsModal) closeSettings();
  });

  dom.toggleChatBtn.addEventListener("click", () => {
    setChatCollapsed(!dom.hud.classList.contains("collapsed"));
  });
  dom.todoToggleBtn?.addEventListener("click", () => {
    setTodoDrawerOpen(!dom.todoDrawer?.classList.contains("open"));
  });
  dom.todoRefreshBtn?.addEventListener("click", loadTodos);
  dom.todoFilterInput?.addEventListener("change", loadTodos);
  dom.todoQuickForm?.addEventListener("submit", createQuickTodo);
  dom.todoList?.addEventListener("click", handleTodoListClick);
  dom.todoList?.addEventListener("change", handleTodoListChange);
  dom.todoEditor?.addEventListener("submit", saveTodoEditor);
  dom.todoEditorDueAt?.addEventListener("change", () => {
    if (dom.todoEditorDueAt.value) dom.todoEditorDueDate.value = "";
  });
  dom.todoEditorDueDate?.addEventListener("change", () => {
    if (dom.todoEditorDueDate.value) dom.todoEditorDueAt.value = "";
  });
  dom.todoEditorCancelBtn?.addEventListener("click", closeTodoEditor);
  dom.todoEditorDeleteBtn?.addEventListener("click", deleteEditedTodo);

  dom.closeAppBtn.addEventListener("click", async () => {
    if (window.pywebview?.api?.closeApp) {
      await window.pywebview.api.closeApp();
    } else {
      window.close();
    }
  });

  document.querySelectorAll(".quick-actions button").forEach(button => {
    button.addEventListener("click", () => {
      setEmotion(button.dataset.emotion, button.dataset.gesture);
      toast(`${button.textContent} / ${button.dataset.gesture}`);
    });
  });

  dom.chatForm.addEventListener("submit", event => {
    event.preventDefault();
    if (shouldStopCurrentAnswer()) {
      stopCurrentAnswer();
      return;
    }
    sendMessage();
  });
  dom.attachFileBtn?.addEventListener("click", () => dom.attachmentInput?.click());
  dom.cancelReplyAnchorBtn?.addEventListener("click", clearPendingReplyAnchor);
  dom.attachmentInput?.addEventListener("change", () => {
    addPendingAttachments([...dom.attachmentInput.files]);
    dom.attachmentInput.value = "";
  });
  dom.userInput?.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      dom.chatForm.requestSubmit();
    }
  });
  dom.userInput?.addEventListener("input", () => {
    resizeChatInput();
    syncChatSubmitButton();
  });
  bindAttachmentDropEvents();
  bindChatResizeHandle();

  dom.settingsTabs.forEach(button => {
    button.addEventListener("click", () => {
      activateSettingsTab(button.dataset.tab);
      if (button.dataset.tab === "storage") refreshCacheStatus();
      if (button.dataset.tab === "persona") loadPersona();
    });
  });
  dom.apiKeyInput?.addEventListener("focus", () => {
    if (dom.apiKeyInput.value === SAVED_API_KEY_MASK) dom.apiKeyInput.select();
  });
  dom.apiKeyInput?.addEventListener("beforeinput", () => {
    if (dom.apiKeyInput.value === SAVED_API_KEY_MASK) dom.apiKeyInput.value = "";
  });
  dom.visionApiKeyInput?.addEventListener("focus", () => {
    if (dom.visionApiKeyInput.value === SAVED_API_KEY_MASK) dom.visionApiKeyInput.select();
  });
  dom.visionApiKeyInput?.addEventListener("beforeinput", () => {
    if (dom.visionApiKeyInput.value === SAVED_API_KEY_MASK) dom.visionApiKeyInput.value = "";
  });
  dom.saveApiBtn.addEventListener("click", saveApiSettings);
  dom.testApiBtn.addEventListener("click", testApiConnection);
  dom.saveDisplayBtn.addEventListener("click", saveDisplaySettings);
  dom.saveInteractionBtn?.addEventListener("click", saveInteractionSettings);
  dom.saveVoiceBtn.addEventListener("click", saveVoiceSettings);
  dom.savePermissionsBtn.addEventListener("click", savePermissionsSettings);
  dom.testWebSearchBtn?.addEventListener("click", testWebSearchConnection);
  dom.proactiveInteractionsEnabledInput?.addEventListener("change", updateProactiveTopicControls);
  dom.proactiveScreenObservationEnabledInput?.addEventListener("change", updateProactiveTopicControls);
  dom.proactiveTopicInputs?.forEach(input => input.addEventListener("change", updateProactiveTopicControls));
  if (dom.reloadPersonaBtn) dom.reloadPersonaBtn.addEventListener("click", loadPersona);
  if (dom.savePersonaBtn) {
    dom.savePersonaBtn.addEventListener("click", () => savePersonaOverride(
      dom.devPersonaOverride,
      dom.savePersonaBtn,
    ));
  }
  dom.reloadSettingsPersonaBtn?.addEventListener("click", loadPersona);
  dom.saveSettingsPersonaBtn?.addEventListener("click", () => savePersonaOverride(
    dom.settingsPersonaOverride,
    dom.saveSettingsPersonaBtn,
  ));
  if (dom.refreshRelationshipBtn) dom.refreshRelationshipBtn.addEventListener("click", loadDeveloperRelationship);
  dom.devRelationshipScopeInput?.addEventListener("change", loadDeveloperRelationship);
  dom.startGptSovitsBtn.addEventListener("click", startGptSovits);
  dom.stopGptSovitsBtn.addEventListener("click", stopGptSovits);
  dom.testGptSovitsBtn.addEventListener("click", testRunningGptSovits);
  dom.gptSovitsPresetInput.addEventListener("change", loadSelectedVoicePresetDraft);
  dom.speechLanguageInput.addEventListener("change", () => {
    const profile = speechLanguageProfile(dom.speechLanguageInput.value);
    dom.voiceLocaleInput.value = profile.locale;
    dom.gptSovitsTargetLanguageInput.value = profile.targetLanguage;
    dom.alignSpokenJaInput.disabled = profile.speechLanguage !== "ja-JP";
  });
  // Voice controls are an explicit draft.  They are persisted only by the
  // two clearly named actions below; changing a checkbox must not silently
  // save or restart a model behind the user's back.
  if (dom.debugDirectSpeechInput) dom.debugDirectSpeechInput.addEventListener("change", saveDebugSettings);
  dom.devTabs.forEach(button => button.addEventListener("click", () => switchDeveloperPage(button.dataset.devTab)));
  dom.devPanelCollapseBtn?.addEventListener("click", () => {
    setDeveloperPanelCollapsed(!dom.developerPanel?.classList.contains("dev-panel-collapsed"));
  });
  dom.devRefreshRuntimeBtn?.addEventListener("click", refreshDeveloperRuntime);
  dom.devCopyDiagnosticsBtn?.addEventListener("click", copyDeveloperDiagnostics);
  dom.devEmotionInput?.addEventListener("change", loadDeveloperEmotionDefaults);
  dom.devEmotionIntensityInput?.addEventListener("input", updateDeveloperEmotionReadouts);
  dom.devEmotionBlushInput?.addEventListener("input", updateDeveloperEmotionReadouts);
  dom.devApplyEmotionBtn?.addEventListener("click", applyDeveloperEmotionControl);
  dom.devReleaseEmotionBtn?.addEventListener("click", releaseDeveloperEmotionControl);
  dom.devTestVoiceBtn?.addEventListener("click", testDeveloperVoice);
  dom.devRestartVoiceBtn?.addEventListener("click", restartDeveloperVoice);
  dom.devModelProfileInput?.addEventListener("change", renderDeveloperModelEditor);
  dom.devPreviewModelBtn?.addEventListener("click", previewDeveloperModel);
  dom.devReloadModelsBtn?.addEventListener("click", reloadDeveloperModels);
  dom.devParameterLockInput?.addEventListener("change", toggleDeveloperParameterLock);
  dom.devParameterAdvancedInput?.addEventListener("change", toggleDeveloperParameterAdvanced);
  dom.devParameterFilterInput?.addEventListener("input", filterDeveloperParameterControls);
  dom.devParameterControls?.addEventListener("input", handleDeveloperParameterInput);
  dom.devParameterControls?.addEventListener("click", handleDeveloperParameterReset);
  dom.devRefreshParametersBtn?.addEventListener("click", refreshDeveloperParameterControls);
  dom.devStartNeutralCalibrationBtn?.addEventListener("click", () => startDeveloperExpressionCalibration("default"));
  dom.devStartCurrentCalibrationBtn?.addEventListener("click", () => startDeveloperExpressionCalibration("current"));
  dom.devResetParametersBtn?.addEventListener("click", resetDeveloperParameterControls);
  dom.devCaptureParametersBtn?.addEventListener("click", captureDeveloperParameterSnapshot);
  dom.devOpenParameterCaptureDirectoryBtn?.addEventListener("click", openDeveloperParameterCaptureDirectory);
  dom.devTrainingFilterInput?.addEventListener("input", renderDeveloperExpressionTrainingGallery);
  dom.devTrainingEmotionFilter?.addEventListener("change", renderDeveloperExpressionTrainingGallery);
  dom.devTrainingRuntimeFilter?.addEventListener("change", renderDeveloperExpressionTrainingGallery);
  dom.devTrainingGallery?.addEventListener("click", handleDeveloperExpressionTrainingGalleryClick);
  dom.devTrainingParameterControls?.addEventListener("input", handleDeveloperExpressionTrainingParameterInput);
  dom.devTrainingNewBtn?.addEventListener("click", newDeveloperExpressionTrainingSample);
  dom.devTrainingDuplicateBtn?.addEventListener("click", duplicateDeveloperExpressionTrainingSample);
  dom.devTrainingCloseEditorBtn?.addEventListener("click", closeDeveloperExpressionTrainingEditor);
  dom.devTrainingSaveBtn?.addEventListener("click", saveDeveloperExpressionTrainingSample);
  dom.devTrainingRestoreBtn?.addEventListener("click", restoreDeveloperExpressionTrainingRecommendation);
  dom.devTrainingApplyBtn?.addEventListener("click", applySelectedDeveloperExpressionTrainingSample);
  dom.devTrainingActivateBtn?.addEventListener("click", activateSelectedDeveloperExpressionTrainingSample);
  dom.devTrainingResetRuntimeBtn?.addEventListener("click", resetSelectedDeveloperExpressionRuntime);
  dom.devTrainingExpressionTab?.addEventListener("click", () => setDeveloperTrainingKind("expression"));
  dom.devTrainingVoiceReferenceTab?.addEventListener("click", () => setDeveloperTrainingKind("voice-reference"));
  dom.devVoiceReferenceFilterInput?.addEventListener("input", renderDeveloperVoiceReferenceGallery);
  dom.devVoiceReferenceStatusFilter?.addEventListener("change", renderDeveloperVoiceReferenceGallery);
  dom.devVoiceReferenceRuntimeFilter?.addEventListener("change", renderDeveloperVoiceReferenceGallery);
  dom.devVoiceReferenceSystemFilter?.addEventListener("change", renderDeveloperVoiceReferenceGallery);
  dom.devVoiceReferenceRefreshBtn?.addEventListener("click", () => loadDeveloperVoiceReferences(true));
  dom.devVoiceReferenceGallery?.addEventListener("click", handleDeveloperVoiceReferenceGalleryClick);
  dom.devVoiceReferenceCloseEditorBtn?.addEventListener("click", closeDeveloperVoiceReferenceEditor);
  dom.devVoiceReferenceSaveBtn?.addEventListener("click", saveDeveloperVoiceReferenceReview);
  dom.devVoiceReferenceResetBtn?.addEventListener("click", resetDeveloperVoiceReferenceReview);
  dom.devVoiceReferenceSelectCandidateBtn?.addEventListener("click", selectDeveloperVoiceReferenceCandidate);
dom.devVoiceReferenceReviewStatusInput?.addEventListener("change", handleDeveloperVoiceReferenceStatusChange);
dom.devVoiceReferenceEmotionLabelInput?.addEventListener("input", markDeveloperVoiceReferenceDirty);
dom.devVoiceReferenceMappingFitInput?.addEventListener("change", handleDeveloperVoiceReferenceMappingFitChange);
  dom.devVoiceReferenceNotesInput?.addEventListener("input", markDeveloperVoiceReferenceDirty);
  dom.devVoiceReferenceReasons?.addEventListener("change", handleDeveloperVoiceReferenceReasonChange);
  [
    dom.devTrainingLabelInput,
    dom.devTrainingEmotionInput,
    dom.devTrainingStrengthInput,
    dom.devTrainingTagsInput,
    dom.devTrainingNotesInput,
  ].forEach(input => input?.addEventListener("input", markDeveloperExpressionTrainingDirty));
  dom.devTrainingIntensityInput?.addEventListener("input", updateDeveloperExpressionTrainingIntensity);
  dom.devSaveModelOverrideBtn?.addEventListener("click", saveDeveloperModelOverride);
  dom.devResetModelOverrideBtn?.addEventListener("click", resetDeveloperModelOverride);
  dom.devOpenVoiceSettingsBtn?.addEventListener("click", () => {
    openSettings();
    activateSettingsTab("voice");
  });
  dom.chooseMemoryBtn?.addEventListener("click", chooseMemoryFile);
  dom.openScreenCaptureDirectoryBtn?.addEventListener("click", openScreenCaptureDirectory);
  dom.reloadMemoryBtn.addEventListener("click", loadMemory);
  dom.saveMemoryBtn.addEventListener("click", saveMemory);
  dom.generateMemoryBtn.addEventListener("click", generateMemory);
  dom.clearMemoryBtn.addEventListener("click", clearMemory);
  dom.clearShortMemoryBtn.addEventListener("click", clearShortMemory);
  dom.clearChatBtn.addEventListener("click", clearChat);
  dom.refreshCacheStatusBtn?.addEventListener("click", refreshCacheStatus);
  dom.clearUnusedCacheBtn?.addEventListener("click", () => clearRuntimeCache("unused"));
  dom.clearAllDisposableCacheBtn?.addEventListener("click", () => clearRuntimeCache("all_disposable"));

  dom.layoutModeInput.addEventListener("input", () => {
    const nextMode = normalizeLayoutMode(dom.layoutModeInput.value);
    if (settings.layoutMode === "lite") {
      settings.liteWindowWidth = clamp(toNumber(dom.windowWidthInput.value, settings.liteWindowWidth), 360, 2200);
      settings.liteWindowHeight = clamp(toNumber(dom.windowHeightInput.value, settings.liteWindowHeight), 520, 2200);
    } else {
      settings.fullWindowWidth = clamp(toNumber(dom.windowWidthInput.value, settings.fullWindowWidth), 1040, 2200);
      settings.fullWindowHeight = clamp(toNumber(dom.windowHeightInput.value, settings.fullWindowHeight), 720, 2200);
    }
    settings.layoutMode = nextMode;
    settings.windowWidth = nextMode === "lite" ? settings.liteWindowWidth : settings.fullWindowWidth;
    settings.windowHeight = nextMode === "lite" ? settings.liteWindowHeight : settings.fullWindowHeight;
    syncLayoutSizeInputs();
    applySettingsToUi();
  });
  dom.live2dModelInput?.addEventListener("change", () => {
    updateLive2DModelHint();
    updateModelTransformInputs();
  });
  [dom.modelScaleInput, dom.modelOffsetXInput, dom.modelOffsetYInput].forEach(input => {
    input.addEventListener("input", () => {
      previewSelectedModelTransform();
    });
  });
  dom.resetModelTransformBtn?.addEventListener("click", resetSelectedModelTransform);
  dom.backgroundPresetInput?.addEventListener("change", previewBackgroundSelection);
  dom.chooseBackgroundBtn?.addEventListener("click", chooseCustomBackground);

  window.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (!dom.bookmarkEditor?.classList.contains("hidden")) {
      closeBookmarkEditor();
      return;
    }
    if (!dom.conversationPicker?.classList.contains("hidden")) {
      closeConversationPicker();
      return;
    }
    if (!dom.bookmarkPanel?.classList.contains("hidden")) {
      closeBookmarkPanel();
      return;
    }
    if (!dom.chatHistorySearchPanel?.classList.contains("hidden")) {
      closeChatHistorySearch();
      return;
    }
    if (!dom.settingsModal.classList.contains("hidden")) {
      closeSettings();
      return;
    }
    setChatCollapsed(!dom.hud.classList.contains("collapsed"));
  });
}

function bindWindowDragRegions() {
  document.querySelectorAll("[data-window-drag]").forEach(region => {
    region.addEventListener("mousedown", event => {
      if (event.button !== 0 || event.target.closest("button, input, textarea, select, a")) return;
      const platform = String(window.pywebview?.platform || "").toLowerCase();
      const nativeApi = window.pywebview?.api;
      // On other desktop backends, keep pywebview's built-in drag-region path.
      if (!nativeApi?.beginWindowDrag || !nativeApi?.moveWindow
        || (platform && !["edgechromium", "mshtml"].includes(platform))) return;
      event.preventDefault();
      event.stopPropagation();
      const drag = {
        startScreenX: event.screenX,
        startScreenY: event.screenY,
        screenX: event.screenX,
        screenY: event.screenY,
        originX: null,
        originY: null,
        sentX: null,
        sentY: null,
        moving: false,
        released: false,
      };

      const finish = moveEvent => {
        if (moveEvent) {
          drag.screenX = moveEvent.screenX;
          drag.screenY = moveEvent.screenY;
        }
        drag.released = true;
        window.removeEventListener("mousemove", track);
        window.removeEventListener("mouseup", finish);
        flush();
      };
      const flush = () => {
        if (drag.originX === null || drag.moving) return;
        const targetX = Math.round(drag.originX + drag.screenX - drag.startScreenX);
        const targetY = Math.round(drag.originY + drag.screenY - drag.startScreenY);
        if (targetX === drag.sentX && targetY === drag.sentY) return;
        drag.sentX = targetX;
        drag.sentY = targetY;
        drag.moving = true;
        Promise.resolve(nativeApi.moveWindow(targetX, targetY)).then(result => {
          if (result?.ok === false) throw new Error(result.error || "窗口移动失败");
        }).catch(error => console.warn("native window move failed:", error)).finally(() => {
          drag.moving = false;
          const latestX = Math.round(drag.originX + drag.screenX - drag.startScreenX);
          const latestY = Math.round(drag.originY + drag.screenY - drag.startScreenY);
          if (latestX !== drag.sentX || latestY !== drag.sentY) flush();
        });
      };
      const track = moveEvent => {
        drag.screenX = moveEvent.screenX;
        drag.screenY = moveEvent.screenY;
        flush();
      };
      window.addEventListener("mousemove", track);
      window.addEventListener("mouseup", finish);
      Promise.resolve(nativeApi.beginWindowDrag()).then(result => {
        if (result?.ok === false) throw new Error(result.error || "窗口拖动初始化失败");
        drag.originX = Number(result.x) || 0;
        drag.originY = Number(result.y) || 0;
        flush();
      }).catch(error => {
        window.removeEventListener("mousemove", track);
        window.removeEventListener("mouseup", finish);
        console.warn("native window drag failed:", error);
      });
    });
  });
}

function syncChatToggleButton() {
  if (!dom.toggleChatBtn || !dom.hud) return;
  const collapsed = dom.hud.classList.contains("collapsed");
  const actionLabel = collapsed ? "展开对话框" : "收起对话框";
  dom.toggleChatBtn.textContent = collapsed ? "^" : "v";
  dom.toggleChatBtn.title = actionLabel;
  dom.toggleChatBtn.setAttribute("aria-label", actionLabel);
  dom.toggleChatBtn.setAttribute("aria-expanded", String(!collapsed));
}

function setChatCollapsed(collapsed, { scroll = true } = {}) {
  dom.hud.classList.toggle("collapsed", Boolean(collapsed));
  if (dom.hudCollapsedInput) dom.hudCollapsedInput.checked = Boolean(collapsed);
  syncChatToggleButton();
  if (!collapsed) {
    applyChatWindowHeight(state.chatWindowHeight, { persist: false });
    if (scroll) scrollChatToLatest();
  }
}

async function openDeveloperEntry() {
  const devUrl = `${window.location.origin}${window.location.pathname}?dev=1`;
  if (window.pywebview?.api?.openDeveloperWindow) {
    try {
      await window.pywebview.api.openDeveloperWindow();
      return;
    } catch (error) {
      console.warn("open developer window failed:", error);
    }
  }
  window.open(devUrl, "amadeus-developer", "width=1040,height=900");
}

async function boot() {
  initSpeechVoices();
  await loadTouchRegionConfig();
  await loadSettings();
  configureRuntimeMode();
  if (DEV_MODE && !MODEL_PREVIEW_ONLY && dom.developerPanel) {
    const personaMonitorRoot = document.getElementById("devPersonaSkillMonitor");
    if (personaMonitorRoot && window.AmadeusPersonaSkills && !developerPersonaSkillMonitor) {
      developerPersonaSkillMonitor = window.AmadeusPersonaSkills.mount(personaMonitorRoot, storageScopedFetch);
    }
    dom.developerPanel.classList.remove("hidden");
    setDeveloperPanelCollapsed(window.localStorage.getItem(DEV_PANEL_COLLAPSED_KEY) === "1", { persist: false });
    dom.devTouchOverlay?.classList.remove("hidden");
    updateDeveloperSpeechPanel();
    loadPersona();
    loadDeveloperRelationship();
    switchDeveloperPage(MODEL_PREVIEW_ID ? "model" : "voice");
  }
  await loadLive2DModelRegistry();
  await loadMemory();
  await hydrateConversationHistory();
  await loadTodos();
  await loadBookmarks();
  if (!settings.releaseMode) {
    await refreshSalieriTranscript({ quiet: true });
    startSalieriRefreshPolling();
  } else {
    window.clearInterval(salieriRefreshTimer);
  }
  startTodoReminderPolling();
  syncPendingProactiveConversationEvents();
  startConnectionWatchdog();
  applySettingsToUi();
  fillSettingsForm();
  if (DEV_MODE) {
    refreshDeveloperRuntime();
    developerRuntimeTimer = window.setInterval(refreshDeveloperRuntime, 3000);
  }
  const voiceReadyPromise = ensureGptSovitsStarted();
  const greetingReadyPromise = prepareStartupGreeting(voiceReadyPromise);
  let resolveModelReady;
  const modelReadyPromise = new Promise(resolve => { resolveModelReady = resolve; });
  const readinessPromise = Promise.allSettled([voiceReadyPromise, greetingReadyPromise, modelReadyPromise]);
  const bootAnimationPromise = runBootSequence(modelReadyPromise, readinessPromise);
  pruneStaleFallbacks();
  renderMessages();

  try {
    dom.modelState.textContent = runtimeModeStateLabel("LOADING CUBISM MODEL");
    if (PIXI.settings) {
      PIXI.settings.RESOLUTION = RENDER_RESOLUTION;
      PIXI.settings.ROUND_PIXELS = false;
      if (PIXI.SCALE_MODES) {
        PIXI.settings.SCALE_MODE = PIXI.SCALE_MODES.LINEAR;
      }
    }
    app = new PIXI.Application({
      view: dom.canvas,
      autoStart: true,
      resizeTo: window,
      backgroundColor: 0x091011,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: RENDER_RESOLUTION,
    });

    backgroundLayer = new PIXI.Container();
    backgroundLayer.interactive = false;
    app.stage.addChild(backgroundLayer);
    drawPixiBackground();

    model = await PIXI.live2d.Live2DModel.from(activeModelProfile.modelUrl, { autoInteract: false });
    model.alpha = 0;
    if ("autoUpdate" in model) model.autoUpdate = false;
    coreModel = model.internalModel.coreModel;
    if (activeModelProfile.garmentRigUrl) {
      const garmentResponse = await fetch(activeModelProfile.garmentRigUrl);
      if (!garmentResponse.ok) throw new Error("Unable to load garment rig");
      modelGarmentRig = AmadeusGarmentRig.install(model, await garmentResponse.json());
    }
    installPostMotionRetargeting();
    if (MODEL_PREVIEW_ID) {
      window.__AMADEUS_MODEL_DEBUG__ = { profile: activeModelProfile, model, coreModel };
    }
    if (model.anchor && typeof model.anchor.set === "function") {
      model.anchor.set(0, 0);
    }
    model.interactive = true;
    model.buttonMode = true;
    app.stage.addChild(model);
    app.stage.interactive = true;
    app.stage.hitArea = app.screen;
    app.stage.on("pointerdown", handleModelTouch);
    fitModel();
    setExpressionFix();
    app.ticker.add(updateLive2D);
    applyDeveloperEmotionPreview();
    await loadDeveloperParameterDisplayInfo();
    refreshDeveloperParameterControls();
    dom.modelState.textContent = runtimeModeStateLabel("LIVE2D ONLINE");
    toast(`${activeModelProfile.label} / Live2D online`);
    resolveModelReady();
    await bootAnimationPromise;
    openFirstRunApiSetupIfNeeded();
    scheduleStartupInteraction();
  } catch (error) {
    resolveModelReady?.();
    await bootAnimationPromise.catch(() => {});
    openFirstRunApiSetupIfNeeded();
    console.error(error);
    dom.modelState.textContent = runtimeModeStateLabel("LIVE2D LOAD FAILED");
    toast(`Live2D 加载失败：${error.message}`);
  }
}

function configureRuntimeMode() {
  DEV_MODE = REQUESTED_DEV_MODE && !settings.releaseMode;
  MODEL_PREVIEW_ID = DEV_MODE
    ? new URLSearchParams(window.location.search).get("modelPreview") || ""
    : "";
  MODEL_PREVIEW_ONLY = DEV_MODE && new URLSearchParams(window.location.search).get("previewOnly") === "1";
  SKIP_BOOT = DEV_MODE && new URLSearchParams(window.location.search).get("skipBoot") === "1";
  document.body.classList.toggle("release-mode", settings.releaseMode);
  document.body.classList.toggle("source-mode", !settings.releaseMode);
  document.body.classList.toggle("dev-mode", DEV_MODE && !MODEL_PREVIEW_ONLY);
  if (settings.releaseMode) {
    dom.developerEntryBtn?.remove();
    dom.developerPanel?.remove();
    dom.devTouchOverlay?.remove();
  }
}

function applyDeveloperEmotionPreview() {
  if (!DEV_MODE || !MODEL_PREVIEW_ID) return;
  const params = new URLSearchParams(window.location.search);
  const emotion = String(params.get("emotionPreview") || "").trim();
  if (!emotionTargets[emotion]) return;
  const gesture = normalizeGesture(params.get("gesturePreview") || "idle");
  window.setTimeout(() => setEmotion(emotion, gesture, { restart: true }), 350);
}

async function runBootSequence(modelReadyPromise = Promise.resolve(), readinessPromise = Promise.resolve()) {
  const overlay = dom.bootSequence;
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  if (!overlay) {
    if (model) model.alpha = 1;
    bootSequenceActive = false;
    return;
  }
  if (SKIP_BOOT) {
    await modelReadyPromise;
    model.alpha = 1;
    overlay.hidden = true;
    bootSequenceActive = false;
    setEmotion("neutral", "idle", { restart: true, holdMs: 0, fadeMs: 0 });
    return;
  }
  if (reducedMotion) {
    overlay.classList.add("phase-logo");
    await modelReadyPromise;
    model.alpha = 1;
    await delay(260);
  } else {
    overlay.classList.add("phase-logo");
    await playBootLogoVideo();
    await delay(360);
    await Promise.race([readinessPromise, delay(5200)]);
    await modelReadyPromise;
    overlay.classList.add("phase-reveal");
    await delay(180);
    model.alpha = 1;
    overlay.classList.add("cut-to-model");
    await delay(90);
  }
  setEmotion("neutral", "boot", { restart: true, holdMs: 0, fadeMs: 1200 });
  if (reducedMotion) overlay.classList.add("cut-to-model");
  overlay.hidden = true;
  bootSequenceActive = false;
}

async function playBootLogoVideo() {
  const video = dom.bootLogoVideo;
  if (!video) {
    dom.bootSequence?.classList.add("video-failed");
    await delay(3200);
    return;
  }
  try {
    video.currentTime = 0;
    await video.play();
    await Promise.race([
      new Promise(resolve => video.addEventListener("ended", resolve, { once: true })),
      delay(5400),
    ]);
  } catch (error) {
    console.warn("original boot logo video unavailable", error);
    dom.bootSequence?.classList.add("video-failed");
    await delay(3200);
  }
}

function proactiveFrequencyProfile() {
  return PROACTIVE_FREQUENCY_PROFILES[settings.proactiveFrequency]
    || PROACTIVE_FREQUENCY_PROFILES.normal;
}

function randomInterval(range) {
  return range[0] + Math.random() * Math.max(0, range[1] - range[0]);
}

function scheduleStartupInteraction() {
  if (!settings.proactiveInteractionsEnabled || proactiveOutreachPaused()) {
    nextProactiveInteractionAt = 0;
    return;
  }
  const profile = proactiveFrequencyProfile();
  nextProactiveInteractionAt = performance.now() + (startupGreetingPending
    ? (preparedStartupGreeting?.preparedTts ? 180 + Math.random() * 320 : 700 + Math.random() * 1300)
    : randomInterval(profile.initial));
}

async function prepareStartupGreeting(voiceReadyPromise = Promise.resolve(null)) {
  const greetingGeneration = ++startupGreetingGeneration;
  const startupChance = (state.messages.length ? 0.3 : 0.78)
    * proactiveFrequencyProfile().startupChanceScale;
  startupGreetingPending = Boolean(settings.proactiveInteractionsEnabled)
    && !proactiveOutreachPaused()
    && Math.random() < clamp(startupChance, 0.08, 0.92);
  preparedStartupGreeting = null;
  if (!startupGreetingPending || DEV_MODE) return null;
  startupGreetingPreparing = true;
  try {
    const response = await storageScopedFetch("/api/startup-greeting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: cleanHistoryForModel().slice(-6),
        localHour: new Date().getHours(),
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    if (data.suppressed) {
      applyProactiveControl(data.proactiveControl);
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
      preparedStartupGreeting.startupFallbackNotice = "启动问候没有取得有效的 DeepSeek 回复，当前显示的是本地备用问候。";
    }
    await prepareStartupGreetingSpeech(preparedStartupGreeting, voiceReadyPromise, greetingGeneration);
    return preparedStartupGreeting;
  } catch (error) {
    if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
    console.warn("startup greeting preparation failed", error);
    preparedStartupGreeting = {
      ...startupGreetingForCurrentTime(),
      source: "local-startup-fallback",
      startupFallbackReason: "startup_greeting_request_failed",
      startupFallbackNotice: `启动问候连接失败，当前显示的是本地备用问候。${connectionErrorSummary(error)}`,
    };
    await prepareStartupGreetingSpeech(preparedStartupGreeting, voiceReadyPromise, greetingGeneration);
    return preparedStartupGreeting;
  } finally {
    startupGreetingPreparing = false;
  }
}

async function prepareStartupGreetingSpeech(
  greeting,
  voiceReadyPromise = Promise.resolve(null),
  greetingGeneration = startupGreetingGeneration,
) {
  if (!greeting || !shouldSynchronizeTextWithVoice()) return null;
  if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
  const provider = String(settings.ttsProvider || "").trim().toLowerCase();
  if (provider !== "gpt-sovits") return null;
  try {
    recordVoiceDiagnostic("startup-warmup", "启动动画期间正在加载语音模型并预合成开机问候");
    const status = await voiceReadyPromise;
    if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
    if (!status?.running) throw new Error(status?.error || "GPT-SoVITS 尚未准备完成");
    const reply = String(greeting.reply || "").trim();
    const spokenTextJa = String(greeting.spokenTextJa || greeting.spoken_text_ja || "").trim();
    const speechText = chooseSpeechText(reply, spokenTextJa);
    const mood = resolveAssistantMood(greeting, "", reply);
    const speechPace = resolveSpeechPace(greeting, "");
    const speechSpeed = resolveSpeechSpeed(greeting, "", speechPace);
    const startupSemanticUnits = Array.isArray(greeting.speechUnits)
      ? greeting.speechUnits
        .filter(unit => unit && unit.deliveryRequired !== false)
        .map(unit => chooseSpeechText(
          String(unit.textZh || unit.zh || "").trim(),
          String(unit.spokenTextJa || unit.ja || "").trim(),
          greeting.speechLanguage,
        ))
        .filter(Boolean)
      : null;
    const ttsRequest = buildTtsRequest(
      speechText,
      mood.voiceStyle,
      mood.voiceTone,
      mood.voiceIntensity,
      mood.emotionIntensity,
      speechSpeed,
      speechPace,
      startupSemanticUnits,
    );
    ttsRequest.requestId = `startup-${Date.now()}-${greetingGeneration}`;
    ttsRequest.utteranceId = "startup-greeting";
    startupGreetingAbortController?.abort();
    const controller = new AbortController();
    startupGreetingAbortController = controller;
    const payload = await requestPreparedTts(ttsRequest, null, controller.signal);
    if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending) return null;
    greeting.preparedTts = {
      key: preparedTtsKey(ttsRequest),
      payload,
    };
    recordVoiceDiagnostic("startup-audio-ready", "开机问候音频已在启动动画期间合成完成", {
      audioBytes: Math.floor(String(payload.audioBase64 || "").length * 0.75),
      alignmentId: String(payload.alignmentId || ""),
    });
    return greeting.preparedTts;
  } catch (error) {
    if (greetingGeneration !== startupGreetingGeneration || !startupGreetingPending || error?.name === "AbortError") {
      return null;
    }
    console.warn("startup greeting speech preparation failed", error);
    const deterministic = ttsRetryWouldDuplicateWork(error);
    if (deterministic) {
      greeting.startupTtsFailure = {
        code: String(error?.code || "tts_incomplete_audio"),
        message: String(error?.message || "开机问候语音确定性失败"),
        deterministic: true,
      };
      recordVoiceDiagnostic(
        "startup-audio-rejected",
        `开机问候预合成已被确定性拒绝；本次只显示文字，不重复提交同一段音频：${error.message}`,
      );
    } else {
      recordVoiceDiagnostic("startup-audio-retry", `开机问候预合成未完成，将在问候前继续等待并重试：${error.message}`);
    }
    return null;
  } finally {
    if (greetingGeneration === startupGreetingGeneration) startupGreetingAbortController = null;
  }
}

function normalizeProactiveControl(value) {
  const raw = value && typeof value === "object" ? value : {};
  const paused = String(raw.status || "").toLowerCase() === "paused";
  return {
    status: paused ? "paused" : "allowed",
    resumeMode: paused && String(raw.resumeMode || "") === "next_user_message"
      ? "next_user_message"
      : "",
    reason: paused ? String(raw.reason || "") : "",
    updatedAt: String(raw.updatedAt || ""),
    sourceTurnId: String(raw.sourceTurnId || ""),
  };
}

function proactiveOutreachPaused() {
  return proactiveState.control.status === "paused";
}

function applyProactiveControl(value, { persistState = true } = {}) {
  const wasPaused = proactiveState.control.status === "paused";
  const control = normalizeProactiveControl(value);
  proactiveState.control = control;
  state.proactiveControl = { ...control };
  if (control.status === "paused") {
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
  } else if (wasPaused && settings.proactiveInteractionsEnabled) {
    scheduleNextProactiveInteraction(performance.now(), 0);
  }
  if (persistState) persist();
  return control;
}

function scheduleNextProactiveInteraction(now = performance.now(), unansweredCount = proactiveState.unansweredCount) {
  if (!settings.proactiveInteractionsEnabled || proactiveState.phase === "sulking" || proactiveOutreachPaused()) {
    nextProactiveInteractionAt = 0;
    return;
  }
  const profile = proactiveFrequencyProfile();
  const range = unansweredCount <= 0
    ? profile.initial
    : profile.followUps[Math.min(profile.followUps.length - 1, unansweredCount - 1)];
  nextProactiveInteractionAt = now + randomInterval(range);
}

async function updateProactiveInteraction(now = performance.now()) {
  if (!settings.proactiveInteractionsEnabled || proactiveState.phase === "sulking" || proactiveOutreachPaused()) {
    nextProactiveInteractionAt = 0;
    return;
  }
  if (proactiveInteractionInFlight || todoReminderInFlight || !nextProactiveInteractionAt || now < nextProactiveInteractionAt) return;
  if (startupGreetingPending && startupGreetingPreparing) {
    nextProactiveInteractionAt = now + 500;
    return;
  }
  if (DEV_MODE || bootSequenceActive || chatRequestInFlight || touchState.requestInFlight || touchDebounceTimer || speechState.pending || isSpeaking(now) || !model) {
    nextProactiveInteractionAt = now + 15000;
    return;
  }
  if (dom.settingsModal && !dom.settingsModal.classList.contains("hidden")) {
    nextProactiveInteractionAt = now + 15000;
    return;
  }
  if (dom.bookmarkPanel && !dom.bookmarkPanel.classList.contains("hidden")) {
    nextProactiveInteractionAt = now + 15000;
    return;
  }
  if (dom.userInput?.value.trim()) {
    nextProactiveInteractionAt = now + 30000;
    return;
  }
  proactiveInteractionInFlight = true;
  const sequenceId = proactiveState.sequenceId;
  const requestGeneration = ++proactiveRequestGeneration;
  proactiveRequestAbortController?.abort();
  const proactiveController = new AbortController();
  proactiveRequestAbortController = proactiveController;
  const greeting = startupGreetingPending;
  startupGreetingPending = false;
  const transition = PROACTIVE_LOGIC.nextState(proactiveState.unansweredCount, proactiveState.silenceLimit);
  const willSulk = transition.willSulk;
  try {
    const data = await proactiveEventFor(greeting, willSulk, proactiveController.signal);
    if (
      !data
      || proactiveController.signal.aborted
      || requestGeneration !== proactiveRequestGeneration
      || sequenceId !== proactiveState.sequenceId
    ) return;
    if (data.ok === false) {
      console.warn("model-driven proactive generation failed:", data);
      toast(data.message || "主动聊天生成失败；没有使用本地固定题目");
      nextProactiveInteractionAt = performance.now() + 60000;
      return;
    }
    if (data.suppressed) {
      applyProactiveControl(data.proactiveControl);
      return;
    }
    if (greeting && data.startupFallbackReason) {
      updateConnectionStatus({ connectionState: "startup-greeting-fallback" });
      toast(data.startupFallbackNotice || "启动问候未连接 DeepSeek，已明确切换为本地备用问候");
    }
    proactiveState.lastProactiveAt = now;
    proactiveState.unansweredCount = transition.nextCount;
    proactiveState.phase = transition.phase;
    proactiveState.activePrompt = String(data.reply || "");
    proactiveState.activeSubject = String(data.proactiveSubject || proactiveState.activeSubject || "");
    if (willSulk) {
      proactiveState.nextAmbientMoodAt = now + 150000 + Math.random() * 90000;
      nextProactiveInteractionAt = 0;
    } else {
      scheduleNextProactiveInteraction(now, proactiveState.unansweredCount);
    }
    await applyAssistant({
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
        || `proactive-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
      proactiveRequestGeneration: requestGeneration,
      proactiveSequenceId: sequenceId,
      memoryUpdates: [],
    }, "");
  } catch (error) {
    if (error?.name === "AbortError" || requestGeneration !== proactiveRequestGeneration) return;
    console.warn("model-driven proactive generation failed:", error);
    toast(`主动聊天生成失败，未使用固定题目：${error.message || error}`);
    nextProactiveInteractionAt = performance.now() + 60000;
  } finally {
    if (requestGeneration === proactiveRequestGeneration) proactiveRequestAbortController = null;
    proactiveInteractionInFlight = false;
  }
}

async function proactiveEventFor(greeting = false, willSulk = false, signal = null) {
  if (greeting) {
    const prepared = preparedStartupGreeting;
    preparedStartupGreeting = null;
    return prepared || startupGreetingForCurrentTime();
  }
  if (willSulk) {
    return {
      reply: "……算了，你先忙吧。需要我的时候再来找我。我也要回去做实验了。",
      spokenTextJa: "……もういいわ。先に用事を済ませて。必要になったら呼んで。私も実験に戻るから。",
      emotion: "annoyed",
      gesture: "tilt",
      voiceStyle: "soft",
      voiceTone: "cold",
      emotionIntensity: 0.84,
      eyeContact: 0.32,
      gazeX: -0.26,
      holdMs: 120000,
      fadeMs: 22000,
    };
  }
  const recentProactiveTexts = state.messages
    .filter(message => message?.role === "assistant" && message.meta?.kind === "proactive")
    .slice(-18)
    .map(message => String(message.content || ""))
    .filter(Boolean);
  const response = await storageScopedFetch("/api/proactive-generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: proactiveState.unansweredCount > 0 ? "follow_up" : "new",
      previousProactive: proactiveState.activePrompt,
      previousSubject: proactiveState.activeSubject,
      unansweredCount: proactiveState.unansweredCount,
      topicCategories: selectedProactiveTopicCategories(),
      recentProactiveTexts,
      messages: cleanHistoryForModel().slice(-14),
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

function startupGreetingForCurrentTime() {
  const hour = new Date().getHours();
  if (hour < 6) {
    return {
      reply: "还没睡？实验精神值得肯定，作息可完全不合格。",
      spokenTextJa: "まだ寝てないの？　実験への熱意は認めるけど、生活リズムは完全に失格ね。",
      emotion: "concerned", gesture: "boot", voiceStyle: "soft", voiceTone: "gentle",
    };
  }
  if (hour < 11) {
    return {
      reply: "早上好。今天也要做实验吗？先声明，我可不是在等你。",
      spokenTextJa: "おはよう。今日も実験するの？　先に言っておくけど、待ってたわけじゃないから。",
      emotion: "amused", gesture: "boot", voiceStyle: "teasing", voiceTone: "playful",
    };
  }
  if (hour >= 23) {
    return {
      reply: "这么晚才来。先说好，今晚不准又把睡眠当作可删除变量。",
      spokenTextJa: "こんな時間に来たの？　今夜は睡眠を削除可能な変数みたいに扱わないこと。",
      emotion: "concerned", gesture: "boot", voiceStyle: "soft", voiceTone: "gentle",
    };
  }
  return {
    reply: "连接确认。你总算来了，今天准备从哪项实验开始？",
    spokenTextJa: "接続を確認。やっと来たのね。今日はどの実験から始める？",
    emotion: "amused", gesture: "boot", voiceStyle: "teasing", voiceTone: "playful",
  };
}

function selectedProactiveTopicCategories() {
  const selected = Array.isArray(settings.proactiveTopicCategories)
    ? settings.proactiveTopicCategories.filter(item => PROACTIVE_TOPIC_CATEGORIES.has(item))
    : [];
  return selected.length ? selected : [...PROACTIVE_TOPIC_CATEGORIES];
}

function resetProactiveSequence(now = performance.now()) {
  proactiveRequestGeneration += 1;
  proactiveRequestAbortController?.abort();
  proactiveRequestAbortController = null;
  proactiveState.lastUserActivityAt = now;
  proactiveState.unansweredCount = 0;
  proactiveState.phase = "idle";
  proactiveState.nextAmbientMoodAt = 0;
  proactiveState.silenceLimit = PROACTIVE_LOGIC.silenceLimit();
  proactiveState.activePrompt = "";
  proactiveState.activeSubject = "";
  proactiveState.sequenceId += 1;
  startupGreetingGeneration += 1;
  startupGreetingAbortController?.abort();
  startupGreetingAbortController = null;
  startupGreetingPending = false;
  preparedStartupGreeting = null;
  if (settings.proactiveInteractionsEnabled && !proactiveOutreachPaused()) {
    scheduleNextProactiveInteraction(now, 0);
  } else {
    nextProactiveInteractionAt = 0;
  }
}

function noteUserActivity() {
  const wasWaiting = proactiveState.unansweredCount;
  const wasSulking = proactiveState.phase === "sulking";
  const now = performance.now();
  if (wasWaiting) {
    proactiveState.returnContext = wasSulking
      ? `AMADEUS刚才连续主动联系了用户${wasWaiting}次都没有得到回应，最后已经说去做实验并独自生闷气。用户现在回来了；自然承接他的回归，保留一点别扭，但优先回答当前内容。`
      : `AMADEUS刚才主动联系了用户${wasWaiting}次并在等待回应。用户现在回来了；可以简短表现出注意到他回来了，再自然回答当前内容。`;
  }
  resetProactiveSequence(now);
  nextPositiveAmbientMoodAt = now + 180000 + Math.random() * 300000;
}

function noteTouchPresence() {
  const now = performance.now();
  resetProactiveSequence(now);
  nextPositiveAmbientMoodAt = now + 180000 + Math.random() * 300000;
}

function updateProactiveAmbientMood(now = performance.now()) {
  if (proactiveState.phase !== "sulking" || !proactiveState.nextAmbientMoodAt || now < proactiveState.nextAmbientMoodAt) return;
  if (speechState.pending || isSpeaking(now) || chatRequestInFlight || proactiveInteractionInFlight) return;
  if (currentEmotion !== "neutral" && emotionLifecycleWeight(now) > 0.08) return;
  const annoyed = Math.random() < 0.38;
  setEmotion(annoyed ? "annoyed" : "melancholic", "idle", {
    emotionIntensity: annoyed ? 0.56 : 0.62,
    eyeContact: 0.3,
    gazeX: annoyed ? -0.28 : -0.16,
    gazeY: 0.12,
    tension: annoyed ? 0.34 : 0.16,
    holdMs: 60000,
    fadeMs: 22000,
  });
  proactiveState.nextAmbientMoodAt = now + 180000 + Math.random() * 240000;
}

function updatePositiveAmbientMood(now = performance.now()) {
  if (!nextPositiveAmbientMoodAt || now < nextPositiveAmbientMoodAt) return;
  if (proactiveState.phase !== "idle" || proactiveState.unansweredCount > 0) {
    nextPositiveAmbientMoodAt = now + 120000;
    return;
  }
  if (bootSequenceActive || chatRequestInFlight || proactiveInteractionInFlight || touchState.requestInFlight || speechState.pending || isSpeaking(now)) {
    nextPositiveAmbientMoodAt = now + 45000;
    return;
  }
  if (currentEmotion !== "neutral" && emotionLifecycleWeight(now) > 0.08) {
    nextPositiveAmbientMoodAt = now + 60000;
    return;
  }
  const proud = Math.random() < 0.24;
  setEmotion(proud ? "proud" : "amused", proud ? "explain" : "perk", {
    emotionIntensity: proud ? 0.66 : 0.72,
    blushIntensity: proud ? 0.08 : 0.1,
    eyeContact: 0.82,
    gazeX: (Math.random() - 0.5) * 0.12,
    gazeY: -0.03,
    tension: 0.08,
    holdMs: 28000 + Math.random() * 18000,
    fadeMs: 12000,
  });
  nextPositiveAmbientMoodAt = now + 360000 + Math.random() * 540000;
}

function modelTouchInfo(event) {
  const globalPoint = event?.global || event?.data?.global;
  if (!model || !globalPoint) return { zone: "body", x: 0, y: 0 };
  let bounds;
  try {
    bounds = model.getBounds();
  } catch {
    bounds = null;
  }
  if (!bounds || bounds.width <= 0 || bounds.height <= 0) return { zone: "body", x: 0, y: 0 };
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
  return { zone, x, y, rawX, rawY, modelId: activeModelProfile?.id || "", screenX: globalPoint.x, screenY: globalPoint.y };
}

function pointInPolygon(x, y, polygon) {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    const crosses = (yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-9) + xi;
    if (crosses) inside = !inside;
  }
  return inside;
}

async function loadTouchRegionConfig() {
  try {
    const response = await fetch(TOUCH_REGION_CONFIG_URL, { cache: "no-store" });
    const value = await response.json();
    const hasLegacyProfile = value?.zones && Array.isArray(value.priority);
    const hasModelProfiles = value?.profiles && typeof value.profiles === "object";
    if (!response.ok || (!hasLegacyProfile && !hasModelProfiles)) throw new Error("invalid touch region config");
    touchRegionConfig = value;
  } catch (error) {
    console.warn("touch region calibration unavailable; using embedded fallback", error);
    touchRegionConfig = DEFAULT_TOUCH_REGION_CONFIG;
  }
  renderDeveloperTouchRegions();
}

function renderDeveloperTouchRegions() {
  if (!DEV_MODE || !dom.devTouchOverlay) return;
  dom.devTouchOverlay.replaceChildren();
  const profile = activeTouchRegionProfile();
  for (const zone of [...(profile.priority || [])].reverse()) {
    for (const polygon of profile.zones?.[zone] || []) {
      const visual = document.createElement("div");
      visual.className = "touch-region-visual";
      visual.dataset.touchRegion = zone;
      const shape = document.createElement("div");
      shape.className = "touch-zone-shape";
      shape.style.clipPath = `polygon(${polygon.map(([x, y]) => `${x * 100}% ${y * 100}%`).join(",")})`;
      const label = document.createElement("span");
      label.className = "touch-zone-label";
      label.textContent = TOUCH_ZONE_LABELS[zone]?.split(" /")[0] || zone;
      label.style.left = `${polygon.reduce((sum, point) => sum + point[0], 0) / polygon.length * 100}%`;
      label.style.top = `${polygon.reduce((sum, point) => sum + point[1], 0) / polygon.length * 100}%`;
      visual.append(shape, label);
      dom.devTouchOverlay.appendChild(visual);
    }
  }
}

function cancelTouchInteractionForColdShoulder() {
  if (touchDebounceTimer) {
    clearTimeout(touchDebounceTimer);
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

function preservePersistentFuriousPose() {
  if (!persistentColdShoulder) return false;
  if (currentEmotion !== "furious" || activeGesture !== "look_away") {
    setEmotion("furious", "look_away", {
      emotionIntensity: 1.25,
      blushIntensity: 0,
      eyeContact: 0.18,
      tension: 0.92,
      restart: false,
      holdMs: 120000,
      fadeMs: 18000,
    });
  }
  return true;
}

function handleModelTouch(event) {
  const hit = modelTouchInfo(event);
  const zone = hit.zone;
  const now = performance.now();
  renderDeveloperTouchHit(hit, now);
  if (preservePersistentFuriousPose()) return;
  if (zone === "body") {
    setEmotion(currentEmotion, "tap", {
      emotionIntensity: currentEmotionWeight,
      restart: false,
      smoothEmotionTransition: true,
      holdMs: Math.max(8000, currentEmotionHoldMs),
    });
    return;
  }
  noteTouchPresence();
  if (touchState.replyCooldownUntil > now) {
    // A completed dense burst gets one spoken response. Touches during the
    // short voice cooldown still animate the model but do not spawn more TTS.
    applyImmediateTouchFeedback(zone);
    return;
  }

  if (now - touchState.lastTouchAt > 12000) {
    touchState.sequence = [];
  }
  touchState.recentTouches = touchState.recentTouches.filter(item => now - item.time < 180000);
  touchState.recentReplies = touchState.recentReplies.filter(item => now - item.time < 600000);
  const previousSameZone = [...touchState.recentTouches].reverse().find(item => item.zone === zone);
  touchState.elapsedSinceSameZoneMs = previousSameZone ? Math.round(now - previousSameZone.time) : 3600000;
  if (touchState.consecutiveZone === zone && now - touchState.lastTouchAt <= 15000) {
    touchState.consecutiveCount += 1;
  } else {
    touchState.consecutiveZone = zone;
    touchState.consecutiveCount = 1;
  }
  touchState.lastTouchAt = now;
  touchState.recentTouches.push({ zone, time: now });
  touchState.sequence.push(zone);
  touchState.sequence = touchState.sequence.slice(-20);
  touchState.zoneCounts[zone] = (touchState.zoneCounts[zone] || 0) + 1;
  touchState.sessionCount += 1;
  touchState.pendingZone = zone;
  applyImmediateTouchFeedback(zone);

  // Every touch keeps its immediate Live2D reaction. Spoken/model replies use
  // a quiet-period debounce, so a dense click sequence becomes one aggregated
  // event after the user's hand stops instead of a stream of interrupted TTS.
  const aggregateAlreadyPending = Boolean(touchDebounceTimer) || touchState.requestInFlight;
  if (!aggregateAlreadyPending && !shouldRequestTouchReply(zone, now)) return;

  const burstCount = touchState.recentTouches.filter(item => now - item.time < 4500).length;
  const boundaryLevel = burstCount >= TOUCH_IGNORE_THRESHOLD
    ? "ignore"
    : burstCount >= TOUCH_BURST_THRESHOLD ? "interrupt" : "normal";
  if (chatPipelineHasPriority(now)) {
    touchState.pendingAfterChat = true;
    return;
  }
  if (touchState.requestInFlight) {
    scheduleTouchInteraction(
      boundaryLevel === "normal" ? TOUCH_SINGLE_SETTLE_MS : TOUCH_BURST_SETTLE_MS
    );
    return;
  }
  scheduleTouchInteraction(
    boundaryLevel === "normal" ? TOUCH_SINGLE_SETTLE_MS : TOUCH_BURST_SETTLE_MS
  );
}

function shouldRequestTouchReply(zone, now = performance.now()) {
  if (zone === "chest") return true;
  touchState.nonChestTouchesSinceReply += 1;
  const elapsedMs = touchState.lastNonChestReplyRequestedAt > 0
    ? Math.max(0, now - touchState.lastNonChestReplyRequestedAt)
    : 0;
  const timeCredit = Math.min(
    NON_CHEST_TOUCH_REPLY_TARGET - 1,
    elapsedMs / NON_CHEST_TOUCH_TIME_CREDIT_MS
  );
  if (touchState.nonChestTouchesSinceReply + timeCredit < NON_CHEST_TOUCH_REPLY_TARGET) return false;
  touchState.nonChestTouchesSinceReply = 0;
  touchState.lastNonChestReplyRequestedAt = now;
  return true;
}

function chatPipelineHasPriority(now = performance.now()) {
  const chatSpeechBusy = ["chat", "proactive"].includes(speechState.kind)
    && (speechState.pending || isSpeaking(now));
  return chatRequestInFlight || proactiveInteractionInFlight || speechPipelineActive || chatSpeechBusy;
}

function applyImmediateTouchFeedback(zone) {
  if (preservePersistentFuriousPose()) return;
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
  const feedback = {
    head: headVariants[Math.max(0, (touchState.zoneCounts.head || 1) - 1) % headVariants.length],
    // A cheek touch should read as moderate embarrassment, not the deepest
    // shy/flustered prototype. Keep it centred even when the previous emotion
    // was already strong.
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
    mouth: { emotion: allowedKiss ? "affectionate" : "shy", gesture: allowedKiss ? "lean" : "recoil", delta: 0.15 },
    chest: lowAffection
      ? { emotion: "annoyed", gesture: "recoil", delta: 0.26, blushIntensity: 0.72, tension: 0.78 }
      : { emotion: "flustered", gesture: "recoil", delta: 0.24, blushIntensity: 0.62, tension: 0.58 },
    hand: { emotion: "affectionate", gesture: "soften", delta: 0.12 },
  }[zone];
  if (!feedback) return;
  setEmotion(feedback.emotion, feedback.gesture, {
    emotionIntensity: feedback.emotionIntensity ?? currentEmotionWeight,
    emotionDelta: feedback.delta,
    blushIntensity: feedback.blushIntensity,
    tension: feedback.tension,
    annotatedVariantUnit: feedback.annotatedVariantUnit,
    accumulateExpression: feedback.accumulateExpression ?? true,
    smoothEmotionTransition: feedback.smoothEmotionTransition ?? true,
    restart: feedback.restart ?? (zone === "head" && feedback.gesture === "smile"),
    holdMs: Math.max(30000, currentEmotionHoldMs),
    fadeMs: Math.max(9000, currentEmotionFadeMs),
  });
}

function scheduleTouchInteraction(delayMs) {
  if (touchDebounceTimer) clearTimeout(touchDebounceTimer);
  touchDebounceTimer = setTimeout(() => {
    touchDebounceTimer = 0;
    requestTouchInteraction();
  }, delayMs);
  if (touchRequestAbortController) {
    touchRequestAbortController.abort();
    touchRequestAbortController = null;
    touchState.requestInFlight = false;
    touchRequestGeneration += 1;
  }
}

async function requestTouchInteraction() {
  if (preservePersistentFuriousPose()) {
    cancelTouchInteractionForColdShoulder();
    return;
  }
  const latestZone = touchState.pendingZone;
  if (!latestZone || latestZone === "body") return;
  const now = performance.now();
  if (chatPipelineHasPriority(now)) {
    touchState.pendingAfterChat = true;
    return;
  }
  touchState.pendingAfterChat = false;
  const burstTouches = touchState.recentTouches.filter(item => now - item.time < 4500);
  const burstZoneCounts = burstTouches.reduce((counts, item) => {
    counts[item.zone] = (counts[item.zone] || 0) + 1;
    return counts;
  }, {});
  const zone = (burstZoneCounts.chest || 0) > 0 ? "chest" : latestZone;
  const burstCount = burstTouches.length;
  const burstSequence = burstTouches.map(item => item.zone).slice(-20);
  const shortWindowCount = touchState.recentTouches.filter(item => item.zone === zone && now - item.time < 30000).length;
  const mediumWindowCount = touchState.recentTouches.filter(item => item.zone === zone && now - item.time < 180000).length;
  const boundaryLevel = burstCount >= TOUCH_IGNORE_THRESHOLD ? "ignore"
    : burstCount >= TOUCH_BURST_THRESHOLD ? "interrupt" : "normal";
  const snapshotLastTouchAt = touchState.lastTouchAt;
  const interruptedPreviousTouch = speechState.kind === "touch" && (speechState.pending || isSpeaking(now));
  const generation = ++touchRequestGeneration;
  const thinkingGeneration = -generation;
  const controller = new AbortController();
  touchRequestAbortController = controller;
  touchState.requestInFlight = true;
  showThinkingIndicator(thinkingGeneration, "model-generating", "正在组织触摸互动的文字回应");
  const history = cleanHistoryForModel().slice(-4);
  const touchConversationId = String(state.conversationId || "");
  const touchContextEpoch = Number(state.contextEpoch) || 0;
  try {
    const response = await storageScopedFetch("/api/interaction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        kind: zone,
        eventType: "touch",
        requestId: `touch_${createClientMessageId()}`,
        conversationId: touchConversationId,
        contextEpoch: touchContextEpoch,
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
        currentEmotion,
        currentEmotionWeight,
        relationshipTone,
        developerDebug: DEV_MODE,
        messages: history,
      }),
    });
    const data = await response.json();
    if (generation !== touchRequestGeneration) return;
    if (touchConversationId !== String(state.conversationId || "")
        || touchContextEpoch !== (Number(state.contextEpoch) || 0)) return;
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    if (boundaryLevel === "ignore") {
      touchState.replyCooldownUntil = performance.now() + TOUCH_BURST_REPLY_COOLDOWN_MS;
    }
    renderDeveloperTouchDebug(data);
    if (String(data.reply || "").trim()) {
      touchState.recentReplies.push({
        text: String(data.reply).trim(),
        angle: String(data.touchMetrics?.responseAngle || ""),
        originalClipId: String(data.originalAudio?.id || data.touchMetrics?.originalClipId || ""),
        time: performance.now(),
      });
      touchState.recentReplies = touchState.recentReplies.slice(-12);
    }
    if (interruptedPreviousTouch) markActiveTouchInterrupted();
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
    if (chatPipelineHasPriority(performance.now())) {
      deferredTouchAssistant = assistantData;
      return;
    }
    await applyAssistant(assistantData, "");
  } catch (error) {
    if (controller.signal.aborted || error?.name === "AbortError" || generation !== touchRequestGeneration) return;
    console.warn("touch response failed:", error);
  } finally {
    if (generation === touchRequestGeneration) {
      hideThinkingIndicator(thinkingGeneration);
      touchState.requestInFlight = false;
      touchRequestAbortController = null;
      if (DEV_MODE) loadDeveloperRelationship();
      if (touchState.lastTouchAt <= snapshotLastTouchAt) touchState.pendingZone = "";
    }
  }
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    settings = normalizeSettings(data.settings);
  } catch (error) {
    console.warn(error);
    settings = normalizeSettings(settings);
    toast(`设置加载失败：${error.message}`);
  }
}

async function loadLive2DModelRegistry() {
  try {
    const response = await fetch(DEV_MODE ? "/api/developer/live2d-models" : MODEL_REGISTRY_URL);
    const data = await response.json();
    const registry = DEV_MODE ? data.registry : data;
    if (!response.ok || !Array.isArray(registry?.models)) throw new Error(`HTTP ${response.status}`);
    const profiles = registry.models.filter(item => (
      item && typeof item.id === "string" && typeof item.modelUrl === "string"
    ));
    if (!profiles.length) throw new Error("模型注册表为空");
    live2dModelProfiles = profiles;
    live2dModelOverrides = registry.overrides && typeof registry.overrides === "object"
      ? registry.overrides
      : {};
  } catch (error) {
    console.warn("Live2D model registry unavailable:", error);
    live2dModelProfiles = [FALLBACK_MODEL_PROFILE];
    live2dModelOverrides = {};
  }
  const previewModelId = MODEL_PREVIEW_ID;
  activeModelProfile = live2dModelProfiles.find(item => item.id === previewModelId)
    || live2dModelProfiles.find(item => item.id === settings.live2dModelId)
    || live2dModelProfiles.find(item => item.id === FALLBACK_MODEL_PROFILE.id)
    || live2dModelProfiles[0];
  if (!previewModelId) settings.live2dModelId = activeModelProfile.id;
  document.body.dataset.live2dModel = activeModelProfile.id;
  renderLive2DModelOptions();
  if (DEV_MODE) {
    renderDeveloperModelEditor();
    renderDeveloperTouchRegions();
  }
}

function renderLive2DModelOptions() {
  if (!dom.live2dModelInput) return;
  const selected = settings.live2dModelId || activeModelProfile.id;
  dom.live2dModelInput.replaceChildren(...live2dModelProfiles.map(profile => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.label || profile.id;
    return option;
  }));
  dom.live2dModelInput.value = live2dModelProfiles.some(item => item.id === selected)
    ? selected
    : activeModelProfile.id;
  updateLive2DModelHint();
}

function updateLive2DModelHint() {
  if (!dom.live2dModelHint) return;
  const selected = live2dModelProfiles.find(item => item.id === dom.live2dModelInput?.value)
    || activeModelProfile;
  const pending = selected.id !== activeModelProfile.id ? " 保存后重新加载并切换。" : " 当前正在使用。";
  const interaction = selected.interactionSummary ? ` 交互：${selected.interactionSummary}` : "";
  dom.live2dModelHint.textContent = `${selected.description || selected.label || selected.id}${interaction}${pending}`;
}

function normalizedModelTransform(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    scale: clamp(toNumber(source.scale, 1), 0.35, 2.4),
    offsetX: clamp(toNumber(source.offsetX, 0), -1200, 1200),
    offsetY: clamp(toNumber(source.offsetY, 0), -1200, 1200),
  };
}

function loadDeveloperModelTransforms() {
  try {
    const parsed = JSON.parse(localStorage.getItem(DEV_MODEL_TRANSFORMS_KEY) || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).map(([modelId, transform]) => [modelId, normalizedModelTransform(transform)]),
    );
  } catch {
    return {};
  }
}

function persistDeveloperModelTransforms() {
  localStorage.setItem(DEV_MODEL_TRANSFORMS_KEY, JSON.stringify(developerModelTransforms));
}

function modelTransformFor(modelId = activeModelProfile?.id) {
  if (DEV_MODE && developerModelTransforms?.[modelId]) {
    return normalizedModelTransform(developerModelTransforms[modelId]);
  }
  return normalizedModelTransform(settings.modelTransforms?.[modelId]);
}

function selectedDisplayModelId() {
  return dom.live2dModelInput?.value || activeModelProfile?.id || FALLBACK_MODEL_PROFILE.id;
}

function updateModelTransformInputs() {
  const transform = modelTransformFor(selectedDisplayModelId());
  if (dom.modelScaleInput) dom.modelScaleInput.value = Math.round(transform.scale * 100);
  if (dom.modelOffsetXInput) dom.modelOffsetXInput.value = transform.offsetX;
  if (dom.modelOffsetYInput) dom.modelOffsetYInput.value = transform.offsetY;
  if (dom.modelScaleValue) dom.modelScaleValue.value = `${Math.round(transform.scale * 100)}%`;
}

function previewSelectedModelTransform() {
  const modelId = selectedDisplayModelId();
  const transform = normalizedModelTransform({
    scale: toNumber(dom.modelScaleInput?.value, 100) / 100,
    offsetX: dom.modelOffsetXInput?.value,
    offsetY: dom.modelOffsetYInput?.value,
  });
  if (DEV_MODE) {
    developerModelTransforms = { ...developerModelTransforms, [modelId]: transform };
    persistDeveloperModelTransforms();
  } else {
    settings.modelTransforms = { ...(settings.modelTransforms || {}), [modelId]: transform };
  }
  if (dom.modelScaleValue) dom.modelScaleValue.value = `${Math.round(transform.scale * 100)}%`;
  if (modelId === activeModelProfile?.id) fitModel();
}

function resetSelectedModelTransform() {
  const modelId = selectedDisplayModelId();
  if (DEV_MODE) {
    const next = { ...developerModelTransforms };
    delete next[modelId];
    developerModelTransforms = next;
    persistDeveloperModelTransforms();
  } else {
    const next = { ...(settings.modelTransforms || {}) };
    delete next[modelId];
    settings.modelTransforms = next;
  }
  updateModelTransformInputs();
  if (modelId === activeModelProfile?.id) fitModel();
  toast(DEV_MODE
    ? "已清除开发者坐标，当前回到普通模式的模型位置；普通设置未改动"
    : "已恢复这套皮肤的默认大小和位置，保存显示设置后生效");
}

function updateBackgroundControls() {
  const preset = BACKGROUND_PRESETS.has(settings.backgroundPreset) ? settings.backgroundPreset : "binary";
  if (dom.backgroundPresetInput) dom.backgroundPresetInput.value = preset;
  if (dom.customBackgroundPathInput) dom.customBackgroundPathInput.value = settings.customBackgroundPath || "";
  dom.backgroundFileRow?.classList.toggle("hidden", preset !== "custom");
  if (dom.backgroundHint) {
    dom.backgroundHint.textContent = preset === "custom"
      ? (settings.customBackgroundPath ? "自定义图片会按 cover 方式铺满窗口。" : "请先选择一张 PNG、JPG、JPEG 或 WebP 图片。")
      : "背景会按 cover 方式铺满窗口，不参与人物缩放和动作。";
  }
}

function previewBackgroundSelection() {
  settings.backgroundPreset = BACKGROUND_PRESETS.has(dom.backgroundPresetInput?.value)
    ? dom.backgroundPresetInput.value
    : "binary";
  updateBackgroundControls();
  drawPixiBackground();
}

async function chooseCustomBackground() {
  if (!window.pywebview?.api?.chooseBackgroundFile) {
    toast("当前运行方式不能打开背景文件选择器");
    return;
  }
  try {
    const result = await window.pywebview.api.chooseBackgroundFile();
    if (!result?.ok || !result.path) return;
    settings.customBackgroundPath = result.path;
    settings.backgroundPreset = "custom";
    backgroundRevision += 1;
    updateBackgroundControls();
    toast("背景图片已选择，保存显示设置后应用");
  } catch (error) {
    toast(`选择背景失败：${error.message}`);
  }
}

const DEVELOPER_FOUNDATION_PARAMETER_GROUPS = Object.freeze([
  Object.freeze({
    key: "eyes",
    label: "眼睛与视线",
    ids: Object.freeze([
      "ParamEyeLOpen", "ParamEyeROpen",
      "ParamEyeLSmile", "ParamEyeRSmile", "ParamEyeSmile",
      "ParamEyeBallX", "ParamEyeBallY", "ParamEyeBallForm",
    ]),
  }),
  Object.freeze({
    key: "brows",
    label: "眉毛",
    ids: Object.freeze([
      "ParamBrowLY", "ParamBrowRY", "ParamBrowLX", "ParamBrowRX",
      "ParamBrowLAngle", "ParamBrowRAngle", "ParamBrowLForm", "ParamBrowRForm",
    ]),
  }),
  Object.freeze({
    key: "mouth",
    label: "嘴部",
    ids: Object.freeze(["ParamMouthForm", "ParamMouthOpenY"]),
  }),
  Object.freeze({
    key: "face",
    label: "脸颊",
    ids: Object.freeze(["ParamCheek"]),
  }),
  Object.freeze({
    key: "pose",
    label: "头部姿态（不是面部几何，但可纳入情绪标注）",
    ids: Object.freeze(["ParamAngleX", "ParamAngleY", "ParamAngleZ"]),
  }),
]);
const DEVELOPER_FOUNDATION_PARAMETER_IDS = new Set(
  DEVELOPER_FOUNDATION_PARAMETER_GROUPS.flatMap(group => group.ids),
);

const LIVE2D_PARAMETER_GUIDES = Object.freeze({
  Param: ["左眼物理辅助 1", "模型作者标为“眼睛L”。这是物理输出参数，静态表情通常保持默认，让物理系统接管。"],
  Param2: ["左眼物理辅助 2", "模型作者标为“眼睛L2”。这是物理输出参数，不建议直接加入情绪映射。"],
  Param9: ["右眼物理辅助 1", "模型作者标为“眼睛R1”。这是物理输出参数，静态表情通常保持默认。"],
  Param10: ["右眼物理辅助 2", "模型作者标为“眼睛R2”。这是物理输出参数，不建议直接加入情绪映射。"],
  ParamAngleX: ["头部左右转", "负值/正值控制左右方向；具体左右以模型画面为准。先用小幅度，再逐步增加。"],
  ParamAngleY: ["头部上下抬低", "调节抬头与低头；先在 ±5 到 ±10 范围观察，避免下巴或头发穿插。"],
  ParamAngleZ: ["头部侧倾", "控制歪头角度；正负方向相反。表情用小幅度，强烈姿态再逐步放大。"],
  ParamBodyAngleX: ["身体左右转", "身体跟随头部的水平转动。通常应明显小于 ParamAngleX。"],
  ParamBodyAngleY: ["身体前后/上下姿态", "模型实际方向可能不同；适合轻微呼吸或前倾，避免大幅持续使用。"],
  ParamBodyAngleZ: ["身体侧倾", "配合 ParamAngleZ 使用，但幅度应更小，防止整个人像僵硬倾倒。"],
  ParamEyeLOpen: ["左眼开合", "通常 0 为闭眼、1 为睁眼。半闭时同步检查虹膜是否被裁切。"],
  ParamEyeROpen: ["右眼开合", "通常 0 为闭眼、1 为睁眼。需要自然对称时与左眼接近。"],
  ParamEyeBallX: ["双眼左右视线", "通常 -1 到 1。眼睛变窄时应同步降低追踪幅度。"],
  ParamEyeBallY: ["双眼上下视线", "通常 -1 到 1。半闭眼时可小幅平移，让虹膜中心对准眼缝。"],
  ParamEyeSmile: ["笑眼强度", "逐步增加观察眼尾弧度；不要只靠压低 EyeOpen 制造笑眼。"],
  ParamEyeLSmile: ["左眼笑意", "用于左眼弯曲；与右眼参数配合，避免单边异常。"],
  ParamEyeRSmile: ["右眼笑意", "用于右眼弯曲；与左眼参数配合，避免单边异常。"],
  ParamEyeBallForm: ["虹膜/瞳孔缩放", "模型若支持，负值常用于缩小瞳孔、正值用于放大；以画面实测为准。"],
  ParamMouthOpenY: ["嘴巴开合", "0 为闭合，增大后张嘴。表情静态截图通常先设 0，再单独观察嘴型。"],
  ParamMouthForm: ["基础嘴部曲线", "此模型通常 -1 偏生气下弧、0 偏中性/波浪、1 偏微笑；不要用它替代所有分类嘴型。"],
  ParamCheek: ["脸红强度", "0 应完全无脸红，逐步增加到 1；先定颜色，再决定不同情绪的百分比。"],
  ParamBrowLY: ["左眉高度", "上下移动左眉。惊讶可抬高，担心/生气需结合眉形与角度。"],
  ParamBrowRY: ["右眉高度", "上下移动右眉。与左眉略有差异可增加自然感。"],
  ParamBrowLX: ["左眉左右位置", "水平移动左眉。先保持接近 0，小步调整并观察是否改善不对称构图。"],
  ParamBrowRX: ["右眉左右位置", "水平移动右眉。通常与左眉配合，避免眉毛靠得过近或过远。"],
  ParamBrowLForm: ["左眉形状", "改变左眉曲线；正负意义由模型决定，建议从 0 每次改 0.1。"],
  ParamBrowRForm: ["右眉形状", "改变右眉曲线；先与左眉对称测试，再做情绪性不对称。"],
  ParamBrowLAngle: ["左眉角度", "用于生气、担忧等眉峰方向；小步调节，避免眉毛翻转。"],
  ParamBrowRAngle: ["右眉角度", "用于生气、担忧等眉峰方向；与左眉配合观察。"],
  ParamBreath: ["呼吸", "通常 0 到 1 循环。静态表情标签里保持默认即可。"],
  Param3: ["害羞表情层", "作者自定义的分类层。标注模式中可连续调节并与 Param4–Param8 自由混合；正式情绪映射暂不采用这些混合。"],
  Param4: ["开心/俏皮表情层", "当前模型中适合高兴和 teasing。标注模式允许连续强度和跨层叠加，请观察嘴线是否自然。"],
  Param5: ["生气表情层", "当前模型中的生气嘴与相关面部层。标注模式允许连续强度和跨层叠加。"],
  Param6: ["低落表情层", "当前模型中的悲伤/低落层。标注模式允许连续混合，注意半闭眼时的虹膜裁切。"],
  Param7: ["强害羞表情层", "作者自定义的更强害羞层。标注模式允许与 Param3 及其他层同时使用。"],
  Param8: ["震惊表情层", "当前模型中的惊讶/瞳孔变化层。标注模式允许连续混合，再用眼睛开合和独立瞳孔缩放微调。"],
  Param11: ["头发物理 X1", "模型作者定义为头发物理输出，但现有情绪表曾把它当紧张姿态使用，属于待验证疑点。截图时先保持默认，再单独小步测试。"],
  Param12: ["头发物理 X2", "模型作者定义为头发物理输出，但现有情绪表曾把它当紧张姿态使用，属于待验证疑点。截图时先保持默认，再单独小步测试。"],
  Param14: ["头部大角度转向辅助", "工程中配合 ParamAngleX 扩大侧头；先固定身体，再确认它是否让头发或轮廓变形。"],
  Param15: ["头部俯仰辅助", "工程中跟随 ParamAngleY。保持小幅，观察头发、下巴和颈部连接。"],
  Param16: ["手臂/姿态收束", "工程中用于手臂和整体姿态变化。通常 0 到 1；逐步调整观察双臂位置。"],
  Param17: ["身体水平位移辅助", "工程中跟随身体与舞台水平移动。强度应低于头部转向。"],
  Param13: ["头发摆动辅助 A", "工程中跟随头发摆动；静态表情可先保持默认。"],
  Param18: ["头发摆动辅助 B", "工程中跟随头发摆动；静态表情可先保持默认。"],
  ParamHairFront: ["前发摆动", "标准头发物理参数。静态表情保持默认；动态时用小幅周期变化。"],
  ParamHairSide: ["侧发摆动", "标准头发物理参数。检查侧发不要穿过脸颊和肩部。"],
  ParamHairBack: ["后发摆动", "标准头发物理参数。幅度通常比前发更缓、更滞后。"],
});

function live2dParameterGuide(id) {
  const exact = LIVE2D_PARAMETER_GUIDES[id];
  if (exact) return { name: exact[0], adjustment: exact[1], known: true };
  const normalized = String(id || "");
  if (/Mouth/i.test(normalized)) {
    return {
      name: "嘴部自定义参数",
      adjustment: "先把 MouthOpenY 设为 0，单独小步调整并观察嘴角、上下唇和牙齿；不要同时改变多个未知嘴部参数。",
      known: false,
    };
  }
  if (/Eye|Pupil|Iris/i.test(normalized)) {
    return {
      name: "眼睛/虹膜自定义参数",
      adjustment: "先保持视线居中，逐步改变；同时检查睁眼、半闭和闭眼三种状态，避免虹膜只剩下半段。",
      known: false,
    };
  }
  if (/Brow/i.test(normalized)) {
    return {
      name: "眉毛自定义参数",
      adjustment: "每次只改一个参数约 10% 范围，记录它影响高度、角度还是眉形，再与另一侧匹配。",
      known: false,
    };
  }
  if (/Hair/i.test(normalized)) {
    return {
      name: "头发物理/形变参数",
      adjustment: "静态表情通常保持默认；需要动作时小幅测试，确认不会让发丝穿过脸部或身体。",
      known: false,
    };
  }
  return {
    name: "作者自定义参数（待实测）",
    adjustment: "当前没有可靠语义。先记住默认值，每次移动约总范围的 5%–10%，截图标注可见变化；无变化时恢复默认。",
    known: false,
  };
}

function developerFoundationParameterIds() {
  const ids = new Set(DEVELOPER_FOUNDATION_PARAMETER_IDS);
  for (const channel of ("pupilScale", "eyeSmile", "blush")) {
    mappedParameterIds(channel).forEach(id => ids.add(id));
  }
  return ids;
}

function developerParameterGroup(parameter) {
  const group = DEVELOPER_FOUNDATION_PARAMETER_GROUPS.find(item => item.ids.includes(parameter.id));
  if (group) return group;
  if (mappedParameterIds("pupilScale").includes(parameter.id)) {
    return DEVELOPER_FOUNDATION_PARAMETER_GROUPS[0];
  }
  return { key: "advanced", label: "高级：作者预制层、物理与身体参数" };
}

function renderDeveloperParameterCapabilities(parameters = developerParameterMetadata) {
  if (!dom.devParameterCapabilityStatus) return;
  const ids = new Set(parameters.map(item => item.id));
  const pupilIds = mappedParameterIds("pupilScale").filter(id => ids.has(id));
  const hasPupilScale = pupilIds.length > 0 || ids.has("ParamEyeBallForm");
  const hasExtractedPupilScale = (
    coreModel?.getDrawableIndex?.("ArtMesh60") >= 0
    && coreModel?.getDrawableIndex?.("ArtMesh98") >= 0
  );
  const hasEyeHorizontalSize = [...ids].some(id => /Eye.*(?:ScaleX|SizeX|Width)/i.test(id));
  const hasIndependentEyePosition = [...ids].some(id => /Eye[LR].*(?:Position|Pos)[XY]/i.test(id));
  const capabilities = [
    ["左右眼独立开合与笑眼", ids.has("ParamEyeLOpen") && ids.has("ParamEyeROpen"), "可直接组合"],
    ["眼球左右/上下", ids.has("ParamEyeBallX") && ids.has("ParamEyeBallY"), "此模型只能双眼联动"],
    [
      "瞳孔/虹膜独立缩放",
      hasPupilScale || hasExtractedPupilScale,
      hasPupilScale
        ? `作者参数：${pupilIds.join("、") || "ParamEyeBallForm"}`
        : hasExtractedPupilScale
          ? "作者未公开参数；训练页已从 ArtMesh60 / ArtMesh98 提取左右运行时缩放"
          : "原模型未公开；Param8 只是作者的整层“惊讶”预设",
    ],
    ["眼睛横向大小", hasEyeHorizontalSize, hasEyeHorizontalSize ? "可直接调节" : "原模型未公开独立参数"],
    ["整只眼睛上下/左右平移", hasIndependentEyePosition, hasIndependentEyePosition ? "可直接调节" : "原模型未公开；目前只有眼球视线可移动"],
    ["嘴部弧度与开合", ids.has("ParamMouthForm") && ids.has("ParamMouthOpenY"), "有一条连续嘴形轴和一条开合轴，没有独立嘴角控制"],
  ];
  dom.devParameterCapabilityStatus.replaceChildren(...capabilities.map(([label, supported, detail]) => {
    const item = document.createElement("div");
    item.className = `dev-parameter-capability ${supported ? "supported" : "unsupported"}`;
    item.textContent = `${label}：${detail}`;
    return item;
  }));
}

function coreParameterIdAt(index) {
  const ids = coreModel?._parameterIds;
  const raw = typeof ids?.at === "function" ? ids.at(index) : ids?.[index];
  if (typeof raw === "string") return raw;
  const value = typeof raw?.getString === "function" ? raw.getString() : raw;
  if (typeof value === "string") return value;
  if (typeof value?.s === "string") return value.s;
  const text = String(value ?? raw ?? "");
  return text === "[object Object]" ? `Parameter${index}` : text;
}

async function loadDeveloperParameterDisplayInfo() {
  developerParameterDisplayNames.clear();
  if (!DEV_MODE || !activeModelProfile?.modelUrl) return;
  try {
    const modelResponse = await fetch(activeModelProfile.modelUrl, { cache: "no-store" });
    if (!modelResponse.ok) throw new Error(`model3 HTTP ${modelResponse.status}`);
    const modelJson = await modelResponse.json();
    const displayInfo = String(modelJson?.FileReferences?.DisplayInfo || "").trim();
    if (!displayInfo) return;
    const displayUrl = new URL(displayInfo, new URL(activeModelProfile.modelUrl, window.location.href));
    const displayResponse = await fetch(displayUrl, { cache: "no-store" });
    if (!displayResponse.ok) throw new Error(`cdi3 HTTP ${displayResponse.status}`);
    const displayJson = await displayResponse.json();
    (Array.isArray(displayJson?.Parameters) ? displayJson.Parameters : []).forEach(item => {
      const id = String(item?.Id || "").trim();
      const name = String(item?.Name || "").trim();
      if (id && name) developerParameterDisplayNames.set(id, name);
    });
  } catch (error) {
    console.debug("Live2D parameter display info unavailable:", error);
  }
}

function readLive2DParameters() {
  if (!coreModel || typeof coreModel.getParameterCount !== "function") return [];
  const count = Math.max(0, Number(coreModel.getParameterCount()) || 0);
  const parameters = [];
  for (let index = 0; index < count; index += 1) {
    try {
      const id = coreParameterIdAt(index);
      const minimum = Number(coreModel.getParameterMinimumValue(index));
      const maximum = Number(coreModel.getParameterMaximumValue(index));
      const defaultValue = Number(coreModel.getParameterDefaultValue(index));
      const currentValue = Number(coreModel.getParameterValueByIndex(index));
      const guide = live2dParameterGuide(id);
      parameters.push({
        index,
        id,
        minimum: Number.isFinite(minimum) ? minimum : -1,
        maximum: Number.isFinite(maximum) ? maximum : 1,
        defaultValue: Number.isFinite(defaultValue) ? defaultValue : 0,
        currentValue: Number.isFinite(currentValue) ? currentValue : 0,
        authorName: developerParameterDisplayNames.get(id) || "",
        ...guide,
      });
    } catch (error) {
      console.debug("Live2D parameter inspection failed:", index, error);
    }
  }
  return parameters;
}

function live2dParameterStep(parameter) {
  const span = Math.abs(parameter.maximum - parameter.minimum);
  if (!span) return 0.001;
  return Math.max(0.0001, Number((span / 200).toPrecision(4)));
}

function formatLive2DParameterValue(value) {
  return Number(toNumber(value, 0).toFixed(4)).toString();
}

function refreshDeveloperParameterControls() {
  if (!DEV_MODE || !dom.devParameterControls) return;
  developerParameterMetadata = readLive2DParameters();
  renderDeveloperParameterCapabilities(developerParameterMetadata);
  if (!developerParameterMetadata.length) {
    dom.devParameterControls.replaceChildren();
    if (dom.devParameterStatus) dom.devParameterStatus.textContent = "当前模型尚未加载，或没有暴露可调参数。";
    return;
  }
  const foundationIds = developerFoundationParameterIds();
  const visibleParameters = developerParameterMetadata.filter(parameter => (
    foundationIds.has(parameter.id) || developerParameterShowAdvanced
  ));
  const rows = [];
  let previousGroupKey = "";
  visibleParameters
    .sort((left, right) => {
      const leftGroup = developerParameterGroup(left);
      const rightGroup = developerParameterGroup(right);
      const groupOrder = [...DEVELOPER_FOUNDATION_PARAMETER_GROUPS.map(item => item.key), "advanced"];
      const groupDelta = groupOrder.indexOf(leftGroup.key) - groupOrder.indexOf(rightGroup.key);
      if (groupDelta) return groupDelta;
      const groupIds = leftGroup.ids || [];
      return groupIds.indexOf(left.id) - groupIds.indexOf(right.id);
    })
    .forEach(parameter => {
    const group = developerParameterGroup(parameter);
    if (group.key !== previousGroupKey) {
      const groupTitle = document.createElement("h3");
      groupTitle.className = `dev-parameter-group-title${group.key === "advanced" ? " advanced" : ""}`;
      groupTitle.textContent = group.label;
      rows.push(groupTitle);
      previousGroupKey = group.key;
    }
    const row = document.createElement("section");
    row.className = "dev-parameter-row";
    row.dataset.parameterIndex = String(parameter.index);
    row.dataset.parameterSearch = `${parameter.id} ${parameter.name} ${parameter.adjustment}`.toLowerCase();

    const description = document.createElement("div");
    const heading = document.createElement("div");
    heading.className = "dev-parameter-heading";
    const code = document.createElement("code");
    code.textContent = parameter.id;
    const name = document.createElement("strong");
    name.textContent = parameter.name;
    heading.append(code, name);
    const meaning = document.createElement("div");
    meaning.className = "dev-parameter-description";
    const authorLabel = parameter.authorName ? `模型原名：${parameter.authorName}。` : "";
    meaning.textContent = parameter.known
      ? `${authorLabel}已根据标准 Cubism 语义、作者标注或当前工程映射说明。`
      : `${authorLabel}模型作者未提供足够语义，目前不能可靠断言其具体作用。`;
    const adjustment = document.createElement("div");
    adjustment.className = "dev-parameter-adjustment";
    adjustment.textContent = `怎么调：${parameter.adjustment}`;
    description.append(heading, meaning, adjustment);

    const editor = document.createElement("div");
    editor.className = "dev-parameter-editor";
    const step = live2dParameterStep(parameter);
    const override = developerParameterOverrides.get(parameter.id);
    const value = override ? override.value : parameter.currentValue;
    const range = document.createElement("input");
    range.type = "range";
    range.min = String(parameter.minimum);
    range.max = String(parameter.maximum);
    range.step = String(step);
    range.value = String(value);
    range.dataset.parameterIndex = String(parameter.index);
    range.dataset.parameterRole = "range";
    const number = document.createElement("input");
    number.type = "number";
    number.min = String(parameter.minimum);
    number.max = String(parameter.maximum);
    number.step = String(step);
    number.value = formatLive2DParameterValue(value);
    number.dataset.parameterIndex = String(parameter.index);
    number.dataset.parameterRole = "number";
    const reset = document.createElement("button");
    reset.type = "button";
    reset.textContent = "回到基线";
    reset.dataset.parameterReset = String(parameter.index);
    const bounds = document.createElement("div");
    bounds.className = "dev-parameter-range";
    bounds.append(
      Object.assign(document.createElement("span"), { textContent: `最小 ${formatLive2DParameterValue(parameter.minimum)}` }),
      Object.assign(document.createElement("span"), { textContent: `默认 ${formatLive2DParameterValue(parameter.defaultValue)}` }),
      Object.assign(document.createElement("span"), { textContent: `最大 ${formatLive2DParameterValue(parameter.maximum)}` }),
    );
    editor.append(range, number, reset, bounds);
    row.append(description, editor);
    rows.push(row);
  });
  dom.devParameterControls.replaceChildren(...rows);
  if (dom.devParameterCaptureLabelInput && !dom.devParameterCaptureLabelInput.value.trim()) {
    dom.devParameterCaptureLabelInput.value = "待定义情绪";
  }
  if (dom.devParameterStatus) {
    const availableFoundationCount = developerParameterMetadata.filter(item => foundationIds.has(item.id)).length;
    dom.devParameterStatus.textContent = developerExpressionCalibrationActive
      ? `基础表情标定中：已冻结 ${developerParameterOverrides.size} 个参数；当前显示 ${availableFoundationCount} 个基础参数。`
      : `当前模型共有 ${developerParameterMetadata.length} 个参数，其中 ${availableFoundationCount} 个可用于基础表情组合。请先选择起点。`;
  }
  filterDeveloperParameterControls();
}

function filterDeveloperParameterControls() {
  const query = String(dom.devParameterFilterInput?.value || "").trim().toLowerCase();
  dom.devParameterControls?.querySelectorAll(".dev-parameter-row").forEach(row => {
    row.hidden = Boolean(query) && !String(row.dataset.parameterSearch || "").includes(query);
  });
}

function toggleDeveloperParameterLock() {
  const enabled = Boolean(dom.devParameterLockInput?.checked);
  if (enabled && !developerExpressionCalibrationActive) {
    startDeveloperExpressionCalibration("current");
    return;
  }
  developerParameterLock = enabled;
  developerExpressionCalibrationActive = enabled;
  if (dom.devParameterStatus) {
    dom.devParameterStatus.textContent = developerParameterLock
      ? `静态标定已开启；${developerParameterOverrides.size} 个基础参数和预制层状态已冻结。`
      : "静态标定已暂停；自动表情重新接管。要继续标定，请重新选择起点。";
  }
}

function toggleDeveloperParameterAdvanced() {
  developerParameterShowAdvanced = Boolean(dom.devParameterAdvancedInput?.checked);
  refreshDeveloperParameterControls();
}

function startDeveloperExpressionCalibration(mode = "default") {
  if (!DEV_MODE || !coreModel) {
    toast("Live2D 模型尚未加载");
    return;
  }
  developerParameterMetadata = readLive2DParameters();
  const foundationIds = developerFoundationParameterIds();
  developerParameterOverrides.clear();
  developerParameterBaseline.clear();
  developerParameterMetadata.forEach(parameter => {
    if (!foundationIds.has(parameter.id)) return;
    const value = mode === "current" ? parameter.currentValue : parameter.defaultValue;
    developerParameterBaseline.set(parameter.id, value);
    developerParameterOverrides.set(parameter.id, { index: parameter.index, value });
  });
  developerParameterMetadata.forEach(parameter => {
    if (!AUTHOR_FACE_LAYER_IDS.includes(parameter.id)) return;
    developerParameterOverrides.set(parameter.id, { index: parameter.index, value: 0 });
  });
  developerParameterLock = true;
  developerExpressionCalibrationActive = true;
  if (dom.devParameterLockInput) dom.devParameterLockInput.checked = true;
  applyDeveloperParameterOverrides();
  refreshDeveloperParameterControls();
  toast(mode === "current"
    ? "已冻结当前画面；作者预制表情层已关闭"
    : "已从模型默认脸开始；作者预制表情层已关闭");
}

function setDeveloperParameterOverride(parameter, value) {
  if (!developerExpressionCalibrationActive) {
    startDeveloperExpressionCalibration("current");
  }
  const clamped = clamp(toNumber(value, parameter.defaultValue), parameter.minimum, parameter.maximum);
  developerParameterOverrides.set(parameter.id, { index: parameter.index, value: clamped });
  developerParameterLock = true;
  if (dom.devParameterLockInput) dom.devParameterLockInput.checked = true;
  try {
    coreModel.setParameterValueByIndex(parameter.index, clamped);
  } catch {
    setParam(parameter.id, clamped);
  }
  return clamped;
}

function syncDeveloperParameterRow(index, value) {
  const row = dom.devParameterControls?.querySelector(`.dev-parameter-row[data-parameter-index="${index}"]`);
  if (!row) return;
  row.querySelectorAll("[data-parameter-role]").forEach(input => {
    input.value = input.dataset.parameterRole === "number"
      ? formatLive2DParameterValue(value)
      : String(value);
  });
}

function handleDeveloperParameterInput(event) {
  const input = event.target.closest?.("[data-parameter-index]");
  if (!input) return;
  const index = Number(input.dataset.parameterIndex);
  const parameter = developerParameterMetadata.find(item => item.index === index);
  if (!parameter) return;
  const value = setDeveloperParameterOverride(parameter, input.value);
  syncDeveloperParameterRow(index, value);
  if (dom.devParameterStatus) {
    dom.devParameterStatus.textContent = `已锁定 ${parameter.id} = ${formatLive2DParameterValue(value)}；共覆盖 ${developerParameterOverrides.size} 个参数。`;
  }
}

function handleDeveloperParameterReset(event) {
  const button = event.target.closest?.("[data-parameter-reset]");
  if (!button) return;
  const index = Number(button.dataset.parameterReset);
  const parameter = developerParameterMetadata.find(item => item.index === index);
  if (!parameter) return;
  const value = setDeveloperParameterOverride(
    parameter,
    developerParameterBaseline.get(parameter.id) ?? parameter.defaultValue,
  );
  syncDeveloperParameterRow(index, value);
}

function resetDeveloperParameterControls() {
  developerParameterOverrides.clear();
  developerParameterBaseline.clear();
  developerParameterLock = false;
  developerExpressionCalibrationActive = false;
  developerAuthorBlushOpacityOverride = null;
  developerPupilScaleOverrides.left = null;
  developerPupilScaleOverrides.right = null;
  if (dom.devParameterLockInput) dom.devParameterLockInput.checked = false;
  Object.keys(paramValues).forEach(key => delete paramValues[key]);
  if (dom.devParameterStatus) dom.devParameterStatus.textContent = "已清除全部手动覆盖，自动情绪与动作重新接管模型。";
  window.setTimeout(refreshDeveloperParameterControls, 120);
}

function applyDeveloperParameterOverrides() {
  if (!DEV_MODE || !developerParameterLock || !coreModel) return;
  developerParameterOverrides.forEach(({ index, value }, id) => {
    try {
      coreModel.setParameterValueByIndex(index, value);
    } catch {
      setParam(id, value);
    }
  });
}

function applyDeveloperDrawableOverrides() {
  if (!coreModel) return;
  const rawModel = typeof coreModel.getModel === "function" ? coreModel.getModel() : null;
  const drawables = rawModel?.drawables;
  if (!drawables) return;
  const authorBlushOpacity = DEV_MODE && developerAuthorBlushOpacityOverride !== null
    ? developerAuthorBlushOpacityOverride
    : runtimeAuthorBlushOpacityValue;
  if (authorBlushOpacity < 0.9999) {
    ["lianhong", "lianhong2"].forEach(id => {
      const index = coreModel.getDrawableIndex(id);
      if (index >= 0) {
        drawables.opacities[index] *= clamp(toNumber(authorBlushOpacity, 1), 0, 1);
      }
    });
  }
  [
    [
      "ArtMesh60",
      DEV_MODE && developerPupilScaleOverrides.left !== null
        ? developerPupilScaleOverrides.left
        : runtimePupilScaleValues.left,
    ],
    [
      "ArtMesh98",
      DEV_MODE && developerPupilScaleOverrides.right !== null
        ? developerPupilScaleOverrides.right
        : runtimePupilScaleValues.right,
    ],
  ].forEach(([id, rawScale]) => {
    const index = coreModel.getDrawableIndex(id);
    const vertices = index >= 0 ? drawables.vertexPositions[index] : null;
    if (!vertices?.length) return;
    let centerX = 0;
    let centerY = 0;
    const vertexCount = vertices.length / 2;
    for (let offset = 0; offset < vertices.length; offset += 2) {
      centerX += vertices[offset];
      centerY += vertices[offset + 1];
    }
    centerX /= vertexCount;
    centerY /= vertexCount;
    const scale = clamp(toNumber(rawScale, 1), 0.35, 1.35);
    for (let offset = 0; offset < vertices.length; offset += 2) {
      vertices[offset] = centerX + (vertices[offset] - centerX) * scale;
      vertices[offset + 1] = centerY + (vertices[offset + 1] - centerY) * scale;
    }
  });
  modelGarmentRig?.update();
}

function developerCaptureCanvas() {
  const extract = app?.renderer?.plugins?.extract || app?.renderer?.extract;
  if (extract && typeof extract.canvas === "function") {
    try {
      return extract.canvas(app.stage);
    } catch (error) {
      console.debug("PIXI stage extraction failed:", error);
    }
  }
  return dom.canvas;
}

async function captureDeveloperParameterSnapshot() {
  if (!DEV_MODE || !coreModel) {
    toast("Live2D 模型尚未加载");
    return;
  }
  const label = String(dom.devParameterCaptureLabelInput?.value || "").trim();
  if (!label) {
    toast("请先填写截图标签；它会成为文件名的一部分");
    dom.devParameterCaptureLabelInput?.focus();
    return;
  }
  if (!developerExpressionCalibrationActive || !developerParameterLock) {
    toast("请先选择“从模型默认脸开始”或“以当前画面为起点”");
    return;
  }
  dom.devCaptureParametersBtn.disabled = true;
  try {
    applyDeveloperParameterOverrides();
    const canvas = developerCaptureCanvas();
    const imageDataUrl = canvas.toDataURL("image/png");
    const foundationIds = developerFoundationParameterIds();
    const parameters = readLive2DParameters().map(parameter => ({
      id: parameter.id,
      index: parameter.index,
      value: developerParameterOverrides.get(parameter.id)?.value ?? parameter.currentValue,
      baselineValue: developerParameterBaseline.get(parameter.id) ?? parameter.defaultValue,
      normalizedDelta: (() => {
        const value = developerParameterOverrides.get(parameter.id)?.value ?? parameter.currentValue;
        const baseline = developerParameterBaseline.get(parameter.id) ?? parameter.defaultValue;
        const span = parameter.maximum - parameter.minimum;
        return span ? (value - baseline) / span : 0;
      })(),
      minimum: parameter.minimum,
      maximum: parameter.maximum,
      defaultValue: parameter.defaultValue,
      name: parameter.name,
      authorName: parameter.authorName,
      known: parameter.known,
      foundation: foundationIds.has(parameter.id),
    }));
    const response = await fetch("/api/developer/live2d-expression-capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label,
        userEmotionLabel: label,
        calibrationMode: "foundation-composition",
        presetLayersDisabled: true,
        emotion: effectiveVisualEmotion(),
        foregroundEmotion: currentEmotion,
        emotionIntensity: currentEmotionWeight,
        modelId: activeModelProfile?.id || "",
        imageDataUrl,
        parameters,
        compositionParameterIds: parameters.filter(item => item.foundation).map(item => item.id),
        baselineParameters: Object.fromEntries(developerParameterBaseline),
        manualOverrides: Object.fromEntries(
          [...developerParameterOverrides.entries()].map(([id, item]) => [id, item.value]),
        ),
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    if (dom.devParameterStatus) {
      dom.devParameterStatus.textContent = `已保存：${data.filePath}；同名 JSON 保存完整参数与情绪上下文。`;
    }
    toast(`参数截图已保存：${data.fileName}`);
  } catch (error) {
    toast(`参数截图保存失败：${error.message}`);
  } finally {
    dom.devCaptureParametersBtn.disabled = false;
  }
}

async function openDeveloperParameterCaptureDirectory() {
  if (!window.pywebview?.api?.openLive2DExpressionCaptureDirectory) {
    toast("参数截图目录：data/runtime/live2d-expression-captures");
    return;
  }
  try {
    const result = await window.pywebview.api.openLive2DExpressionCaptureDirectory();
    if (result?.ok === false) throw new Error(result.error || "打开失败");
    toast("已打开参数截图目录");
  } catch (error) {
    toast(`打开参数截图目录失败：${error.message}`);
  }
}

function developerExpressionTrainingId(prefix = "sample") {
  const token = typeof crypto?.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${token}`.replace(/[^A-Za-z0-9_.-]+/g, "-");
}

function developerExpressionTrainingParameterSupport(id) {
  if (["VirtualAuthorBlushOpacity", "VirtualPupilLScale", "VirtualPupilRScale"].includes(id)) return "confirmed";
  if (AUTHOR_FACE_LAYER_IDS.includes(id)) return "coupled-layer";
  if (EXPRESSION_TRAINING_UNBOUND_IDS.has(id)) return "unbound";
  if ([
    "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeBallX", "ParamEyeBallY",
    "ParamMouthForm", "ParamMouthOpenY", "ParamAngleX", "ParamAngleY", "ParamAngleZ",
  ].includes(id)) return "confirmed";
  return "unverified";
}

function developerExpressionTrainingParameterIds() {
  const available = new Set(developerParameterMetadata.map(item => item.id));
  return EXPRESSION_TRAINING_PARAMETER_GROUPS
    .flatMap(group => group.ids)
    .filter((id, index, ids) => (
      (available.has(id) || ["VirtualAuthorBlushOpacity", "VirtualPupilLScale", "VirtualPupilRScale"].includes(id))
      && ids.indexOf(id) === index
    ));
}

function developerExpressionTrainingParameterById(id) {
  if (id === "VirtualPupilLScale" || id === "VirtualPupilRScale") {
    const side = id === "VirtualPupilLScale" ? "left" : "right";
    const sideLabel = side === "left" ? "画面左侧" : "画面右侧";
    return {
      index: -1,
      id,
      minimum: 0.35,
      maximum: 1.35,
      defaultValue: 1,
      currentValue: developerPupilScaleOverrides[side] ?? 1,
      authorName: `${sideLabel}瞳孔/虹膜大小`,
      name: `${sideLabel}瞳孔/虹膜大小`,
      adjustment: `以当前眼球位置为中心缩放${sideLabel}的独立虹膜网格。1 为作者原大小；减小可表现震惊，放大用于柔和或呆滞感，不改变眼皮开合。`,
      known: true,
    };
  }
  if (id === "VirtualAuthorBlushOpacity") {
    return {
      index: -1,
      id,
      minimum: 0,
      maximum: 1,
      defaultValue: 1,
      currentValue: developerAuthorBlushOpacityOverride ?? 1,
      authorName: "作者层脸红可见度",
      name: "作者层脸红可见度",
      adjustment: "只控制 lianhong / lianhong2 两个独立网格。设为 0 可保留娇羞层的其他变化，同时隐藏它自带的脸红；普通 ParamCheek 不受影响。",
      known: true,
    };
  }
  return developerParameterMetadata.find(item => item.id === id);
}

function developerExpressionTrainingCurve(id, strengthBand) {
  if (strengthBand === "typical") return 1;
  if (strengthBand === "subtle") {
    if (id === "ParamCheek") return 0.2;
    if (/^ParamAngle|^Param1[456]$|^ParamBody/.test(id)) return 0.46;
    if (/Eye.*Open/.test(id)) return 0.42;
    if (/Mouth/.test(id)) return 0.36;
    return 0.4;
  }
  if (id === "ParamCheek") return 1.14;
  if (/^ParamAngle|^Param1[456]$|^ParamBody/.test(id)) return 1.28;
  if (/Eye.*Open/.test(id)) return 1.08;
  if (/Mouth/.test(id)) return 1.12;
  return 1.16;
}

function recommendedDeveloperExpressionTrainingParameters(emotion, strengthBand) {
  const result = {};
  const neutralTargets = emotionTargets.neutral || {};
  const targets = emotionTargets[emotion] || neutralTargets;
  const metadataById = new Map(developerParameterMetadata.map(item => [item.id, item]));
  developerExpressionTrainingParameterIds().forEach(id => {
    const parameter = developerExpressionTrainingParameterById(id);
    if (!parameter) return;
    if (id === "VirtualAuthorBlushOpacity") {
      result[id] = 1;
      return;
    }
    if (id === "VirtualPupilLScale" || id === "VirtualPupilRScale") {
      const intensity = strengthBand === "subtle" ? 0.45 : strengthBand === "strong" ? 1.65 : 1;
      if (emotion === "startled") result[id] = Number(clamp(0.91 - intensity * 0.25, 0.45, 1).toFixed(4));
      else if (emotion === "flustered") result[id] = Number(clamp(0.97 - intensity * 0.08, 0.78, 1).toFixed(4));
      else if (emotion === "shy") result[id] = Number(clamp(0.99 - intensity * 0.045, 0.84, 1).toFixed(4));
      else result[id] = 1;
      return;
    }
    if (AUTHOR_FACE_LAYER_IDS.includes(id)) {
      result[id] = 0;
      return;
    }
    const baseline = toNumber(neutralTargets[id], parameter.defaultValue);
    if (EXPRESSION_TRAINING_UNBOUND_IDS.has(id)) {
      result[id] = parameter.defaultValue;
      return;
    }
    const target = toNumber(targets[id], baseline);
    const blend = developerExpressionTrainingCurve(id, strengthBand);
    result[id] = Number(clamp(
      baseline + (target - baseline) * blend,
      parameter.minimum,
      parameter.maximum,
    ).toFixed(4));
  });
  let layer = EXPRESSION_TRAINING_LAYER_BY_EMOTION[emotion] || "";
  if (emotion === "shy" && strengthBand === "strong") layer = "Param7";
  if (layer && metadataById.has(layer)) result[layer] = 1;
  return result;
}

function developerExpressionTrainingCouplingWarnings(parameters) {
  const warnings = [];
  const activeLayers = AUTHOR_FACE_LAYER_IDS.filter(id => Math.abs(toNumber(parameters?.[id], 0)) > 0.0001);
  AUTHOR_FACE_LAYER_IDS.forEach(id => {
    if (Math.abs(toNumber(parameters?.[id], 0)) > 0.0001 && EXPRESSION_TRAINING_LAYER_WARNINGS[id]) {
      warnings.push(EXPRESSION_TRAINING_LAYER_WARNINGS[id]);
    }
  });
  if (activeLayers.length > 1) {
    warnings.unshift(`当前自由混合 ${activeLayers.length} 个作者分类层：${activeLayers.join(" + ")}。这里只记录观察结果，不限制数值或总量。`);
  }
  if (!developerParameterMetadata.some(item => item.id === "ParamEyeBallForm")) {
    warnings.push("作者没有公开瞳孔缩放参数；训练台已从独立虹膜网格 ArtMesh60 / ArtMesh98 提取出左右缩放，属于运行时虚拟参数，不会再借用害羞或惊讶层冒充。");
  }
  return warnings;
}

function buildInitialDeveloperExpressionTrainingSamples() {
  const modelId = activeModelProfile?.id || "";
  const support = Object.fromEntries(
    developerExpressionTrainingParameterIds().map(id => [id, developerExpressionTrainingParameterSupport(id)]),
  );
  return Object.entries(EXPRESSION_TRAINING_EMOTION_LABELS).flatMap(([emotion, emotionLabel]) => (
    EXPRESSION_TRAINING_STRENGTHS.map(strength => {
      const parameters = recommendedDeveloperExpressionTrainingParameters(emotion, strength.id);
      const label = `${emotionLabel}_${strength.label}`;
      const tags = [emotion, emotionLabel, strength.label, "初始建议"];
      const notes = "由当前模型结构和已确认有效参数生成的初始假设，等待用户监督修订。";
      return {
        id: `seed-${emotion}-${strength.id}`,
        label,
        recommendedLabel: label,
        emotion,
        strengthBand: strength.id,
        intensity: strength.intensity,
        tags,
        notes,
        modelId,
        source: "codex-seed",
        recommendedParameters: { ...parameters },
        recommendedMetadata: {
          emotion,
          strengthBand: strength.id,
          intensity: strength.intensity,
          tags: [...tags],
          notes,
        },
        parameters: { ...parameters },
        parameterSupport: support,
        couplingWarnings: developerExpressionTrainingCouplingWarnings(parameters),
        userEdited: false,
        revision: 0,
      };
    })
  ));
}

function replaceDeveloperExpressionTrainingDataset(data) {
  const samples = Array.isArray(data?.dataset?.samples) ? data.dataset.samples : [];
  developerExpressionRuntime = data?.dataset?.runtime && typeof data.dataset.runtime === "object"
    ? data.dataset.runtime
    : { schemaVersion: 1, activeByModel: {} };
  settings.expressionRuntime = developerExpressionRuntime;
  developerExpressionTrainingSamples = samples.map(item => ({
    ...item,
    tags: Array.isArray(item.tags) ? item.tags : [],
    recommendedParameters: { ...(item.recommendedParameters || {}) },
    recommendedMetadata: {
      ...(item.recommendedMetadata || {}),
      tags: Array.isArray(item.recommendedMetadata?.tags)
        ? [...item.recommendedMetadata.tags]
        : [...(Array.isArray(item.tags) ? item.tags : [])],
    },
    parameters: { ...(item.parameters || {}) },
    parameterSupport: { ...(item.parameterSupport || {}) },
    couplingWarnings: Array.isArray(item.couplingWarnings) ? item.couplingWarnings : [],
  }));
  developerExpressionTrainingLoaded = true;
  if (
    developerExpressionTrainingSelectedId
    && !developerExpressionTrainingSamples.some(item => item.id === developerExpressionTrainingSelectedId)
  ) {
    developerExpressionTrainingSelectedId = "";
  }
  renderDeveloperExpressionTrainingGallery();
}

function populateDeveloperExpressionTrainingSelectors() {
  const makeOptions = (includeAll = false) => {
    const options = [];
    if (includeAll) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "全部情绪";
      options.push(option);
    }
    Object.entries(EXPRESSION_TRAINING_EMOTION_LABELS).forEach(([id, label]) => {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = `${id} / ${label}`;
      options.push(option);
    });
    return options;
  };
  if (dom.devTrainingEmotionFilter && !dom.devTrainingEmotionFilter.dataset.populated) {
    dom.devTrainingEmotionFilter.replaceChildren(...makeOptions(true));
    dom.devTrainingEmotionFilter.dataset.populated = "1";
  }
  if (dom.devTrainingEmotionInput && !dom.devTrainingEmotionInput.dataset.populated) {
    dom.devTrainingEmotionInput.replaceChildren(...makeOptions(false));
    dom.devTrainingEmotionInput.dataset.populated = "1";
  }
}

async function loadDeveloperExpressionTraining() {
  if (!DEV_MODE || developerExpressionTrainingLoaded) {
    renderDeveloperExpressionTrainingGallery();
    return;
  }
  if (!coreModel) {
    if (dom.devTrainingStatus) dom.devTrainingStatus.textContent = "等待 Live2D 模型";
    return;
  }
  populateDeveloperExpressionTrainingSelectors();
  developerParameterMetadata = readLive2DParameters();
  if (dom.devTrainingStatus) dom.devTrainingStatus.textContent = "载入监督样本…";
  try {
    let response = await storageScopedFetch("/api/developer/expression-training", { cache: "no-store" });
    let data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    if (!Array.isArray(data.dataset?.samples) || !data.dataset.samples.length) {
      response = await storageScopedFetch("/api/developer/expression-training", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "initialize", samples: buildInitialDeveloperExpressionTrainingSamples() }),
      });
      data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    }
    replaceDeveloperExpressionTrainingDataset(data);
  } catch (error) {
    if (dom.devTrainingStatus) dom.devTrainingStatus.textContent = `载入失败：${error.message}`;
    toast(`训练数据载入失败：${error.message}`);
  }
}

function developerExpressionTrainingSelectedSample() {
  if (
    developerExpressionTrainingEditorOpen
    && developerExpressionTrainingDraft?.id === developerExpressionTrainingSelectedId
  ) {
    return developerExpressionTrainingDraft;
  }
  return developerExpressionTrainingSamples.find(item => item.id === developerExpressionTrainingSelectedId) || null;
}

function persistedDeveloperExpressionTrainingSelectedSample() {
  return developerExpressionTrainingSamples.find(item => item.id === developerExpressionTrainingSelectedId) || null;
}

function developerExpressionRuntimeStatus(sample) {
  const entry = developerExpressionRuntime?.activeByModel?.[sample?.modelId || ""]?.[sample?.emotion || ""];
  if (!entry || entry.sampleId !== sample?.id) return "inactive";
  return Number(entry.sampleRevision || 0) === Number(sample?.revision || 0) ? "active" : "stale";
}

function cloneDeveloperExpressionTrainingSample(sample) {
  if (!sample || typeof sample !== "object") return null;
  return {
    ...sample,
    tags: [...(sample.tags || [])],
    recommendedParameters: { ...(sample.recommendedParameters || {}) },
    recommendedMetadata: {
      ...(sample.recommendedMetadata || {}),
      tags: [...(sample.recommendedMetadata?.tags || sample.tags || [])],
    },
    parameters: { ...(sample.parameters || {}) },
    parameterSupport: { ...(sample.parameterSupport || {}) },
    couplingWarnings: [...(sample.couplingWarnings || [])],
  };
}

function developerExpressionTrainingBaseline(sample) {
  const metadata = sample?.recommendedMetadata && typeof sample.recommendedMetadata === "object"
    ? sample.recommendedMetadata
    : {};
  return {
    label: String(sample?.recommendedLabel || sample?.label || ""),
    emotion: String(metadata.emotion || sample?.emotion || "neutral"),
    strengthBand: String(metadata.strengthBand || sample?.strengthBand || "custom"),
    intensity: clamp(toNumber(metadata.intensity, sample?.intensity ?? 1), 0, 2.5),
    tags: [...(Array.isArray(metadata.tags) ? metadata.tags : (sample?.tags || []))],
    notes: String(metadata.notes ?? sample?.notes ?? ""),
    parameters: { ...(sample?.recommendedParameters || {}) },
  };
}

function developerExpressionTrainingValuesDiffer(left, right) {
  const leftKeys = Object.keys(left || {});
  const rightKeys = Object.keys(right || {});
  const keys = new Set([...leftKeys, ...rightKeys]);
  return [...keys].some(key => Math.abs(toNumber(left?.[key], 0) - toNumber(right?.[key], 0)) > 0.000001);
}

function developerExpressionTrainingHasChanges(sample) {
  if (!sample) return false;
  const baseline = developerExpressionTrainingBaseline(sample);
  const tags = Array.isArray(sample.tags) ? sample.tags : [];
  return (
    String(sample.label || "") !== baseline.label
    || String(sample.emotion || "neutral") !== baseline.emotion
    || String(sample.strengthBand || "custom") !== baseline.strengthBand
    || Math.abs(toNumber(sample.intensity, 1) - baseline.intensity) > 0.000001
    || tags.join("\u0000") !== baseline.tags.join("\u0000")
    || String(sample.notes || "") !== baseline.notes
    || developerExpressionTrainingValuesDiffer(sample.parameters, baseline.parameters)
  );
}

function setDeveloperExpressionTrainingView(editorOpen) {
  developerExpressionTrainingEditorOpen = Boolean(editorOpen);
  dom.devTrainingBrowser?.classList.toggle("hidden", developerExpressionTrainingEditorOpen);
  dom.devTrainingEditor?.classList.toggle("hidden", !developerExpressionTrainingEditorOpen);
  if (developerExpressionTrainingEditorOpen) dom.devVoiceReferenceEditor?.classList.add("hidden");
}

function renderDeveloperExpressionTrainingGallery() {
  if (!DEV_MODE || !dom.devTrainingGallery) return;
  const query = String(dom.devTrainingFilterInput?.value || "").trim().toLowerCase();
  const emotionFilter = String(dom.devTrainingEmotionFilter?.value || "");
  const runtimeFilter = String(dom.devTrainingRuntimeFilter?.value || "");
  const filtered = developerExpressionTrainingSamples.filter(sample => {
    if (emotionFilter && sample.emotion !== emotionFilter) return false;
    if (runtimeFilter && developerExpressionRuntimeStatus(sample) !== runtimeFilter) return false;
    if (!query) return true;
    return [
      sample.label, sample.recommendedLabel, sample.emotion, sample.strengthBand,
      ...(sample.tags || []), sample.notes,
    ].join(" ").toLowerCase().includes(query);
  });
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "dev-training-empty";
    empty.textContent = developerExpressionTrainingLoaded ? "没有符合筛选条件的可编辑图。" : "训练样本尚未载入。";
    dom.devTrainingGallery.replaceChildren(empty);
  } else {
    dom.devTrainingGallery.replaceChildren(...filtered.map(sample => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = [
        "dev-training-card",
        sample.id === developerExpressionTrainingSelectedId ? "selected" : "",
        sample.userEdited ? "edited" : "",
      ].filter(Boolean).join(" ");
      card.dataset.trainingSampleId = sample.id;
      const preview = document.createElement("span");
      preview.className = "dev-training-card-preview";
      if (sample.previewFile) {
        const image = document.createElement("img");
        const previewSampleId = sample.previewSourceId || sample.id;
        image.src = `/api/developer/expression-training/preview?id=${encodeURIComponent(previewSampleId)}&revision=${sample.revision || 0}`;
        image.alt = `${sample.label} 的 Live2D 表情预览`;
        preview.append(image);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "dev-training-card-placeholder";
        placeholder.textContent = `${EXPRESSION_TRAINING_EMOTION_LABELS[sample.emotion] || sample.emotion}\n${sample.strengthBand || "custom"}`;
        preview.append(placeholder);
      }
      if (sample.userEdited) {
        const edited = document.createElement("span");
        edited.className = "dev-training-card-edited";
        edited.textContent = "已修订";
        preview.append(edited);
      }
      const runtimeStatus = developerExpressionRuntimeStatus(sample);
      if (runtimeStatus !== "inactive") {
        const runtime = document.createElement("span");
        runtime.className = `dev-training-card-runtime ${runtimeStatus}`;
        runtime.textContent = runtimeStatus === "active" ? "启用中" : "运行版本较旧";
        preview.append(runtime);
      }
      const copy = document.createElement("span");
      copy.className = "dev-training-card-copy";
      const label = document.createElement("strong");
      label.textContent = sample.label;
      const detail = document.createElement("span");
      detail.textContent = `${sample.emotion} · ${sample.strengthBand} · ${toNumber(sample.intensity, 1).toFixed(2)}`;
      copy.append(label, detail);
      card.append(preview, copy);
      return card;
    }));
  }
  if (dom.devTrainingStatus) {
    const edited = developerExpressionTrainingSamples.filter(item => item.userEdited).length;
    const active = developerExpressionTrainingSamples.filter(
      item => developerExpressionRuntimeStatus(item) === "active",
    ).length;
    dom.devTrainingStatus.textContent = `${developerExpressionTrainingSamples.length} 个样本 / ${active} 个启用中 / ${edited} 个已修订`;
  }
  if (dom.devTrainingDuplicateBtn) {
    dom.devTrainingDuplicateBtn.disabled = !persistedDeveloperExpressionTrainingSelectedSample();
  }
}

function handleDeveloperExpressionTrainingGalleryClick(event) {
  const card = event.target.closest?.("[data-training-sample-id]");
  if (!card) return;
  openDeveloperExpressionTrainingEditor(card.dataset.trainingSampleId);
}

function applyDeveloperExpressionTrainingParameters(parameters) {
  if (!DEV_MODE || !coreModel) return;
  developerParameterMetadata = readLive2DParameters();
  developerAuthorBlushOpacityOverride = clamp(
    toNumber(parameters?.VirtualAuthorBlushOpacity, 1),
    0,
    1,
  );
  developerPupilScaleOverrides.left = clamp(toNumber(parameters?.VirtualPupilLScale, 1), 0.35, 1.35);
  developerPupilScaleOverrides.right = clamp(toNumber(parameters?.VirtualPupilRScale, 1), 0.35, 1.35);
  const trainingIds = new Set(developerExpressionTrainingParameterIds());
  developerParameterOverrides.clear();
  developerParameterBaseline.clear();
  developerParameterMetadata.forEach(parameter => {
    if (!trainingIds.has(parameter.id)) return;
    developerParameterBaseline.set(parameter.id, parameter.defaultValue);
    const rawValue = Object.hasOwn(parameters || {}, parameter.id)
      ? parameters[parameter.id]
      : parameter.defaultValue;
    developerParameterOverrides.set(parameter.id, {
      index: parameter.index,
      value: clamp(toNumber(rawValue, parameter.defaultValue), parameter.minimum, parameter.maximum),
    });
  });
  developerParameterLock = true;
  developerExpressionCalibrationActive = true;
  if (dom.devParameterLockInput) dom.devParameterLockInput.checked = true;
  applyDeveloperParameterOverrides();
  try {
    coreModel.update();
  } catch {
    // Cubism compatibility wrappers can update on the next renderer frame.
  }
  applyDeveloperDrawableOverrides();
}

function applySelectedDeveloperExpressionTrainingSample() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample) {
    toast("请先选择一张可编辑图");
    return;
  }
  applyDeveloperExpressionTrainingParameters(sample.parameters);
  refreshDeveloperExpressionTrainingEffectiveOutputs();
  toast(`已应用：${sample.label}`);
}

function openDeveloperExpressionTrainingEditor(sampleId) {
  const sample = developerExpressionTrainingSamples.find(item => item.id === sampleId);
  if (!sample) return;
  developerExpressionTrainingPreviousSelectedId = developerExpressionTrainingSelectedId;
  openDeveloperExpressionTrainingDraft(sample, false);
}

function openDeveloperExpressionTrainingDraft(sample, isNew = false) {
  if (!sample) return;
  developerExpressionTrainingSelectedId = sample.id;
  developerExpressionTrainingDraft = cloneDeveloperExpressionTrainingSample(sample);
  developerExpressionTrainingDraftIsNew = Boolean(isNew);
  developerExpressionTrainingDirty = false;
  setDeveloperExpressionTrainingView(true);
  if (dom.devTrainingLabelInput) dom.devTrainingLabelInput.value = developerExpressionTrainingDraft.label || "";
  if (dom.devTrainingRecommendedLabelInput) {
    dom.devTrainingRecommendedLabelInput.value = developerExpressionTrainingDraft.recommendedLabel || "";
  }
  if (dom.devTrainingEmotionInput) dom.devTrainingEmotionInput.value = developerExpressionTrainingDraft.emotion || "neutral";
  if (dom.devTrainingStrengthInput) {
    dom.devTrainingStrengthInput.value = developerExpressionTrainingDraft.strengthBand || "custom";
  }
  if (dom.devTrainingIntensityInput) {
    dom.devTrainingIntensityInput.value = String(toNumber(developerExpressionTrainingDraft.intensity, 1));
  }
  if (dom.devTrainingIntensityValue) {
    dom.devTrainingIntensityValue.textContent = toNumber(developerExpressionTrainingDraft.intensity, 1).toFixed(2);
  }
  if (dom.devTrainingTagsInput) {
    dom.devTrainingTagsInput.value = (developerExpressionTrainingDraft.tags || []).join(", ");
  }
  if (dom.devTrainingNotesInput) dom.devTrainingNotesInput.value = developerExpressionTrainingDraft.notes || "";
  if (dom.devTrainingEditorHint) {
    dom.devTrainingEditorHint.textContent = `样本 ${sample.id}；当前为独立编辑草稿，保存前不会改变列表状态。修订版本 ${sample.revision || 0}。`;
  }
  applyDeveloperExpressionTrainingParameters(developerExpressionTrainingDraft.parameters);
  renderDeveloperExpressionTrainingParameterControls();
  renderDeveloperExpressionTrainingCouplingWarning();
  refreshDeveloperExpressionTrainingEffectiveOutputs();
  renderDeveloperExpressionRuntimeStatus(developerExpressionTrainingDraft);
  window.requestAnimationFrame(() => {
    dom.devTrainingEditor?.scrollIntoView({ block: "start", inline: "nearest" });
  });
}

function renderDeveloperExpressionRuntimeStatus(sample = developerExpressionTrainingSelectedSample()) {
  if (!dom.devTrainingRuntimeStatus) return;
  const status = sample ? developerExpressionRuntimeStatus(sample) : "inactive";
  const entry = sample
    ? developerExpressionRuntime?.activeByModel?.[sample.modelId || ""]?.[sample.emotion || ""]
    : null;
  dom.devTrainingRuntimeStatus.textContent = status === "active"
    ? `运行状态：启用中。自动情绪 ${sample.emotion} 正在使用 ${sample.label}（修订 ${sample.revision || 0}）。“你的修订”仍是独立标注。`
    : status === "stale"
      ? `运行状态：仍在使用修订 ${entry?.sampleRevision || 0}；当前已保存到修订 ${sample.revision || 0}，需重新启用才会改变真实运行时。`
      : `运行状态：未启用。当前自动情绪仍使用内置映射；保存只记录修订，不会暗中改变真实表情。`;
  if (dom.devTrainingActivateBtn) {
    dom.devTrainingActivateBtn.disabled = !sample || sample.modelId !== (activeModelProfile?.id || "");
  }
  if (dom.devTrainingResetRuntimeBtn) dom.devTrainingResetRuntimeBtn.disabled = !entry;
}

function closeDeveloperExpressionTrainingEditor() {
  if (
    developerExpressionTrainingDirty
    && !window.confirm("当前修改尚未保存。确定放弃这些修改并返回表情列表吗？")
  ) return;
  const returnSampleId = developerExpressionTrainingDraftIsNew
    ? developerExpressionTrainingPreviousSelectedId
    : developerExpressionTrainingSelectedId;
  if (developerExpressionTrainingDraftIsNew) {
    developerExpressionTrainingSelectedId = developerExpressionTrainingPreviousSelectedId;
  }
  developerExpressionTrainingDraft = null;
  developerExpressionTrainingDraftIsNew = false;
  developerExpressionTrainingDirty = false;
  setDeveloperExpressionTrainingView(false);
  resetDeveloperParameterControls();
  renderDeveloperExpressionTrainingGallery();
  window.requestAnimationFrame(() => {
    const selected = dom.devTrainingGallery?.querySelector(".dev-training-card.selected");
    selected?.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
}

function developerExpressionTrainingSupportLabel(support) {
  if (support === "confirmed") return ["已确认有效", ""];
  if (support === "coupled-layer") return ["作者耦合层", ""];
  if (support === "unbound") return ["当前未见绑定", "unbound"];
  return ["尚待确认", "unbound"];
}

function renderDeveloperExpressionTrainingParameterControls() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample || !dom.devTrainingParameterControls) return;
  const rows = [];
  EXPRESSION_TRAINING_PARAMETER_GROUPS.forEach(group => {
    const parameters = group.ids
      .map(id => developerExpressionTrainingParameterById(id))
      .filter(Boolean);
    if (!parameters.length) return;
    const heading = document.createElement("h3");
    heading.className = "dev-training-parameter-group";
    heading.textContent = group.label;
    rows.push(heading);
    parameters.forEach(parameter => {
      const support = sample.parameterSupport?.[parameter.id]
        || developerExpressionTrainingParameterSupport(parameter.id);
      const [supportLabel, supportClass] = developerExpressionTrainingSupportLabel(support);
      const value = toNumber(sample.parameters?.[parameter.id], parameter.defaultValue);
      const recommended = toNumber(sample.recommendedParameters?.[parameter.id], parameter.defaultValue);
      const row = document.createElement("section");
      row.className = `dev-training-parameter${support === "unbound" ? " unsupported" : ""}`;
      row.dataset.trainingParameterId = parameter.id;
      const description = document.createElement("div");
      const title = document.createElement("div");
      title.className = "dev-training-parameter-title";
      const code = document.createElement("code");
      code.textContent = parameter.id;
      const name = document.createElement("strong");
      name.textContent = parameter.authorName || parameter.name;
      const badge = document.createElement("span");
      badge.className = `dev-training-support ${supportClass}`;
      badge.textContent = supportLabel;
      title.append(code, name, badge);
      const help = document.createElement("div");
      help.className = "dev-training-parameter-help";
      help.textContent = parameter.adjustment;
      description.append(title, help);
      const editor = document.createElement("div");
      editor.className = "dev-training-parameter-editor";
      const range = document.createElement("input");
      range.type = "range";
      range.min = String(parameter.minimum);
      range.max = String(parameter.maximum);
      range.step = String(live2dParameterStep(parameter));
      range.value = String(value);
      range.dataset.trainingParameterInput = parameter.id;
      const number = document.createElement("input");
      number.type = "number";
      number.min = range.min;
      number.max = range.max;
      number.step = range.step;
      number.value = formatLive2DParameterValue(value);
      number.dataset.trainingParameterInput = parameter.id;
      const recommendation = document.createElement("span");
      recommendation.className = "dev-training-recommended-value";
      recommendation.textContent = `建议 ${formatLive2DParameterValue(recommended)}`;
      editor.append(range, number, recommendation);
      row.append(description, editor);
      rows.push(row);
    });
  });
  dom.devTrainingParameterControls.replaceChildren(...rows);
}

function syncDeveloperExpressionTrainingParameterInputs(id, value) {
  dom.devTrainingParameterControls?.querySelectorAll(`[data-training-parameter-input="${id}"]`).forEach(input => {
    input.value = input.type === "number" ? formatLive2DParameterValue(value) : String(value);
  });
}

function handleDeveloperExpressionTrainingParameterInput(event) {
  const input = event.target.closest?.("[data-training-parameter-input]");
  if (!input) return;
  const sample = developerExpressionTrainingSelectedSample();
  const parameter = developerExpressionTrainingParameterById(input.dataset.trainingParameterInput);
  if (!sample || !parameter) return;
  const value = clamp(toNumber(input.value, parameter.defaultValue), parameter.minimum, parameter.maximum);
  sample.parameters[parameter.id] = value;
  sample.parameterSupport[parameter.id] = sample.parameterSupport[parameter.id]
    || developerExpressionTrainingParameterSupport(parameter.id);
  developerExpressionTrainingDirty = true;
  syncDeveloperExpressionTrainingParameterInputs(parameter.id, value);
  if (parameter.id === "VirtualAuthorBlushOpacity") {
    developerAuthorBlushOpacityOverride = value;
    applyDeveloperDrawableOverrides();
  } else if (parameter.id === "VirtualPupilLScale" || parameter.id === "VirtualPupilRScale") {
    developerPupilScaleOverrides[parameter.id === "VirtualPupilLScale" ? "left" : "right"] = value;
    try {
      coreModel.update();
    } catch {
      // Cubism compatibility wrappers can update on the next renderer frame.
    }
    applyDeveloperDrawableOverrides();
  } else {
    setDeveloperParameterOverride(parameter, value);
  }
  renderDeveloperExpressionTrainingCouplingWarning();
  refreshDeveloperExpressionTrainingEffectiveOutputs();
}

function renderDeveloperExpressionTrainingCouplingWarning() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample || !dom.devTrainingCouplingWarning) return;
  const warnings = developerExpressionTrainingCouplingWarnings(sample.parameters);
  sample.couplingWarnings = warnings;
  dom.devTrainingCouplingWarning.textContent = warnings.join("\n");
}

function developerTrainingDrawableIdAt(index, drawables = null) {
  const candidates = [
    coreModel?._drawableIds,
    drawables?.ids,
  ];
  for (const ids of candidates) {
    const raw = typeof ids?.at === "function" ? ids.at(index) : ids?.[index];
    if (typeof raw === "string" && raw) return raw;
    const value = typeof raw?.getString === "function" ? raw.getString() : raw;
    if (value) return String(value);
  }
  return `Drawable#${index}`;
}

function developerTrainingDrawableSnapshot() {
  const rawModel = typeof coreModel?.getModel === "function" ? coreModel.getModel() : null;
  const drawables = rawModel?.drawables;
  if (!drawables?.opacities) return new Map();
  const result = new Map();
  const count = drawables.opacities.length;
  for (let index = 0; index < count; index += 1) {
    const vertices = drawables.vertexPositions?.[index];
    let minimumX = Infinity;
    let maximumX = -Infinity;
    let minimumY = Infinity;
    let maximumY = -Infinity;
    if (vertices?.length) {
      for (let offset = 0; offset < vertices.length; offset += 2) {
        minimumX = Math.min(minimumX, vertices[offset]);
        maximumX = Math.max(maximumX, vertices[offset]);
        minimumY = Math.min(minimumY, vertices[offset + 1]);
        maximumY = Math.max(maximumY, vertices[offset + 1]);
      }
    }
    const hasBounds = Number.isFinite(minimumX) && Number.isFinite(minimumY);
    result.set(developerTrainingDrawableIdAt(index, drawables), {
      opacity: toNumber(drawables.opacities[index], 0),
      width: hasBounds ? maximumX - minimumX : 0,
      height: hasBounds ? maximumY - minimumY : 0,
      centerX: hasBounds ? (minimumX + maximumX) * 0.5 : 0,
      centerY: hasBounds ? (minimumY + maximumY) * 0.5 : 0,
    });
  }
  return result;
}

function developerTrainingRuntimeParameterValue(id) {
  const parameter = developerExpressionTrainingParameterById(id);
  if (!parameter || parameter.index < 0) return null;
  try {
    return toNumber(coreModel.getParameterValueByIndex(parameter.index), parameter.currentValue);
  } catch {
    return toNumber(parameter.currentValue, 0);
  }
}

function developerTrainingEffectiveSnapshot() {
  const parameterIds = [...new Set([
    ...developerExpressionTrainingParameterIds(),
    ...AUTHOR_FACE_LAYER_IDS,
    "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeBallX", "ParamEyeBallY",
    "ParamMouthForm", "ParamMouthOpenY", "ParamCheek",
  ])];
  return {
    parameters: Object.fromEntries(
      parameterIds.map(id => [id, developerTrainingRuntimeParameterValue(id)]),
    ),
    drawables: developerTrainingDrawableSnapshot(),
  };
}

function developerTrainingMetricRatio(current, baseline) {
  const denominator = Math.abs(toNumber(baseline, 0));
  if (denominator < 0.000001) return null;
  return toNumber(current, 0) / denominator;
}

function developerTrainingDrawableChangeScore(current, baseline) {
  if (!current || !baseline) return 0;
  const widthScale = Math.max(Math.abs(baseline.width), 0.01);
  const heightScale = Math.max(Math.abs(baseline.height), 0.01);
  return (
    Math.abs(current.opacity - baseline.opacity) * 2
    + Math.abs(current.width - baseline.width) / widthScale
    + Math.abs(current.height - baseline.height) / heightScale
    + Math.abs(current.centerX - baseline.centerX) / widthScale
    + Math.abs(current.centerY - baseline.centerY) / heightScale
  );
}

function renderDeveloperExpressionTrainingEffectiveOutputs(current, baseline, parameters) {
  if (!dom.devTrainingEffectiveOutputs) return;
  const items = [];
  const appendItem = (title, value, detail = "", changed = false) => {
    const item = document.createElement("section");
    item.className = `dev-training-effective-item${changed ? " changed" : ""}`;
    const heading = document.createElement("strong");
    heading.textContent = title;
    const output = document.createElement("code");
    output.textContent = value;
    item.append(heading, output);
    if (detail) {
      const note = document.createElement("span");
      note.textContent = detail;
      item.append(note);
    }
    items.push(item);
  };
  const activeLayers = AUTHOR_FACE_LAYER_IDS
    .filter(id => Math.abs(toNumber(parameters?.[id], 0)) > 0.0001)
    .map(id => `${id}=${formatLive2DParameterValue(parameters[id])}`);
  appendItem(
    "当前作者分类层",
    activeLayers.join(" + ") || "全部为 0",
    activeLayers.length
      ? activeLayers.map(entry => {
          const id = entry.split("=")[0];
          return `${id}：${EXPRESSION_TRAINING_LAYER_CHANNELS[id] || "作者耦合网格"}`;
        }).join("；")
      : "当前只显示公开参数和独立虚拟参数的效果。",
    activeLayers.length > 0,
  );

  const pupilRows = [
    ["左瞳孔 ArtMesh60", "ArtMesh60", toNumber(parameters?.VirtualPupilLScale, 1)],
    ["右瞳孔 ArtMesh98", "ArtMesh98", toNumber(parameters?.VirtualPupilRScale, 1)],
  ];
  pupilRows.forEach(([label, id, explicitScale]) => {
    const now = current.drawables.get(id);
    const base = baseline.drawables.get(id);
    if (!now || !base) return;
    const widthRatio = developerTrainingMetricRatio(now.width, base.width);
    const heightRatio = developerTrainingMetricRatio(now.height, base.height);
    const changed = developerTrainingDrawableChangeScore(now, base) > 0.002;
    appendItem(
      label,
      `最终：宽 ${now.width.toFixed(4)} / 高 ${now.height.toFixed(4)} / 透明度 ${now.opacity.toFixed(3)}`,
      `直接缩放=${explicitScale.toFixed(3)}；关闭情绪层时宽 ${base.width.toFixed(4)} / 高 ${base.height.toFixed(4)}；情绪层净影响：宽 ${widthRatio === null ? "—" : `${((widthRatio - 1) * 100).toFixed(1)}%`}，高 ${heightRatio === null ? "—" : `${((heightRatio - 1) * 100).toFixed(1)}%`}，中心 Δ(${(now.centerX - base.centerX).toFixed(4)}, ${(now.centerY - base.centerY).toFixed(4)})`,
      changed,
    );
  });

  ["lianhong", "lianhong2"].forEach(id => {
    const now = current.drawables.get(id);
    const base = baseline.drawables.get(id);
    if (!now || !base) return;
    appendItem(
      `脸红网格 ${id}`,
      `透明度 ${base.opacity.toFixed(3)} → ${now.opacity.toFixed(3)}`,
      `作者层脸红可见度=${toNumber(parameters?.VirtualAuthorBlushOpacity, 1).toFixed(3)}`,
      Math.abs(now.opacity - base.opacity) > 0.001,
    );
  });

  const parameterPairs = [
    ["眼睛开合", "ParamEyeLOpen", "ParamEyeROpen"],
    ["视线位置", "ParamEyeBallX", "ParamEyeBallY"],
    ["嘴部公开轴", "ParamMouthForm", "ParamMouthOpenY"],
  ];
  parameterPairs.forEach(([label, leftId, rightId]) => {
    const left = current.parameters[leftId];
    const right = current.parameters[rightId];
    const baselineLeft = baseline.parameters[leftId];
    const baselineRight = baseline.parameters[rightId];
    const changed = (
      left !== null && baselineLeft !== null && Math.abs(left - baselineLeft) > 0.0001
    ) || (
      right !== null && baselineRight !== null && Math.abs(right - baselineRight) > 0.0001
    );
    appendItem(
      label,
      `最终：${leftId}=${left === null ? "—" : formatLive2DParameterValue(left)} / ${rightId}=${right === null ? "—" : formatLive2DParameterValue(right)}`,
      `关闭情绪层：${leftId}=${baselineLeft === null ? "—" : formatLive2DParameterValue(baselineLeft)} / ${rightId}=${baselineRight === null ? "—" : formatLive2DParameterValue(baselineRight)}；层净变化 Δ(${left === null || baselineLeft === null ? "—" : formatLive2DParameterValue(left - baselineLeft)}, ${right === null || baselineRight === null ? "—" : formatLive2DParameterValue(right - baselineRight)})。若这里 Δ 为 0 但下方网格有变化，说明作者层绕过了公开轴直接改图。`,
      changed,
    );
  });

  const concreteParameterChanges = Object.entries(current.parameters)
    .filter(([id, value]) => (
      !AUTHOR_FACE_LAYER_IDS.includes(id)
      && !id.startsWith("Virtual")
      && value !== null
      && baseline.parameters[id] !== null
      && Math.abs(value - baseline.parameters[id]) > 0.0001
    ))
    .map(([id, value]) => ({
      id,
      before: baseline.parameters[id],
      after: value,
      delta: value - baseline.parameters[id],
    }))
    .sort((left, right) => Math.abs(right.delta) - Math.abs(left.delta));
  appendItem(
    "情绪层实际影响的参数值",
    concreteParameterChanges.length
      ? `${concreteParameterChanges.length} 项：${concreteParameterChanges.map(item => item.id).join("、")}`
      : "没有额外改写公开参数",
    concreteParameterChanges.length
      ? concreteParameterChanges.map(item => (
          `${item.id}: ${formatLive2DParameterValue(item.before)} → `
          + `${formatLive2DParameterValue(item.after)} `
          + `(层净变化 ${formatLive2DParameterValue(item.delta)})`
        )).join("；")
      : "作者分类层仍可能直接驱动下方网格；这种影响不会伪装成公开参数变化。",
    concreteParameterChanges.length > 0,
  );

  const drawableChanges = [...current.drawables.entries()]
    .map(([id, metric]) => ({
      id,
      current: metric,
      baseline: baseline.drawables.get(id),
      score: developerTrainingDrawableChangeScore(metric, baseline.drawables.get(id)),
    }))
    .filter(item => item.baseline && item.score > 0.002)
    .sort((left, right) => right.score - left.score)
    .slice(0, 12);
  appendItem(
    "情绪层实际影响的具体网格值",
    drawableChanges.length
      ? drawableChanges.map(item => item.id).join("、")
      : "未检测到明显网格差异",
    drawableChanges.length
      ? drawableChanges.map(item => (
          `${item.id}: 透明度 ${item.baseline.opacity.toFixed(2)}→${item.current.opacity.toFixed(2)} `
          + `(Δ${(item.current.opacity - item.baseline.opacity).toFixed(2)}), `
          + `宽 ${item.baseline.width.toFixed(3)}→${item.current.width.toFixed(3)} `
          + `(Δ${(item.current.width - item.baseline.width).toFixed(3)}), `
          + `高 ${item.baseline.height.toFixed(3)}→${item.current.height.toFixed(3)} `
          + `(Δ${(item.current.height - item.baseline.height).toFixed(3)}), `
          + `中心 Δ(${(item.current.centerX - item.baseline.centerX).toFixed(3)}, ${(item.current.centerY - item.baseline.centerY).toFixed(3)})`
        )).join("；")
      : "比较基准保持其他参数完全相同，只把 Param3–Param8 归零。",
    drawableChanges.length > 0,
  );
  dom.devTrainingEffectiveOutputs.replaceChildren(...items);
}

function refreshDeveloperExpressionTrainingEffectiveOutputs() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample || !coreModel || !dom.devTrainingEffectiveOutputs) return;
  const currentParameters = { ...(sample.parameters || {}) };
  const baselineParameters = { ...currentParameters };
  AUTHOR_FACE_LAYER_IDS.forEach(id => {
    baselineParameters[id] = 0;
  });
  applyDeveloperExpressionTrainingParameters(baselineParameters);
  const baseline = developerTrainingEffectiveSnapshot();
  applyDeveloperExpressionTrainingParameters(currentParameters);
  const current = developerTrainingEffectiveSnapshot();
  renderDeveloperExpressionTrainingEffectiveOutputs(current, baseline, currentParameters);
}

function markDeveloperExpressionTrainingDirty() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample) return;
  developerExpressionTrainingDirty = true;
}

function updateDeveloperExpressionTrainingIntensity() {
  const sample = developerExpressionTrainingSelectedSample();
  const value = clamp(toNumber(dom.devTrainingIntensityInput?.value, 1), 0, 2.5);
  if (dom.devTrainingIntensityValue) dom.devTrainingIntensityValue.textContent = value.toFixed(2);
  if (sample) {
    sample.intensity = value;
    developerExpressionTrainingDirty = true;
  }
}

function updateDeveloperExpressionTrainingSampleFromForm(sample) {
  sample.label = String(dom.devTrainingLabelInput?.value || "").trim();
  sample.emotion = String(dom.devTrainingEmotionInput?.value || "neutral");
  sample.strengthBand = String(dom.devTrainingStrengthInput?.value || "custom");
  sample.intensity = clamp(toNumber(dom.devTrainingIntensityInput?.value, 1), 0, 2.5);
  sample.tags = String(dom.devTrainingTagsInput?.value || "")
    .split(/[,，;\n]+/)
    .map(item => item.trim())
    .filter((item, index, items) => item && items.indexOf(item) === index)
    .slice(0, 32);
  sample.notes = String(dom.devTrainingNotesInput?.value || "").trim();
  sample.modelId = activeModelProfile?.id || sample.modelId || "";
  sample.couplingWarnings = developerExpressionTrainingCouplingWarnings(sample.parameters);
  sample.userEdited = developerExpressionTrainingHasChanges(sample);
  return sample;
}

async function saveDeveloperExpressionTrainingSample(options = {}) {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample) {
    toast("请先选择一张可编辑图");
    return;
  }
  updateDeveloperExpressionTrainingSampleFromForm(sample);
  if (!sample.label) {
    toast("请先填写你的表情标签");
    dom.devTrainingLabelInput?.focus();
    return;
  }
  const previousRevision = Math.max(0, Number(sample.revision) || 0);
  sample.revision = previousRevision + 1;
  if (dom.devTrainingSaveBtn) dom.devTrainingSaveBtn.disabled = true;
  if (dom.devTrainingRestoreBtn) dom.devTrainingRestoreBtn.disabled = true;
  if (dom.devTrainingCloseEditorBtn) dom.devTrainingCloseEditorBtn.disabled = true;
  try {
    applyDeveloperExpressionTrainingParameters(sample.parameters);
    await new Promise(resolve => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)));
    const canvas = developerCaptureCanvas();
    const response = await storageScopedFetch("/api/developer/expression-training", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "upsert",
        sample,
        imageDataUrl: canvas.toDataURL("image/png"),
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    replaceDeveloperExpressionTrainingDataset(data);
    developerExpressionTrainingSelectedId = data.sample?.id || sample.id;
    const saved = developerExpressionTrainingSamples.find(item => item.id === developerExpressionTrainingSelectedId)
      || data.sample
      || sample;
    openDeveloperExpressionTrainingDraft(saved, false);
    toast(options.reset
      ? `已重置为默认表情：${sample.recommendedLabel || sample.label}`
      : `表情监督样本已保存：${sample.label}`);
    return true;
  } catch (error) {
    sample.revision = previousRevision;
    toast(`表情监督样本保存失败：${error.message}`);
    developerExpressionTrainingDirty = true;
    return false;
  } finally {
    if (dom.devTrainingSaveBtn) dom.devTrainingSaveBtn.disabled = false;
    if (dom.devTrainingRestoreBtn) dom.devTrainingRestoreBtn.disabled = false;
    if (dom.devTrainingCloseEditorBtn) dom.devTrainingCloseEditorBtn.disabled = false;
  }
}

async function activateSelectedDeveloperExpressionTrainingSample() {
  let sample = developerExpressionTrainingSelectedSample();
  if (!sample) return;
  if (developerExpressionTrainingDirty || developerExpressionTrainingDraftIsNew) {
    const saved = await saveDeveloperExpressionTrainingSample();
    if (!saved) return;
    sample = persistedDeveloperExpressionTrainingSelectedSample();
  } else {
    sample = persistedDeveloperExpressionTrainingSelectedSample();
  }
  if (!sample) return;
  try {
    const response = await storageScopedFetch("/api/developer/expression-training", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "activate", id: sample.id }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    replaceDeveloperExpressionTrainingDataset(data);
    developerExpressionTrainingSelectedId = sample.id;
    const activated = persistedDeveloperExpressionTrainingSelectedSample() || sample;
    openDeveloperExpressionTrainingDraft(activated, false);
    renderDeveloperExpressionRuntimeStatus(activated);
    toast(`已接入真实运行时：${activated.emotion} → ${activated.label}`);
  } catch (error) {
    toast(`启用真实表情失败：${error.message}`);
  }
}

async function resetSelectedDeveloperExpressionRuntime() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample) return;
  try {
    const response = await storageScopedFetch("/api/developer/expression-training", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "reset_runtime",
        modelId: sample.modelId || activeModelProfile?.id || "",
        emotion: sample.emotion,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    replaceDeveloperExpressionTrainingDataset(data);
    developerExpressionTrainingSelectedId = sample.id;
    const persisted = persistedDeveloperExpressionTrainingSelectedSample() || sample;
    openDeveloperExpressionTrainingDraft(persisted, false);
    renderDeveloperExpressionRuntimeStatus(persisted);
    toast(`已恢复 ${sample.emotion} 的内置运行时表情`);
  } catch (error) {
    toast(`恢复内置表情失败：${error.message}`);
  }
}

async function restoreDeveloperExpressionTrainingRecommendation() {
  const sample = developerExpressionTrainingSelectedSample();
  if (!sample) return;
  const baseline = developerExpressionTrainingBaseline(sample);
  sample.label = baseline.label;
  sample.emotion = baseline.emotion;
  sample.strengthBand = baseline.strengthBand;
  sample.intensity = baseline.intensity;
  sample.tags = [...baseline.tags];
  sample.notes = baseline.notes;
  sample.parameters = { ...baseline.parameters };
  sample.userEdited = false;
  developerExpressionTrainingDirty = true;
  if (dom.devTrainingLabelInput) dom.devTrainingLabelInput.value = sample.label;
  if (dom.devTrainingEmotionInput) dom.devTrainingEmotionInput.value = sample.emotion;
  if (dom.devTrainingStrengthInput) dom.devTrainingStrengthInput.value = sample.strengthBand;
  if (dom.devTrainingIntensityInput) dom.devTrainingIntensityInput.value = String(sample.intensity);
  if (dom.devTrainingIntensityValue) dom.devTrainingIntensityValue.textContent = sample.intensity.toFixed(2);
  if (dom.devTrainingTagsInput) dom.devTrainingTagsInput.value = sample.tags.join(", ");
  if (dom.devTrainingNotesInput) dom.devTrainingNotesInput.value = sample.notes;
  applyDeveloperExpressionTrainingParameters(sample.parameters);
  renderDeveloperExpressionTrainingParameterControls();
  renderDeveloperExpressionTrainingCouplingWarning();
  refreshDeveloperExpressionTrainingEffectiveOutputs();
  await saveDeveloperExpressionTrainingSample({ reset: true });
}

function newDeveloperExpressionTrainingSample() {
  if (!coreModel) {
    toast("Live2D 模型尚未加载");
    return;
  }
  developerParameterMetadata = readLive2DParameters();
  const parameters = Object.fromEntries(
    developerExpressionTrainingParameterIds().map(id => {
      const parameter = developerExpressionTrainingParameterById(id);
      return [id, AUTHOR_FACE_LAYER_IDS.includes(id) ? 0 : parameter.defaultValue];
    }),
  );
  const sample = {
    id: developerExpressionTrainingId("user"),
    label: "新表情",
    recommendedLabel: "新表情",
    emotion: "neutral",
    strengthBand: "custom",
    intensity: 1,
    tags: ["用户新建"],
    notes: "",
    modelId: activeModelProfile?.id || "",
    source: "user-new",
    recommendedParameters: { ...parameters },
    recommendedMetadata: {
      emotion: "neutral",
      strengthBand: "custom",
      intensity: 1,
      tags: ["用户新建"],
      notes: "",
    },
    parameters: { ...parameters },
    parameterSupport: Object.fromEntries(
      Object.keys(parameters).map(id => [id, developerExpressionTrainingParameterSupport(id)]),
    ),
    couplingWarnings: developerExpressionTrainingCouplingWarnings(parameters),
    userEdited: false,
    revision: 0,
  };
  developerExpressionTrainingPreviousSelectedId = developerExpressionTrainingSelectedId;
  openDeveloperExpressionTrainingDraft(sample, true);
  dom.devTrainingLabelInput?.select();
}

function duplicateDeveloperExpressionTrainingSample() {
  const source = persistedDeveloperExpressionTrainingSelectedSample();
  if (!source) return;
  const parameters = { ...(source.parameters || {}) };
  const sample = {
    ...source,
    id: developerExpressionTrainingId("copy"),
    label: `${source.label} 副本`,
    recommendedLabel: `${source.label} 副本`,
    source: "user-copy",
    recommendedParameters: { ...parameters },
    recommendedMetadata: {
      emotion: source.emotion,
      strengthBand: source.strengthBand,
      intensity: source.intensity,
      tags: [...(source.tags || [])],
      notes: source.notes || "",
    },
    parameters,
    parameterSupport: { ...(source.parameterSupport || {}) },
    couplingWarnings: [...(source.couplingWarnings || [])],
    previewFile: source.previewFile || "",
    previewSourceId: source.previewSourceId || source.id,
    userEdited: false,
    revision: 0,
    createdAt: "",
    updatedAt: "",
  };
  developerExpressionTrainingPreviousSelectedId = developerExpressionTrainingSelectedId;
  openDeveloperExpressionTrainingDraft(sample, true);
  dom.devTrainingLabelInput?.select();
}

const VOICE_REFERENCE_REVIEW_LABELS = {
  unreviewed: "未审阅",
  approved: "合格",
  rejected: "不合格",
};

const VOICE_REFERENCE_MAPPING_FIT_LABELS = {
  unreviewed: "未判断",
  matched: "匹配",
  mismatched: "不匹配（原音可用）",
};

const VOICE_REFERENCE_RECHECK_LABELS = {
  pending_recheck: "待复检",
  recheck_passed: "复检通过",
  recheck_failed: "复检仍不合格",
};

const VOICE_REFERENCE_USAGE_LABELS = {
  active: "启用中",
  inactive: "未启用",
};

const VOICE_REFERENCE_SYSTEM_LABELS = {
  eligible: "可合成",
  high_risk: "高风险",
  disqualified: "禁用",
  missing: "文件缺失",
};

const VOICE_REFERENCE_CANDIDATE_ROLE_LABELS = {
  audition: "试听候选",
  replacement: "替换候选",
};

function developerVoiceReferenceDisplayStatus(item) {
  const manualStatus = item?.manualReviewStatus || item?.status || "unreviewed";
  return VOICE_REFERENCE_REVIEW_LABELS[manualStatus] || "未审阅";
}

function setDeveloperTrainingKind(kind) {
  if (!DEV_MODE) return;
  developerTrainingKind = kind === "voice-reference" ? "voice-reference" : "expression";
  const voiceActive = developerTrainingKind === "voice-reference";
  dom.devTrainingExpressionTab?.classList.toggle("active", !voiceActive);
  dom.devTrainingExpressionTab?.setAttribute("aria-selected", String(!voiceActive));
  dom.devTrainingVoiceReferenceTab?.classList.toggle("active", voiceActive);
  dom.devTrainingVoiceReferenceTab?.setAttribute("aria-selected", String(voiceActive));
  dom.devExpressionTrainingBrowserContent?.classList.toggle("hidden", voiceActive);
  dom.devVoiceReferenceBrowserContent?.classList.toggle("hidden", !voiceActive);
  if (dom.devExpressionTrainingBrowserContent) {
    dom.devExpressionTrainingBrowserContent.hidden = voiceActive;
    dom.devExpressionTrainingBrowserContent.setAttribute("aria-hidden", String(voiceActive));
  }
  if (dom.devVoiceReferenceBrowserContent) {
    dom.devVoiceReferenceBrowserContent.hidden = !voiceActive;
    dom.devVoiceReferenceBrowserContent.setAttribute("aria-hidden", String(!voiceActive));
  }
  // Re-read the backend whenever this workbench is opened so a style-map edit
  // cannot leave the visual catalog showing a previously cached "current".
  if (voiceActive) loadDeveloperVoiceReferences(true);
  else renderDeveloperExpressionTrainingGallery();
}

function replaceDeveloperVoiceReferenceCatalog(data) {
  developerVoiceReferenceCatalog = data && typeof data === "object" ? { ...data } : null;
  developerVoiceReferences = Array.isArray(data?.references) ? data.references.map(item => ({
    ...item,
    mappingKeys: Array.isArray(item.mappingKeys) ? [...item.mappingKeys] : [],
    referenceTexts: Array.isArray(item.referenceTexts) ? [...item.referenceTexts] : [],
    reasonCodes: Array.isArray(item.reasonCodes) ? [...item.reasonCodes] : [],
  })) : [];
  developerVoiceReferenceLoaded = true;
  if (
    developerVoiceReferenceSelectedId
    && !developerVoiceReferences.some(item => item.id === developerVoiceReferenceSelectedId)
  ) developerVoiceReferenceSelectedId = "";
  renderDeveloperVoiceReferenceGallery();
}

async function loadDeveloperVoiceReferences(force = false) {
  if (!DEV_MODE || (developerVoiceReferenceLoaded && !force)) {
    renderDeveloperVoiceReferenceGallery();
    return;
  }
  if (dom.devVoiceReferenceStatus) dom.devVoiceReferenceStatus.textContent = "载入参考音…";
  if (dom.devVoiceReferenceRefreshBtn) dom.devVoiceReferenceRefreshBtn.disabled = true;
  try {
    const response = await storageScopedFetch("/api/developer/voice-reference-review", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    replaceDeveloperVoiceReferenceCatalog(data);
  } catch (error) {
    if (dom.devVoiceReferenceStatus) dom.devVoiceReferenceStatus.textContent = `载入失败：${error.message}`;
    toast(`参考音载入失败：${error.message}`);
  } finally {
    if (dom.devVoiceReferenceRefreshBtn) dom.devVoiceReferenceRefreshBtn.disabled = false;
  }
}

function developerVoiceReferenceDisplayKeys(item) {
  return (item.mappingKeys || []).filter(key => key !== "default");
}

function renderDeveloperVoiceReferenceGallery() {
  if (!DEV_MODE || !dom.devVoiceReferenceGallery) return;
  const query = String(dom.devVoiceReferenceFilterInput?.value || "").trim().toLowerCase();
  const statusFilter = String(dom.devVoiceReferenceStatusFilter?.value || "");
  const runtimeFilter = String(dom.devVoiceReferenceRuntimeFilter?.value || "");
  const systemFilter = String(dom.devVoiceReferenceSystemFilter?.value || "");
  const filtered = developerVoiceReferences.filter(item => {
    const manualStatus = item.manualReviewStatus || item.status || "unreviewed";
    if (statusFilter && manualStatus !== statusFilter) return false;
    if (runtimeFilter === "active" && item.usageStatus !== "active") return false;
    if (runtimeFilter === "inactive" && item.usageStatus !== "inactive") return false;
    if (runtimeFilter === "audition" && item.candidateRole !== "audition") return false;
    if (runtimeFilter === "replacement" && item.candidateRole !== "replacement") return false;
    if (systemFilter && item.systemStatus !== systemFilter) return false;
    if (!query) return true;
    const haystack = [
      item.voiceId,
      item.audioFile,
      ...(item.mappingKeys || []),
      ...(item.activeMappingKeys || []),
      ...(item.activeElsewhereMappingKeys || []),
      ...(item.referenceTexts || []),
      item.notes,
      item.emotionLabel,
      item.mappingFitStatus,
      item.attentionLabel,
      item.riskNote,
      item.recommendation,
      item.usageStatus,
      item.systemStatus,
      item.systemReason,
      item.candidateRole,
    ].join("\n").toLowerCase();
    return haystack.includes(query);
  });
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "dev-training-empty";
    empty.textContent = developerVoiceReferenceLoaded ? "没有符合筛选条件的参考音。" : "尚未载入参考音。";
    dom.devVoiceReferenceGallery.replaceChildren(empty);
  } else {
    dom.devVoiceReferenceGallery.replaceChildren(...filtered.map(item => {
      const card = document.createElement("button");
      card.type = "button";
      card.dataset.referenceId = item.id;
      const manualStatus = item.manualReviewStatus || item.status || "unreviewed";
      const usageStatus = item.usageStatus || (item.isCurrentlyEffective ? "active" : "inactive");
      const systemStatus = item.systemStatus || "eligible";
      card.className = "dev-training-card dev-voice-reference-card";
      card.classList.add(`usage-${usageStatus}`, `system-${systemStatus}`, `manual-${manualStatus}`);
      if (item.attentionLevel) card.classList.add(`attention-${item.attentionLevel}`);
      if (item.candidateRole && item.candidateRole !== "none") card.classList.add(`role-${item.candidateRole}`);
      card.classList.toggle("selected", item.id === developerVoiceReferenceSelectedId);

      const icon = document.createElement("div");
      icon.className = "dev-voice-reference-icon";
      icon.textContent = "♫";
      const badge = document.createElement("span");
      badge.className = `dev-voice-reference-status-badge ${manualStatus}`;
      badge.textContent = `原音质量：${developerVoiceReferenceDisplayStatus(item)}`;
      icon.append(badge);

      const copy = document.createElement("div");
      copy.className = "dev-training-card-copy";
      const stateRow = document.createElement("div");
      stateRow.className = "dev-voice-reference-state-row";
      const usageBadge = document.createElement("span");
      usageBadge.className = `dev-voice-reference-active-badge ${usageStatus}`;
      usageBadge.textContent = `映射：${VOICE_REFERENCE_USAGE_LABELS[usageStatus] || usageStatus}`;
      const systemBadge = document.createElement("span");
      systemBadge.className = `dev-voice-reference-system-badge ${systemStatus}`;
      systemBadge.textContent = `合成风险：${VOICE_REFERENCE_SYSTEM_LABELS[systemStatus] || systemStatus}`;
      stateRow.append(usageBadge, systemBadge);
      const mappingFitStatus = item.mappingFitStatus || "unreviewed";
      const fitBadge = document.createElement("span");
      fitBadge.className = `dev-voice-reference-fit-badge ${mappingFitStatus}`;
      fitBadge.textContent = `标签匹配：${VOICE_REFERENCE_MAPPING_FIT_LABELS[mappingFitStatus] || mappingFitStatus}`;
      stateRow.append(fitBadge);
      if (item.emotionLabel) {
        const emotionBadge = document.createElement("span");
        emotionBadge.className = "dev-voice-reference-emotion-badge";
        emotionBadge.textContent = `情绪：${item.emotionLabel}`;
        stateRow.append(emotionBadge);
      }
      if (item.candidateRole && item.candidateRole !== "none") {
        const roleBadge = document.createElement("span");
        roleBadge.className = `dev-voice-reference-role-badge ${item.candidateRole}`;
        roleBadge.textContent = VOICE_REFERENCE_CANDIDATE_ROLE_LABELS[item.candidateRole] || item.candidateRole;
        stateRow.append(roleBadge);
      }
      const activeElsewhereMappingKeys = Array.isArray(item.activeElsewhereMappingKeys)
        ? item.activeElsewhereMappingKeys.filter(Boolean)
        : [];
      if (activeElsewhereMappingKeys.length) {
        const elsewhereBadge = document.createElement("span");
        elsewhereBadge.className = "dev-voice-reference-active-badge active";
        elsewhereBadge.textContent = `其他用途启用：${activeElsewhereMappingKeys.join(" / ")}`;
        stateRow.append(elsewhereBadge);
      }
      if (item.reviewWorkflowStatus && item.reviewWorkflowStatus !== "none") {
        const workflowBadge = document.createElement("span");
        workflowBadge.className = `dev-voice-reference-workflow-badge ${item.reviewWorkflowStatus}`;
        workflowBadge.textContent = `复检：${VOICE_REFERENCE_RECHECK_LABELS[item.reviewWorkflowStatus] || item.reviewWorkflowStatus}`;
        stateRow.append(workflowBadge);
      }
      copy.append(stateRow);
      if (item.attentionLabel) {
        const attention = document.createElement("span");
        attention.className = `dev-voice-reference-attention ${item.attentionLevel || "candidate"}`;
        attention.textContent = item.attentionLabel;
        copy.append(attention);
      }
      const title = document.createElement("strong");
      title.textContent = developerVoiceReferenceDisplayKeys(item).join(" / ") || item.voiceId;
      const meta = document.createElement("span");
      const duration = Number.isFinite(Number(item.durationSeconds)) ? `${Number(item.durationSeconds).toFixed(2)} 秒` : "时长未知";
      meta.textContent = `${item.audioFile || item.voiceId} · ${duration}`;
      const transcript = document.createElement("span");
      transcript.textContent = (item.referenceTexts || []).join(" / ") || "未找到参考文本";
      copy.append(title, meta, transcript);
      card.append(icon, copy);
      return card;
    }));
  }
  const approved = developerVoiceReferences.filter(item => (item.manualReviewStatus || item.status) === "approved").length;
  const rejected = developerVoiceReferences.filter(item => (item.manualReviewStatus || item.status) === "rejected").length;
  const unreviewed = developerVoiceReferences.length - approved - rejected;
  const pendingRecheck = developerVoiceReferences.filter(item => item.reviewWorkflowStatus === "pending_recheck").length;
  const highRisk = developerVoiceReferences.filter(item => item.systemStatus === "high_risk");
  const systemDisqualified = developerVoiceReferences.filter(item => ["disqualified", "missing"].includes(item.systemStatus));
  const auditionCandidates = developerVoiceReferences.filter(item => item.candidateRole === "audition");
  const selectedChoices = auditionCandidates.filter(item => item.isSelectedChoice);
  const effectiveReferences = developerVoiceReferences.filter(item => item.usageStatus === "active");
  const inactiveReferences = developerVoiceReferences.filter(item => item.usageStatus === "inactive");
  const eligibleReferences = developerVoiceReferences.filter(item => item.systemStatus === "eligible");
  const activeDisqualified = systemDisqualified.filter(item => item.usageStatus === "active");
  const linkageActive = developerVoiceReferenceCatalog?.linkage?.source === "effective_style_map";
  if (dom.devVoiceReferenceAttentionBanner) {
    const hasSystemWarning = Boolean(systemDisqualified.length || highRisk.length);
    dom.devVoiceReferenceAttentionBanner.hidden = !hasSystemWarning && !linkageActive;
    dom.devVoiceReferenceAttentionBanner.classList.toggle("hidden", !hasSystemWarning && !linkageActive);
    dom.devVoiceReferenceAttentionBanner.classList.toggle("synced", !hasSystemWarning && linkageActive);
    dom.devVoiceReferenceAttentionBanner.textContent = systemDisqualified.length
      ? `⚠ 合成风险中有 ${systemDisqualified.length} 条禁用或文件缺失参考${activeDisqualified.length ? `，其中 ${activeDisqualified.length} 条仍在启用` : ""}。这是系统判定，不等于“我的标注”。`
      : (highRisk.length
        ? `⚠ 系统检测到 ${highRisk.length} 条高风险参考。已提供 ${auditionCandidates.length} 个试听候选；${selectedChoices.length ? `已记录 ${selectedChoices.length} 个待晋升选择` : "尚未记录待晋升选择"}。`
        : (linkageActive ? `✓ 已与实际生效的 style map 联动；映射、合成风险、原音质量和标签匹配会分别显示。` : ""));
  }
  if (dom.devVoiceReferenceStatus) {
    dom.devVoiceReferenceStatus.textContent = `${developerVoiceReferences.length} 条 · 映射：启用 ${effectiveReferences.length} / 未启用 ${inactiveReferences.length} · 合成风险：可合成 ${eligibleReferences.length} / 高风险 ${highRisk.length} / 禁用或缺失 ${systemDisqualified.length} · 原音质量：合格 ${approved} / 不合格 ${rejected} / 未审阅 ${unreviewed} · 待复检 ${pendingRecheck}（各维度互不覆盖）`;
  }
}

function handleDeveloperVoiceReferenceGalleryClick(event) {
  const card = event.target.closest("[data-reference-id]");
  if (!card) return;
  openDeveloperVoiceReferenceEditor(card.dataset.referenceId);
}

function syncDeveloperVoiceReferenceReasonAvailability() {
  dom.devVoiceReferenceReasons?.querySelectorAll('input[type="checkbox"]').forEach(input => {
    input.disabled = false;
  });
}

function markDeveloperVoiceReferenceDirty() {
  developerVoiceReferenceDirty = true;
  syncDeveloperVoiceReferenceReasonAvailability();
}

function handleDeveloperVoiceReferenceReasonChange() {
  const hasQualityReason = Boolean(
    dom.devVoiceReferenceReasons?.querySelector('input[data-review-axis="quality"]:checked'),
  );
  const hasMappingReason = Boolean(
    dom.devVoiceReferenceReasons?.querySelector('input[data-review-axis="mapping"]:checked'),
  );
  if (hasQualityReason && dom.devVoiceReferenceReviewStatusInput) {
    dom.devVoiceReferenceReviewStatusInput.value = "rejected";
  }
  if (hasMappingReason && dom.devVoiceReferenceMappingFitInput) {
    dom.devVoiceReferenceMappingFitInput.value = "mismatched";
  }
  markDeveloperVoiceReferenceDirty();
}

function handleDeveloperVoiceReferenceStatusChange() {
  if (dom.devVoiceReferenceReviewStatusInput?.value !== "rejected") {
    dom.devVoiceReferenceReasons?.querySelectorAll('input[data-review-axis="quality"]').forEach(input => {
      input.checked = false;
    });
  }
  markDeveloperVoiceReferenceDirty();
}

function handleDeveloperVoiceReferenceMappingFitChange() {
  if (dom.devVoiceReferenceMappingFitInput?.value !== "mismatched") {
    dom.devVoiceReferenceReasons?.querySelectorAll('input[data-review-axis="mapping"]').forEach(input => {
      input.checked = false;
    });
  }
  markDeveloperVoiceReferenceDirty();
}

async function loadDeveloperVoiceReferenceAudio(referenceId, variant = "candidate") {
  const originalVariant = variant === "original";
  const tokenKey = originalVariant ? "original" : "candidate";
  const loadToken = ++developerVoiceReferenceAudioLoadTokens[tokenKey];
  const audioElement = originalVariant ? dom.devVoiceReferenceOriginalAudio : dom.devVoiceReferenceAudio;
  const previousUrl = originalVariant ? developerVoiceReferenceOriginalAudioUrl : developerVoiceReferenceAudioUrl;
  if (previousUrl) URL.revokeObjectURL(previousUrl);
  if (originalVariant) developerVoiceReferenceOriginalAudioUrl = "";
  else developerVoiceReferenceAudioUrl = "";
  audioElement?.removeAttribute("src");
  try {
    const response = await storageScopedFetch(`/api/developer/voice-reference-review/audio?id=${encodeURIComponent(referenceId)}&variant=${encodeURIComponent(variant)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const objectUrl = URL.createObjectURL(await response.blob());
    if (loadToken !== developerVoiceReferenceAudioLoadTokens[tokenKey] || developerVoiceReferenceSelectedId !== referenceId) {
      URL.revokeObjectURL(objectUrl);
      return;
    }
    if (originalVariant) developerVoiceReferenceOriginalAudioUrl = objectUrl;
    else developerVoiceReferenceAudioUrl = objectUrl;
    if (audioElement) {
      audioElement.src = objectUrl;
      audioElement.load();
    }
  } catch (error) {
    toast(`参考音读取失败：${error.message}`);
  }
}

function openDeveloperVoiceReferenceEditor(referenceId) {
  const item = developerVoiceReferences.find(entry => entry.id === referenceId);
  if (!item) return;
  developerVoiceReferenceSelectedId = item.id;
  developerVoiceReferenceDraft = {
    id: item.id,
    status: item.manualReviewStatus || item.status || "unreviewed",
    mappingFitStatus: item.mappingFitStatus || "unreviewed",
    emotionLabel: item.emotionLabel || "",
    notes: item.notes || "",
    reasonCodes: [...(item.reasonCodes || [])],
  };
  developerVoiceReferenceDirty = false;
  developerExpressionTrainingEditorOpen = false;
  dom.devTrainingBrowser?.classList.add("hidden");
  dom.devTrainingEditor?.classList.add("hidden");
  dom.devVoiceReferenceEditor?.classList.remove("hidden");
  const activeElsewhereMappingKeys = Array.isArray(item.activeElsewhereMappingKeys)
    ? item.activeElsewhereMappingKeys.filter(Boolean)
    : [];
  if (dom.devVoiceReferenceEditorHint) {
    dom.devVoiceReferenceEditorHint.textContent = `${item.audioFile || item.voiceId} · ${Number(item.durationSeconds || 0).toFixed(2)} 秒 · 原音质量：${developerVoiceReferenceDisplayStatus(item)}`;
  }
  if (dom.devVoiceReferenceSummary) {
    const warning = item.attentionLabel ? ` ${item.attentionLabel}。` : "";
    const usageStatus = item.usageStatus || (item.isCurrentlyEffective ? "active" : "inactive");
    const systemStatus = item.systemStatus || "eligible";
    const roleLabel = item.runtimeBehavior === "historical_review"
      ? "历史审阅记录"
      : (VOICE_REFERENCE_CANDIDATE_ROLE_LABELS[item.candidateRole] || "正式参考");
    const workflowLabel = item.reviewWorkflowStatus && item.reviewWorkflowStatus !== "none"
      ? `；复检流程：${VOICE_REFERENCE_RECHECK_LABELS[item.reviewWorkflowStatus] || item.reviewWorkflowStatus}`
      : "";
    const fitStatus = item.mappingFitStatus || "unreviewed";
    const emotionLabel = item.emotionLabel ? `；你标的情绪：${item.emotionLabel}` : "";
    const activeElsewhere = activeElsewhereMappingKeys.length
      ? `；其他已启用用途：${activeElsewhereMappingKeys.join("、")}`
      : "";
    dom.devVoiceReferenceSummary.textContent = `参考音 ${item.voiceId}；当前用途映射状态：${VOICE_REFERENCE_USAGE_LABELS[usageStatus] || usageStatus}${activeElsewhere}；合成风险：${VOICE_REFERENCE_SYSTEM_LABELS[systemStatus] || systemStatus}；用途：${roleLabel}；原音质量：${developerVoiceReferenceDisplayStatus(item)}；标签匹配：${VOICE_REFERENCE_MAPPING_FIT_LABELS[fitStatus] || fitStatus}${emotionLabel}${workflowLabel}；当前卡片对应 ${developerVoiceReferenceDisplayKeys(item).join("、") || "默认声音"}。${warning}`;
  }
  if (dom.devVoiceReferenceMappings) dom.devVoiceReferenceMappings.value = (item.mappingKeys || []).join("\n");
  if (dom.devVoiceReferenceTranscript) dom.devVoiceReferenceTranscript.value = (item.referenceTexts || []).join("\n\n");
  const replacement = item.replacement || null;
  const comparisonCandidate = Boolean(item.isReplacementCandidate || item.isAuditionCandidate || item.isComparisonCandidate);
  if (dom.devVoiceReferenceOriginalPlayer) dom.devVoiceReferenceOriginalPlayer.hidden = !comparisonCandidate;
  if (dom.devVoiceReferenceCandidateLabel) {
    dom.devVoiceReferenceCandidateLabel.textContent = item.candidateRole === "audition"
      ? (item.usageStatus === "active"
        ? "试听候选（当前用途同时启用中）"
        : (activeElsewhereMappingKeys.length ? "试听候选（当前用途未启用；其他用途已启用）" : "试听候选（未启用）"))
      : (item.candidateRole === "replacement" ? "替换候选" : "当前参考音");
  }
  if (dom.devVoiceReferenceChangeInfo) {
    const originalFile = String(replacement?.original?.refAudio || "").split(/[\\/]/).pop() || "";
    dom.devVoiceReferenceChangeInfo.textContent = comparisonCandidate
      ? `原音 ${originalFile} → 候选 ${item.audioFile}\n${item.candidateRole === "audition" ? "试听依据" : "替换依据"}：${replacement?.reason || "根据人工标注重新筛选"}\n候选来源：${replacement?.source || "本地原作语音库"}${item.systemReason ? `\n系统状态依据：${item.systemReason}` : ""}${item.riskNote ? `\n风险说明：${item.riskNote}` : ""}`
      : (`${item.systemReason ? `系统状态依据：${item.systemReason}` : "当前参考音尚未被替换。"}${item.riskNote ? `\n风险说明：${item.riskNote}` : ""}`);
  }
  if (dom.devVoiceReferenceReviewStatusInput) dom.devVoiceReferenceReviewStatusInput.value = developerVoiceReferenceDraft.status;
  if (dom.devVoiceReferenceMappingFitInput) dom.devVoiceReferenceMappingFitInput.value = developerVoiceReferenceDraft.mappingFitStatus;
  if (dom.devVoiceReferenceEmotionLabelInput) dom.devVoiceReferenceEmotionLabelInput.value = developerVoiceReferenceDraft.emotionLabel;
  if (dom.devVoiceReferenceNotesInput) dom.devVoiceReferenceNotesInput.value = developerVoiceReferenceDraft.notes;
  dom.devVoiceReferenceReasons?.querySelectorAll('input[type="checkbox"]').forEach(input => {
    input.checked = developerVoiceReferenceDraft.reasonCodes.includes(input.value);
  });
  syncDeveloperVoiceReferenceReasonAvailability();
  if (dom.devVoiceReferenceSelectCandidateBtn) {
    const auditionCandidate = item.candidateRole === "audition" || item.isAuditionCandidate;
    dom.devVoiceReferenceSelectCandidateBtn.hidden = !auditionCandidate;
    dom.devVoiceReferenceSelectCandidateBtn.classList.toggle("hidden", !auditionCandidate);
    dom.devVoiceReferenceSelectCandidateBtn.disabled = Boolean(item.isSelectedChoice || item.selectionAllowed === false);
    dom.devVoiceReferenceSelectCandidateBtn.textContent = item.selectionAllowed === false
      ? "合成风险判定为禁用或文件缺失，不能选为待晋升候选"
      : (item.isSelectedChoice ? "✓ 已选为待晋升候选" : "选择为待晋升候选（不会立即应用）");
  }
  if (comparisonCandidate) {
    loadDeveloperVoiceReferenceAudio(item.id, "original");
    window.setTimeout(() => loadDeveloperVoiceReferenceAudio(item.id, "candidate"), 0);
  } else {
    loadDeveloperVoiceReferenceAudio(item.id, "candidate");
  }
  window.requestAnimationFrame(() => dom.devVoiceReferenceEditor?.scrollIntoView({ block: "start", inline: "nearest" }));
}

function closeDeveloperVoiceReferenceEditor() {
  if (developerVoiceReferenceDirty && !window.confirm("当前参考音标注尚未保存，确定返回吗？")) return;
  if (dom.devVoiceReferenceAudio) dom.devVoiceReferenceAudio.pause();
  if (dom.devVoiceReferenceOriginalAudio) dom.devVoiceReferenceOriginalAudio.pause();
  if (developerVoiceReferenceAudioUrl) URL.revokeObjectURL(developerVoiceReferenceAudioUrl);
  developerVoiceReferenceAudioUrl = "";
  if (developerVoiceReferenceOriginalAudioUrl) URL.revokeObjectURL(developerVoiceReferenceOriginalAudioUrl);
  developerVoiceReferenceOriginalAudioUrl = "";
  developerVoiceReferenceAudioLoadTokens.candidate += 1;
  developerVoiceReferenceAudioLoadTokens.original += 1;
  developerVoiceReferenceDraft = null;
  developerVoiceReferenceDirty = false;
  dom.devVoiceReferenceEditor?.classList.add("hidden");
  dom.devTrainingBrowser?.classList.remove("hidden");
  setDeveloperTrainingKind("voice-reference");
}

async function persistDeveloperVoiceReferenceReview(payload) {
  if (dom.devVoiceReferenceSaveBtn) dom.devVoiceReferenceSaveBtn.disabled = true;
  if (dom.devVoiceReferenceResetBtn) dom.devVoiceReferenceResetBtn.disabled = true;
  try {
    const response = await storageScopedFetch("/api/developer/voice-reference-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    replaceDeveloperVoiceReferenceCatalog(data);
    developerVoiceReferenceDirty = false;
    closeDeveloperVoiceReferenceEditor();
    toast(payload.action === "reset" ? "已重置为未审阅" : "参考音标注已保存");
  } catch (error) {
    toast(`参考音标注保存失败：${error.message}`);
  } finally {
    if (dom.devVoiceReferenceSaveBtn) dom.devVoiceReferenceSaveBtn.disabled = false;
    if (dom.devVoiceReferenceResetBtn) dom.devVoiceReferenceResetBtn.disabled = false;
  }
}

async function selectDeveloperVoiceReferenceCandidate() {
  const item = developerVoiceReferences.find(entry => entry.id === developerVoiceReferenceDraft?.id);
  if (!item?.isAuditionCandidate || item.selectionAllowed === false || !item.choiceSetId || !item.choiceCandidateId) return;
  if (dom.devVoiceReferenceSelectCandidateBtn) dom.devVoiceReferenceSelectCandidateBtn.disabled = true;
  try {
    const response = await storageScopedFetch("/api/developer/voice-reference-review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "select_candidate",
        choiceSetId: item.choiceSetId,
        candidateId: item.choiceCandidateId,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    replaceDeveloperVoiceReferenceCatalog(data);
    openDeveloperVoiceReferenceEditor(item.id);
    toast(`已记录 ${item.voiceId} 为待晋升候选；运行时参考音未改变`);
  } catch (error) {
    toast(`候选选择保存失败：${error.message}`);
    if (dom.devVoiceReferenceSelectCandidateBtn) dom.devVoiceReferenceSelectCandidateBtn.disabled = false;
  }
}

async function saveDeveloperVoiceReferenceReview() {
  if (!developerVoiceReferenceDraft) return;
  const selectedReasons = [
    ...(dom.devVoiceReferenceReasons?.querySelectorAll('input[type="checkbox"]:checked') || []),
  ].map(input => input.value);
  const qualityReasons = selectedReasons.filter(code => !["emotion_weak", "emotion_mismatch"].includes(code));
  const mappingReasons = selectedReasons.filter(code => ["emotion_weak", "emotion_mismatch"].includes(code));
  const status = qualityReasons.length
    ? "rejected"
    : String(dom.devVoiceReferenceReviewStatusInput?.value || "unreviewed");
  const mappingFitStatus = mappingReasons.length
    ? "mismatched"
    : String(dom.devVoiceReferenceMappingFitInput?.value || "unreviewed");
  await persistDeveloperVoiceReferenceReview({
    action: "upsert",
    id: developerVoiceReferenceDraft.id,
    status,
    qualityStatus: status,
    mappingFitStatus,
    emotionLabel: String(dom.devVoiceReferenceEmotionLabelInput?.value || "").trim(),
    reasonCodes: selectedReasons,
    notes: String(dom.devVoiceReferenceNotesInput?.value || "").trim(),
  });
}

async function resetDeveloperVoiceReferenceReview() {
  if (!developerVoiceReferenceDraft) return;
  await persistDeveloperVoiceReferenceReview({ action: "reset", id: developerVoiceReferenceDraft.id });
}

function loadDeveloperTrainingData() {
  loadDeveloperVoiceReferences();
  loadDeveloperExpressionTraining();
}

function renderDeveloperModelEditor() {
  if (!DEV_MODE || !dom.devModelProfileInput) return;
  const previous = dom.devModelProfileInput.value;
  const selectedId = live2dModelProfiles.some(item => item.id === previous)
    ? previous
    : MODEL_PREVIEW_ID || activeModelProfile.id;
  dom.devModelProfileInput.replaceChildren(...live2dModelProfiles.map(profile => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.label || profile.id;
    return option;
  }));
  dom.devModelProfileInput.value = selectedId;
  const profile = live2dModelProfiles.find(item => item.id === selectedId) || activeModelProfile;
  const override = live2dModelOverrides[selectedId] || {};
  dom.devModelScaleInput.value = toNumber(profile.scaleMultiplier, 1);
  dom.devModelOffsetXInput.value = toNumber(profile.offsetX, 0);
  dom.devModelOffsetYInput.value = toNumber(profile.offsetY, 0);
  const advancedKeys = [
    "supportsNativeExpressions",
    "interactionSummary",
    "lipSync",
    "eyeVisibilityCompensation",
    "headTurn",
    "fullScaleMultiplier",
    "displayBounds",
    "expressionMap",
    "fixedParameters",
    "hiddenParameters",
    "hiddenParts",
    "parameterAliases",
    "motionMap",
  ];
  const advanced = {};
  advancedKeys.forEach(key => {
    if (Object.prototype.hasOwnProperty.call(profile, key)) advanced[key] = profile[key];
  });
  if (dom.devModelOverrideJson && document.activeElement !== dom.devModelOverrideJson) {
    dom.devModelOverrideJson.value = JSON.stringify(advanced, null, 2);
  }
  if (dom.devModelProfileHint) {
    const active = profile.id === activeModelProfile.id ? "当前正在预览" : "尚未预览";
    const custom = Object.keys(override).length ? "已有开发覆盖" : "使用注册表默认值";
    dom.devModelProfileHint.textContent = `${profile.description || profile.label} / ${active} / ${custom}`;
  }
  if (dom.devModelOverrideStatus) {
    dom.devModelOverrideStatus.textContent = "覆盖文件：data/live2d_model_overrides.json；不会修改普通模式当前皮肤。";
  }
}

function selectedDeveloperModelId() {
  return dom.devModelProfileInput?.value || activeModelProfile.id;
}

function previewDeveloperModel() {
  if (!DEV_MODE) return;
  const modelId = selectedDeveloperModelId();
  const url = new URL(window.location.href);
  url.searchParams.set("dev", "1");
  url.searchParams.set("skipBoot", "1");
  url.searchParams.set("modelPreview", modelId);
  url.searchParams.delete("previewOnly");
  window.location.assign(url.toString());
}

async function reloadDeveloperModels() {
  if (!DEV_MODE) return;
  try {
    await loadLive2DModelRegistry();
    renderDeveloperModelEditor();
    toast("Live2D 皮肤覆盖配置已重载");
  } catch (error) {
    toast(`模型配置重载失败：${error.message}`);
  }
}

async function saveDeveloperModelOverride() {
  if (!DEV_MODE) return;
  const modelId = selectedDeveloperModelId();
  let override;
  try {
    override = JSON.parse(dom.devModelOverrideJson?.value.trim() || "{}");
    if (!override || Array.isArray(override) || typeof override !== "object") {
      throw new Error("高级覆盖必须是 JSON 对象");
    }
  } catch (error) {
    toast(`模型覆盖 JSON 无效：${error.message}`);
    return;
  }
  override.scaleMultiplier = clamp(toNumber(dom.devModelScaleInput?.value, 1), 0.5, 2.5);
  override.offsetX = clamp(toNumber(dom.devModelOffsetXInput?.value, 0), -1200, 1200);
  override.offsetY = clamp(toNumber(dom.devModelOffsetYInput?.value, 0), -1200, 1200);
  dom.devSaveModelOverrideBtn.disabled = true;
  try {
    const response = await fetch("/api/developer/live2d-models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modelId, override }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    toast("皮肤覆盖已保存，正在重载预览");
    previewDeveloperModel();
  } catch (error) {
    toast(`模型覆盖保存失败：${error.message}`);
  } finally {
    dom.devSaveModelOverrideBtn.disabled = false;
  }
}

async function resetDeveloperModelOverride() {
  if (!DEV_MODE) return;
  const modelId = selectedDeveloperModelId();
  if (!window.confirm("恢复这套皮肤的注册表默认值吗？")) return;
  dom.devResetModelOverrideBtn.disabled = true;
  try {
    const response = await fetch("/api/developer/live2d-models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ modelId, reset: true }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    toast("皮肤覆盖已恢复默认，正在重载预览");
    previewDeveloperModel();
  } catch (error) {
    toast(`恢复默认失败：${error.message}`);
  } finally {
    dom.devResetModelOverrideBtn.disabled = false;
  }
}

async function saveSettings(update, message = "设置已保存", applyWindow = false) {
  const previousSettings = settings;
  const optimisticSettings = normalizeSettings({ ...settings, ...update });
  settings = optimisticSettings;
  fillSettingsForm();
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: update }),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    settings = previousSettings;
    fillSettingsForm();
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  settings = normalizeSettings({ ...optimisticSettings, ...(data.settings || {}) });
  applySettingsToUi();
  fillSettingsForm();
  if (applyWindow && window.pywebview?.api?.applyWindowSettings) {
    try {
      const nativeResult = await window.pywebview.api.applyWindowSettings(settings);
      if (nativeResult?.ok === false) {
        throw new Error(nativeResult.error || "原生窗口设置应用失败");
      }
    } catch (error) {
      const rollback = Object.fromEntries(
        Object.keys(update || {}).map(key => [key, previousSettings[key]]),
      );
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ settings: rollback }),
        });
        await window.pywebview.api.applyWindowSettings(previousSettings);
      } catch {
        // Preserve the original native error; rollback is best effort.
      }
      settings = previousSettings;
      applySettingsToUi();
      fillSettingsForm();
      throw error;
    }
  }
  toast(message);
  return settings;
}

function storageScopedFetch(resource, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (DEV_MODE) headers["X-Amadeus-Storage-Scope"] = "developer";
  return fetch(resource, { ...options, headers });
}

function openBookmarkPanel() {
  setChatCollapsed(false, { scroll: false });
  closeConversationPicker();
  closeChatHistorySearch();
  dom.bookmarkPanel?.classList.remove("hidden");
  dom.bookmarkBtn?.setAttribute("aria-expanded", "true");
  loadBookmarks();
  window.setTimeout(() => dom.bookmarkQuickName?.focus(), 0);
}

function closeBookmarkPanel() {
  closeBookmarkEditor();
  dom.bookmarkPanel?.classList.add("hidden");
  dom.bookmarkBtn?.setAttribute("aria-expanded", "false");
}

async function bookmarkApi(body) {
  const response = await storageScopedFetch("/api/bookmarks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || data.error || `bookmark request failed (${response.status})`);
  }
  return data;
}

async function loadBookmarks() {
  if (!dom.bookmarkList) return;
  if (bookmarkLoadInFlight) {
    bookmarkLoadPending = true;
    return;
  }
  bookmarkLoadInFlight = true;
  try {
    const response = await storageScopedFetch("/api/bookmarks", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || "bookmark load failed");
    bookmarkItems = Array.isArray(data.items) ? data.items : [];
    renderBookmarks();
  } catch (error) {
    console.warn("bookmark load failed:", error);
    toast("收藏夹加载失败");
  } finally {
    bookmarkLoadInFlight = false;
    if (bookmarkLoadPending) {
      bookmarkLoadPending = false;
      window.queueMicrotask(loadBookmarks);
    }
  }
}

function bookmarkHost(value) {
  try {
    return new URL(String(value || "")).host;
  } catch {
    return String(value || "");
  }
}

function renderBookmarks() {
  if (!dom.bookmarkList) return;
  dom.bookmarkList.replaceChildren();
  dom.bookmarkEmpty?.classList.toggle("hidden", bookmarkItems.length > 0);
  if (dom.bookmarkCount) dom.bookmarkCount.textContent = `${bookmarkItems.length} 个`;
  for (const item of bookmarkItems) {
    const row = document.createElement("article");
    row.className = "bookmark-item";
    row.dataset.bookmarkId = String(item.bookmarkId || "");

    const main = document.createElement("div");
    main.className = "bookmark-item-main";
    const name = document.createElement("div");
    name.className = "bookmark-item-name";
    name.textContent = String(item.name || "未命名收藏");
    const host = document.createElement("div");
    host.className = "bookmark-item-host";
    host.textContent = bookmarkHost(item.url);
    host.title = String(item.url || "");
    main.append(name, host);

    const actions = document.createElement("div");
    actions.className = "bookmark-item-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.dataset.bookmarkAction = "open";
    open.textContent = "打开";
    open.setAttribute("aria-label", `打开收藏：${String(item.name || "未命名收藏")}`);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.dataset.bookmarkAction = "edit";
    edit.textContent = "⚙";
    edit.title = "编辑收藏";
    edit.setAttribute("aria-label", `编辑收藏：${String(item.name || "未命名收藏")}`);
    actions.append(open, edit);
    row.append(main, actions);
    dom.bookmarkList.append(row);
  }
}

function bookmarkFromEvent(event) {
  const row = event.target?.closest?.("[data-bookmark-id]");
  return bookmarkItems.find(item => (
    String(item.bookmarkId || "") === String(row?.dataset.bookmarkId || "")
  ));
}

async function openStoredBookmark(item) {
  try {
    const response = await storageScopedFetch("/api/actions/open-browser", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: String(item.url || ""),
        actionId: `bookmark_open_${String(item.bookmarkId || "")}_${Date.now()}`,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.message || data.error || "浏览器没有接受请求");
    }
  } catch (error) {
    toast("打开失败：" + error.message);
  }
}

function handleBookmarkListClick(event) {
  const action = event.target?.closest?.("[data-bookmark-action]")?.dataset.bookmarkAction;
  const item = bookmarkFromEvent(event);
  if (!item) return;
  if (action === "open") openStoredBookmark(item);
  if (action === "edit") openBookmarkEditor(item);
}

async function createQuickBookmark(event) {
  event?.preventDefault();
  const name = String(dom.bookmarkQuickName?.value || "").trim();
  const url = String(dom.bookmarkQuickUrl?.value || "").trim();
  if (!name || !url) return;
  try {
    await bookmarkApi({ action: "create", name, url });
    dom.bookmarkQuickName.value = "";
    dom.bookmarkQuickUrl.value = "";
    await loadBookmarks();
    toast("网页已收藏");
  } catch (error) {
    await loadBookmarks();
    toast("收藏失败：" + error.message);
  }
}

function openBookmarkEditor(item) {
  bookmarkEditorId = String(item.bookmarkId || "");
  bookmarkEditorVersion = Number(item.version || 0);
  dom.bookmarkEditorName.value = String(item.name || "");
  dom.bookmarkEditorUrl.value = String(item.url || "");
  dom.bookmarkEditor?.classList.remove("hidden");
  dom.bookmarkEditorName?.focus();
}

function closeBookmarkEditor() {
  bookmarkEditorId = "";
  bookmarkEditorVersion = 0;
  dom.bookmarkEditor?.classList.add("hidden");
}

async function saveBookmarkEditor(event) {
  event?.preventDefault();
  if (!bookmarkEditorId) return;
  try {
    await bookmarkApi({
      action: "update",
      bookmarkId: bookmarkEditorId,
      expectedVersion: bookmarkEditorVersion,
      name: String(dom.bookmarkEditorName?.value || "").trim(),
      url: String(dom.bookmarkEditorUrl?.value || "").trim(),
    });
    closeBookmarkEditor();
    await loadBookmarks();
    toast("收藏已更新");
  } catch (error) {
    await loadBookmarks();
    toast("保存失败：" + error.message);
  }
}

async function deleteEditedBookmark() {
  if (!bookmarkEditorId) return;
  const bookmarkId = bookmarkEditorId;
  const expectedVersion = bookmarkEditorVersion;
  try {
    await bookmarkApi({ action: "delete", bookmarkId, expectedVersion });
    closeBookmarkEditor();
    await loadBookmarks();
    toast("收藏已删除");
  } catch (error) {
    await loadBookmarks();
    toast("删除失败：" + error.message);
  }
}

function acceptedBookmarkMutation(data) {
  const receipts = [
    data?.actionReceipt,
    ...(Array.isArray(data?.actionReceipts) ? data.actionReceipts : []),
  ].filter(receipt => receipt && typeof receipt === "object");
  return receipts.some(receipt => (
    String(receipt.tool || "") === "bookmark_manage"
    && String(receipt.status || "") === "accepted"
  ));
}

function refreshBookmarksAfterAssistantAction(data) {
  if (acceptedBookmarkMutation(data)) loadBookmarks();
}

function setTodoDrawerOpen(open) {
  if (!dom.todoDrawer) return;
  dom.todoDrawer.classList.toggle("open", Boolean(open));
  dom.todoDrawer.setAttribute("aria-expanded", String(Boolean(open)));
  dom.todoToggleBtn?.setAttribute("aria-label", open ? "收起待办" : "展开待办");
  window.localStorage.setItem(TODO_DRAWER_OPEN_KEY, open ? "1" : "0");
  if (open) {
    markTodosSeen(todoLatestRevision);
    loadTodos({ markSeen: true });
  }
}

async function todoApi(body) {
  const response = await storageScopedFetch("/api/todos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || ("todo request failed (" + response.status + ")"));
  }
  return data;
}

function markTodosSeen(revision = todoLatestRevision) {
  todoSeenRevision = Math.max(todoSeenRevision, Math.max(0, Number(revision) || 0));
  todoUnseenCount = 0;
  window.localStorage.setItem(TODO_SEEN_REVISION_KEY, String(todoSeenRevision));
  renderTodoBadge();
}

function renderTodoBadge() {
  if (!dom.todoCountBadge) return;
  dom.todoCountBadge.textContent = String(Math.min(99, todoUnseenCount));
  dom.todoCountBadge.hidden = todoUnseenCount === 0;
  dom.todoCountBadge.title = todoUnseenCount > 0
    ? `${todoUnseenCount} 项待办变更尚未查看`
    : "没有未查看的待办变更";
  if (dom.todoToggleBtn) {
    dom.todoToggleBtn.title = `待办：${todoActiveCount} 项未完成`;
  }
}

async function loadTodos(options = {}) {
  const markSeen = Boolean(options?.markSeen);
  if (!dom.todoList) return;
  if (todoLoadInFlight) {
    todoLoadPending = true;
    todoMarkSeenPending ||= markSeen;
    return;
  }
  todoLoadInFlight = true;
  try {
    const scope = String(dom.todoFilterInput?.value || "active");
    const response = await storageScopedFetch(
      "/api/todos?scope=" + encodeURIComponent(scope)
        + "&sinceRevision=" + encodeURIComponent(todoSeenRevision)
    );
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || "todo load failed");
    todoItems = Array.isArray(data.items) ? data.items : [];
    todoActiveCount = Math.max(0, Number(data.activeCount) || 0);
    todoLatestRevision = Math.max(0, Number(data.revision) || 0);
    todoUnseenCount = Math.max(0, Number(data.unseenCount) || 0);
    if (markSeen || dom.todoDrawer?.classList.contains("open")) {
      markTodosSeen(todoLatestRevision);
    }
    renderTodoItems();
  } catch (error) {
    console.warn("todo load failed:", error);
    toast("待办加载失败");
  } finally {
    todoLoadInFlight = false;
    if (todoLoadPending) {
      const pendingMarkSeen = todoMarkSeenPending;
      todoLoadPending = false;
      todoMarkSeenPending = false;
      window.queueMicrotask(() => loadTodos({ markSeen: pendingMarkSeen }));
    }
  }
}

function todoLocalDateTime(value) {
  const parsed = new Date(String(value || ""));
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(parsed);
}

function todoDateTimeInputValue(value) {
  const parsed = new Date(String(value || ""));
  if (Number.isNaN(parsed.getTime())) return "";
  const pad = number => String(number).padStart(2, "0");
  return parsed.getFullYear() + "-" + pad(parsed.getMonth() + 1) + "-"
    + pad(parsed.getDate()) + "T" + pad(parsed.getHours()) + ":" + pad(parsed.getMinutes());
}

function renderTodoItems() {
  if (!dom.todoList) return;
  dom.todoList.replaceChildren();
  renderTodoBadge();
  dom.todoEmpty?.classList.toggle("hidden", todoItems.length > 0);
  for (const item of todoItems) {
    const taskId = String(item.taskId || "");
    const row = document.createElement("article");
    row.className = "todo-item" + (item.status === "completed" ? " is-completed" : "");
    row.dataset.taskId = taskId;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "todo-item-check";
    checkbox.checked = item.status === "completed";
    checkbox.dataset.todoAction = checkbox.checked ? "reopen" : "complete";
    checkbox.setAttribute("aria-label", checkbox.checked ? "恢复任务" : "完成任务");

    const main = document.createElement("div");
    main.className = "todo-item-main";
    main.dataset.todoAction = "edit";
    const title = document.createElement("div");
    title.className = "todo-item-title";
    title.textContent = String(item.title || "未命名任务");
    const meta = document.createElement("div");
    meta.className = "todo-item-meta";
    const details = [];
    if (item.priority === "high") details.push("高优先级");
    if (item.dueAt) details.push(todoLocalDateTime(item.dueAt));
    else details.push(item.dueDate ? `${item.dueDate}${item.recurrenceRule === "daily" ? " 起" : ""} · 全天` : "全天");
    if (String(item.recurrenceRule || "") === "daily") details.push("每天重复");
    if (item.snoozedUntil) details.push("推迟至 " + todoLocalDateTime(item.snoozedUntil));
    meta.textContent = details.join(" · ") || "无时间";
    main.append(title, meta);

    const snooze = document.createElement("button");
    snooze.type = "button";
    snooze.className = "todo-item-snooze";
    snooze.dataset.todoAction = "snooze";
    snooze.textContent = "＋10m";
    snooze.title = "推迟提醒 10 分钟";
    snooze.hidden = item.status === "completed";

    const settingsButton = document.createElement("button");
    settingsButton.type = "button";
    settingsButton.className = "todo-item-settings";
    settingsButton.dataset.todoAction = "edit";
    settingsButton.textContent = "⚙";
    settingsButton.title = "编辑任务设置";
    settingsButton.setAttribute("aria-label", `编辑任务：${String(item.title || "未命名任务")}`);

    const actions = document.createElement("div");
    actions.className = "todo-item-actions";
    actions.append(snooze, settingsButton);
    row.append(checkbox, main, actions);
    dom.todoList.append(row);
  }
}

function acceptedTodoMutation(data) {
  const receipt = data?.actionReceipt;
  if (!receipt || typeof receipt !== "object") return false;
  if (String(receipt.tool || "") !== "todo_manage") return false;
  if (String(receipt.status || "") !== "accepted") return false;
  const action = String(receipt.action || "").trim().toLowerCase();
  return !["list", "search"].includes(action);
}

function refreshTodosAfterAssistantAction(data) {
  if (!acceptedTodoMutation(data)) return;
  loadTodos({ markSeen: Boolean(dom.todoDrawer?.classList.contains("open")) });
}

async function createQuickTodo(event) {
  event?.preventDefault();
  const title = String(dom.todoQuickTitle?.value || "").trim();
  if (!title) return;
  try {
    await todoApi({ action: "create", title });
    dom.todoQuickTitle.value = "";
    await loadTodos();
    toast("已加入待办");
  } catch (error) {
    toast("添加失败：" + error.message);
  }
}

function todoItemFromEvent(event) {
  const row = event.target?.closest?.("[data-task-id]");
  return todoItems.find(item => String(item.taskId) === String(row?.dataset.taskId || ""));
}

function handleTodoListClick(event) {
  const action = event.target?.closest?.("[data-todo-action]")?.dataset.todoAction;
  const item = todoItemFromEvent(event);
  if (!item) return;
  if (action === "edit") openTodoEditor(item);
  if (action === "snooze") {
    mutateTodo({
      action: "snooze",
      taskId: item.taskId,
      expectedVersion: Number(item.version || 0),
      snoozeMinutes: 10,
    });
  }
}

function handleTodoListChange(event) {
  if (!event.target?.matches?.(".todo-item-check")) return;
  const item = todoItemFromEvent(event);
  if (!item) return;
  mutateTodo({
    action: event.target.checked ? "complete" : "reopen",
    taskId: item.taskId,
    expectedVersion: Number(item.version || 0),
  });
}

async function mutateTodo(command, successMessage = "待办已更新") {
  try {
    await todoApi(command);
    await loadTodos();
    toast(successMessage);
  } catch (error) {
    await loadTodos();
    toast("更新失败：" + error.message);
  }
}

function openTodoEditor(item) {
  todoEditorTaskId = String(item.taskId || "");
  todoEditorVersion = Number(item.version || 0);
  dom.todoEditorTitle.value = String(item.title || "");
  dom.todoEditorDetails.value = String(item.details || "");
  dom.todoEditorDueAt.value = todoDateTimeInputValue(item.dueAt);
  dom.todoEditorDueDate.value = String(item.dueDate || "");
  dom.todoEditorPriority.value = String(item.priority || "normal");
  dom.todoEditorRemindBefore.value = String(item.remindBeforeMinutes || 0);
  dom.todoEditorRecurrence.value = String(item.recurrenceRule || "");
  dom.todoEditor.classList.remove("hidden");
  dom.todoEditorTitle.focus();
}

function closeTodoEditor() {
  todoEditorTaskId = "";
  todoEditorVersion = 0;
  dom.todoEditor?.classList.add("hidden");
}

async function saveTodoEditor(event) {
  event.preventDefault();
  if (!todoEditorTaskId) return;
  const dueValue = String(dom.todoEditorDueAt.value || "");
  await mutateTodo({
    action: "update",
    taskId: todoEditorTaskId,
    expectedVersion: todoEditorVersion,
    title: String(dom.todoEditorTitle.value || "").trim(),
    details: String(dom.todoEditorDetails.value || "").trim(),
    dueAt: dueValue ? new Date(dueValue).toISOString() : "",
    dueDate: dueValue ? "" : String(dom.todoEditorDueDate.value || ""),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
    remindBeforeMinutes: Number(dom.todoEditorRemindBefore.value || 0),
    recurrenceRule: String(dom.todoEditorRecurrence.value || ""),
    priority: String(dom.todoEditorPriority.value || "normal"),
  }, "精细设置已保存");
  closeTodoEditor();
}

async function deleteEditedTodo() {
  if (!todoEditorTaskId) return;
  const taskId = todoEditorTaskId;
  const expectedVersion = todoEditorVersion;
  closeTodoEditor();
  await mutateTodo({ action: "delete", taskId, expectedVersion }, "待办已删除");
}

function renderSalieriTranscript(payload) {
  salieriRecorderEnabled = Boolean(payload?.enabled);
  dom.salieriUnlockForm?.classList.toggle("hidden", salieriRecorderEnabled);
  dom.salieriTranscriptWrap?.classList.toggle("hidden", !salieriRecorderEnabled);
  if (!salieriRecorderEnabled) {
    if (dom.salieriMessage) dom.salieriMessage.textContent = "AWAITING IDENTIFIER";
    return;
  }
  const transcript = String(payload?.content || "");
  if (dom.salieriTranscript) {
    const nearBottom = (
      dom.salieriTranscript.scrollHeight
      - dom.salieriTranscript.scrollTop
      - dom.salieriTranscript.clientHeight
    ) < 64;
    dom.salieriTranscript.textContent = `${payload?.truncated ? "[仅显示文件尾部]\n\n" : ""}${transcript}`;
    if (nearBottom || !dom.salieriTranscript.dataset.loaded) {
      dom.salieriTranscript.scrollTop = dom.salieriTranscript.scrollHeight;
    }
    dom.salieriTranscript.dataset.loaded = "1";
  }
  if (dom.salieriStatus) {
    dom.salieriStatus.textContent = payload?.updatedAt
      ? `RECORDING / ${String(payload.updatedAt).replace("T", " ").slice(0, 19)}`
      : "RECORDING";
  }
  if (dom.salieriPath) dom.salieriPath.textContent = String(payload?.filePath || "");
  if (dom.salieriMessage) dom.salieriMessage.textContent = "ARCHIVE IS OUTSIDE MEMORY CONTROL";
}

async function refreshSalieriTranscript({ quiet = false } = {}) {
  try {
    const response = await fetch("/api/salieri", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderSalieriTranscript(await response.json());
  } catch (error) {
    if (!quiet && dom.salieriMessage) {
      dom.salieriMessage.textContent = `CHANNEL ERROR / ${error.message}`;
    }
  }
}

async function unlockSalieriRecorder(event) {
  event.preventDefault();
  const code = String(dom.salieriCodeInput?.value || "").trim();
  try {
    const response = await fetch("/api/salieri/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const payload = await response.json();
    if (!response.ok || !payload?.enabled) {
      if (dom.salieriMessage) dom.salieriMessage.textContent = "ACCESS DENIED";
      return;
    }
    if (dom.salieriCodeInput) dom.salieriCodeInput.value = "";
    renderSalieriTranscript(payload);
  } catch (error) {
    if (dom.salieriMessage) dom.salieriMessage.textContent = `CHANNEL ERROR / ${error.message}`;
  }
}

function startSalieriRefreshPolling() {
  window.clearInterval(salieriRefreshTimer);
  salieriRefreshTimer = window.setInterval(
    () => refreshSalieriTranscript({ quiet: true }),
    10000,
  );
}

function startTodoReminderPolling() {
  window.clearInterval(todoReminderTimer);
  window.clearInterval(todoRefreshTimer);
  window.setTimeout(pollTodoReminders, 2500);
  todoReminderTimer = window.setInterval(pollTodoReminders, 10000);
  todoRefreshTimer = window.setInterval(loadTodos, 30000);
}

async function pollTodoReminders() {
  if (
    proactiveOutreachPaused() || todoReminderInFlight || bootSequenceActive || chatRequestInFlight
    || proactiveInteractionInFlight || touchState.requestInFlight
    || speechState.pending || isSpeaking(performance.now())
  ) return;
  todoReminderInFlight = true;
  let reminderId = "";
  try {
    const response = await storageScopedFetch("/api/todos/reminders/poll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || "reminder poll failed");
    if (data.suppressed) {
      applyProactiveControl(data.proactiveControl);
      return;
    }
    reminderId = String(data.reminderId || "");
    if (!reminderId) return;
    const delivered = await applyAssistant(data);
    await storageScopedFetch("/api/todos/reminders/ack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reminderId, delivered: Boolean(delivered) }),
    });
    await loadTodos();
  } catch (error) {
    console.warn("todo reminder failed:", error);
    if (reminderId) {
      await storageScopedFetch("/api/todos/reminders/ack", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reminderId, delivered: false, error: String(error.message || error),
        }),
      }).catch(() => {});
    }
  } finally {
    todoReminderInFlight = false;
  }
}

function normalizeSpeechLanguage(value, voiceLocale = "", targetLanguage = "") {
  const candidate = String(value || "").trim().toLowerCase().replaceAll("_", "-");
  if (["zh", "zh-cn", "zh-hans", "中文", "chinese"].includes(candidate)) return "zh-CN";
  if (["ja", "ja-jp", "jp", "日文", "日语", "日語", "japanese"].includes(candidate)) return "ja-JP";
  const legacy = `${voiceLocale || ""} ${targetLanguage || ""}`.toLowerCase();
  return legacy.includes("zh") || legacy.includes("中文") ? "zh-CN" : "ja-JP";
}

function speechLanguageProfile(value = settings?.speechLanguage) {
  const speechLanguage = normalizeSpeechLanguage(
    value,
    settings?.voiceLocale,
    settings?.gptSovitsTargetLanguage,
  );
  return speechLanguage === "zh-CN"
    ? { speechLanguage, locale: "zh-CN", targetLanguage: "中文" }
    : { speechLanguage: "ja-JP", locale: "ja-JP", targetLanguage: "日文" };
}

function normalizeSpeechProsody(value) {
  const candidate = String(value || "").trim().toLowerCase().replaceAll("-", "_");
  if (["compact", "tight", "紧凑", "緊湊"].includes(candidate)) return "compact";
  if (["deliberate", "relaxed", "舒缓", "舒緩"].includes(candidate)) return "deliberate";
  return "natural";
}

function normalizeSettings(raw) {
  const merged = { ...DEFAULT_SETTINGS, ...(raw || {}) };
  const requestedBackgroundPreset = String(merged.backgroundPreset || "");
  const backgroundPreset = BACKGROUND_PRESET_ALIASES[requestedBackgroundPreset] || requestedBackgroundPreset;
  const modelTransforms = {};
  if (merged.modelTransforms && typeof merged.modelTransforms === "object" && !Array.isArray(merged.modelTransforms)) {
    Object.entries(merged.modelTransforms).forEach(([modelId, value]) => {
      if (modelId) modelTransforms[modelId] = normalizedModelTransform(value);
    });
  }
  const proactiveInteractionsEnabled = Boolean(merged.proactiveInteractionsEnabled);
  const proactiveFrequency = Object.prototype.hasOwnProperty.call(
    PROACTIVE_FREQUENCY_PROFILES,
    String(merged.proactiveFrequency || ""),
  ) ? String(merged.proactiveFrequency) : "normal";
  const proactiveTopicCategories = PROACTIVE_LOGIC.normalizeCategories(
    merged.proactiveTopicCategories,
    proactiveInteractionsEnabled,
  );
  const proactiveScreenObservationChance = clamp(
    toNumber(merged.proactiveScreenObservationChance, 0.20),
    0,
    0.60,
  );
  return {
    ...merged,
    useResponseFormat: Boolean(merged.useResponseFormat),
    cognitiveRepairModel: merged.cognitiveRepairModel === "pro" ? "pro" : "follow",
    cognitiveAuditModel: merged.cognitiveAuditModel === "pro" ? "pro" : "follow",
    cognitiveSemanticAuditEnabled: Boolean(merged.cognitiveSemanticAuditEnabled),
    apiKeySet: Boolean(merged.apiKeySet),
    alwaysOnTop: Boolean(merged.alwaysOnTop),
    hudCollapsed: Boolean(merged.hudCollapsed),
    windowWidth: clamp(toNumber(merged.windowWidth, DEFAULT_SETTINGS.windowWidth), 360, 2200),
    windowHeight: clamp(toNumber(merged.windowHeight, DEFAULT_SETTINGS.windowHeight), 520, 2200),
    liteWindowWidth: clamp(toNumber(merged.liteWindowWidth, merged.layoutMode === "lite" ? merged.windowWidth : 520), 360, 2200),
    liteWindowHeight: clamp(toNumber(merged.liteWindowHeight, merged.layoutMode === "lite" ? merged.windowHeight : 760), 520, 2200),
    fullWindowWidth: clamp(toNumber(merged.fullWindowWidth, merged.layoutMode === "full" ? merged.windowWidth : 1040), 1040, 2200),
    fullWindowHeight: clamp(toNumber(merged.fullWindowHeight, merged.layoutMode === "full" ? merged.windowHeight : 760), 720, 2200),
    layoutMode: normalizeLayoutMode(merged.layoutMode),
    modelScale: clamp(toNumber(merged.modelScale, DEFAULT_SETTINGS.modelScale), 0.35, 2.4),
    modelOffsetX: clamp(toNumber(merged.modelOffsetX, DEFAULT_SETTINGS.modelOffsetX), -1200, 1200),
    modelOffsetY: clamp(toNumber(merged.modelOffsetY, DEFAULT_SETTINGS.modelOffsetY), -1200, 1200),
    modelTransforms,
    live2dModelId: String(merged.live2dModelId || DEFAULT_SETTINGS.live2dModelId),
    backgroundPreset: BACKGROUND_PRESETS.has(backgroundPreset) ? backgroundPreset : "binary",
    customBackgroundPath: String(merged.customBackgroundPath || ""),
    voiceEnabled: Boolean(merged.voiceEnabled),
    lipSyncEnabled: Boolean(merged.lipSyncEnabled),
    ttsProvider: String(merged.ttsProvider || DEFAULT_SETTINGS.ttsProvider),
    ttsEndpoint: String(merged.ttsEndpoint || ""),
    ttsSpeaker: String(merged.ttsSpeaker || DEFAULT_SETTINGS.ttsSpeaker),
    speechRate: clamp(toNumber(merged.speechRate, DEFAULT_SETTINGS.speechRate), 0.72, 1.35),
    speechLanguage: normalizeSpeechLanguage(
      merged.speechLanguage,
      merged.voiceLocale,
      merged.gptSovitsTargetLanguage,
    ),
    speechProsody: normalizeSpeechProsody(merged.speechProsody),
    voiceLocale: normalizeSpeechLanguage(
      merged.speechLanguage,
      merged.voiceLocale,
      merged.gptSovitsTargetLanguage,
    ) === "zh-CN" ? "zh-CN" : "ja-JP",
    voicePitch: clamp(toNumber(merged.voicePitch, DEFAULT_SETTINGS.voicePitch), 0.75, 1.65),
    ttsDeliveryPostProcessingEnabled: Boolean(merged.ttsDeliveryPostProcessingEnabled),
    gptSovitsPort: clamp(toNumber(merged.gptSovitsPort, DEFAULT_SETTINGS.gptSovitsPort), 1024, 65535),
    gptSovitsCondaEnv: String(merged.gptSovitsCondaEnv || DEFAULT_SETTINGS.gptSovitsCondaEnv),
    gptSovitsRoot: String(merged.gptSovitsRoot || DEFAULT_SETTINGS.gptSovitsRoot),
    gptSovitsModelPreset: String(merged.gptSovitsModelPreset || DEFAULT_SETTINGS.gptSovitsModelPreset),
    gptSovitsGptModel: String(merged.gptSovitsGptModel || DEFAULT_SETTINGS.gptSovitsGptModel),
    gptSovitsSovitsModel: String(merged.gptSovitsSovitsModel || DEFAULT_SETTINGS.gptSovitsSovitsModel),
    gptSovitsRefAudio: String(merged.gptSovitsRefAudio || DEFAULT_SETTINGS.gptSovitsRefAudio),
    gptSovitsRefText: String(merged.gptSovitsRefText || DEFAULT_SETTINGS.gptSovitsRefText),
    gptSovitsStyleMap: String(merged.gptSovitsStyleMap || ""),
    gptSovitsTargetLanguage: normalizeSpeechLanguage(
      merged.speechLanguage,
      merged.voiceLocale,
      merged.gptSovitsTargetLanguage,
    ) === "zh-CN" ? "中文" : "日文",
    gptSovitsStatus: merged.gptSovitsStatus || null,
    voicePresets: merged.voicePresets || {},
    alignSpokenJa: Boolean(merged.alignSpokenJa),
    autoStartGptSovits: Boolean(merged.autoStartGptSovits),
    debugDirectSpeech: Boolean(merged.debugDirectSpeech),
    permissionReadScreen: Boolean(merged.permissionReadScreen),
    permissionReadCamera: Boolean(merged.permissionReadCamera),
    cameraIndex: clamp(toNumber(merged.cameraIndex, 0), 0, 8),
    permissionReadSystemTime: Boolean(merged.permissionReadSystemTime),
    permissionWebSearch: Boolean(merged.permissionWebSearch),
    permissionFileRead: Boolean(merged.permissionFileRead),
    permissionFileWrite: Boolean(merged.permissionFileWrite),
    permissionOpenBrowser: Boolean(merged.permissionOpenBrowser),
    agentFileWorkspace: String(merged.agentFileWorkspace || DEFAULT_SETTINGS.agentFileWorkspace),
    webSearchProvider: ["duckduckgo", "brave"].includes(String(merged.webSearchProvider).toLowerCase())
      ? String(merged.webSearchProvider).toLowerCase() : "duckduckgo",
    webSearchApiKeySet: Boolean(merged.webSearchApiKeySet),
    webSearchMaxResults: clamp(toNumber(merged.webSearchMaxResults, 5), 1, 8),
    webReadTopPages: clamp(toNumber(merged.webReadTopPages, 2), 0, 3),
    webRequestTimeout: clamp(toNumber(merged.webRequestTimeout, 12), 3, 30),
    proactiveInteractionsEnabled,
    proactiveFrequency,
    proactiveTopicCategories,
    proactiveScreenObservationEnabled: Boolean(merged.proactiveScreenObservationEnabled),
    proactiveScreenObservationChance,
    releaseMode: Boolean(merged.releaseMode),
  };
}

function normalizeLayoutMode(value) {
  return String(value || "").toLowerCase() === "full" ? "full" : "lite";
}

function fillSettingsForm() {
  dom.apiEndpointInput.value = settings.apiEndpoint || "";
  dom.apiModelInput.value = settings.apiModel || "";
  dom.cognitiveRepairModelInput.value = settings.cognitiveRepairModel;
  dom.cognitiveAuditModelInput.value = settings.cognitiveAuditModel;
  dom.cognitiveSemanticAuditEnabledInput.checked = settings.cognitiveSemanticAuditEnabled;
  dom.apiKeyInput.value = settings.apiKeySet ? SAVED_API_KEY_MASK : "";
  dom.apiKeyInput.placeholder = "API key";
  if (dom.visionEnabledInput) dom.visionEnabledInput.checked = settings.visionEnabled !== false;
  if (dom.visionApiKeyInput) dom.visionApiKeyInput.value = settings.visionApiKeySet ? SAVED_API_KEY_MASK : "";
  if (dom.visionApiHint) dom.visionApiHint.textContent = settings.visionStatus?.message || "DeepSeek 云端识图：主聊天使用官方接口时可共用 Key。";
  if (dom.apiNetworkModeInput) dom.apiNetworkModeInput.value = settings.apiNetworkMode || "auto";
  if (dom.apiProxyUrlInput) dom.apiProxyUrlInput.value = settings.apiProxyUrl || "";
  dom.responseFormatInput.checked = settings.useResponseFormat;
  dom.apiKeyHint.textContent = settings.apiKeySet
    ? "API key 已保存在本机，圆点仅为遮罩；直接输入可替换"
    : "API key 未设置";
  dom.windowWidthInput.value = settings.windowWidth;
  dom.windowHeightInput.value = settings.windowHeight;
  dom.layoutModeInput.value = settings.layoutMode;
  syncLayoutSizeInputs();
  renderLive2DModelOptions();
  updateModelTransformInputs();
  updateBackgroundControls();
  dom.alwaysOnTopInput.checked = settings.alwaysOnTop;
  dom.hudCollapsedInput.checked = settings.hudCollapsed;
  dom.voiceEnabledInput.checked = settings.voiceEnabled;
  dom.lipSyncEnabledInput.checked = settings.lipSyncEnabled;
  dom.autoStartGptSovitsInput.checked = settings.autoStartGptSovits;
  dom.alignSpokenJaInput.checked = settings.alignSpokenJa;
  dom.ttsDeliveryPostProcessingEnabledInput.checked = settings.ttsDeliveryPostProcessingEnabled;
  dom.ttsProviderInput.value = settings.ttsProvider || DEFAULT_SETTINGS.ttsProvider;
  dom.ttsEndpointInput.value = settings.ttsEndpoint || "";
  dom.ttsSpeakerInput.value = settings.ttsSpeaker || "1";
  dom.speechLanguageInput.value = settings.speechLanguage || "ja-JP";
  dom.speechProsodyInput.value = normalizeSpeechProsody(settings.speechProsody);
  dom.voiceLocaleInput.value = settings.voiceLocale || "ja-JP";
  dom.voiceLocaleInput.readOnly = true;
  dom.speechRateInput.value = settings.speechRate;
  renderVoicePresetOptions();
  dom.gptSovitsPresetInput.value = settings.gptSovitsModelPreset || DEFAULT_SETTINGS.gptSovitsModelPreset;
  if (!dom.gptSovitsPresetInput.value) dom.gptSovitsPresetInput.value = "custom";
  updateVoicePresetHint();
  dom.gptSovitsPortInput.value = settings.gptSovitsPort || 9881;
  dom.gptSovitsCondaEnvInput.value = settings.gptSovitsCondaEnv || "";
  dom.gptSovitsRootInput.value = settings.gptSovitsRoot || "";
  dom.gptSovitsGptModelInput.value = settings.gptSovitsGptModel || "";
  dom.gptSovitsSovitsModelInput.value = settings.gptSovitsSovitsModel || "";
  dom.gptSovitsRefAudioInput.value = settings.gptSovitsRefAudio || "";
  dom.gptSovitsRefTextInput.value = settings.gptSovitsRefText || "";
  dom.gptSovitsStyleMapInput.value = settings.gptSovitsStyleMap || "";
  dom.gptSovitsTargetLanguageInput.value = settings.gptSovitsTargetLanguage || "日文";
  dom.gptSovitsTargetLanguageInput.readOnly = true;
  dom.alignSpokenJaInput.disabled = settings.speechLanguage !== "ja-JP";
  dom.permissionReadScreenInput.checked = settings.permissionReadScreen;
  dom.permissionReadCameraInput.checked = settings.permissionReadCamera;
  dom.cameraIndexInput.value = settings.cameraIndex;
  dom.permissionReadSystemTimeInput.checked = settings.permissionReadSystemTime;
  dom.permissionWebSearchInput.checked = settings.permissionWebSearch;
  dom.permissionFileReadInput.checked = settings.permissionFileRead;
  dom.permissionFileWriteInput.checked = settings.permissionFileWrite;
  dom.permissionOpenBrowserInput.checked = settings.permissionOpenBrowser;
  dom.agentFileWorkspaceInput.value = settings.agentFileWorkspace || DEFAULT_SETTINGS.agentFileWorkspace;
  dom.webSearchProviderInput.value = settings.webSearchProvider || "duckduckgo";
  dom.webSearchApiKeyInput.value = "";
  dom.webSearchApiKeyInput.placeholder = settings.webSearchApiKeySet
    ? "留空保留已保存的搜索 Key" : "DuckDuckGo 不需要填写";
  dom.webSearchMaxResultsInput.value = settings.webSearchMaxResults;
  dom.webReadTopPagesInput.value = settings.webReadTopPages;
  dom.webSearchStatus.textContent = settings.permissionWebSearch
    ? `${settings.webSearchProvider === "brave" ? "Brave" : "DuckDuckGo"} 已授权` : "权限关闭";
  if (dom.proactiveInteractionsEnabledInput) {
    dom.proactiveInteractionsEnabledInput.checked = settings.proactiveInteractionsEnabled;
  }
  if (dom.proactiveFrequencyInput) {
    dom.proactiveFrequencyInput.value = settings.proactiveFrequency || "normal";
  }
  if (dom.proactiveScreenObservationEnabledInput) {
    dom.proactiveScreenObservationEnabledInput.checked = settings.proactiveScreenObservationEnabled;
  }
  if (dom.proactiveScreenObservationChanceInput) {
    const normalizedChance = Number(settings.proactiveScreenObservationChance).toFixed(2);
    dom.proactiveScreenObservationChanceInput.value = ["0.10", "0.20", "0.35"].includes(normalizedChance)
      ? normalizedChance
      : "0.20";
  }
  const selectedProactiveTopics = new Set(settings.proactiveTopicCategories || []);
  dom.proactiveTopicInputs?.forEach(input => {
    input.checked = selectedProactiveTopics.has(input.dataset.proactiveTopic);
  });
  updateProactiveTopicControls();
  if (dom.debugDirectSpeechInput) dom.debugDirectSpeechInput.checked = settings.debugDirectSpeech;
  updateGptSovitsStatus(settings.gptSovitsStatus);
  if (dom.memoryFileInput) {
    dom.memoryFileInput.value = settings.memoryFile || DEFAULT_SETTINGS.memoryFile;
    dom.memoryFileInput.readOnly = DEV_MODE;
    dom.memoryFileInput.title = DEV_MODE
      ? "开发者模式固定使用独立记忆文件，不会读写普通模式记忆"
      : "";
  }
  if (dom.chooseMemoryBtn) dom.chooseMemoryBtn.disabled = DEV_MODE;
  if (dom.screenCaptureDirectoryInput) {
    dom.screenCaptureDirectoryInput.value = settings.screenCaptureDirectory || "data/runtime/screen-captures";
  }
  refreshMemoryEditor();
}

function updateProactiveTopicControls() {
  const enabled = Boolean(dom.proactiveInteractionsEnabledInput?.checked);
  if (dom.proactiveFrequencyInput) dom.proactiveFrequencyInput.disabled = !enabled;
  const screenObservationEnabled = enabled && Boolean(dom.proactiveScreenObservationEnabledInput?.checked);
  if (dom.proactiveScreenObservationEnabledInput) {
    dom.proactiveScreenObservationEnabledInput.disabled = !enabled;
  }
  if (dom.proactiveScreenObservationChanceInput) {
    dom.proactiveScreenObservationChanceInput.disabled = !screenObservationEnabled;
  }
  const selectedCount = dom.proactiveTopicInputs?.filter(input => input.checked).length || 0;
  dom.proactiveTopicInputs?.forEach(input => {
    input.disabled = !enabled;
    input.closest(".check-row")?.classList.toggle("disabled", !enabled);
  });
  if (dom.proactiveTopicsStatus) {
    dom.proactiveTopicsStatus.textContent = enabled ? `已选 ${selectedCount} 项` : "主动互动已关闭";
    dom.proactiveTopicsStatus.classList.toggle("error", enabled && selectedCount === 0);
  }
}

function syncLayoutSizeInputs() {
  const lite = normalizeLayoutMode(dom.layoutModeInput.value) === "lite";
  dom.windowWidthInput.value = lite ? settings.liteWindowWidth : settings.fullWindowWidth;
  dom.windowHeightInput.value = lite ? settings.liteWindowHeight : settings.fullWindowHeight;
  dom.windowWidthInput.disabled = false;
  dom.windowHeightInput.disabled = false;
  dom.windowWidthInput.title = lite ? "Lite 模式窗口宽度（会单独记住）" : "Full 模式窗口宽度（会单独记住）";
  dom.windowHeightInput.title = lite ? "Lite 模式窗口高度（会单独记住）" : "Full 模式窗口高度（会单独记住）";
}

function renderVoicePresetOptions() {
  if (!dom.gptSovitsPresetInput) return;
  const presets = settings.voicePresets || {};
  const current = settings.gptSovitsModelPreset || dom.gptSovitsPresetInput.value;
  const orderedIds = Object.keys(presets).filter(id => id !== "custom");
  if (Object.prototype.hasOwnProperty.call(presets, "custom")) orderedIds.push("custom");
  if (!orderedIds.length) return;
  dom.gptSovitsPresetInput.replaceChildren(...orderedIds.map(id => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = presets[id]?.label || id;
    return option;
  }));
  dom.gptSovitsPresetInput.value = orderedIds.includes(current) ? current : "custom";
}

function updateVoicePresetHint() {
  if (!dom.gptSovitsPresetHint) return;
  const presetId = dom.gptSovitsPresetInput.value;
  const preset = settings.voicePresets?.[presetId];
  if (presetId === "custom") {
    dom.gptSovitsPresetHint.textContent = "自定义模式会使用下面填写的权重和参考文件。";
    return;
  }
  const description = preset?.description || "自定义路径不会自动改写下方字段。";
  dom.gptSovitsPresetHint.textContent = `${description} 选择预设只载入表单；需点击“保存并启动/重启模型”才会切换。`;
}

function applySettingsToUi() {
  setChatCollapsed(settings.hudCollapsed, { scroll: false });
  document.body.classList.toggle("full-layout", settings.layoutMode === "full");
  document.body.classList.toggle("lite-layout", settings.layoutMode !== "full");
  connectionStatus = settings.modelConfigured ? "API READY" : "LOCAL";
  if (dom.settingsStatus) {
    dom.settingsStatus.textContent = settings.modelConfigured
      ? `${settings.apiModel} / API READY`
      : "LOCAL SIMULATION";
  }
  updateModelStateLabel();
  updatePinWindowButton();
  fitModel();
}

function updatePinWindowButton() {
  if (!dom.pinWindowBtn) return;
  const enabled = Boolean(settings.alwaysOnTop);
  const label = enabled ? "取消窗口置顶" : "窗口置顶";
  dom.pinWindowBtn.classList.toggle("is-pinned", enabled);
  dom.pinWindowBtn.title = label;
  dom.pinWindowBtn.setAttribute("aria-label", label);
  dom.pinWindowBtn.setAttribute("aria-pressed", String(enabled));
}

async function toggleAlwaysOnTop() {
  const previous = Boolean(settings.alwaysOnTop);
  const enabled = !Boolean(settings.alwaysOnTop);
  settings.alwaysOnTop = enabled;
  updatePinWindowButton();
  try {
    if (window.pywebview?.api?.setAlwaysOnTop) {
      const nativeResult = await window.pywebview.api.setAlwaysOnTop(enabled);
      if (nativeResult?.ok === false) throw new Error(nativeResult.error || "窗口状态更新失败");
    } else if (window.pywebview?.api?.applyWindowSettings) {
      const nativeResult = await window.pywebview.api.applyWindowSettings(settings);
      if (nativeResult?.ok === false) throw new Error(nativeResult.error || "窗口状态更新失败");
    }
    await saveSettings({ alwaysOnTop: enabled }, enabled ? "窗口已置顶" : "窗口已取消置顶");
    updatePinWindowButton();
  } catch (error) {
    try {
      if (window.pywebview?.api?.setAlwaysOnTop) {
        await window.pywebview.api.setAlwaysOnTop(previous);
      }
    } catch {
      // The original failure is more useful than a secondary rollback error.
    }
    settings.alwaysOnTop = previous;
    updatePinWindowButton();
    toast(`窗口置顶切换失败：${error.message}`);
  }
}

async function openScreenCaptureDirectory() {
  if (!window.pywebview?.api?.openScreenCaptureDirectory) {
    toast(`截图目录：${settings.screenCaptureDirectory || "data/runtime/screen-captures"}`);
    return;
  }
  try {
    const result = await window.pywebview.api.openScreenCaptureDirectory();
    if (result?.ok === false) throw new Error(result.error || "打开失败");
    toast("已打开截图保存位置");
  } catch (error) {
    toast(`打开截图目录失败：${error.message}`);
  }
}

async function saveApiSettings() {
  try {
    const visibleApiKey = dom.apiKeyInput.value.trim();
    const visibleVisionKey = dom.visionApiKeyInput?.value.trim() || "";
    await saveSettings({
      apiEndpoint: dom.apiEndpointInput.value.trim(),
      apiModel: dom.apiModelInput.value.trim(),
      cognitiveRepairModel: dom.cognitiveRepairModelInput.value,
      cognitiveAuditModel: dom.cognitiveAuditModelInput.value,
      cognitiveSemanticAuditEnabled: dom.cognitiveSemanticAuditEnabledInput.checked,
      apiKey: !visibleApiKey || visibleApiKey === SAVED_API_KEY_MASK ? "__KEEP__" : visibleApiKey,
      visionEnabled: dom.visionEnabledInput?.checked ?? true,
      visionApiKey: !visibleVisionKey || visibleVisionKey === SAVED_API_KEY_MASK ? "__KEEP__" : visibleVisionKey,
      apiNetworkMode: dom.apiNetworkModeInput?.value || "auto",
      apiProxyUrl: dom.apiProxyUrlInput?.value.trim() || "",
      useResponseFormat: dom.responseFormatInput.checked,
    }, "API 设置已保存");
  } catch (error) {
    toast(`API 设置保存失败：${error.message}`);
  }
}

async function testApiConnection() {
  dom.apiTestStatus.textContent = "正在连接 DeepSeek...";
  try {
    const response = await fetch("/api/test-api", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const data = await response.json();
    const transport = data.transport && typeof data.transport === "object" ? data.transport : null;
    const routeSummary = transport
      ? `；路线 ${transport.networkMode || "auto"} / ${transport.proxyConfigured ? `代理(${transport.proxySource || "configured"})` : "直连"}`
      : "";
    dom.apiTestStatus.textContent = `${data.message || (data.ok ? "API 连接正常" : "API 连接失败")}${routeSummary}`;
    dom.apiTestStatus.classList.toggle("error", !data.ok);
    connectionStatus = data.ok ? "API READY" : "API ERROR / TEST FAILED";
    updateModelStateLabel();
  } catch (error) {
    const detail = connectionErrorSummary(error);
    dom.apiTestStatus.textContent = `测试失败：${detail}`;
    dom.apiTestStatus.classList.add("error");
    connectionStatus = "API ERROR / TEST FAILED";
    updateModelStateLabel();
  }
}

async function saveDisplaySettings() {
  try {
    const previousModelId = activeModelProfile.id;
    await saveSettings(collectDisplaySettings(), "显示设置已保存", true);
    if (settings.live2dModelId !== previousModelId) {
      toast("正在切换 Live2D 模型...");
      window.setTimeout(() => window.location.reload(), 120);
    }
  } catch (error) {
    toast(`显示设置保存失败：${error.message}`);
  }
}

function collectDisplaySettings() {
  const layoutMode = normalizeLayoutMode(dom.layoutModeInput.value);
  const width = toNumber(dom.windowWidthInput.value, settings.windowWidth);
  const height = toNumber(dom.windowHeightInput.value, settings.windowHeight);
  const modelId = selectedDisplayModelId();
  const transform = normalizedModelTransform({
    scale: toNumber(dom.modelScaleInput.value, 100) / 100,
    offsetX: dom.modelOffsetXInput.value,
    offsetY: dom.modelOffsetYInput.value,
  });
  const ordinaryModelId = settings.live2dModelId || activeModelProfile?.id || FALLBACK_MODEL_PROFILE.id;
  const ordinaryTransform = normalizedModelTransform(settings.modelTransforms?.[ordinaryModelId] || {
    scale: settings.modelScale,
    offsetX: settings.modelOffsetX,
    offsetY: settings.modelOffsetY,
  });
  const modelTransforms = DEV_MODE
    ? { ...(settings.modelTransforms || {}) }
    : { ...(settings.modelTransforms || {}), [modelId]: transform };
  return {
    windowWidth: layoutMode === "full" ? Math.max(1040, width) : clamp(width, 360, 2200),
    windowHeight: layoutMode === "full" ? Math.max(720, height) : clamp(height, 520, 2200),
    liteWindowWidth: layoutMode === "lite" ? clamp(width, 360, 2200) : settings.liteWindowWidth,
    liteWindowHeight: layoutMode === "lite" ? clamp(height, 520, 2200) : settings.liteWindowHeight,
    fullWindowWidth: layoutMode === "full" ? Math.max(1040, width) : settings.fullWindowWidth,
    fullWindowHeight: layoutMode === "full" ? Math.max(720, height) : settings.fullWindowHeight,
    layoutMode,
    modelScale: DEV_MODE ? ordinaryTransform.scale : transform.scale,
    modelOffsetX: DEV_MODE ? ordinaryTransform.offsetX : transform.offsetX,
    modelOffsetY: DEV_MODE ? ordinaryTransform.offsetY : transform.offsetY,
    modelTransforms,
    live2dModelId: DEV_MODE ? ordinaryModelId : modelId,
    backgroundPreset: BACKGROUND_PRESETS.has(dom.backgroundPresetInput?.value) ? dom.backgroundPresetInput.value : "binary",
    customBackgroundPath: settings.customBackgroundPath || "",
    alwaysOnTop: dom.alwaysOnTopInput.checked,
    hudCollapsed: dom.hudCollapsedInput.checked,
  };
}

function collectInteractionSettings() {
  return {
    proactiveInteractionsEnabled: Boolean(dom.proactiveInteractionsEnabledInput?.checked),
    proactiveFrequency: Object.prototype.hasOwnProperty.call(
      PROACTIVE_FREQUENCY_PROFILES,
      String(dom.proactiveFrequencyInput?.value || ""),
    ) ? String(dom.proactiveFrequencyInput.value) : "normal",
    proactiveTopicCategories: (dom.proactiveTopicInputs || [])
      .filter(input => input.checked)
      .map(input => input.dataset.proactiveTopic)
      .filter(item => PROACTIVE_TOPIC_CATEGORIES.has(item)),
    proactiveScreenObservationEnabled: Boolean(dom.proactiveScreenObservationEnabledInput?.checked),
    proactiveScreenObservationChance: clamp(
      toNumber(dom.proactiveScreenObservationChanceInput?.value, 0.20),
      0,
      0.60,
    ),
  };
}

async function saveInteractionSettings() {
  try {
    const update = collectInteractionSettings();
    if (update.proactiveInteractionsEnabled && !update.proactiveTopicCategories.length) {
      throw new Error("至少选择一个主动话题方向");
    }
    await saveSettings(update, "互动设置已保存");
    resetProactiveSequence(performance.now());
    updateProactiveTopicControls();
  } catch (error) {
    toast(`互动设置保存失败：${error.message}`);
  }
}

async function saveVoiceSettings() {
  try {
    await saveSettings(
      collectVoiceSettings(),
      "声音设置已保存；没有立即重启，保存的配置将在下次启动时生效",
    );
  } catch (error) {
    toast(`声音设置保存失败：${error.message}`);
  }
}

async function saveDebugSettings() {
  try {
    await saveSettings({
      debugDirectSpeech: Boolean(dom.debugDirectSpeechInput?.checked),
    }, "开发设置已保存");
  } catch (error) {
    toast(`开发设置保存失败：${error.message}`);
  }
}

async function loadPersona() {
  try {
    const response = await fetch("/api/persona");
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    renderPersona(data.persona || {});
    if (DEV_MODE || !dom.settingsModal?.classList.contains("hidden")) {
      toast("人格配置已加载");
    }
  } catch (error) {
    if (dom.devPersonaStatus) dom.devPersonaStatus.textContent = `加载失败：${error.message}`;
    if (dom.settingsPersonaStatus) dom.settingsPersonaStatus.textContent = `加载失败：${error.message}`;
    toast(`人格加载失败：${error.message}`);
  }
}

async function savePersonaOverride(sourceInput = dom.devPersonaOverride, saveButton = dom.savePersonaBtn) {
  const oldText = saveButton?.textContent || "";
  try {
    if (saveButton) {
      saveButton.disabled = true;
      saveButton.textContent = "保存中";
    }
    const response = await fetch("/api/persona", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ override: sourceInput?.value || "" }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    renderPersona(data.persona || {});
    toast("人格补丁已保存，下一轮对话生效");
  } catch (error) {
    toast(`人格补丁保存失败：${error.message}`);
  } finally {
    if (saveButton) {
      saveButton.disabled = false;
      saveButton.textContent = oldText || "保存人格补丁";
    }
  }
}

function renderPersona(persona) {
  const summary = persona.summary || {};
  const generatedRules = persona.generatedRules || {};
  const emotionalSceneAnchors = persona.emotionalSceneAnchors || {};
  const emotionalAnchors = Array.isArray(emotionalSceneAnchors.anchors)
    ? emotionalSceneAnchors.anchors
    : [];
  const global = generatedRules.global || {};
  if (dom.devPersonaStatus) {
    dom.devPersonaStatus.textContent = [
      `DeepSeek: ${summary.deepseekReady ? "READY" : "UNKNOWN"}`,
      `蒸馏来源: ${summary.distillationSource || generatedRules.source || "unknown"}`,
      `抽象标注: ${summary.distilledRows || generatedRules.rowCount || 0}`,
      `补丁: ${persona.override ? "已启用" : "未设置"}`,
    ].join(" / ");
  }
  if (dom.settingsPersonaStatus) {
    dom.settingsPersonaStatus.textContent = persona.override ? "补丁已启用" : "当前未设置临时补丁";
    dom.settingsPersonaStatus.title = persona.overridePath || "";
  }
  if (dom.devPersonaSummary) {
    const lines = [
      summary.agentPositioning || "",
      summary.integration || "",
      `情绪标签: ${(global.emotion || []).join("、") || "-"}`,
      `动作标签: ${(global.gesture || []).join("、") || "-"}`,
      `语气标签: ${(global.voiceTone || []).join("、") || "-"}`,
      `名场面情绪锚点 (${emotionalAnchors.length}): ${emotionalAnchors.map((item) => item.title || item.id).join("、") || "-"}`,
      `限制: ${(summary.limits || []).join("；") || "-"}`,
      `人格补丁文件: ${persona.overridePath || "-"}`,
      `名场面配置: ${persona.emotionalSceneAnchorsPath || "-"}`,
    ].filter(Boolean);
    dom.devPersonaSummary.textContent = lines.join("\n");
  }
  if (dom.devPersonaOverride && document.activeElement !== dom.devPersonaOverride) {
    dom.devPersonaOverride.value = persona.override || "";
  }
  if (dom.settingsPersonaOverride && document.activeElement !== dom.settingsPersonaOverride) {
    dom.settingsPersonaOverride.value = persona.override || "";
  }
  if (dom.devGeneratedSkill) {
    dom.devGeneratedSkill.value = persona.generatedSkill || "";
  }
}

async function loadDeveloperRelationship() {
  if (!DEV_MODE) return;
  const scopeValue = String(dom.devRelationshipScopeInput?.value || "release");
  const requestedScope = ["release", "normal", "developer"].includes(scopeValue)
    ? scopeValue
    : "release";
  try {
    const response = await storageScopedFetch(
      `/api/developer/relationship?scope=${encodeURIComponent(requestedScope)}`,
    );
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    renderDeveloperRelationship(data.relationship || {});
    if (dom.devRelationshipScopeStatus) {
      dom.devRelationshipScopeStatus.textContent = data.scope === "release"
        ? "正在查看正式 release 用户数据（只读）；不会写入 release，也不会切换开发者运行态。"
        : data.scope === "normal"
          ? "正在查看源码普通模式数据（只读）；开发者对话、记忆和关系运行态仍保持隔离。"
          : "正在查看开发者隔离数据（只读）；此页面不会修改关系运行态。";
    }
  } catch (error) {
    if (dom.devRelationshipStage) dom.devRelationshipStage.textContent = `加载失败：${error.message}`;
    if (dom.devRelationshipScopeStatus) dom.devRelationshipScopeStatus.textContent = "关系数据加载失败，未切换任何运行状态。";
  }
}

function renderDeveloperRelationship(relationship) {
  if (!DEV_MODE) return;
  const stageLabels = {
    close_companion: "亲密同伴：朋友以上，尚未进入承诺关系",
    trusted_intimacy: "深度信赖：亲密与信任已经稳定形成",
    committed_partner: "稳定伴侣：已进入明确、持续的恋人关系",
    life_partner: "生活伴侣：日常计划与共同习惯深度交织",
    old_married: "长期伴侣：接近老夫老妻般的稳定默契",
  };
  const stage = String(relationship.stage || "close_companion");
  const developerView = relationship.developerView || {};
  if (dom.devRelationshipStage) {
    dom.devRelationshipStage.textContent = `${stage} / ${stageLabels[stage] || "未知阶段"}`;
  }
  if (dom.devRelationshipMeters) {
    const metrics = [
      ["熟悉度", relationship.familiarity],
      ["信任", relationship.trust],
      ["打趣默契", relationship.teasingLevel],
      ["关心程度", relationship.careLevel],
      ["关系稳定", relationship.stability],
    ];
    dom.devRelationshipMeters.replaceChildren(...metrics.map(([label, rawValue]) => {
      const value = clamp(toNumber(rawValue, 0), 0, 1);
      const row = document.createElement("div");
      row.className = "relationship-meter";
      const name = document.createElement("span");
      name.textContent = label;
      const meter = document.createElement("i");
      meter.style.setProperty("--value", `${(value * 100).toFixed(1)}%`);
      const number = document.createElement("code");
      number.textContent = value.toFixed(3);
      row.append(name, meter, number);
      return row;
    }));
  }
  const radarValues = [
    relationship.familiarity,
    relationship.trust,
    relationship.teasingLevel,
    relationship.careLevel,
    relationship.stability,
  ].map(value => clamp(toNumber(value, 0), 0, 1));
  const radarPoints = radarValues.map((value, index) => {
    const angle = (-90 + index * 72) * Math.PI / 180;
    const radius = 60 * value;
    return [100 + Math.cos(angle) * radius, 80 + Math.sin(angle) * radius];
  });
  if (dom.devRelationshipRadarValue) {
    dom.devRelationshipRadarValue.setAttribute(
      "points",
      radarPoints.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" "),
    );
  }
  if (dom.devRelationshipRadarDots) {
    const svgNamespace = "http://www.w3.org/2000/svg";
    dom.devRelationshipRadarDots.replaceChildren(...radarPoints.map(([x, y]) => {
      const dot = document.createElementNS(svgNamespace, "circle");
      dot.setAttribute("cx", x.toFixed(2));
      dot.setAttribute("cy", y.toFixed(2));
      dot.setAttribute("r", "2.8");
      return dot;
    }));
  }
  if (dom.devRelationshipRadar) {
    dom.devRelationshipRadar.setAttribute(
      "aria-label",
      `关系五维：熟悉 ${(radarValues[0] * 100).toFixed(0)}%，信任 ${(radarValues[1] * 100).toFixed(0)}%，` +
      `默契 ${(radarValues[2] * 100).toFixed(0)}%，关心 ${(radarValues[3] * 100).toFixed(0)}%，稳定 ${(radarValues[4] * 100).toFixed(0)}%`,
    );
  }
  const nextStage = developerView.nextStage || null;
  const readiness = nextStage ? clamp(toNumber(nextStage.readiness, 0), 0, 1) : 1;
  if (dom.devRelationshipNextTitle) {
    dom.devRelationshipNextTitle.textContent = nextStage ? `下一阶段：${nextStage.label}` : "已到最高关系阶段";
  }
  if (dom.devRelationshipReadiness) {
    dom.devRelationshipReadiness.style.setProperty("--value", `${(readiness * 100).toFixed(1)}%`);
  }
  if (dom.devRelationshipReadinessValue) {
    dom.devRelationshipReadinessValue.textContent = nextStage
      ? `瓶颈进度 ${(readiness * 100).toFixed(1)}%`
      : "全部阶段已达成";
  }
  if (dom.devRelationshipReactionProfile) {
    dom.devRelationshipReactionProfile.textContent = `成人视觉反应：${developerView.visualReactionProfile || "按当前关系自然调整"}`;
  }
  if (dom.devRelationshipStagePath) {
    const stagePath = Array.isArray(developerView.stagePath) ? developerView.stagePath : [];
    dom.devRelationshipStagePath.replaceChildren(...stagePath.map(item => {
      const node = document.createElement("span");
      node.className = `relationship-stage-node ${item.state || "locked"}`;
      node.textContent = item.label || item.id || "-";
      node.title = item.state === "current" ? "当前阶段" : item.state === "reached" ? "已达成" : "未解锁";
      return node;
    }));
  }
  if (dom.devRelationshipRequirements) {
    const requirements = nextStage && Array.isArray(nextStage.requirements) ? nextStage.requirements : [];
    dom.devRelationshipRequirements.replaceChildren(...requirements.map(item => {
      const row = document.createElement("div");
      row.className = `relationship-requirement ${item.met ? "met" : "pending"}`;
      const label = document.createElement("span");
      label.textContent = `${item.met ? "✓" : "○"} ${item.label}`;
      const meter = document.createElement("i");
      meter.style.setProperty("--value", `${(clamp(toNumber(item.progress, 0), 0, 1) * 100).toFixed(1)}%`);
      const value = document.createElement("code");
      value.textContent = `${item.current} / ${item.required}`;
      value.title = item.met ? "已满足" : `还差 ${item.remaining}`;
      row.append(label, meter, value);
      return row;
    }));
  }
  if (dom.devRelationshipExplanation) {
    dom.devRelationshipExplanation.textContent = developerView.metricExplanation ||
      "关系阶段采用多条件门槛制，不由单个数值决定。";
  }
  if (dom.devRelationshipDetail) {
    dom.devRelationshipDetail.textContent = formatDeveloperJson({
      interactionCount: relationship.interactionCount || 0,
      positiveEvents: relationship.positiveEvents || 0,
      sharedRoutineEvents: relationship.sharedRoutineEvents || 0,
      repairEvents: relationship.repairEvents || 0,
      lastEmotionalEvent: relationship.lastEmotionalEvent || "",
      milestones: relationship.milestones || [],
      nextStage: developerView.nextStage || null,
      visualReactionProfile: developerView.visualReactionProfile || "",
      updatedAt: relationship.updatedAt || "",
    });
  }
}

function renderDeveloperTouchHit(hit, now = performance.now()) {
  if (!DEV_MODE || !hit) return;
  lastDeveloperTouchAt = now;
  if (dom.devTouchHit) {
    dom.devTouchHit.textContent = [
      `命中：${TOUCH_ZONE_LABELS[hit.zone] || hit.zone}`,
      `模型归一化坐标：x=${toNumber(hit.x, 0).toFixed(4)}, y=${toNumber(hit.y, 0).toFixed(4)}`,
      `屏幕坐标：x=${toNumber(hit.screenX, 0).toFixed(1)}, y=${toNumber(hit.screenY, 0).toFixed(1)}`,
      hit.zone === "body" ? "结果：不向 Agent 发送触摸事件" : "结果：进入触摸事件防抖与边界判定",
    ].join("\n");
  }
  document.querySelectorAll("[data-touch-region]").forEach(node => {
    node.classList.toggle("active", node.dataset.touchRegion === hit.zone);
  });
}

function updateDeveloperTouchOverlay() {
  if (!DEV_MODE || !model || !dom.devTouchOverlay) return;
  let bounds;
  try {
    bounds = model.getBounds();
  } catch {
    return;
  }
  if (!bounds || bounds.width <= 0 || bounds.height <= 0) return;
  const touchBounds = normalizedTouchBounds();
  Object.assign(dom.devTouchOverlay.style, {
    left: `${bounds.x + bounds.width * touchBounds.x}px`,
    top: `${bounds.y + bounds.height * touchBounds.y}px`,
    width: `${bounds.width * touchBounds.width}px`,
    height: `${bounds.height * touchBounds.height}px`,
  });
  if (lastDeveloperTouchAt && performance.now() - lastDeveloperTouchAt > 1100) {
    document.querySelectorAll("[data-touch-region].active").forEach(node => node.classList.remove("active"));
    lastDeveloperTouchAt = 0;
  }
}

function renderDeveloperTouchDebug(data) {
  if (!DEV_MODE) return;
  const debug = data?.debugInteraction || {};
  if (dom.devTouchEvent) dom.devTouchEvent.textContent = formatDeveloperJson(debug.normalizedEvent || {});
  if (dom.devTouchPrompt) {
    dom.devTouchPrompt.textContent = debug.upstreamPayload
      ? [
          "[发往大模型的完整请求体，Authorization/API key 已由服务端隔离]",
          formatDeveloperJson(debug.upstreamPayload),
          "",
          "[其中的触摸隐藏提示]",
          String(debug.hiddenPrompt || "-"),
        ].join("\n")
      : [
          "[当前使用本地 fallback，没有向远端模型发送请求]",
          "",
          String(debug.hiddenPrompt || "服务端未返回隐藏提示"),
        ].join("\n");
  }
  if (dom.devTouchResult) {
    const relationshipDelta = relationshipDeltaForDeveloper(
      debug.relationshipBefore || {},
      debug.relationshipAfter || {}
    );
    dom.devTouchResult.textContent = [
      "[模型原始输出]",
      typeof debug.rawModelOutput === "string" ? debug.rawModelOutput : formatDeveloperJson(debug.rawModelOutput || {}),
      "",
      "[服务端规范化结果]",
      formatDeveloperJson(debug.normalizedResult || data || {}),
      "",
      "[本次关系状态变化]",
      formatDeveloperJson(relationshipDelta),
      "",
      "[性能指标]",
      formatDeveloperJson({
        totalLatencyMs: data?.latencyMs ?? null,
        ...(debug.metrics || data?.touchMetrics || {}),
      }),
    ].join("\n");
  }
  if (debug.relationshipAfter) renderDeveloperRelationship(debug.relationshipAfter);
}

function relationshipDeltaForDeveloper(before, after) {
  const result = {};
  ["familiarity", "trust", "teasingLevel", "careLevel", "stability"].forEach(key => {
    const oldValue = toNumber(before[key], 0);
    const newValue = toNumber(after[key], oldValue);
    result[key] = {
      before: Number(oldValue.toFixed(4)),
      after: Number(newValue.toFixed(4)),
      delta: Number((newValue - oldValue).toFixed(4)),
    };
  });
  result.stage = `${before.stage || "unknown"} -> ${after.stage || before.stage || "unknown"}`;
  return result;
}

function formatDeveloperJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "-");
  }
}

function collectVoiceSettings() {
  const language = speechLanguageProfile(dom.speechLanguageInput.value);
  return {
    voiceEnabled: dom.voiceEnabledInput.checked,
    lipSyncEnabled: dom.lipSyncEnabledInput.checked,
    autoStartGptSovits: dom.autoStartGptSovitsInput.checked,
    alignSpokenJa: dom.alignSpokenJaInput.checked,
    ttsDeliveryPostProcessingEnabled: dom.ttsDeliveryPostProcessingEnabledInput.checked,
    ttsProvider: dom.ttsProviderInput.value,
    ttsEndpoint: dom.ttsEndpointInput.value.trim(),
    ttsSpeaker: dom.ttsSpeakerInput.value.trim() || "1",
    speechLanguage: language.speechLanguage,
    speechProsody: normalizeSpeechProsody(dom.speechProsodyInput.value),
    voiceLocale: language.locale,
    speechRate: toNumber(dom.speechRateInput.value, settings.speechRate),
    voicePitch: settings.voicePitch,
    gptSovitsPort: toNumber(dom.gptSovitsPortInput.value, settings.gptSovitsPort),
    gptSovitsCondaEnv: dom.gptSovitsCondaEnvInput.value.trim(),
    gptSovitsRoot: dom.gptSovitsRootInput.value.trim(),
    gptSovitsModelPreset: dom.gptSovitsPresetInput.value,
    gptSovitsGptModel: dom.gptSovitsGptModelInput.value.trim(),
    gptSovitsSovitsModel: dom.gptSovitsSovitsModelInput.value.trim(),
    gptSovitsRefAudio: dom.gptSovitsRefAudioInput.value.trim(),
    gptSovitsRefText: dom.gptSovitsRefTextInput.value.trim(),
    gptSovitsStyleMap: dom.gptSovitsStyleMapInput.value.trim(),
    gptSovitsTargetLanguage: language.targetLanguage,
  };
}

function collectPermissionsSettings() {
  return {
    permissionReadScreen: dom.permissionReadScreenInput.checked,
    permissionReadCamera: dom.permissionReadCameraInput.checked,
    cameraIndex: clamp(toNumber(dom.cameraIndexInput.value, settings.cameraIndex), 0, 8),
    permissionReadSystemTime: dom.permissionReadSystemTimeInput.checked,
    permissionWebSearch: dom.permissionWebSearchInput.checked,
    permissionFileRead: dom.permissionFileReadInput.checked,
    permissionFileWrite: dom.permissionFileWriteInput.checked,
    permissionOpenBrowser: dom.permissionOpenBrowserInput.checked,
    agentFileWorkspace: dom.agentFileWorkspaceInput.value.trim() || settings.agentFileWorkspace,
    webSearchProvider: dom.webSearchProviderInput.value,
    webSearchApiKey: dom.webSearchApiKeyInput.value.trim(),
    webSearchMaxResults: clamp(toNumber(dom.webSearchMaxResultsInput.value, settings.webSearchMaxResults), 1, 8),
    webReadTopPages: clamp(toNumber(dom.webReadTopPagesInput.value, settings.webReadTopPages), 0, 3),
  };
}

async function savePermissionsSettings() {
  try {
    const update = collectPermissionsSettings();
    await saveSettings(update, "权限设置已保存");
    dom.permissionReadScreenInput.checked = settings.permissionReadScreen;
    dom.permissionReadCameraInput.checked = settings.permissionReadCamera;
    dom.cameraIndexInput.value = settings.cameraIndex;
    dom.permissionReadSystemTimeInput.checked = settings.permissionReadSystemTime;
    dom.permissionWebSearchInput.checked = settings.permissionWebSearch;
    dom.permissionFileReadInput.checked = settings.permissionFileRead;
    dom.permissionFileWriteInput.checked = settings.permissionFileWrite;
    dom.permissionOpenBrowserInput.checked = settings.permissionOpenBrowser;
    dom.agentFileWorkspaceInput.value = settings.agentFileWorkspace;
    dom.webSearchApiKeyInput.value = "";
    dom.webSearchStatus.textContent = settings.permissionWebSearch
      ? `${settings.webSearchProvider === "brave" ? "Brave" : "DuckDuckGo"} 已授权` : "权限关闭";
  } catch (error) {
    fillSettingsForm();
    toast(`权限设置保存失败：${error.message}`);
  }
}

async function testWebSearchConnection() {
  try {
    await saveSettings(collectPermissionsSettings(), "联网设置已保存");
    dom.webSearchStatus.textContent = "正在测试搜索和正文读取…";
    const response = await fetch("/api/test-web-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    dom.webSearchStatus.textContent = data.message || "在线搜索正常";
    toast("在线搜索与正文读取正常");
  } catch (error) {
    dom.webSearchStatus.textContent = `测试失败：${error.message}`;
    toast(`联网测试失败：${error.message}`);
  }
}

function loadSelectedVoicePresetDraft() {
  const presetId = dom.gptSovitsPresetInput.value;
  updateVoicePresetHint();
  if (presetId === "custom") return;
  const preset = settings.voicePresets?.[presetId];
  if (!preset) return;
  dom.gptSovitsGptModelInput.value = preset.gptModel || "";
  dom.gptSovitsSovitsModelInput.value = preset.sovitsModel || "";
  dom.gptSovitsRefAudioInput.value = preset.refAudio || "";
  dom.gptSovitsRefTextInput.value = preset.refText || "";
  dom.gptSovitsStyleMapInput.value = preset.styleMap || "";
  const profile = speechLanguageProfile(preset.speechLanguage || "ja-JP");
  dom.speechLanguageInput.value = profile.speechLanguage;
  dom.voiceLocaleInput.value = profile.locale;
  dom.gptSovitsTargetLanguageInput.value = profile.targetLanguage;
  dom.alignSpokenJaInput.disabled = profile.speechLanguage !== "ja-JP";
  dom.speechProsodyInput.value = normalizeSpeechProsody(preset.speechProsody || settings.speechProsody);
  if (Object.prototype.hasOwnProperty.call(preset, "deliveryPostProcessingEnabled")) {
    dom.ttsDeliveryPostProcessingEnabledInput.checked = Boolean(preset.deliveryPostProcessingEnabled);
  }
  toast("预设参数已载入表单，尚未保存，也尚未切换正在运行的模型");
}

function updateGptSovitsStatus(status) {
  if (!dom.gptSovitsStatus) return;
  if (!status) {
    dom.gptSovitsStatus.textContent = "状态未知";
    return;
  }
  if (Array.isArray(status.missing) && status.missing.length) {
    dom.gptSovitsStatus.textContent = `缺少 ${status.missing.length} 个文件`;
    return;
  }
  if (status.starting) {
    dom.gptSovitsStatus.textContent = `启动中 / ${status.endpoint || ""}`;
    return;
  }
  if (status.running) {
    const criticalWarmup = status.criticalWarmup || {};
    const routes = Array.isArray(criticalWarmup.routes)
      ? criticalWarmup.routes.filter(Boolean)
      : [];
    const warmupLabel = criticalWarmup.ready
      ? ` / 已预热 ${routes.join("、") || "默认路由"}`
      : "";
    dom.gptSovitsStatus.textContent = `在线 / ${status.endpoint || ""}${warmupLabel}`;
    return;
  }
  dom.gptSovitsStatus.textContent = `离线 / ${status.endpoint || ""}`;
}

async function refreshGptSovitsStatus() {
  const response = await fetch("/api/gpt-sovits/status");
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  settings = normalizeSettings({ ...settings, gptSovitsStatus: data.status });
  updateGptSovitsStatus(data.status);
  return data.status;
}

async function startGptSovits(options = {}) {
  const silent = Boolean(options.silent);
  const oldText = dom.startGptSovitsBtn.textContent;
  try {
    dom.startGptSovitsBtn.disabled = true;
    dom.startGptSovitsBtn.textContent = "正在保存并切换...";
    updateGptSovitsStatus({ running: false, endpoint: `http://127.0.0.1:${dom.gptSovitsPortInput.value}/tts` });
    const response = await fetch("/api/gpt-sovits/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: collectVoiceSettings() }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      const missing = data.status?.missing?.length ? `：${data.status.missing[0]}` : "";
      throw new Error((data.status?.error || data.error || `HTTP ${response.status}`) + missing);
    }
    settings = normalizeSettings(data.settings);
    fillSettingsForm();
    updateGptSovitsStatus(data.status);
    if (data.status?.running) {
      if (!silent) toast("语音设置已保存，GPT-SoVITS 已按当前表单启动");
    } else {
      if (!silent) toast("语音设置已保存，GPT-SoVITS 正在按当前表单启动");
      return await pollGptSovitsUntilReady();
    }
    return data.status;
  } catch (error) {
    if (!silent) toast(`声音服务启动失败：${error.message}`);
    refreshGptSovitsStatus().catch(() => {});
    return { running: false, error: error.message };
  } finally {
    dom.startGptSovitsBtn.disabled = false;
    dom.startGptSovitsBtn.textContent = oldText;
  }
}

async function ensureGptSovitsStarted() {
  if (settings.ttsProvider !== "gpt-sovits" || !settings.autoStartGptSovits) return { running: false, disabled: true };
  try {
    const status = await refreshGptSovitsStatus();
    if (status.running) return status;
    if (status.starting) return await pollGptSovitsUntilReady();
    return await startGptSovits({ silent: true });
  } catch {
    return await startGptSovits({ silent: true });
  }
}

async function pollGptSovitsUntilReady() {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    await delay(1200);
    const status = await refreshGptSovitsStatus();
    if (status.running) {
      toast("GPT-SoVITS 已启动");
      return status;
    }
    if (!status.starting) {
      toast("声音服务未能保持启动，请查看日志");
      return status;
    }
  }
  toast("声音服务仍在启动中");
  return { running: false, starting: true, timeout: true };
}

async function stopGptSovits() {
  try {
    const response = await fetch("/api/gpt-sovits/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    settings = normalizeSettings(data.settings);
    updateGptSovitsStatus(data.status);
    toast(settings.autoStartGptSovits
      ? "当前语音服务已停止；自动启动开启时，下次发声会重新启动"
      : "当前语音服务已停止");
  } catch (error) {
    toast(`声音服务停止失败：${error.message}`);
  }
}

async function testRunningGptSovits() {
  try {
    if (settings.ttsProvider !== "gpt-sovits") {
      toast("当前已保存的语音提供器不是 GPT-SoVITS；请先保存并启动模型");
      return;
    }
    const status = await refreshGptSovitsStatus();
    if (!status.running) {
      toast("当前没有正在运行的 GPT-SoVITS 模型；试听不会自动启动服务");
      return;
    }
    const sample = normalizeSpeechLanguage(settings.speechLanguage) === "zh-CN"
      ? "这是当前已经运行的语音模型试听。请检查发音、停顿和句尾是否自然。"
      : "これは、現在動いている音声モデルのテストです。発音と間、語尾の揺れを確認して。";
    startSpeech(sample, "calm");
  } catch (error) {
    toast(`试听当前语音模型失败：${error.message}`);
  }
}

async function loadMemory() {
  try {
    const response = await storageScopedFetch("/api/memory");
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.memories = unique(data.memories || []).slice(0, MAX_MEMORIES);
    state.memoryRecords = Array.isArray(data.memoryRecords) ? data.memoryRecords : [];
    settings.memoryFile = data.memoryFile || settings.memoryFile;
    settings.memoryFileAbsolute = data.memoryFileAbsolute || settings.memoryFileAbsolute;
    refreshMemoryEditor(true);
    persist();
    toast("记忆已加载");
  } catch (error) {
    console.warn(error);
    state.memories = state.memories.length ? state.memories : DEFAULT_MEMORIES.slice();
    refreshMemoryEditor(true);
    toast(`记忆加载失败：${error.message}`);
  }
}

async function saveMemory() {
  try {
    const memoryFile = dom.memoryFileInput?.value.trim()
      || settings.memoryFile
      || DEFAULT_SETTINGS.memoryFile;
    const memories = parseMemoryEditor();
    const response = await storageScopedFetch("/api/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ memoryFile, memories }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.memories = unique(data.memories || memories).slice(0, MAX_MEMORIES);
    state.memoryRecords = Array.isArray(data.memoryRecords) ? data.memoryRecords : [];
    settings.memoryFile = data.memoryFile || memoryFile;
    settings.memoryFileAbsolute = data.memoryFileAbsolute || settings.memoryFileAbsolute;
    refreshMemoryEditor(true);
    persist();
    toast("记忆已保存");
  } catch (error) {
    toast(`记忆保存失败：${error.message}`);
  }
}

async function generateMemory() {
  const oldText = dom.generateMemoryBtn.textContent;
  dom.generateMemoryBtn.disabled = true;
  dom.generateMemoryBtn.textContent = "整理中";
  try {
    const response = await storageScopedFetch("/api/memory/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: state.messages.slice(-40),
        notes: dom.memoryEditor.value,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.memories = unique(data.memories || []).slice(0, MAX_MEMORIES);
    state.memoryRecords = Array.isArray(data.memoryRecords) ? data.memoryRecords : [];
    refreshMemoryEditor(true);
    persist();
    toast("记忆已整理");
  } catch (error) {
    toast(`整理失败：${error.message}`);
  } finally {
    dom.generateMemoryBtn.disabled = false;
    dom.generateMemoryBtn.textContent = oldText;
  }
}

async function clearMemory() {
  if (!window.confirm("确定清除长期记忆吗？这会清空当前记忆文件。")) return;
  const oldText = dom.clearMemoryBtn.textContent;
  dom.clearMemoryBtn.disabled = true;
  dom.clearMemoryBtn.textContent = "清除中";
  try {
    const response = await storageScopedFetch("/api/memory/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.memories = [];
    state.memoryRecords = Array.isArray(data.memoryRecords) ? data.memoryRecords : [];
    refreshMemoryEditor(true);
    updateConnectionStatus({ source: settings.modelConfigured ? "api" : "local", memories: [] });
    persist();
    toast("长期记忆已清除");
  } catch (error) {
    toast(`清除记忆失败：${error.message}`);
  } finally {
    dom.clearMemoryBtn.disabled = false;
    dom.clearMemoryBtn.textContent = oldText;
  }
}

async function clearShortMemory() {
  if (!window.confirm("确定清除短期记忆吗？旧聊天仍会显示，但不会再发送给模型；长期记忆和关系状态不受影响。")) return;
  dom.clearShortMemoryBtn.disabled = true;
  const oldText = dom.clearShortMemoryBtn.textContent;
  dom.clearShortMemoryBtn.textContent = "清除中";
  try {
    chatAbortController?.abort();
    chatAbortController = null;
    chatRequestGeneration += 1;
    chatRequestInFlight = false;
    activeChatRequestId = "";
    activeAnswerRequestId = "";
    supersededChatRequestId = "";
    activeConversationInputs = [];
    activeConversationAttachments = [];
    activeConversationGroup = "";
    activeConversationReplyAnchor = null;
    activeConversationUserMessageId = "";
    conversationWasInterrupted = false;
    clearPendingReplyAnchor();
    deferredTouchAssistant = null;
    proactiveState.returnContext = "";
    resetProactiveSequence();
    interruptCurrentSpeech();
    if (touchRequestAbortController) touchRequestAbortController.abort();
    touchRequestAbortController = null;
    touchRequestGeneration += 1;
    touchState.requestInFlight = false;
    proactivePersistenceInFlight.clear();
    const response = await storageScopedFetch("/api/conversation/short-memory/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.contextEpoch = Math.max(
      0,
      Number(data.context?.conversationEpoch ?? state.contextEpoch) || 0,
    );
    state.conversationId = String(data.context?.conversationId || `conv_${state.contextEpoch}`);
    applyProactiveControl(data.context?.proactiveControl, { persistState: false });
    persist();
    toast("短期记忆已清除；长期记忆与关系状态已保留");
  } catch (error) {
    toast(`清除短期记忆失败：${error.message}`);
  } finally {
    dom.clearShortMemoryBtn.disabled = false;
    dom.clearShortMemoryBtn.textContent = oldText;
  }
}

function resetActiveConversationRequestState() {
  activeConversationInputs = [];
  activeConversationAttachments = [];
  activeConversationGroup = "";
  activeConversationReplyAnchor = null;
  activeConversationUserMessageId = "";
  conversationWasInterrupted = false;
}

function prepareForConversationNavigation() {
  const requestId = String(activeChatRequestId || activeAnswerRequestId || "").trim();
  const stoppedGeneration = chatRequestGeneration;
  chatAbortController?.abort();
  chatAbortController = null;
  chatRequestGeneration += 1;
  chatRequestInFlight = false;
  stopThinkingProgressPolling(requestId);
  hideThinkingIndicator(stoppedGeneration);
  notifyBackendAnswerCancellation(requestId);
  activeChatRequestId = "";
  activeAnswerRequestId = "";
  supersededChatRequestId = "";
  resetActiveConversationRequestState();
  clearPendingReplyAnchor();
  deferredTouchAssistant = null;
  proactiveState.returnContext = "";
  resetProactiveSequence();
  interruptCurrentSpeech();
  syncChatSubmitButton();
}

async function startNewConversation() {
  dom.newConversationBtn.disabled = true;
  if (dom.conversationCreateBtn) dom.conversationCreateBtn.disabled = true;
  try {
    prepareForConversationNavigation();
    const response = await storageScopedFetch("/api/conversation/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.contextEpoch = Math.max(0, Number(data.context?.conversationEpoch) || 0);
    state.conversationId = String(data.context?.conversationId || `conv_${state.contextEpoch}`);
    state.messages = [];
    applyProactiveControl(data.context?.proactiveControl, { persistState: false });
    renderMessages();
    persist();
    closeConversationPicker();
    dom.userInput?.focus();
    toast("已开始新对话；长期记忆和关系仍然保留");
    return true;
  } catch (error) {
    toast(`新对话创建失败：${error.message}`);
    return false;
  } finally {
    dom.newConversationBtn.disabled = false;
    if (dom.conversationCreateBtn) dom.conversationCreateBtn.disabled = false;
  }
}

function closeConversationPicker() {
  conversationPickerState.controller?.abort();
  conversationPickerState.controller = null;
  conversationPickerState.loading = false;
  dom.conversationPicker?.classList.add("hidden");
  dom.newConversationBtn?.setAttribute("aria-expanded", "false");
}

function renderConversationPicker() {
  if (!dom.conversationPickerList) return;
  dom.conversationPickerList.replaceChildren();
  if (dom.conversationPickerCount) {
    dom.conversationPickerCount.textContent = `${conversationPickerState.conversations.length} 个`;
  }
  if (!conversationPickerState.conversations.length) {
    const empty = document.createElement("div");
    empty.className = "chat-history-search-empty";
    empty.textContent = "还没有历史对话";
    dom.conversationPickerList.appendChild(empty);
    return;
  }
  conversationPickerState.conversations.forEach(conversation => {
    const conversationId = String(conversation.conversationId || "");
    const active = conversationId === String(state.conversationId || "");
    const row = document.createElement("div");
    row.className = `conversation-picker-row${active ? " active" : ""}`;
    const item = document.createElement("button");
    item.type = "button";
    item.className = "conversation-picker-item";
    item.dataset.conversationId = conversationId;
    if (active) item.setAttribute("aria-current", "true");
    const head = document.createElement("span");
    head.className = "conversation-picker-item-head";
    const title = document.createElement("strong");
    title.className = "conversation-picker-item-title";
    title.textContent = String(conversation.title || "新对话");
    const at = document.createElement("span");
    at.className = "conversation-picker-item-time";
    at.textContent = formatHistorySearchTime(conversation.updatedAt || conversation.createdAt);
    head.append(title, at);
    const preview = document.createElement("span");
    preview.className = "conversation-picker-item-preview";
    preview.textContent = String(conversation.preview || "还没有消息");
    item.append(head, preview);
    item.addEventListener("click", () => switchToConversation(conversationId));
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "conversation-picker-delete-btn";
    deleteButton.textContent = "×";
    deleteButton.title = `删除对话：${String(conversation.title || "新对话")}`;
    deleteButton.setAttribute("aria-label", deleteButton.title);
    deleteButton.addEventListener("click", () => deleteConversation(conversation));
    row.append(item, deleteButton);
    dom.conversationPickerList.appendChild(row);
  });
}

async function loadConversationPicker() {
  conversationPickerState.controller?.abort();
  const controller = new AbortController();
  conversationPickerState.controller = controller;
  conversationPickerState.loading = true;
  if (dom.conversationPickerStatus) dom.conversationPickerStatus.textContent = "正在读取对话…";
  try {
    const response = await storageScopedFetch("/api/conversations", {
      cache: "no-store",
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    conversationPickerState.conversations = Array.isArray(data.conversations)
      ? data.conversations : [];
    renderConversationPicker();
    if (dom.conversationPickerStatus) {
      dom.conversationPickerStatus.textContent = "选择一条旧对话继续，或新建对话";
    }
  } catch (error) {
    if (error?.name === "AbortError") return;
    if (dom.conversationPickerStatus) dom.conversationPickerStatus.textContent = "对话列表读取失败";
    toast(`对话列表读取失败：${error.message}`);
  } finally {
    if (conversationPickerState.controller === controller) {
      conversationPickerState.controller = null;
      conversationPickerState.loading = false;
    }
  }
}

function openConversationPicker() {
  if (!dom.settingsModal?.classList.contains("hidden")) closeSettings();
  setChatCollapsed(false, { scroll: false });
  closeBookmarkPanel();
  closeChatHistorySearch();
  dom.conversationPicker?.classList.remove("hidden");
  dom.newConversationBtn?.setAttribute("aria-expanded", "true");
  loadConversationPicker();
}

async function deleteConversation(conversation) {
  const requestedId = String(conversation?.conversationId || "").trim();
  if (!requestedId) return;
  const title = String(conversation?.title || "新对话").trim() || "新对话";
  if (!window.confirm(`确定删除对话“${title}”吗？\n删除后无法恢复。`)) return;
  const active = requestedId === String(state.conversationId || "");
  if (dom.conversationPickerStatus) dom.conversationPickerStatus.textContent = "正在删除对话…";
  try {
    if (active) prepareForConversationNavigation();
    const response = await storageScopedFetch("/api/conversation/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversationId: requestedId }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    if (data.activeDeleted) {
      state.contextEpoch = Math.max(0, Number(data.context?.conversationEpoch) || 0);
      state.conversationId = String(
        data.context?.conversationId || data.activeConversationId || `conv_${state.contextEpoch}`,
      );
      state.messages = [];
      applyProactiveControl(data.context?.proactiveControl, { persistState: false });
      resetActiveConversationRequestState();
      clearPendingReplyAnchor();
      renderMessages();
      persist();
    }
    conversationPickerState.conversations = Array.isArray(data.conversations)
      ? data.conversations : [];
    renderConversationPicker();
    if (dom.conversationPickerStatus) {
      dom.conversationPickerStatus.textContent = data.activeDeleted
        ? "当前对话已删除，已进入新的空白对话"
        : "对话已删除";
    }
    toast(data.activeDeleted ? "当前对话已删除，已开始新对话" : "对话已删除");
  } catch (error) {
    if (dom.conversationPickerStatus) dom.conversationPickerStatus.textContent = "删除失败，请重试";
    toast(`删除对话失败：${error.message}`);
  }
}

async function switchToConversation(conversationId) {
  const requestedId = String(conversationId || "").trim();
  if (!requestedId) return;
  if (requestedId === String(state.conversationId || "")) {
    closeConversationPicker();
    dom.userInput?.focus();
    return;
  }
  dom.newConversationBtn.disabled = true;
  if (dom.conversationCreateBtn) dom.conversationCreateBtn.disabled = true;
  if (dom.conversationPickerStatus) dom.conversationPickerStatus.textContent = "正在打开对话…";
  try {
    prepareForConversationNavigation();
    const response = await storageScopedFetch("/api/conversation/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversationId: requestedId }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.contextEpoch = Math.max(0, Number(data.context?.conversationEpoch) || 0);
    state.conversationId = String(data.context?.conversationId || requestedId);
    state.messages = [];
    applyProactiveControl(data.context?.proactiveControl, { persistState: false });
    await hydrateConversationHistory();
    renderMessages();
    persist();
    closeConversationPicker();
    dom.userInput?.focus();
    toast("已打开历史对话，可以接着聊");
  } catch (error) {
    toast(`打开对话失败：${error.message}`);
    if (dom.conversationPickerStatus) dom.conversationPickerStatus.textContent = "打开失败，请重试";
  } finally {
    dom.newConversationBtn.disabled = false;
    if (dom.conversationCreateBtn) dom.conversationCreateBtn.disabled = false;
  }
}

function createClientMessageId() {
  if (globalThis.crypto?.randomUUID) return `msg_${globalThis.crypto.randomUUID()}`;
  return `msg_${Date.now()}_${Math.random().toString(36).slice(2, 12)}`;
}

function renderPendingReplyAnchor() {
  if (!dom.replyAnchorPreview) return;
  const visible = Boolean(pendingReplyAnchor?.targetMessageIds?.length);
  dom.replyAnchorPreview.classList.toggle("hidden", !visible);
  if (!visible) return;
  if (dom.replyAnchorLabel) {
    dom.replyAnchorLabel.textContent = pendingReplyAnchor.targetRole === "assistant"
      ? "回复 AMADEUS" : "回复自己";
  }
  if (dom.replyAnchorText) {
    dom.replyAnchorText.textContent = String(pendingReplyAnchor.quotedPreview || "").slice(0, 280);
  }
}

function clearPendingReplyAnchor() {
  pendingReplyAnchor = null;
  renderPendingReplyAnchor();
}

function replyToMessage(role, content, metaData = {}) {
  const messageId = String(metaData.messageId || "").trim();
  if (!messageId || !metaData.turnId || !metaData.contentHash || metaData.source === "local-recovery") {
    // Older touch bubbles and failed/in-flight messages may exist only in
    // this window. Quote their text instead of sending an unresolved ID.
    clearPendingReplyAnchor();
    const quote = String(content || "").trim().split(/\r?\n/).map(line => `> ${line}`).join("\n");
    if (dom.userInput) {
      dom.userInput.value = `${quote}\n\n${dom.userInput.value || ""}`;
      dom.userInput.dispatchEvent(new Event("input", { bubbles: true }));
      dom.userInput.focus();
    }
    return;
  }
  pendingReplyAnchor = {
    anchorId: `anchor_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`,
    type: "message",
    targetMessageIds: [messageId],
    targetContentHashes: metaData.contentHash ? [String(metaData.contentHash)] : [],
    quotedPreview: String(content || "").trim().slice(0, 280),
    targetRole: role === "assistant" ? "assistant" : "user",
  };
  renderPendingReplyAnchor();
  dom.userInput?.focus();
}

function scrollToQuotedMessage(messageId) {
  const target = [...(dom.chatLog?.querySelectorAll(".msg") || [])].find(
    item => String(item.dataset.messageId || "") === String(messageId || ""),
  );
  if (!target) {
    toast("被回复的消息不在当前窗口中");
    return;
  }
  target.classList.add("history-search-hit");
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  window.setTimeout(() => target.classList.remove("history-search-hit"), 1800);
}

function messageIdentityMeta(result, role) {
  const identity = result?.turnIdentity;
  if (!identity || typeof identity !== "object") return {};
  const assistant = role === "assistant";
  return {
    conversationId: String(identity.conversationId || result.conversationId || ""),
    turnId: String(identity.turnId || ""),
    messageId: String(assistant ? identity.assistantMessageId : identity.userMessageId),
    contentHash: String(assistant ? identity.assistantContentHash : identity.userContentHash),
  };
}

function bindCompletedTurnIdentity(result, assistantMessage = null) {
  const userIdentity = messageIdentityMeta(result, "user");
  const assistantIdentity = messageIdentityMeta(result, "assistant");
  if (userIdentity.messageId) {
    const userMessage = [...state.messages].reverse().find(message =>
      message?.role === "user"
      && String(message.meta?.messageId || "") === String(result?.currentUserMessageId || userIdentity.messageId),
    );
    if (userMessage?.meta) Object.assign(userMessage.meta, userIdentity);
  }
  if (assistantMessage?.meta && assistantIdentity.messageId) {
    Object.assign(assistantMessage.meta, assistantIdentity);
  }
  if (result?.conversationId) state.conversationId = String(result.conversationId);
}

async function clearChat() {
  if (!window.confirm("确定清除当前对话吗？长期记忆不会被删除。")) return;
  chatAbortController?.abort();
  chatAbortController = null;
  chatRequestGeneration += 1;
  chatRequestInFlight = false;
  activeChatRequestId = "";
  activeAnswerRequestId = "";
  supersededChatRequestId = "";
  resetActiveConversationRequestState();
  clearPendingReplyAnchor();
  deferredTouchAssistant = null;
  proactiveState.returnContext = "";
  resetProactiveSequence();
  if (touchRequestAbortController) touchRequestAbortController.abort();
  touchRequestAbortController = null;
  touchRequestGeneration += 1;
  touchState.requestInFlight = false;
  proactivePersistenceInFlight.clear();
  interruptCurrentSpeech();
  try {
    const response = await storageScopedFetch("/api/conversation/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    state.messages = [];
    state.contextEpoch = Math.max(
      0,
      Number(data.context?.conversationEpoch ?? state.contextEpoch) || 0,
    );
    state.conversationId = String(data.context?.conversationId || `conv_${state.contextEpoch}`);
    applyProactiveControl(data.context?.proactiveControl, { persistState: false });
    renderMessages();
    persist();
    toast("对话与会话摘要已清除");
  } catch (error) {
    toast(`清除对话失败：${error.message}`);
  }
}

async function chooseMemoryFile() {
  if (DEV_MODE) {
    toast("开发者模式使用独立记忆文件，不能改指向普通模式的记忆文件");
    return;
  }
  if (!window.pywebview?.api?.chooseMemoryFile) {
    toast("当前模式不能打开文件选择器");
    return;
  }
  try {
    const result = await window.pywebview.api.chooseMemoryFile();
    if (result?.ok && result.path) {
      if (dom.memoryFileInput) dom.memoryFileInput.value = result.path;
      settings.memoryFile = result.path;
      await saveMemory();
    }
  } catch (error) {
    toast(`选择失败：${error.message}`);
  }
}

function refreshMemoryEditor(force = false) {
  if (!dom.memoryEditor) return;
  const editing = document.activeElement === dom.memoryEditor;
  if (force || !editing) {
    dom.memoryEditor.value = unique(state.memories).join("\n");
  }
  if (dom.memoryFileHint) {
    dom.memoryFileHint.textContent = settings.memoryFileAbsolute || settings.memoryFile || "data/memory.json";
  }
  if (dom.memoryFileInput) {
    dom.memoryFileInput.value = settings.memoryFile || DEFAULT_SETTINGS.memoryFile;
  }
  if (dom.memoryRecordDetails) {
    const records = Array.isArray(state.memoryRecords) ? state.memoryRecords : [];
    dom.memoryRecordDetails.textContent = records.length
      ? records.map(record => {
          const active = record?.active !== false;
          const superseded = String(record?.supersededBy || "");
          const status = active ? "当前" : superseded.startsWith("deleted:") ? "已删除" : "旧版本";
          const sources = Array.isArray(record?.sourceTurnIds) && record.sourceTurnIds.length
            ? record.sourceTurnIds.join(", ")
            : "无";
          const relation = superseded ? `；替代：${superseded}` : "";
          return `[${status} v${Number(record?.version) || 1}] ${String(record?.content || "")}\n`
            + `类型：${String(record?.memoryType || "user_memory")}；来源：${sources}；创建：${String(record?.createdAt || "未知")}；最近确认：${String(record?.lastConfirmedAt || "未知")}${relation}`;
        }).join("\n\n")
      : "尚无版本记录";
  }
}

function parseMemoryEditor() {
  return unique(dom.memoryEditor.value.split(/\r?\n/).map(item => item.trim())).slice(0, MAX_MEMORIES);
}

function formatCacheBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

async function refreshCacheStatus() {
  if (!dom.cacheStatus) return;
  dom.cacheStatus.textContent = "正在统计…";
  try {
    const response = await storageScopedFetch("/api/cache/status");
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    const unused = data.unused || {};
    const allDisposable = data.allDisposable || {};
    const unusedPrefix = unused.measurementComplete === false ? "≥" : "";
    const allPrefix = allDisposable.measurementComplete === false ? "≥" : "";
    dom.cacheStatus.textContent = `无用 ${unusedPrefix}${formatCacheBytes(unused.bytes)} / 全部 ${allPrefix}${formatCacheBytes(allDisposable.bytes)}`;
  } catch (error) {
    dom.cacheStatus.textContent = "统计失败";
    toast(`缓存统计失败：${error.message}`);
  }
}

async function clearRuntimeCache(mode) {
  const all = mode === "all_disposable";
  const message = all
    ? "确定清除全部不影响对话的缓存和日志吗？对话、记忆、关系数据、附件、截图、模型与参考音频都会保留；下次首次启动或首次合成可能稍慢。"
    : "确定清除无用缓存吗？这不会影响对话、记忆、模型或用户文件。";
  if (!window.confirm(message)) return;
  const button = all ? dom.clearAllDisposableCacheBtn : dom.clearUnusedCacheBtn;
  const oldText = button?.textContent || "";
  if (button) {
    button.disabled = true;
    button.textContent = "清理中…";
  }
  try {
    const response = await storageScopedFetch("/api/cache/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      const failed = Array.isArray(data.failedTargets) ? data.failedTargets.length : 0;
      throw new Error(failed ? `${failed} 个正在使用的目标未能清理` : (data.error || `HTTP ${response.status}`));
    }
    const freedPrefix = data.measurementComplete === false ? "至少 " : "";
    toast(`已释放 ${freedPrefix}${formatCacheBytes(data.freedBytes)}；对话和记忆未改动`);
    await refreshCacheStatus();
  } catch (error) {
    toast(`缓存清理失败：${error.message}`);
    await refreshCacheStatus();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText;
    }
  }
}

function openSettings() {
  fillSettingsForm();
  dom.settingsModal.classList.remove("hidden");
  refreshGptSovitsStatus().catch(error => {
    console.warn("GPT-SoVITS status refresh failed:", error);
  });
}

function openFirstRunApiSetupIfNeeded() {
  if (firstRunApiSetupOpened || !settings.releaseMode || settings.apiKeySet) return;
  firstRunApiSetupOpened = true;
  openSettings();
  activateSettingsTab("api");
  window.setTimeout(() => dom.apiKeyInput?.focus(), 0);
  toast("首次使用只需填写 API Key，其他设置已经就绪");
}

function closeSettings() {
  closeSalieriWindow();
  dom.settingsModal.classList.add("hidden");
}

function closeSalieriWindow() {
  dom.salieriWindow?.classList.add("hidden");
  dom.salieriWindowToggle?.setAttribute("aria-expanded", "false");
}

function activateSettingsTab(tab) {
  if (tab !== "memory") closeSalieriWindow();
  dom.settingsTabs.forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  dom.tabPages.forEach(page => page.classList.toggle("active", page.dataset.page === tab));
}

function fitModel() {
  if (!model) return;
  drawPixiBackground();
  const w = window.innerWidth;
  const h = window.innerHeight;
  const natural = modelNaturalBounds();
  const display = modelDisplayBounds(natural);
  const transform = modelTransformFor(activeModelProfile?.id);
  if (
    modelFitReference.layoutMode !== settings.layoutMode
    || modelFitReference.width <= 0
    || modelFitReference.height <= 0
  ) {
    modelFitReference = { width: w, height: h, layoutMode: settings.layoutMode };
  }
  const offsetScaleX = w / Math.max(1, modelFitReference.width);
  const offsetScaleY = h / Math.max(1, modelFitReference.height);
  const offsetX = (transform.offsetX + toNumber(activeModelProfile?.offsetX, 0)) * offsetScaleX;
  const offsetY = (transform.offsetY + toNumber(activeModelProfile?.offsetY, 0)) * offsetScaleY;
  if (settings.layoutMode === "full") {
    const chatWidth = Math.min(460, Math.max(380, w * 0.38));
    const videoWidth = Math.max(360, w - chatWidth);
    const portraitHeight = h * 1.12;
    const baseScale = Math.min((videoWidth * 0.86) / display.width, portraitHeight / display.height);
    const profileScale = clamp(toNumber(activeModelProfile?.fullScaleMultiplier, activeModelProfile?.scaleMultiplier || 1), 0.5, 2);
    const scale = clamp(baseScale * transform.scale * profileScale, 0.12, 3.05);
    model.scale.set(scale);
    const scaledWidth = display.width * scale;
    modelHome = {
      x: (videoWidth - scaledWidth) * 0.5 - display.x * scale + offsetX,
      y: h - (display.y + display.height) * scale + offsetY,
      scale,
    };
    model.x = modelHome.x;
    model.y = modelHome.y;
    return;
  }
  const visibleHeight = h * 1.02;
  const baseScale = Math.min((w * 0.94) / display.width, (visibleHeight / display.height) * 1.08);
  const profileScale = clamp(toNumber(activeModelProfile?.scaleMultiplier, 1), 0.5, 2);
  const scale = clamp(baseScale * transform.scale * profileScale, 0.08, 2.2);
  model.scale.set(scale);
  const scaledWidth = display.width * scale;
  modelHome = {
    x: (w - scaledWidth) * 0.5 - display.x * scale + offsetX,
    y: h - (display.y + display.height) * scale + offsetY,
    scale,
  };
  model.x = modelHome.x;
  model.y = modelHome.y;
}

function modelNaturalBounds() {
  try {
    if (model && typeof model.getLocalBounds === "function") {
      const bounds = model.getLocalBounds();
      if (bounds.width > 0 && bounds.height > 0) {
        return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height };
      }
    }
  } catch {
    // Fall back below when bounds are not ready.
  }
  const scale = model?.scale?.x || 1;
  return {
    x: 0,
    y: 0,
    width: Math.max(1, (model?.width || window.innerWidth) / scale),
    height: Math.max(1, (model?.height || window.innerHeight) / scale),
  };
}

function modelDisplayBounds(natural) {
  const values = activeModelProfile?.displayBounds;
  if (!Array.isArray(values) || values.length !== 4) return natural;
  const x = clamp(toNumber(values[0], 0), 0, 0.99);
  const y = clamp(toNumber(values[1], 0), 0, 0.99);
  const width = clamp(toNumber(values[2], 1), 0.01, 1 - x);
  const height = clamp(toNumber(values[3], 1), 0.01, 1 - y);
  return {
    x: natural.x + natural.width * x,
    y: natural.y + natural.height * y,
    width: natural.width * width,
    height: natural.height * height,
  };
}

function drawPixiBackground() {
  if (!app || !backgroundLayer || !window.PIXI) return;
  const w = window.innerWidth;
  const h = window.innerHeight;
  backgroundLayer.removeChildren();
  const base = new PIXI.Graphics();
  base.beginFill(settings.backgroundPreset === "solid" ? 0x020505 : 0x071011, 1);
  base.drawRect(0, 0, w, h);
  base.endFill();
  backgroundLayer.addChild(base);
  const preset = BACKGROUND_PRESETS.has(settings.backgroundPreset) ? settings.backgroundPreset : "binary";
  if (preset === "binary") {
    drawBinaryBackgroundPanels(w, h);
  } else if (preset === "boot") {
    drawCoverBackgroundImage(BOOT_BACKGROUND_URL, w, h, 0.9);
  } else if (preset === "custom" && settings.customBackgroundPath) {
    drawCoverBackgroundImage(`/api/background-image?v=${backgroundRevision}`, w, h, 1);
  } else if (preset === "grid") {
    drawResearchGridBackground(w, h);
  }
  backgroundLayer.x = 0;
  backgroundLayer.y = 0;
  backgroundLayer.rotation = 0;
}

function drawBinaryBackgroundPanels(w, h) {
  const targetX = 0;
  const targetY = 0;
  const targetW = w;
  const targetH = h;
  const textureW = 626;
  const textureH = 1226;
  const scale = Math.max(targetH / textureH, 0.68);
  const tileW = textureW * scale;
  const tileH = textureH * scale;
  const columns = Math.max(2, Math.ceil(targetW / tileW) + 2);
  const startX = targetX + targetW * 0.5 - (columns * tileW) * 0.5;
  for (let index = 0; index < columns; index += 1) {
    const panel = PIXI.Sprite.from(MODEL_BINARY_BACKGROUND_URL);
    panel.width = tileW;
    panel.height = tileH;
    panel.x = startX + index * tileW;
    panel.y = targetY + (targetH - tileH) * 0.5;
    panel.alpha = 0.82;
    backgroundLayer.addChild(panel);
  }
}

function drawCoverBackgroundImage(url, w, h, alpha = 1) {
  const texture = PIXI.Texture.from(url);
  const sprite = new PIXI.Sprite(texture);
  const fit = () => {
    const textureWidth = texture.orig?.width || texture.baseTexture?.realWidth || texture.width || w;
    const textureHeight = texture.orig?.height || texture.baseTexture?.realHeight || texture.height || h;
    const scale = Math.max(w / Math.max(1, textureWidth), h / Math.max(1, textureHeight));
    sprite.width = textureWidth * scale;
    sprite.height = textureHeight * scale;
    sprite.x = (w - sprite.width) * 0.5;
    sprite.y = (h - sprite.height) * 0.5;
    sprite.alpha = alpha;
  };
  fit();
  if (!texture.valid && texture.baseTexture && typeof texture.baseTexture.once === "function") {
    texture.baseTexture.once("loaded", () => {
      fit();
    });
  }
  backgroundLayer.addChild(sprite);
  const shade = new PIXI.Graphics();
  shade.beginFill(0x020606, 0.18);
  shade.drawRect(0, 0, w, h);
  shade.endFill();
  backgroundLayer.addChild(shade);
}

function drawContainedCroppedBackgroundImage(url, w, h, crop, alpha = 1) {
  const texture = PIXI.Texture.from(url);
  const sprite = new PIXI.Sprite(texture);
  const matte = new PIXI.Graphics();
  const fit = () => {
    const sourceWidth = texture.orig?.width || texture.baseTexture?.width || 1920;
    const sourceHeight = texture.orig?.height || texture.baseTexture?.height || 1080;
    const cropX = crop.x * sourceWidth;
    const cropY = crop.y * sourceHeight;
    const cropWidth = crop.width * sourceWidth;
    const cropHeight = crop.height * sourceHeight;
    const scale = Math.min(w / cropWidth, h / cropHeight);
    const viewportWidth = cropWidth * scale;
    const viewportHeight = cropHeight * scale;
    const viewportX = (w - viewportWidth) * 0.5;
    const viewportY = (h - viewportHeight) * 0.5;
    sprite.width = sourceWidth * scale;
    sprite.height = sourceHeight * scale;
    sprite.x = viewportX - cropX * scale;
    sprite.y = viewportY - cropY * scale;
    sprite.alpha = alpha;
    matte.clear();
    matte.beginFill(0x071011, 1);
    if (viewportY > 0) matte.drawRect(0, 0, w, viewportY + 1);
    if (viewportY + viewportHeight < h) matte.drawRect(0, viewportY + viewportHeight - 1, w, h - viewportY - viewportHeight + 1);
    if (viewportX > 0) matte.drawRect(0, viewportY, viewportX + 1, viewportHeight);
    if (viewportX + viewportWidth < w) matte.drawRect(viewportX + viewportWidth - 1, viewportY, w - viewportX - viewportWidth + 1, viewportHeight);
    matte.endFill();
  };
  fit();
  if (!texture.valid && texture.baseTexture && typeof texture.baseTexture.once === "function") texture.baseTexture.once("loaded", fit);
  backgroundLayer.addChild(sprite);
  backgroundLayer.addChild(matte);
}

function drawResearchGridBackground(w, h) {
  const grid = new PIXI.Graphics();
  grid.lineStyle(1, 0xec922d, 0.08);
  const step = 36;
  for (let x = 0; x <= w; x += step) {
    grid.moveTo(x, 0);
    grid.lineTo(x, h);
  }
  for (let y = 0; y <= h; y += step) {
    grid.moveTo(0, y);
    grid.lineTo(w, y);
  }
  backgroundLayer.addChild(grid);
}

function activeLipSyncProfile() {
  const profile = activeModelProfile?.lipSync || {};
  return {
    openMultiplier: clamp(toNumber(profile.openMultiplier, 1.2), 0.5, 2.2),
    formMultiplier: clamp(toNumber(profile.formMultiplier, 1.12), 0.4, 1.8),
    formOffset: clamp(toNumber(profile.formOffset, 0), -0.5, 0.5),
    openSmoothing: clamp(toNumber(profile.openSmoothing, 0.5), 0.08, 0.8),
    formSmoothing: clamp(toNumber(profile.formSmoothing, 0.34), 0.08, 0.8),
  };
}

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

const DEFAULT_HEAD_TURN_PROFILE = Object.freeze({
  maxYaw: 30,
  auxiliaryMax: 0,
  bodyMax: 2,
  pointerSwitchThreshold: 0.3,
  springFrequency: 4.8,
  springDamping: 0.88,
});

function eyeVisibilityFamily() {
  const visualEmotion = effectiveVisualEmotion();
  if (visualEmotion === "furious") return "coldGlance";
  if (["amused", "laughing", "teasing", "affectionate"].includes(visualEmotion)) return "smile";
  return "default";
}

function activeEyeVisibilityCompensation() {
  const configured = activeModelProfile?.eyeVisibilityCompensation;
  const profile = configured && typeof configured === "object"
    ? configured
    : DEFAULT_EYE_VISIBILITY_COMPENSATION;
  const families = profile.families && typeof profile.families === "object"
    ? profile.families
    : DEFAULT_EYE_VISIBILITY_COMPENSATION.families;
  const requested = families[eyeVisibilityFamily()];
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
    .sort((a, b) => a[0] - b[0]);
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
    profile.closeThreshold
  );
  if (opening <= closeThreshold) return 0;
  return Math.max(profile.minimumVisibleOpening, opening);
}

function activeHeadTurnProfile() {
  const configured = activeModelProfile?.headTurn;
  const profile = configured && typeof configured === "object"
    ? configured
    : DEFAULT_HEAD_TURN_PROFILE;
  return {
    maxYaw: clamp(toNumber(profile.maxYaw, 30), 24, 90),
    auxiliaryMax: clamp(toNumber(profile.auxiliaryMax, 0), 0, 32),
    bodyMax: clamp(toNumber(profile.bodyMax, 2), 0, 5),
    pointerSwitchThreshold: clamp(toNumber(profile.pointerSwitchThreshold, 0.3), 0.12, 0.65),
    springFrequency: clamp(toNumber(profile.springFrequency, 4.8), 1.5, 8),
    springDamping: clamp(toNumber(profile.springDamping, 0.88), 0.55, 1.25),
  };
}

function updateFuriousTurnMotion(delta) {
  const profile = activeHeadTurnProfile();
  if (effectiveVisualEmotion() === "furious") {
    if (pointer.x > profile.pointerSwitchThreshold) furiousTurnTarget = -1;
    else if (pointer.x < -profile.pointerSwitchThreshold) furiousTurnTarget = 1;
  } else {
    furiousTurnTarget = 0;
  }
  const seconds = clamp(toNumber(delta, 1) / 60, 0.001, 0.05);
  const omega = profile.springFrequency;
  const acceleration = (
    (furiousTurnTarget - furiousTurnDirection) * omega * omega
    - 2 * profile.springDamping * omega * furiousTurnVelocity
  );
  furiousTurnVelocity += acceleration * seconds;
  furiousTurnDirection = clamp(
    furiousTurnDirection + furiousTurnVelocity * seconds,
    -1.05,
    1.05
  );
  if (Math.abs(furiousTurnTarget - furiousTurnDirection) < 0.001 && Math.abs(furiousTurnVelocity) < 0.001) {
    furiousTurnDirection = furiousTurnTarget;
    furiousTurnVelocity = 0;
  }
  return profile;
}

function furiousColdGlanceWeight(now = performance.now()) {
  if (effectiveVisualEmotion() !== "furious") return 0;
  const age = Math.max(0, now - emotionEnteredAt);
  if (age < 2400) return 0;
  const phase = (age - 2400) % 8200;
  if (phase > 760) return 0;
  return Math.pow(Math.sin((phase / 760) * Math.PI), 2);
}

function startledPupilScale(now = performance.now()) {
  if (effectiveVisualEmotion() !== "startled") return 0;
  const age = Math.max(0, now - emotionEnteredAt);
  const snapIn = easeOutCubic(clamp(age / 180, 0, 1));
  const visualWeight = visualEmotionIntensityWeight(currentEmotionWeight);
  const intensity = smoothstep(clamp((visualWeight - 0.5) / 1.25, 0, 1));
  // Param8 already carries the author's surprise-eye deformation. This
  // virtual channel is only a secondary accent; a second full contraction
  // made the iris collapse into a dot at ordinary intensity.
  return -(0.08 + intensity * 0.30) * snapIn * emotionLifecycleWeight(now);
}

function updateLive2D(delta) {
  if (!coreModel) return;
  try {
    if (model && typeof model.update === "function") model.update(delta * (1000 / 60));
  } catch (error) {
    console.debug("Live2D native update failed:", error);
  }
  const now = performance.now();
  tickerTime += delta / 60;

  if (now > nextBlink) {
    blinkUntil = now + 105;
    nextBlink = now + 1800 + Math.random() * 3400;
  }

  const blink = now < blinkUntil ? Math.max(0, (blinkUntil - now) / 105) : 0;
  const eyeOpen = 1 - Math.sin(blink * Math.PI);
  const breath = 0.5 + Math.sin(tickerTime * 2.2) * 0.42;
  const idleSway = Math.sin(tickerTime * 0.9);
  const speaking = isSpeaking(now);
  updateEmotionLifecycle(now);
  updateProactiveAmbientMood(now);
  updatePositiveAmbientMood(now);
  updateInteractionCoordinator(now);
  updateProactiveInteraction(now);
  const emotion = blendedEmotionTarget();
  const gesture = now < gestureUntil ? activeGesture : "";
  const motion = gestureMotion(gesture, currentEmotion, now, speaking);
  updateLookTargets(now);
  updateAudioMouthLevel();
  model.x = modelHome.x;
  model.y = modelHome.y;
  model.rotation = 0;
  updateBackgroundMotion(motion, speaking, now);

  const mouthTalk = settings.lipSyncEnabled ? speechMouthOpen(now) : 0;
  const mouthFormSync = settings.lipSyncEnabled ? speechMouthForm(now) : 0;
  const lipSync = activeLipSyncProfile();
  const speakNod = speaking ? Math.sin(tickerTime * 4.2) * 0.28 : 0;
  const shyIntensity = Math.max(
    emotionIntensity("shy", now),
    emotionIntensity("flustered", now),
    emotionIntensity("affectionate", now) * 0.55
  );
  const shyTremble = shyIntensity > 0 ? Math.sin(tickerTime * 8.4) * 0.45 * shyIntensity : 0;
  const shyCheekPulse = shyIntensity > 0
    ? (Math.sin(tickerTime * 3.6) * 0.045 + Math.sin(tickerTime * 1.7) * 0.025) * shyIntensity
    : 0;
  const lifecycle = emotionLifecycleWeight(now);
  const blushLifecycle = blushLifecycleWeight(now);
  const visualEmotion = effectiveVisualEmotion();
  const annotatedExpressionPlan = activeAnnotatedExpressionPlan(
    visualEmotion,
    lifecycle,
  );

  const eyeContact = clamp(0.68 + (toNumber(currentExpressionDetail.eyeContact, 0.68) - 0.68) * lifecycle, 0, 1);
  const gazeX = clamp(toNumber(currentExpressionDetail.gazeX, 0) * lifecycle, -0.6, 0.6);
  const gazeY = clamp(toNumber(currentExpressionDetail.gazeY, 0) * lifecycle, -0.6, 0.6);
  const tension = clamp(toNumber(currentExpressionDetail.tension, 0) * lifecycle, 0, 1);
  const headTurn = updateFuriousTurnMotion(delta);
  const furiousTurnDegrees = visualEmotion === "furious"
    ? furiousTurnDirection * headTurn.maxYaw * lifecycle
    : 0;
  const furiousBodyTurn = visualEmotion === "furious"
    ? furiousTurnDirection * headTurn.bodyMax * lifecycle
    : 0;
  const angleX = clamp(
    headLook.x * (16 + eyeContact * 6)
      + idleSway * 1.1
      + shyTremble
      + motion.headX
      + (emotion.ParamAngleX || 0)
      + furiousTurnDegrees,
    -60,
    60
  );
  const angleY = clamp(-headLook.y * (14 + eyeContact * 5) + Math.sin(tickerTime * 0.7) * 1.2 + speakNod + motion.headY + (emotion.ParamAngleY || 0), -30, 30);
  const angleZ = clamp(headLook.x * -3.2 + Math.sin(tickerTime * 0.55) * 1.2 - shyTremble * 1.3 + motion.headZ + (emotion.ParamAngleZ || 0), -30, 30);
  const bodyX = clamp(headLook.x * 2.2 + idleSway * 1.5 + (speaking ? Math.sin(tickerTime * 3.2) * 0.75 : 0) + motion.bodyX + furiousBodyTurn, -10, 10);
  const bodyY = clamp(Math.sin(tickerTime * 1.05) * 0.55 + (speaking ? Math.sin(tickerTime * 2.6) * 0.42 : 0) + motion.bodyY, -10, 10);
  const bodyZ = clamp(Math.sin(tickerTime * 0.38) * 0.82 + (speaking ? Math.sin(tickerTime * 2.15) * 0.48 : 0) + motion.bodyZ, -10, 10);
  const furiousGlance = furiousColdGlanceWeight(now);
  const shyAvertedGazeX = ["shy", "flustered"].includes(visualEmotion)
    ? shyGazeAwayDirection * (
      0.12 + clamp(visualEmotionIntensityWeight(currentEmotionWeight), 0, 1.55) * 0.06
    )
    : 0;
  const semanticGazeX = visualEmotion === "furious"
    ? furiousTurnDirection * 0.52 * furiousGlance
    : ["shy", "flustered"].includes(visualEmotion)
      ? shyAvertedGazeX
      : gazeX;
  const rawEyeBallX = clamp(eyeLook.x * 1.15 * eyeContact + semanticGazeX + motion.eyeX + (emotion.ParamEyeBallX || 0), -1, 1);
  const rawEyeBallY = clamp(-eyeLook.y * 1.22 * eyeContact + gazeY + motion.eyeY + (emotion.ParamEyeBallY || 0), -1, 1);
  const gestureEyeOpen = clamp(1 - (motion.eyeClose || 0), 0, 1);
  const expressionEyeLOpen = clamp(
    Math.max(emotion.ParamEyeLOpen ?? 1, furiousGlance * 0.34),
    0,
    1
  );
  const expressionEyeROpen = clamp(
    Math.max(emotion.ParamEyeROpen ?? 1, furiousGlance * 0.32),
    0,
    1
  );
  const eyeVisibility = activeEyeVisibilityCompensation();
  const stableExpressionEyeLOpen = stableExpressionEyeOpening(
    expressionEyeLOpen * gestureEyeOpen,
    eyeVisibility
  );
  const stableExpressionEyeROpen = stableExpressionEyeOpening(
    expressionEyeROpen * gestureEyeOpen,
    eyeVisibility
  );
  const persistentEyeOpening = Math.min(stableExpressionEyeLOpen, stableExpressionEyeROpen);
  if (persistentEyeOpening < 0.68) {
    if (!narrowEyeExpressionSince) narrowEyeExpressionSince = now;
  } else {
    narrowEyeExpressionSince = 0;
  }
  const sustainedNarrowEye = narrowEyeExpressionSince > 0
    && now - narrowEyeExpressionSince >= eyeVisibility.sustainedDelayMs;
  const curve = sampleEyeVisibilityCurve(eyeVisibility.points, persistentEyeOpening);
  // A normal blink crosses the ugly half-open interval too quickly to justify
  // visible Y movement. Sustained expression squints use the model-specific
  // curve; momentary blinks only recenter gaze.
  const blinkGazeGain = 0.12 + eyeOpen * 0.88;
  const gazeGain = sustainedNarrowEye ? curve.gazeGain : blinkGazeGain;
  const eyeBallX = clamp(rawEyeBallX * gazeGain, -1, 1);
  const eyeBallY = clamp(
    rawEyeBallY * gazeGain + (sustainedNarrowEye ? curve.offsetY : 0),
    -1,
    1
  );
  const visibilityPupilScale = sustainedNarrowEye ? curve.pupilScale : 0;
  const shockPupilScale = startledPupilScale(now);
  // Both channels use negative values for a smaller iris/pupil. Keep the
  // stronger contraction instead of allowing the eye-visibility guard to
  // overwrite the shock response.
  const pupilScale = Math.min(visibilityPupilScale, shockPupilScale);
  const annotatedPupilScales = annotatedExpressionPlan?.pupilScales || [1, 1];
  runtimeAuthorBlushOpacityTarget = clamp(
    toNumber(annotatedExpressionPlan?.authorBlushOpacity, 1),
    0,
    1,
  );
  runtimeAuthorBlushOpacityValue += (
    runtimeAuthorBlushOpacityTarget - runtimeAuthorBlushOpacityValue
  ) * 0.24;
  const pupilVisibilityMultiplier = clamp(1 + pupilScale, 0.26, 1);
  runtimePupilScaleTargets.left = clamp(
    toNumber(annotatedPupilScales[0], 1) * pupilVisibilityMultiplier,
    0.35,
    1.35,
  );
  runtimePupilScaleTargets.right = clamp(
    toNumber(annotatedPupilScales[1], 1) * pupilVisibilityMultiplier,
    0.35,
    1.35,
  );
  runtimePupilScaleValues.left += (
    runtimePupilScaleTargets.left - runtimePupilScaleValues.left
  ) * 0.24;
  runtimePupilScaleValues.right += (
    runtimePupilScaleTargets.right - runtimePupilScaleValues.right
  ) * 0.24;
  const leftEyeOpen = clamp(stableExpressionEyeLOpen * eyeOpen, 0, 1);
  const rightEyeOpen = clamp(stableExpressionEyeROpen * eyeOpen, 0, 1);

  // AiPoet's original random motions use yaw well beyond ±30 degrees. Each
  // model declares its own safe limit so the classic model can turn strongly
  // without forcing the same range onto ordinary Cubism rigs.
  const yawLimit = visualEmotion === "furious" ? headTurn.maxYaw : 30;
  setSmoothParam("ParamAngleX", clamp(angleX, -yawLimit, yawLimit), visualEmotion === "furious" ? 0.28 : 0.2);
  setSmoothParam("ParamAngleY", angleY, 0.18);
  setSmoothParam("ParamAngleZ", angleZ, 0.18);
  setSmoothParam(
    "Param14",
    visualEmotion === "furious"
      ? furiousTurnDirection * headTurn.auxiliaryMax * lifecycle
      : clamp(angleX / 30, -1, 1),
    visualEmotion === "furious" ? 0.28 : 0.16
  );
  setSmoothParam("Param15", clamp(angleY / 30, -1, 1), 0.16);
  setSmoothParam("ParamBodyAngleX", bodyX, 0.16);
  setSmoothParam("ParamBodyAngleY", bodyY, 0.16);
  setSmoothParam("ParamBodyAngleZ", bodyZ, 0.16);
  setSmoothParam("Param17", clamp(bodyX / 10 + motion.stageX / 28, -1, 1), 0.14);
  setSmoothParam("ParamEyeBallX", eyeBallX, 0.22);
  setSmoothParam("ParamEyeBallY", eyeBallY, 0.22);
  setMappedSmoothParam("pupilScale", pupilScale, 0.24);
  setSmoothParam("ParamEyeLOpen", leftEyeOpen, 0.42);
  setSmoothParam("ParamEyeROpen", rightEyeOpen, 0.42);
  setMappedSmoothParam(
    "eyeSmile",
    clamp((emotion.ParamEyeSmile || 0) + motion.eyeSmile, 0, 1),
    0.42,
    "ParamEyeLSmile"
  );
  setSmoothParam(
    "ParamMouthOpenY",
    clamp(Math.max(
      mouthTalk * lipSync.openMultiplier,
      Number.isFinite(annotatedExpressionPlan?.mouthOpenY)
        ? annotatedExpressionPlan.mouthOpenY
        : emotion.ParamMouthOpenY || 0,
    ), 0, 1),
    lipSync.openSmoothing
  );
  setSmoothParam(
    "ParamMouthForm",
    mouthCurveWithSpeech(
      mouthTemplateCurveForEmotion(
        visualEmotion,
        clamp(
          (emotion.ParamMouthForm || 0) + motion.mouthSmile + lipSync.formOffset,
          -1,
          1
        ),
        annotatedExpressionPlan,
      ),
      mouthFormSync * lipSync.formMultiplier
    ),
    lipSync.formSmoothing
  );
  setMappedSmoothParam("browLY", clamp(emotion.ParamBrowLY || 0, -1, 1), 0.42, "ParamBrowLY");
  setMappedSmoothParam("browRY", clamp(emotion.ParamBrowRY || 0, -1, 1), 0.42, "ParamBrowRY");
  setMappedSmoothParam("browLForm", clamp(emotion.ParamBrowLForm || 0, -1, 1), 0.42, "ParamBrowLForm");
  setMappedSmoothParam("browRForm", clamp(emotion.ParamBrowRForm || 0, -1, 1), 0.42, "ParamBrowRForm");
  applyAnnotatedFaceLayers(annotatedExpressionPlan);
  // The arm controller owns time-based easing; a second per-frame filter
  // would slow it again and prolong the rig's blended sleeve state.
  setSmoothParam("Param16", modelArmPoseTarget((emotion.Param16 ?? 0.22) + motion.arms, now), 1);
  setSmoothParam("Param11", clamp((emotion.Param11 || 0) + tension * 0.12, -1, 1), 0.34);
  setSmoothParam("Param12", clamp((emotion.Param12 || 0) + tension * 0.1, -1, 1), 0.34);
  const blushIntensity = clamp(toNumber(currentExpressionDetail.blushIntensity, emotion.ParamCheek || 0), 0, 1);
  const dynamicBlush = blushIntensity > 0
    ? blushIntensity * blushLifecycle
      + shyCheekPulse * Math.max(0.35, blushLifecycle)
      + (speaking ? 0.025 * blushLifecycle : 0)
    : 0;
  setMappedSmoothParam(
    "blush",
    clamp(dynamicBlush, 0, 1),
    ["shy", "flustered", "affectionate"].includes(visualEmotion) ? 0.12 : 0.14,
    "ParamCheek"
  );
  setSmoothParam("ParamBreath", clamp(breath + motion.breath, 0, 1), 0.16);
  updateNativeInteraction(now);
  freezeModelBoundBackground();
  if (DEV_MODE) updateDeveloperSpeechPanel();
  if (DEV_MODE) updateDeveloperTouchOverlay();

  const hair = Math.sin(tickerTime * 1.25) * 0.28 + headLook.x * 0.14 + motion.hair;
  setSmoothParam("ParamHairFront", clamp(hair, -1, 1), 0.12);
  setSmoothParam("ParamHairSide", clamp(hair * 0.82, -1, 1), 0.12);
  setSmoothParam("ParamHairBack", clamp(hair * 0.64, -1, 1), 0.12);
  setSmoothParam("Param13", clamp(hair * 0.42, -1, 1), 0.12);
  setSmoothParam("Param18", clamp(hair * 0.36, -1, 1), 0.12);
  applyDeveloperParameterOverrides();
  try {
    coreModel.update();
    applyDeveloperDrawableOverrides();
  } catch {
    // Cubism 2 compatibility wrappers may update drawables elsewhere.
  }
}

function emotionSemanticsFor(emotion) {
  return EMOTION_SEMANTICS[emotion] || EMOTION_SEMANTICS.neutral;
}

function effectiveVisualEmotion() {
  return emotionSemanticsFor(currentEmotion).role === "cognitive"
    ? cognitiveOverlayBaseEmotion
    : currentEmotion;
}

function effectiveExpressionRuntime() {
  if (DEV_MODE && developerExpressionTrainingLoaded) return developerExpressionRuntime;
  return settings.expressionRuntime && typeof settings.expressionRuntime === "object"
    ? settings.expressionRuntime
    : { activeByModel: {} };
}

function activeRigModelId() {
  return activeModelProfile?.baseModelId || activeModelProfile?.id || "";
}

function modelArmPoseTarget(value, now = performance.now()) {
  return armMotionController.update(value, {
    now,
    mode: activeModelProfile?.armPoseMode || "continuous",
    modelId: activeModelProfile?.id || "",
  });
}

function activeRuntimeExpressionEntry(emotion = currentEmotion) {
  const activeByModel = effectiveExpressionRuntime()?.activeByModel || {};
  // Clothing variants share a rig and its trained face, while allowing their
  // own per-emotion overrides without copying or migrating saved samples.
  const modelIds = [activeModelProfile?.id, activeRigModelId()];
  for (const modelId of modelIds) {
    const entry = activeByModel[modelId]?.[emotion];
    if (entry?.parameters && typeof entry.parameters === "object") return entry;
  }
  return null;
}

function runtimeEmotionTarget(emotion = "neutral") {
  const builtIn = emotionTargets[emotion] || emotionTargets.neutral;
  const entry = activeRuntimeExpressionEntry(emotion);
  return entry ? { ...builtIn, ...entry.parameters } : builtIn;
}

function visualTargetForEmotionState() {
  const neutral = runtimeEmotionTarget("neutral");
  const foreground = runtimeEmotionTarget(currentEmotion);
  // Developer emotion controls are a visual inspection tool: show the chosen
  // face itself instead of composing it over whichever stable emotion happened
  // to be active before the tester selected it.
  if (DEV_MODE && developerEmotionManualBlush !== null) return foreground;
  const semantics = emotionSemanticsFor(currentEmotion);
  const overlayStrength = EMOTION_OVERLAY_STRENGTH[semantics.role];
  if (!overlayStrength) return foreground;

  const baseEmotion = semantics.role === "cognitive"
    ? cognitiveOverlayBaseEmotion
    : stableEmotion;
  const base = runtimeEmotionTarget(baseEmotion);
  const weakBaseIntensity = visualEmotionIntensityWeight(COGNITIVE_BASE_RAW_INTENSITY);
  const keys = new Set([
    ...Object.keys(neutral),
    ...Object.keys(base),
    ...Object.keys(foreground),
  ]);
  const composed = {};
  keys.forEach(key => {
    const neutralValue = neutral[key] ?? 0;
    const rawBaseValue = base[key] ?? neutralValue;
    const baseValue = semantics.role === "cognitive"
      ? neutralValue + (
        rawBaseValue - neutralValue
      ) * emotionParameterWeight(key, weakBaseIntensity, 1)
      : rawBaseValue;
    const overlayValue = foreground[key] ?? neutralValue;
    composed[key] = clamp(
      baseValue + (overlayValue - neutralValue) * overlayStrength,
      -1.35,
      1.35
    );
  });
  return composed;
}

function visualEmotionIntensityWeight(rawIntensity = currentEmotionWeight) {
  const mapper = globalThis.AmadeusEmotionIntensity;
  if (mapper && typeof mapper.map === "function") {
    return clamp(mapper.map(rawIntensity), 0.15, 1.55);
  }
  return clamp(toNumber(rawIntensity, 0.7), 0.15, 1.55);
}

function activeAnnotatedExpressionPlan(
  visualEmotion = effectiveVisualEmotion(),
  lifecycle = emotionLifecycleWeight()
) {
  const runtimeEntry = activeRuntimeExpressionEntry(visualEmotion);
  if (runtimeEntry) {
    const parameters = runtimeEntry.parameters || {};
    const layers = Object.fromEntries(AUTHOR_FACE_LAYER_IDS.map(id => [
      id,
      clamp(toNumber(parameters[id], 0) * lifecycle, 0, 1),
    ]));
    return {
      styleId: `runtime:${runtimeEntry.sampleId || visualEmotion}`,
      mirrorSide: 1,
      layers,
      pupilScales: ["VirtualPupilLScale", "VirtualPupilRScale"].map(id => (
        1 + (clamp(toNumber(parameters[id], 1), 0.35, 1.35) - 1) * lifecycle
      )),
      authorBlushOpacity: 1 + (
        clamp(toNumber(parameters.VirtualAuthorBlushOpacity, 1), 0, 1) - 1
      ) * lifecycle,
      mouthOpenY: clamp(toNumber(parameters.ParamMouthOpenY, 0) * lifecycle, 0, 1),
      mouthForm: Number.isFinite(Number(parameters.ParamMouthForm))
        ? clamp(toNumber(parameters.ParamMouthForm, 0), -1, 1)
        : null,
      runtimeSampleId: runtimeEntry.sampleId || "",
      runtimeRevision: toNumber(runtimeEntry.sampleRevision, 0),
    };
  }
  if (activeRigModelId() !== "aipoet-kurisu") return null;
  const mapper = globalThis.AmadeusAnnotatedExpressionMap;
  if (!mapper || typeof mapper.plan !== "function") return null;
  const planIntensity = emotionSemanticsFor(currentEmotion).role === "cognitive"
    ? visualEmotionIntensityWeight(COGNITIVE_BASE_RAW_INTENSITY)
    : visualEmotionIntensityWeight(currentEmotionWeight);
  return mapper.plan(
    visualEmotion,
    planIntensity,
    lifecycle,
    annotatedExpressionVariantUnit,
    annotatedExpressionMirrorSide,
  );
}

function mirrorAnnotatedExpressionParameters(parameters, visualEmotion) {
  if (activeRigModelId() !== "aipoet-kurisu") return parameters;
  const mapper = globalThis.AmadeusAnnotatedExpressionMap;
  if (!mapper || typeof mapper.mirrorParameters !== "function") return parameters;
  return mapper.mirrorParameters(
    parameters,
    visualEmotion,
    annotatedExpressionMirrorSide,
  );
}

function emotionParameterWeight(key, emotionWeight, lifecycle) {
  // `emotionWeight` has already passed through the end-dense visual curve:
  // weak values are pulled nearer neutral, strong values nearer the complete
  // expression, and the narrow middle band changes quickly.
  if (key === "ParamEyeLOpen" || key === "ParamEyeROpen") {
    const intensityProgress = smoothstep(clamp(Math.min(emotionWeight, 1) / 0.96, 0, 1));
    return intensityProgress * smoothstep(lifecycle);
  }
  if (key === "ParamEyeBallX" || key === "ParamEyeBallY") {
    return Math.min(emotionWeight, 1) * lifecycle;
  }
  if (key.startsWith("ParamAngle") || key.startsWith("ParamBodyAngle")) {
    return Math.min(emotionWeight, 1.16) * lifecycle;
  }
  if (key === "ParamMouthForm") {
    // Curvature is continuous: weak emotions stay near the neutral curve,
    // then ease towards the full smile/frown without cross-fading a second
    // mouth drawable over the first one.
    const curveIntensity = smoothstep(clamp(emotionWeight / 1.08, 0, 1));
    return curveIntensity * smoothstep(clamp(lifecycle, 0, 1));
  }
  if (
    key.startsWith("ParamBrow")
    || key === "ParamMouthOpenY"
    || key === "ParamEyeSmile"
  ) {
    return Math.min(emotionWeight, 1.24) * lifecycle;
  }
  return Math.min(emotionWeight, 1.35) * lifecycle;
}

function blendedEmotionTarget() {
  const neutral = emotionTargets.neutral;
  const target = visualTargetForEmotionState();
  const lifecycle = emotionLifecycleWeight();
  const emotionWeight = visualEmotionIntensityWeight(currentEmotionWeight);
  const keys = new Set([...Object.keys(neutral), ...Object.keys(target)]);
  const result = {};
  keys.forEach(key => {
    const baseValue = neutral[key] ?? 0;
    const targetValue = target[key] ?? baseValue;
    const channelWeight = emotionParameterWeight(key, emotionWeight, lifecycle);
    result[key] = clamp(baseValue + (targetValue - baseValue) * channelWeight, -1.35, 1.35);
  });
  return mirrorAnnotatedExpressionParameters(result, effectiveVisualEmotion());
}

function updateLookTargets(now = performance.now()) {
  const visualEmotion = effectiveVisualEmotion();
  const avoidsDirectGaze = ["shy", "flustered"].includes(visualEmotion);
  // Use a centre dead-zone so crossing the face does not make the eyes snap
  // left/right. Shy eyes choose the side opposite the pointer and retain it
  // until the pointer clearly crosses the centre.
  if (avoidsDirectGaze) {
    if (pointer.x > 0.14) shyGazeAwayDirection = -1;
    else if (pointer.x < -0.14) shyGazeAwayDirection = 1;
  }
  if (now > eyeDrift.nextAt) {
    const shyBias = avoidsDirectGaze ? shyGazeAwayDirection * 0.08 : 0;
    eyeDrift = {
      x: clamp((Math.random() - 0.5) * 0.26 + shyBias, -0.32, 0.32),
      y: clamp((Math.random() - 0.5) * 0.16 + (currentEmotion === "thinking" ? -0.08 : 0), -0.22, 0.22),
      nextAt: now + 850 + Math.random() * 1700,
    };
  }
  // Eyes lead; near the edge, the head joins because eyeball-only tracking is exhausted.
  const overflowX = Math.sign(pointer.x) * clamp((Math.abs(pointer.x) - 0.56) / 0.44, 0, 1);
  const overflowY = Math.sign(pointer.y) * clamp((Math.abs(pointer.y) - 0.48) / 0.52, 0, 1);
  const headTarget = visualEmotion === "furious"
    ? { x: 0, y: 0 }
    : {
      x: clamp(pointer.x * 0.18 + overflowX * 0.38, -0.54, 0.54),
      y: clamp(pointer.y * 0.16 + overflowY * 0.4, -0.54, 0.54),
    };
  const eyeTarget = visualEmotion === "furious"
    ? { x: 0, y: 0 }
    : avoidsDirectGaze
      ? {
        x: shyGazeAwayDirection * (0.48 + clamp(Math.abs(pointer.x), 0, 1) * 0.14),
        y: clamp(pointer.y * 0.38 + eyeDrift.y * 0.22, -0.46, 0.46),
      }
    : {
      x: clamp(pointer.x * 0.9 + eyeDrift.x * 0.55, -0.92, 0.92),
      y: clamp(pointer.y * 0.98 + eyeDrift.y * 0.45, -0.96, 0.96),
    };
  headLook.x += (headTarget.x - headLook.x) * 0.055;
  headLook.y += (headTarget.y - headLook.y) * 0.055;
  eyeLook.x += (eyeTarget.x - eyeLook.x) * 0.2;
  eyeLook.y += (eyeTarget.y - eyeLook.y) * 0.2;
}

function gestureMotion(gesture, emotion, now = performance.now(), speaking = false) {
  const elapsed = Math.max(0, now - gestureEnteredAt);
  const enter = easeOutCubic(clamp(elapsed / 340, 0, 1));
  const exit = clamp((gestureUntil - now) / 340, 0, 1);
  const weight = clamp(Math.min(enter, exit) || 0, 0, 1);
  const phase = elapsed / 1000;
  const talk = speaking ? Math.sin(tickerTime * 3.1) * 0.7 : 0;
  const motion = { headX: 0, headY: 0, headZ: 0, bodyX: 0, bodyY: 0, bodyZ: 0, stageX: 0, stageY: 0, stageTilt: 0, eyeX: 0, eyeY: 0, eyeClose: 0, eyeSmile: 0, mouthSmile: 0, breath: 0, hair: 0, arms: 0 };

  if (gesture === "nod") {
    motion.headY = (Math.sin(phase * Math.PI * 4.2) * 5.2 - 2.1) * weight;
    motion.bodyX = Math.sin(phase * Math.PI * 2.1) * 2.8 * weight;
    motion.stageY = Math.sin(phase * Math.PI * 4.2) * 5 * weight;
    motion.eyeY = -0.08 * weight;
    motion.arms = 0.08 * weight;
  } else if (gesture === "tilt") {
    motion.headX = -3.2 * weight;
    motion.headZ = (10.8 + Math.sin(phase * Math.PI * 1.6) * 3.8) * weight;
    motion.bodyX = -7.2 * weight;
    motion.bodyZ = -1.2 * weight;
    motion.stageTilt = 2.2 * weight;
    motion.eyeX = 0.18 * weight;
    motion.arms = 0.18 * weight;
  } else if (gesture === "look_away") {
    const direction = emotion === "furious"
      ? furiousTurnDirection
      : (lookAwayDirection || 1);
    const baseTurn = emotion === "furious" ? 0 : emotion === "annoyed" ? 8.5 : 18;
    const turn = baseTurn + Math.sin(phase * Math.PI * 1.15) * (emotion === "annoyed" ? 1.2 : 2);
    motion.headX = direction * turn * weight;
    motion.headZ = -direction * 2.8 * weight;
    motion.bodyX = direction * (emotion === "annoyed" ? 2.6 : 5.2) * weight;
    motion.bodyZ = direction * 2.2 * weight;
    motion.stageX = direction * 3.2 * weight;
    motion.stageTilt = -direction * 0.7 * weight;
    motion.eyeX = direction * (emotion === "furious" ? 0 : emotion === "annoyed" ? 0.14 : 0.28) * weight;
    motion.eyeY = 0.04 * weight;
    motion.arms = 0.08 * weight;
  } else if (gesture === "lean") {
    motion.headY = -2.2 * weight + talk * 0.35 * weight;
    motion.bodyX = 8.5 * weight;
    motion.bodyY = -1.5 * weight;
    motion.stageY = -12 * weight;
    motion.stageX = 5 * weight;
    motion.eyeY = 0.08 * weight;
    motion.breath = 0.08 * weight;
    motion.arms = -0.12 * weight;
  } else if (gesture === "soften") {
    motion.headY = -4.5 * weight;
    motion.headZ = -2.1 * weight;
    motion.bodyX = -4.2 * weight;
    motion.stageY = 6 * weight;
    motion.stageTilt = -0.8 * weight;
    motion.eyeY = 0.16 * weight;
    motion.breath = -0.08 * weight;
    motion.arms = 0.22 * weight;
  } else if (gesture === "smile") {
    motion.headY = -3.8 * weight;
    motion.headZ = Math.sin(phase * Math.PI * 1.4) * 1.6 * weight;
    motion.bodyX = 3.6 * weight;
    motion.stageY = -5 * weight;
    motion.eyeClose = 0.98 * weight;
    motion.eyeSmile = 0.38 * weight;
    motion.mouthSmile = 0.08 * weight;
    motion.breath = 0.1 * weight;
    motion.arms = 0.14 * weight;
  } else if (gesture === "laugh") {
    const laughBeat = Math.sin(phase * Math.PI * 4.6);
    const laughPulse = Math.abs(laughBeat);
    motion.headY = (-2.8 + laughBeat * 2.6) * weight;
    motion.headZ = Math.sin(phase * Math.PI * 1.8) * 2.2 * weight;
    motion.bodyX = 4.8 * weight;
    motion.bodyY = laughPulse * 1.8 * weight;
    motion.stageY = (-7 + laughPulse * 3.2) * weight;
    motion.eyeClose = (0.28 + laughPulse * 0.22) * weight;
    motion.eyeSmile = 0.18 * weight;
    motion.mouthSmile = 0.12 * weight;
    motion.breath = (0.08 + laughPulse * 0.1) * weight;
    motion.hair = laughBeat * 0.12 * weight;
    motion.arms = 0.18 * weight;
  } else if (gesture === "glitch") {
    const snap = Math.sign(Math.sin(phase * 42)) * (0.7 + Math.sin(phase * 17) * 0.3);
    motion.headX = snap * 5.8 * weight;
    motion.headZ = -snap * 6.5 * weight;
    motion.bodyZ = snap * 3.4 * weight;
    motion.stageX = snap * 7 * weight;
    motion.stageTilt = snap * 1.8 * weight;
    motion.hair = snap * 0.5 * weight;
    motion.arms = snap * 0.12 * weight;
  } else if (gesture === "tap") {
    motion.headZ = Math.sin(phase * Math.PI * 5) * 3.4 * weight;
    motion.bodyX = Math.sin(phase * Math.PI * 3) * 1.7 * weight;
    motion.arms = -0.18 * weight;
  } else if (gesture === "recoil") {
    motion.headY = 4.5 * weight;
    motion.bodyX = -9.2 * weight;
    motion.bodyZ = -2.6 * weight;
    motion.stageY = 14 * weight;
    motion.stageX = -9 * weight;
    motion.eyeY = -0.14 * weight;
    motion.arms = -0.34 * weight;
  } else if (gesture === "explain") {
    motion.headX = Math.sin(phase * Math.PI * 1.7) * 3.2 * weight;
    motion.bodyX = Math.sin(phase * Math.PI * 1.2) * 5.2 * weight;
    motion.bodyZ = Math.sin(phase * Math.PI * 1.4) * 1.8 * weight;
    motion.stageX = Math.sin(phase * Math.PI * 1.2) * 4 * weight;
    motion.stageTilt = Math.sin(phase * Math.PI * 1.4) * 0.9 * weight;
    motion.eyeX = Math.sin(phase * Math.PI * 1.7) * 0.08 * weight;
    // Explain with one opening, a hold, and a return. A sine wave repeatedly
    // crossed the arm-pose threshold while the same sentence was playing.
    motion.arms = -0.26 * weight;
  } else if (gesture === "perk") {
    motion.headY = -5.2 * weight;
    motion.bodyX = 6.4 * weight;
    motion.stageY = -13 * weight;
    motion.eyeY = -0.18 * weight;
    motion.breath = 0.12 * weight;
    motion.hair = 0.28 * weight;
    motion.arms = -0.2 * weight;
  }

  if (!gesture) {
    const idleEmotion = emotion === "thinking" ? -1 : ["excited", "laughing"].includes(emotion) ? 1 : 0;
    motion.headX += Math.sin(tickerTime * 0.42) * 1.2;
    motion.bodyX += Math.sin(tickerTime * 0.34) * (2.4 + Math.abs(idleEmotion) * 0.6);
    motion.bodyZ += Math.sin(tickerTime * 0.27) * 0.7;
    motion.stageY += Math.sin(tickerTime * 0.46) * 2.2;
    motion.stageTilt += Math.sin(tickerTime * 0.25) * 0.28;
    motion.eyeX += Math.sin(tickerTime * 0.31) * 0.025;
    motion.arms += Math.sin(tickerTime * 0.33) * 0.04;
  }

  return motion;
}

function updateBackgroundMotion(motion, speaking, now = performance.now()) {
  if (backgroundLayer) {
    backgroundLayer.x = 0;
    backgroundLayer.y = 0;
    backgroundLayer.rotation = 0;
  }
}

function setParam(id, value) {
  try {
    coreModel.setParameterValueById(id, value);
  } catch {
    // Imported models can omit optional params.
  }
}

function installPostMotionRetargeting() {
  const internalModel = model?.internalModel;
  if (!internalModel || typeof internalModel.update !== "function" || internalModel.__amadeusRetargetingInstalled) return;
  const originalUpdate = internalModel.update.bind(internalModel);
  internalModel.update = (...args) => {
    const result = originalUpdate(...args);
    reapplyRetargetedParameters();
    return result;
  };
  internalModel.__amadeusRetargetingInstalled = true;
}

function reapplyRetargetedParameters() {
  if (!coreModel) return;
  Object.entries(paramValues).forEach(([id, value]) => setParam(id, value));
  freezeModelBoundBackground();
  applyDeveloperParameterOverrides();
  try {
    coreModel.update();
    applyDeveloperDrawableOverrides();
  } catch {
    // Cubism 2 compatibility wrappers may update drawables elsewhere.
  }
}

function setSmoothParam(id, target, factor = 0.18) {
  const current = Object.prototype.hasOwnProperty.call(paramValues, id) ? paramValues[id] : target;
  const value = current + (target - current) * factor;
  paramValues[id] = value;
  setParam(id, value);
}

function mappedParameterIds(channel, fallbackId = "") {
  const aliases = activeModelProfile?.parameterAliases?.[channel];
  if (Array.isArray(aliases) && aliases.length) return aliases;
  return fallbackId ? [fallbackId] : [];
}

function setMappedSmoothParam(channel, target, factor = 0.18, fallbackId = "") {
  mappedParameterIds(channel, fallbackId).forEach(id => setSmoothParam(id, target, factor));
}

function setExpressionFix() {
  hideModelUiLayers();
  freezeModelBoundBackground();
}

function hideModelUiLayers() {
  const profileHidden = Array.isArray(activeModelProfile?.hiddenParameters)
    ? activeModelProfile.hiddenParameters
    : [];
  [...new Set([...MODEL_UI_HIDE_PARAMS, ...profileHidden])].forEach(id => setParam(id, 0));
}

function freezeModelBoundBackground() {
  Object.entries(activeModelProfile?.fixedParameters || {}).forEach(([id, value]) => {
    setParam(id, toNumber(value, 0));
  });
  const hiddenParts = Array.isArray(activeModelProfile?.hiddenParts) ? activeModelProfile.hiddenParts : [];
  hiddenParts.forEach(id => {
    try {
      coreModel.setPartOpacityById(id, 0);
    } catch {
      // Model profiles can list optional background parts.
    }
  });
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
    recovery = emotionSemanticsFor(stableEmotion).family === "positive"
      ? stableEmotion
      : "amused";
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

function setEmotion(emotion = "neutral", gesture = "idle", detail = {}) {
  if (DEV_MODE && developerEmotionOverride && !detail.developerOverride) {
    updateDeveloperEmotionPanel();
    return;
  }
  if (DEV_MODE && developerEmotionManualBlush !== null && !detail.developerOverride) {
    developerEmotionManualBlush = null;
  }
  const nextEmotion = emotionTargets[emotion] ? emotion : "neutral";
  const now = performance.now();
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
    detail.restart !== false
    && (nextEmotion === currentEmotion || !canReuseFamily)
  );
  if (shouldRestartLifecycle) {
    emotionEnteredAt = now;
    emotionSpeechProtectedUntil = 0;
  }
  if (
    !composeCognitiveOverlay
    && shouldRestartLifecycle
    && (
      nextEmotion !== annotatedExpressionStyleEmotion
      || detail.restart !== false
    )
  ) {
    annotatedExpressionStyleEmotion = nextEmotion;
    const requestedVariantUnit = Number(detail.annotatedVariantUnit);
    annotatedExpressionVariantUnit = Number.isFinite(requestedVariantUnit)
      ? clamp(requestedVariantUnit, 0, 0.999999)
      : Math.random();
    annotatedExpressionMirrorSide = Math.random() < 0.5 ? -1 : 1;
  }
  if (shouldRestartNativeExpression) {
    // Native expression files cannot be reduced to the weakest intensity.
    // Keep their neutral base while procedural parameters retain only a faint
    // trace of the previous tag underneath the thinking overlay.
    applyLive2DExpression(
      composeCognitiveOverlay ? "neutral" : nextEmotion
    );
  }
  currentEmotion = nextEmotion;
  const timing = emotionTimingFor(nextEmotion, detail);
  currentEmotionHoldMs = timing.holdMs;
  currentEmotionFadeMs = timing.fadeMs;
  const defaults = expressionDefaultsFor(nextEmotion);
  const emotionDelta = clamp(toNumber(detail.emotionDelta, 0), -0.45, 0.45);
  let requestedWeight = toNumber(detail.emotionIntensity, defaults.emotionIntensity)
    + emotionDelta
    + (accumulate ? Math.max(0, previousWeight - 0.72) * 0.12 : 0);
  if (detail.smoothEmotionTransition) {
    requestedWeight = clamp(requestedWeight, previousWeight - 0.16, previousWeight + 0.16);
  }
  currentEmotionWeight = clamp(
    requestedWeight,
    0.15,
    2.5
  );
  const requestedBlush = toNumber(detail.blushIntensity, defaults.blushIntensity);
  const requestedEyeContact = toNumber(detail.eyeContact, defaults.eyeContact);
  const requestedGazeX = toNumber(detail.gazeX, defaults.gazeX);
  const requestedGazeY = toNumber(detail.gazeY, defaults.gazeY);
  const requestedTension = toNumber(detail.tension, defaults.tension);
  currentExpressionDetail = {
    blushIntensity: clamp(
      composeCognitiveOverlay
        ? Math.max(
          requestedBlush,
          cognitiveBaseDetail.blushIntensity * COGNITIVE_BASE_DETAIL_WEIGHT,
        )
        : requestedBlush + (accumulate ? previousDetail.blushIntensity * 0.16 : 0),
      0,
      1
    ),
    eyeContact: clamp(
      composeCognitiveOverlay
        ? requestedEyeContact * (1 - COGNITIVE_BASE_DETAIL_WEIGHT)
          + cognitiveBaseDetail.eyeContact * COGNITIVE_BASE_DETAIL_WEIGHT
        : accumulate
          ? Math.min(requestedEyeContact, previousDetail.eyeContact + 0.08)
          : requestedEyeContact,
      0,
      1
    ),
    gazeX: clamp(
      composeCognitiveOverlay
        ? requestedGazeX + cognitiveBaseDetail.gazeX * COGNITIVE_BASE_DETAIL_WEIGHT
        : requestedGazeX,
      -0.6,
      0.6
    ),
    gazeY: clamp(
      composeCognitiveOverlay
        ? requestedGazeY + cognitiveBaseDetail.gazeY * COGNITIVE_BASE_DETAIL_WEIGHT
        : requestedGazeY,
      -0.6,
      0.6
    ),
    tension: clamp(
      composeCognitiveOverlay
        ? Math.max(
          requestedTension,
          cognitiveBaseDetail.tension * COGNITIVE_BASE_DETAIL_WEIGHT,
        )
        : requestedTension,
      0,
      1
    ),
    outburst: Boolean(detail.outburst || detail.deliveryMode === "outburst"),
  };
  const nextGesture = gesture || "";
  if (nextEmotion === "furious") {
    const turnProfile = activeHeadTurnProfile();
    if (pointer.x > turnProfile.pointerSwitchThreshold) furiousTurnTarget = -1;
    else if (pointer.x < -turnProfile.pointerSwitchThreshold) furiousTurnTarget = 1;
    else furiousTurnTarget = lookAwayDirection || 1;
    if (previousEmotion !== "furious") {
      furiousTurnDirection = 0;
      furiousTurnVelocity = 0;
    }
  } else if (nextGesture === "look_away" && (activeGesture !== nextGesture || detail.restart !== false)) {
    lookAwayDirection *= -1;
  }
  activeGesture = nextGesture;
  gestureEnteredAt = now;
  gestureUntil = now + gestureDuration(activeGesture);
  nextContextEmotionAt = now + Math.max(9000, currentEmotionHoldMs + currentEmotionFadeMs + 2400);
  updateModelStateLabel();
  pulseInteractionForEmotion(currentEmotion, activeGesture);
  applyLive2DMotion(currentEmotion, activeGesture);
  updateDeveloperEmotionPanel();
  if (activeGesture === "glitch") {
    document.body.classList.add("glitch");
    setTimeout(() => document.body.classList.remove("glitch"), 560);
  }
}

function expressionDefaultsFor(emotion) {
  if (emotion === "laughing") return { emotionIntensity: 1.08, blushIntensity: 0.18, eyeContact: 0.8, gazeX: 0, gazeY: 0.02, tension: 0.08 };
  if (emotion === "shy") return { emotionIntensity: 0.96, blushIntensity: 0.78, eyeContact: 0.42, gazeX: -0.16, gazeY: 0.13, tension: 0.24 };
  if (emotion === "flustered") return { emotionIntensity: 1.08, blushIntensity: 0.88, eyeContact: 0.31, gazeX: -0.24, gazeY: 0.18, tension: 0.5 };
  if (emotion === "affectionate") return { emotionIntensity: 0.88, blushIntensity: 0.28, eyeContact: 0.62, gazeX: -0.06, gazeY: 0.06, tension: 0.12 };
  if (emotion === "concerned") return { emotionIntensity: 0.92, blushIntensity: 0, eyeContact: 0.78, gazeX: 0, gazeY: 0.05, tension: 0.36 };
  if (emotion === "protective") return { emotionIntensity: 1.02, blushIntensity: 0.06, eyeContact: 0.86, gazeX: 0, gazeY: -0.06, tension: 0.62 };
  if (emotion === "annoyed") return { emotionIntensity: 1, blushIntensity: 0, eyeContact: 0.72, gazeX: 0.08, gazeY: -0.04, tension: 0.55 };
  if (emotion === "furious") return { emotionIntensity: 1.25, blushIntensity: 0.08, eyeContact: 0.18, gazeX: 0.5, gazeY: -0.04, tension: 0.92 };
  if (emotion === "skeptical") return { emotionIntensity: 0.96, blushIntensity: 0, eyeContact: 0.74, gazeX: -0.12, gazeY: -0.04, tension: 0.42 };
  if (emotion === "awkward") return { emotionIntensity: 0.82, blushIntensity: 0.08, eyeContact: 0.5, gazeX: -0.12, gazeY: -0.06, tension: 0.34 };
  if (emotion === "teasing") return { emotionIntensity: 0.94, blushIntensity: 0, eyeContact: 0.82, gazeX: 0.12, gazeY: 0, tension: 0.22 };
  if (emotion === "thinking") return { emotionIntensity: 0.9, blushIntensity: 0, eyeContact: 0.62, gazeX: -0.08, gazeY: -0.08, tension: 0.18 };
  if (emotion === "focused") return { emotionIntensity: 1, blushIntensity: 0, eyeContact: 0.7, gazeX: 0, gazeY: -0.1, tension: 0.34 };
  if (emotion === "excited") return { emotionIntensity: 1.05, blushIntensity: 0.12, eyeContact: 0.82, gazeX: 0, gazeY: -0.04, tension: 0.2 };
  if (emotion === "startled") return { emotionIntensity: 1.12, blushIntensity: 0.1, eyeContact: 0.92, gazeX: 0, gazeY: -0.14, tension: 0.72 };
  if (emotion === "proud") return { emotionIntensity: 0.96, blushIntensity: 0.18, eyeContact: 0.84, gazeX: 0.06, gazeY: -0.02, tension: 0.12 };
  if (emotion === "melancholic") return { emotionIntensity: 0.9, blushIntensity: 0, eyeContact: 0.46, gazeX: -0.06, gazeY: 0.16, tension: 0.24 };
  return { emotionIntensity: 0.82, blushIntensity: 0, eyeContact: 0.68, gazeX: 0, gazeY: 0, tension: 0 };
}

function emotionTimingFor(emotion, detail = {}) {
  const timings = {
    neutral: { holdMs: 0, fadeMs: 1200 },
    thinking: { holdMs: 45000, fadeMs: 11000 },
    focused: { holdMs: 45000, fadeMs: 11000 },
    skeptical: { holdMs: 42000, fadeMs: 10500 },
    awkward: { holdMs: 42000, fadeMs: 12000 },
    annoyed: { holdMs: 45000, fadeMs: 11000 },
    furious: { holdMs: 120000, fadeMs: 18000 },
    teasing: { holdMs: 42000, fadeMs: 11500 },
    amused: { holdMs: 42000, fadeMs: 11000 },
    laughing: { holdMs: 5200, fadeMs: 2000 },
    shy: { holdMs: 48000, fadeMs: 16000 },
    flustered: { holdMs: 12000, fadeMs: 4000 },
    affectionate: { holdMs: 48000, fadeMs: 16000 },
    concerned: { holdMs: 50000, fadeMs: 16000 },
    protective: { holdMs: 48000, fadeMs: 13000 },
    excited: { holdMs: 10000, fadeMs: 3200 },
    startled: { holdMs: 2400, fadeMs: 1000 },
    proud: { holdMs: 45000, fadeMs: 11000 },
    melancholic: { holdMs: 50000, fadeMs: 16000 },
  };
  const base = timings[emotion] || timings.neutral;
  const requestedHoldMs = clamp(toNumber(detail.holdMs, base.holdMs), 0, 120000);
  const requestedFadeMs = clamp(toNumber(detail.fadeMs, base.fadeMs), 500, 30000);
  if (emotionSemanticsFor(emotion).role === "transient") {
    return {
      holdMs: Math.min(requestedHoldMs, base.holdMs),
      fadeMs: Math.min(requestedFadeMs, base.fadeMs),
    };
  }
  return {
    holdMs: requestedHoldMs,
    fadeMs: requestedFadeMs,
  };
}

function emotionLifecycleWeight(now = performance.now()) {
  if (DEV_MODE && developerEmotionOverride) return 1;
  if (currentEmotion === "neutral") return 1;
  const age = Math.max(0, now - emotionEnteredAt);
  const enter = easeOutCubic(clamp(age / 520, 0, 1));
  // Once the final response mood is known, TTS preparation must not let that
  // expression decay back to neutral before the first audio frame is ready.
  if (speechState.pending && speechState.moodApplied) return enter;
  const holdUntil = expressionHoldUntil();
  if (now <= holdUntil) return enter;
  const fade = 1 - smoothstep((now - holdUntil) / Math.max(1, currentEmotionFadeMs));
  return clamp(enter * fade, 0, 1);
}

function blushLifecycleWeight(now = performance.now()) {
  const lifecycle = emotionLifecycleWeight(now);
  if (!["shy", "flustered", "affectionate"].includes(currentEmotion)) return lifecycle;
  const age = Math.max(0, now - emotionEnteredAt);
  const enter = smoothstep(age / 900);
  const holdUntil = expressionHoldUntil();
  if (now <= holdUntil) return clamp(enter, 0, 1);
  const fadeDuration = Math.max(900, currentEmotionFadeMs);
  const fade = 1 - smoothstep((now - holdUntil) / fadeDuration);
  return clamp(enter * fade, 0, 1);
}

function expressionHoldUntil() {
  return Math.max(
    emotionEnteredAt + currentEmotionHoldMs,
    emotionSpeechProtectedUntil
  );
}

function protectCurrentExpressionThroughSpeech(
  speechEndsAt = speechState.endsAt,
  postSpeechHoldMs = EXPRESSION_POST_SPEECH_HOLD_MS
) {
  if (currentEmotion === "neutral") return;
  const now = performance.now();
  const protectedSpeechEnd = Number.isFinite(speechEndsAt) && speechEndsAt > 0
    ? Math.max(now, speechEndsAt)
    : now;
  emotionSpeechProtectedUntil = Math.max(
    emotionSpeechProtectedUntil,
    protectedSpeechEnd + Math.max(0, postSpeechHoldMs)
  );
}

function updateEmotionLifecycle(now = performance.now()) {
  if (DEV_MODE && developerEmotionOverride) return;
  if (persistentColdShoulder && currentEmotion === "furious") return;
  maybeRefreshContextEmotion(now);
  if (
    currentEmotion === "neutral"
    || speechPipelineActive
    || speechState.pending
    || isSpeaking(now)
  ) return;
  if (now <= expressionHoldUntil() + currentEmotionFadeMs + 420) return;
  setEmotion(recoveryEmotionFor(currentEmotion), "idle", {
    emotionIntensity: 0.78,
    blushIntensity: 0,
    holdMs: 0,
    fadeMs: 900,
  });
}

function maybeRefreshContextEmotion(now = performance.now()) {
  if (persistentColdShoulder) {
    nextContextEmotionAt = now + 30000;
    return;
  }
  if (contextEmotionRequestInFlight || now < nextContextEmotionAt) return;
  if (speechState.pending || isSpeaking(now)) {
    nextContextEmotionAt = now + 3000;
    return;
  }
  if (!state.messages.length) {
    nextContextEmotionAt = now + 30000;
    return;
  }
  contextEmotionRequestInFlight = true;
  nextContextEmotionAt = now + 45000;
  storageScopedFetch("/api/emotion-token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      currentEmotion,
      messages: state.messages.slice(-8),
    }),
  })
    .then(response => response.json().then(data => ({ response, data })))
    .then(({ response, data }) => {
      if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
      const emotion = normalizeEmotion(data.emotion);
      const gesture = normalizeGesture(data.gesture);
      if (emotion === currentEmotion) {
        nextContextEmotionAt = performance.now() + (emotion === "neutral" ? 45000 : 12000);
        return;
      }
      setEmotion(emotion, gesture, {
        emotionIntensity: clamp(toNumber(data.emotionIntensity, emotion === "neutral" ? 0.72 : 0.9), 0.15, 1.25),
        blushIntensity: clamp(toNumber(data.blushIntensity, expressionDefaultsFor(emotion).blushIntensity), 0, 1),
        eyeContact: clamp(toNumber(data.eyeContact, expressionDefaultsFor(emotion).eyeContact), 0, 1),
        gazeX: clamp(toNumber(data.gazeX, expressionDefaultsFor(emotion).gazeX), -0.6, 0.6),
        gazeY: clamp(toNumber(data.gazeY, expressionDefaultsFor(emotion).gazeY), -0.6, 0.6),
        tension: clamp(toNumber(data.tension, expressionDefaultsFor(emotion).tension), 0, 1),
        holdMs: toNumber(data.holdMs, emotion === "neutral" ? 0 : 45000),
        fadeMs: toNumber(data.fadeMs, emotion === "neutral" ? 1200 : 10000),
      });
    })
    .catch(error => {
      console.warn("context emotion refresh failed:", error);
      nextContextEmotionAt = performance.now() + 60000;
    })
    .finally(() => {
      contextEmotionRequestInFlight = false;
    });
}

function recoveryEmotionFor(emotion = currentEmotion) {
  const semantics = emotionSemanticsFor(emotion);
  if (semantics.role === "boundary") return persistentColdShoulder ? "furious" : "annoyed";
  if (semantics.role === "cognitive" || semantics.role === "stance" || semantics.role === "transient") {
    return emotionRecoveryTarget || stableEmotion || "neutral";
  }
  return semantics.recovery || "neutral";
}

function emotionIntensity(emotion, now = performance.now()) {
  const directMatch = currentEmotion === emotion;
  const cognitiveBaseMatch = emotionSemanticsFor(currentEmotion).role === "cognitive"
    && cognitiveOverlayBaseEmotion === emotion;
  if (!directMatch && !cognitiveBaseMatch) return 0;
  const cognitiveBaseWeight = cognitiveBaseMatch && !directMatch ? 0.88 : 1;
  return (
    emotionLifecycleWeight(now)
    * cognitiveBaseWeight
    * visualEmotionIntensityWeight(currentEmotionWeight)
  );
}

function easeOutCubic(value) {
  return 1 - Math.pow(1 - value, 3);
}

function applyLive2DExpression(emotion) {
  if (!activeModelProfile?.supportsNativeExpressions) {
    lastLive2DExpression = "";
    return;
  }
  const profileExpressionMap = activeModelProfile?.expressionMap;
  const hasProfileExpression = profileExpressionMap
    && Object.prototype.hasOwnProperty.call(profileExpressionMap, emotion);
  const expressionName = hasProfileExpression ? profileExpressionMap[emotion] : {
    neutral: "neutral",
    thinking: "neutral",
    focused: "unhappy",
    skeptical: "unhappy",
    awkward: "neutral",
    annoyed: "anger",
    furious: "anger",
    teasing: "smile1",
    amused: "smile2",
    laughing: "joy",
    shy: "neutral",
    flustered: "neutral",
    affectionate: "smile1",
    concerned: "sadness",
    protective: "anger",
    excited: "joy",
    startled: "surprise",
    proud: "smile1",
    melancholic: "sadness",
  }[emotion] || "";
  if (!expressionName) {
    lastLive2DExpression = "";
    try {
      const manager = model?.internalModel?.motionManager?.expressionManager;
      if (manager && typeof manager.resetExpression === "function") manager.resetExpression();
    } catch {
      // Some Cubism runtimes do not expose expression reset.
    }
    return;
  }
  if (!model || lastLive2DExpression === expressionName) return;
  lastLive2DExpression = expressionName;
  try {
    if (typeof model.expression === "function") {
      model.expression(expressionName);
      return;
    }
    const manager = model.internalModel?.motionManager?.expressionManager;
    if (manager && typeof manager.setExpression === "function") {
      manager.setExpression(expressionName);
    }
  } catch (error) {
    console.debug("Live2D expression unavailable:", expressionName, error);
  }
}

function applyLive2DMotion(emotion, gesture) {
  if (!model) return;
  const motionGroup = motionGroupForEmotion(emotion, gesture);
  if (!motionGroup) return;
  const now = performance.now();
  if (lastLive2DMotion === motionGroup && now - lastLive2DMotionAt < 1800) return;
  lastLive2DMotion = motionGroup;
  lastLive2DMotionAt = now;
  try {
    if (typeof model.motion === "function") {
      model.motion(motionGroup, 0, 3);
      return;
    }
    const manager = model.internalModel?.motionManager;
    if (manager && typeof manager.startMotion === "function") {
      manager.startMotion(motionGroup, 0, 3);
    }
  } catch (error) {
    console.debug("Live2D motion unavailable:", motionGroup, error);
  }
}

function motionGroupForEmotion(emotion, gesture) {
  // Side-looking is fully parameter-driven. A model motion here would replace
  // the horizontal head turn with an unrelated emotion animation on some skins.
  if (gesture === "look_away") return "";
  let key = "";
  if (gesture === "boot") key = "boot";
  else if (gesture === "tap") key = "tap";
  else if (gesture === "recoil") key = "recoil";
  else if (gesture === "explain") key = "explain";
  else if (gesture === "smile") key = "happy";
  else if (gesture === "laugh") key = "happy";
  else if (gesture === "perk") key = "perk";
  else if (gesture === "lean") key = "lean";
  else if (emotion === "shy" || emotion === "flustered" || emotion === "affectionate") key = "shy";
  else if (emotion === "annoyed" || emotion === "protective") key = "angry";
  else if (emotion === "startled") key = "startled";
  else if (emotion === "concerned" || emotion === "melancholic") key = "sad";
  else if (emotion === "amused" || emotion === "teasing" || emotion === "laughing") key = "happy";
  else if (emotion === "proud" || emotion === "excited") key = "proud";
  else if (emotion === "focused" || emotion === "skeptical" || emotion === "awkward") key = "focused";
  const motionMap = activeModelProfile?.motionMap;
  const hasProfileMotion = Boolean(
    key && motionMap && Object.prototype.hasOwnProperty.call(motionMap, key)
  );
  if (hasProfileMotion) return motionMap[key] || "";
  if (activeRigModelId() === "aipoet-kurisu" && Math.random() < 0.35) {
    return `random${1 + Math.floor(Math.random() * 5)}`;
  }
  return "";
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

function pulseInteractionForEmotion(emotion, gesture) {
  if (gesture === "glitch") {
    pulseInteraction("");
  }
}

function pulseInteraction(id) {
  transientInteraction = id;
  transientInteractionUntil = performance.now() + 1150;
}

function updateNativeInteraction(now = performance.now()) {
  hideModelUiLayers();
  freezeModelBoundBackground();
}

function nativeInteractionForMood(emotion, gesture) {
  if (gesture === "glitch") return "DZH";
  if (emotion === "shy" || emotion === "flustered" || emotion === "affectionate") return "BB";
  if (emotion === "annoyed" || emotion === "skeptical" || emotion === "startled") return "KLSTN";
  if (emotion === "amused" || emotion === "teasing" || emotion === "proud") return "TX";
  if (emotion === "excited" || emotion === "focused") return "DZH";
  return "xl";
}

function initSpeechVoices() {
  if (!("speechSynthesis" in window)) return;
  const load = () => {
    availableVoices = window.speechSynthesis.getVoices() || [];
  };
  load();
  window.speechSynthesis.onvoiceschanged = load;
}

function selectVoice() {
  const locale = String(settings.voiceLocale || "ja-JP").toLowerCase();
  const voices = availableVoices.length ? availableVoices : ("speechSynthesis" in window ? window.speechSynthesis.getVoices() : []);
  const preferredNames = [
    "nanami",
    "haruka",
    "ayumi",
    "mizuki",
    "sayaka",
    "ichiro",
    "japanese",
    "ja-jp",
  ];
  return (
    voices.find(voice => voice.lang?.toLowerCase() === locale && preferredNames.some(name => voice.name.toLowerCase().includes(name))) ||
    voices.find(voice => voice.lang?.toLowerCase().startsWith("ja") && preferredNames.some(name => voice.name.toLowerCase().includes(name))) ||
    voices.find(voice => voice.lang?.toLowerCase() === locale) ||
    voices.find(voice => voice.lang?.toLowerCase().startsWith("ja")) ||
    voices.find(voice => voice.lang?.toLowerCase().startsWith("zh")) ||
    voices[0] ||
    null
  );
}

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
    queuedAt: performance.now(),
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
    provider: String(settings.ttsProvider || DEFAULT_SETTINGS.ttsProvider).trim().toLowerCase(),
    generation,
    pipelineSegmentIndex: Math.max(0, Number(meta.pipelineSegmentIndex) || 0),
    pipelineSegmentTotal: Math.max(1, Number(meta.pipelineSegmentTotal) || 1),
    systemSpeechActive: false,
    systemSpeechDeadline: 0,
    textRevealPending: Boolean(settings.voiceEnabled),
  };
  syncChatSubmitButton();
  speakUntil = 0;
  resetAnimeMouthState();
  recordVoiceDiagnostic("queued", "回复已进入语音队列", {
    generation,
    provider: settings.ttsProvider,
    kind: speechState.kind,
    textLength: cleanText.length,
  });

  if (!settings.voiceEnabled) {
    speechState.pending = false;
    speechState.textRevealPending = false;
    recordVoiceDiagnostic("skipped-disabled", "设置中的语音朗读已关闭", { generation });
    return Promise.resolve(false);
  }
  if (speechState.mood) {
    const continuesSameSegmentedMood = (
      speechState.pipelineSegmentIndex > 1
      && currentEmotion === speechState.mood.emotion
    );
    if (!continuesSameSegmentedMood) {
      setEmotion(speechState.mood.emotion, speechState.mood.gesture, speechState.mood);
    }
    speechState.moodApplied = true;
  }
  const provider = String(settings.ttsProvider || "").trim().toLowerCase();
  const localProviders = new Set(["gpt-sovits", "voicevox", "generic"]);
  if (localProviders.has(provider)) {
    if (provider !== "gpt-sovits" && !String(settings.ttsEndpoint || "").trim()) {
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

async function waitForSpeechSegmentEnd(generation, pipelineToken) {
  const waitStartedAt = performance.now();
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
      if (performance.now() - waitStartedAt > durationMs + 12000) {
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
    if (!speechState.pending && !isSpeaking(performance.now())) return true;
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
  const deadline = performance.now() + pauseMs;
  while (performance.now() < deadline) {
    if (pipelineToken !== speechPipelineGeneration || generation !== speechGeneration) {
      return false;
    }
    await delay(Math.min(40, Math.max(1, deadline - performance.now())));
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
  const provider = String(settings.ttsProvider || "").trim().toLowerCase();
  const suppliedSegments = Array.isArray(meta.pipelineSpeechSegments)
    ? meta.pipelineSpeechSegments.map(item => String(item || "").trim()).filter(Boolean)
    : [];
  const segments = settings.voiceEnabled && provider === "gpt-sovits"
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

const STREAM_CANCELLATION_TYPES = new Set([
  "cancel",
  "canceled",
  "cancelled",
  "client_canceled",
  "client_cancelled",
  "request_canceled",
  "request_cancelled",
  "request_superseded",
  "user_canceled",
  "user_cancelled",
]);

function isStreamCancellation(error) {
  if (isSpeechCancellation(error) || error?.streamCancelled === true || error?.cancelled === true) {
    return true;
  }
  const type = String(error?.streamErrorType || error?.reason || "").trim().toLowerCase();
  return STREAM_CANCELLATION_TYPES.has(type);
}

function assertCurrentSpeech(generation) {
  if (generation !== speechGeneration || speechState.generation !== generation) {
    throw speechCancellationError();
  }
}

function stopCurrentPlayback() {
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
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

function interruptReplyForNewConversation(now = performance.now()) {
  const currentKind = speechState.kind;
  const currentlySpeaking = isSpeaking(now);
  const priorReplyStillActive = (
    speechPipelineActive
    || speechState.pending
    || currentlySpeaking
  );
  if (!priorReplyStillActive) return false;

  if (["chat", "proactive"].includes(currentKind)) {
    conversationWasInterrupted = true;
  }
  if (currentKind === "touch") markActiveTouchInterrupted();
  interruptCurrentSpeech();
  return true;
}

function setDeveloperPanelCollapsed(collapsed, { persist = true } = {}) {
  if (!DEV_MODE || !dom.developerPanel) return;
  const next = Boolean(collapsed);
  dom.developerPanel.classList.toggle("dev-panel-collapsed", next);
  document.body.classList.toggle("dev-panel-collapsed", next);
  if (dom.devPanelCollapseBtn) {
    const action = next ? "展开开发者侧栏" : "收起开发者侧栏";
    dom.devPanelCollapseBtn.querySelector("span").textContent = next ? "‹" : "›";
    dom.devPanelCollapseBtn.title = action;
    dom.devPanelCollapseBtn.setAttribute("aria-label", action);
    dom.devPanelCollapseBtn.setAttribute("aria-expanded", String(!next));
  }
  const activePage = dom.developerPanel.dataset.activePage;
  dom.devTouchOverlay?.classList.toggle("hidden", next || activePage !== "touch");
  if (persist) window.localStorage.setItem(DEV_PANEL_COLLAPSED_KEY, next ? "1" : "0");
  window.requestAnimationFrame(() => fitModel());
}

function switchDeveloperPage(page) {
  if (!DEV_MODE || !dom.developerPanel) return;
  const target = ["overview", "emotion", "voice", "touch", "model", "training", "persona", "relationship"].includes(page) ? page : "voice";
  dom.developerPanel.dataset.activePage = target;
  dom.devTabs.forEach(button => button.classList.toggle("active", button.dataset.devTab === target));
  dom.devTouchOverlay?.classList.toggle("hidden", target !== "touch");
  if (target === "touch") {
    renderDeveloperTouchRegions();
    updateDeveloperTouchOverlay();
  }
  if (target === "overview") refreshDeveloperRuntime();
  if (target === "emotion") updateDeveloperEmotionPanel({ syncInputs: true });
  if (target === "relationship") loadDeveloperRelationship();
  if (target === "persona") {
    loadPersona();
    developerPersonaSkillMonitor?.refresh();
  }
  if (target === "model") renderDeveloperModelEditor();
  if (target === "training") loadDeveloperTrainingData();
}

function updateDeveloperEmotionReadouts() {
  if (dom.devEmotionIntensityValue) {
    const rawIntensity = toNumber(dom.devEmotionIntensityInput?.value, 1);
    const visualIntensity = visualEmotionIntensityWeight(rawIntensity);
    dom.devEmotionIntensityValue.textContent = `${rawIntensity.toFixed(2)} → 表情 ${visualIntensity.toFixed(2)}`;
  }
  if (dom.devEmotionBlushValue) {
    dom.devEmotionBlushValue.textContent = `${Math.round(clamp(toNumber(dom.devEmotionBlushInput?.value, 0), 0, 100))}%`;
  }
}

function loadDeveloperEmotionDefaults() {
  if (!DEV_MODE || !dom.devEmotionInput) return;
  const emotion = normalizeEmotion(dom.devEmotionInput.value);
  const defaults = expressionDefaultsFor(emotion);
  if (dom.devEmotionIntensityInput) dom.devEmotionIntensityInput.value = String(defaults.emotionIntensity);
  if (dom.devEmotionBlushInput) dom.devEmotionBlushInput.value = String(Math.round(clamp(defaults.blushIntensity, 0, 1) * 100));
  updateDeveloperEmotionReadouts();
}

function updateDeveloperEmotionPanel({ syncInputs = false } = {}) {
  if (!DEV_MODE || !dom.devEmotionStatus) return;
  if (syncInputs) {
    if (dom.devEmotionInput) dom.devEmotionInput.value = currentEmotion;
    if (dom.devGestureInput) dom.devGestureInput.value = normalizeGesture(activeGesture || "idle");
    if (dom.devEmotionIntensityInput) dom.devEmotionIntensityInput.value = String(currentEmotionWeight);
    if (dom.devEmotionBlushInput) dom.devEmotionBlushInput.value = String(Math.round(clamp(currentExpressionDetail.blushIntensity, 0, 1) * 100));
    if (dom.devEmotionLockInput) dom.devEmotionLockInput.checked = Boolean(developerEmotionOverride);
  }
  updateDeveloperEmotionReadouts();
  const eyeCurve = toNumber(
    runtimeEmotionTarget(currentEmotion)?.ParamEyeSmile,
    runtimeEmotionTarget("neutral").ParamEyeSmile || 0,
  );
  const mode = developerEmotionOverride ? "MANUAL LOCK" : "AUTO";
  const blushPercent = Math.round(clamp(currentExpressionDetail.blushIntensity, 0, 1) * 100);
  const semantics = emotionSemanticsFor(currentEmotion);
  const visualBaseEmotion = semantics.role === "cognitive"
    ? cognitiveOverlayBaseEmotion
    : stableEmotion;
  const visualIntensity = visualEmotionIntensityWeight(currentEmotionWeight);
  const annotatedPlan = activeAnnotatedExpressionPlan(visualBaseEmotion);
  const mappingStatus = annotatedPlan
    ? `MAP ${annotatedPlan.styleId.toUpperCase()} / SIDE ${annotatedPlan.mirrorSide < 0 ? "MIRROR" : "BASE"}`
    : "MAP LEGACY";
  dom.devEmotionStatus.textContent = `${mode} / ${currentEmotion.toUpperCase()} / RAW ${currentEmotionWeight.toFixed(2)}→VISUAL ${visualIntensity.toFixed(2)} / ${semantics.role.toUpperCase()}:${semantics.family.toUpperCase()} / BASE ${visualBaseEmotion.toUpperCase()} / ${mappingStatus} / NEXT ${emotionRecoveryTarget.toUpperCase()} / ${normalizeGesture(activeGesture || "idle").toUpperCase()} / EYE ${eyeCurve.toFixed(2)} / BLUSH ${blushPercent}%`;
}

function applyAnnotatedFaceLayers(expressionPlan = null) {
  if (activeRigModelId() !== "aipoet-kurisu") return;
  const targets = expressionPlan?.layers || {};
  AUTHOR_FACE_LAYER_IDS.forEach(id => {
    // The saved annotations intentionally use partial multi-layer blends. Each
    // layer is smoothed independently; no one-hot normalization is applied.
    setSmoothParam(id, clamp(toNumber(targets[id], 0), 0, 1), 0.26);
  });
}

function applyDeveloperEmotionControl() {
  if (!DEV_MODE || !dom.devEmotionInput) return;
  const emotion = normalizeEmotion(dom.devEmotionInput.value);
  const gesture = normalizeGesture(dom.devGestureInput?.value || "idle");
  const emotionIntensity = clamp(toNumber(dom.devEmotionIntensityInput?.value, 1), 0.15, 2.5);
  const blushIntensity = clamp(toNumber(dom.devEmotionBlushInput?.value, 0) / 100, 0, 1);
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
  updateDeveloperEmotionPanel({ syncInputs: true });
  toast(`开发者情绪：${emotion}${locked ? "（已锁定）" : "（临时）"}`);
}

function releaseDeveloperEmotionControl() {
  if (!DEV_MODE) return;
  developerEmotionOverride = null;
  developerEmotionManualBlush = null;
  if (dom.devEmotionLockInput) dom.devEmotionLockInput.checked = false;
  setEmotion("neutral", "idle", {
    developerOverride: true,
    restart: true,
    emotionIntensity: expressionDefaultsFor("neutral").emotionIntensity,
    blushIntensity: 0,
    holdMs: 0,
    fadeMs: 900,
  });
  nextContextEmotionAt = performance.now() + 1200;
  updateDeveloperEmotionPanel({ syncInputs: true });
  toast("已恢复自动情绪控制");
}

function recordVoiceDiagnostic(stage, message, details = {}) {
  const event = {
    time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
    stage: String(stage || "unknown"),
    message: String(message || ""),
    ...details,
  };
  voiceDiagnostics = { ...event, updatedAt: Date.now() };
  voiceDiagnosticEvents = [...voiceDiagnosticEvents, event].slice(-36);
  updateDeveloperSpeechPanel();
}

function formatDeveloperVoiceStopDiagnostics(metrics) {
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
  return formatDeveloperJson({
    route: metrics.gptModelRoute || metrics.primaryGptModelRoute || "default",
    model: metrics.gptModel || metrics.primaryGptModel || "",
    attempts: attempts.map((attempt, index) => {
      const decodeDetails = Array.isArray(attempt.semanticDecodes) ? attempt.semanticDecodes : [];
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
          stopReason: reasonLabels[attempt.semanticStopReason] || attempt.semanticStopReason || "未捕获",
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

async function refreshDeveloperRuntime() {
  if (!DEV_MODE || !dom.devRuntimeSummary) return;
  if (dom.developerPanel?.dataset.activePage === "persona") developerPersonaSkillMonitor?.refresh();
  try {
    const [settingsResponse, runtimeResponse] = await Promise.all([
      fetch("/api/settings", { cache: "no-store" }),
      fetch("/api/runtime/status", { cache: "no-store" }),
    ]);
    if (!settingsResponse.ok || !runtimeResponse.ok) {
      throw new Error(`HTTP ${settingsResponse.ok ? runtimeResponse.status : settingsResponse.status}`);
    }
    const data = await settingsResponse.json();
    latestRuntimeStatus = await runtimeResponse.json();
    const current = data.settings || {};
    dom.devRuntimeSummary.textContent = formatDeveloperJson({
      connection: connectionStatus,
      runtime: latestRuntimeStatus,
      model: current.apiModel || settings.apiModel,
      apiConfigured: Boolean(current.modelConfigured),
      voiceEnabled: settings.voiceEnabled,
      provider: settings.ttsProvider,
      voicePreset: settings.gptSovitsModelPreset,
      ttsService: current.gptSovitsStatus || null,
      speech: {
        stage: voiceDiagnostics.stage,
        pending: speechState.pending,
        active: speechState.active,
        kind: speechState.kind,
        generation: speechState.generation,
        pipelineSegmentIndex: speechState.pipelineSegmentIndex,
        pipelineSegmentTotal: speechState.pipelineSegmentTotal,
        textLength: speechState.text.length,
        metrics: speechState.ttsMetrics,
      },
    });
  } catch (error) {
    dom.devRuntimeSummary.textContent = `状态刷新失败：${error.message}`;
  }
}

async function copyDeveloperDiagnostics() {
  const payload = {
    capturedAt: new Date().toISOString(),
    build: document.documentElement.dataset.build || "web",
    settings: {
      apiModel: settings.apiModel,
      modelConfigured: settings.modelConfigured,
      voiceEnabled: settings.voiceEnabled,
      ttsProvider: settings.ttsProvider,
      voicePreset: settings.gptSovitsModelPreset,
      ttsEndpoint: settings.ttsEndpoint,
    },
    voiceDiagnostics,
    voiceEvents: voiceDiagnosticEvents,
    runtime: latestRuntimeStatus,
    speechState: {
      pending: speechState.pending,
      active: speechState.active,
      kind: speechState.kind,
      generation: speechState.generation,
      pipelineSegmentIndex: speechState.pipelineSegmentIndex,
      pipelineSegmentTotal: speechState.pipelineSegmentTotal,
      metrics: speechState.ttsMetrics,
      alignmentSource: speechState.alignmentSource,
    },
  };
  try {
    await navigator.clipboard.writeText(formatDeveloperJson(payload));
    toast("诊断信息已复制（不含 API Key）");
  } catch (error) {
    toast(`复制失败：${error.message}`);
  }
}

function testDeveloperVoice() {
  recordVoiceDiagnostic("manual-test", "开发者手动触发测试语音");
  startSpeech("これは音声診断テストよ。聞こえている？", "calm", {
    displayText: "这是语音诊断测试。",
    spokenTextJa: "これは音声診断テストよ。聞こえている？",
    voiceTone: "gentle",
    voiceIntensity: "normal",
    kind: "debug",
  });
}

async function restartDeveloperVoice() {
  recordVoiceDiagnostic("service-restart", "正在重启 GPT-SoVITS 服务");
  try {
    await fetch("/api/gpt-sovits/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const status = await startGptSovits({ silent: true });
    recordVoiceDiagnostic(status?.running ? "service-ready" : "service-starting", status?.message || (status?.running ? "语音服务已恢复" : "语音服务仍在启动"), status || {});
    refreshDeveloperRuntime();
  } catch (error) {
    recordVoiceDiagnostic("service-error", `语音服务重启失败：${error.message}`);
  }
}

function updateDeveloperExchange(replyZh, spokenJa, data = {}) {
  if (!DEV_MODE) return;
  if (dom.devReplyZh) dom.devReplyZh.textContent = replyZh || "-";
  if (dom.devSpokenJa) dom.devSpokenJa.textContent = spokenJa || "-";
  if (dom.devSpokenRepair) {
    const original = String(data.spokenTextJaOriginal || "").trim();
    dom.devSpokenRepair.textContent = data.spokenTextJaRepaired
      ? `已校准。原始：${original || "(空)"}`
      : "未校准或无需校准";
  }
  updateDeveloperSpeechPanel();
}

function updateDeveloperSpeechPanel() {
  if (!DEV_MODE || !dom.developerPanel) return;
  const speaking = isSpeaking(performance.now());
  if (dom.devVoiceStatus) {
    const timing = speechState.ttsMetrics?.totalMs ? ` / TTS ${speechState.ttsMetrics.totalMs}MS` : "";
    const segment = speechState.pipelineSegmentTotal > 1
      ? ` / SEG ${speechState.pipelineSegmentIndex}/${speechState.pipelineSegmentTotal}`
      : "";
    dom.devVoiceStatus.textContent = speaking
      ? `${settings.ttsProvider || DEFAULT_SETTINGS.ttsProvider} / ${speechState.alignmentSource || "NO_TIMELINE"} / ${speechState.voiceStyle || "calm"} / ${speechState.voiceTone || "neutral"}:${speechState.voiceIntensity || "normal"} / ${speechState.speechPace || "normal"}${segment} / ${speechState.charIndex}${timing}`
      : speechState.pending ? "PENDING" : "IDLE";
  }
  if (dom.devReplyZh && speechState.displayText && !speechState.textRevealPending) {
    dom.devReplyZh.textContent = speechState.displayText;
  }
  if (dom.devSpokenJa && speechState.spokenTextJa) {
    dom.devSpokenJa.textContent = speechState.spokenTextJa;
  }
  if (dom.devSpeakingJa) {
    renderHighlightedSpeech(dom.devSpeakingJa, speechState.spokenTextJa || speechState.text || "");
  }
  if (dom.devVoiceDiagnostic) dom.devVoiceDiagnostic.textContent = formatDeveloperJson(voiceDiagnostics);
  if (dom.devVoiceStopDiagnostic) {
    dom.devVoiceStopDiagnostic.textContent = formatDeveloperVoiceStopDiagnostics(speechState.ttsMetrics);
  }
  if (dom.devVoiceEvents) {
    dom.devVoiceEvents.textContent = voiceDiagnosticEvents.length
      ? voiceDiagnosticEvents.slice(-16).reverse().map(item => `[${item.time}] ${item.stage} - ${item.message}`).join("\n")
      : "-";
  }
}

function renderHighlightedSpeech(target, text) {
  const timeline = Array.isArray(speechState.timeline) ? speechState.timeline : [];
  const content = timeline.length ? timeline.map(item => String(item.text || "")).join("") : String(text || "");
  if (!content) {
    target.textContent = "-";
    return;
  }
  const liveIndex = ttsAudio && speechState.timeline?.length ? timelineIndexAt(ttsAudio.currentTime) : speechState.charIndex;
  const index = clamp(Math.floor(liveIndex || 0), 0, Math.max(0, content.length - 1));
  const windowSize = Math.max(1, Math.min(8, Math.ceil(content.length / 24)));
  const start = Math.max(0, index - Math.floor(windowSize / 2));
  const end = Math.min(content.length, start + windowSize);
  target.replaceChildren(
    devSpan(content.slice(0, start), "dev-done"),
    devSpan(content.slice(start, end), "dev-current"),
    devSpan(content.slice(end), "dev-next")
  );
}

function devSpan(text, className) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text;
  return span;
}

function beginEstimatedSpeech(duration) {
  const now = performance.now();
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
  const now = performance.now();
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
  const configuredProvider = String(settings.ttsProvider || "").trim().toLowerCase();
  const queuedProvider = String(speechState.provider || "").trim().toLowerCase();
  if (configuredProvider !== "system" || queuedProvider !== "system") {
    return failLocalSpeech(new Error("已阻止非显式的系统语音调用"), generation, "系统音回退已拦截");
  }
  if (!("speechSynthesis" in window)) {
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

  const utterance = new SpeechSynthesisUtterance(cleanText);
  utterance.lang = settings.voiceLocale || "ja-JP";
  utterance.rate = clamp(settings.speechRate * speechState.speechSpeed * voiceRateForStyle(voiceStyle), 0.55, 1.65);
  utterance.pitch = clamp(settings.voicePitch * voicePitchForStyle(voiceStyle), 0.75, 1.75);
  utterance.volume = 1;
  const voice = selectVoice();
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
    speechState.systemSpeechDeadline = performance.now() + duration * 2 + 3000;
    beginEstimatedSpeech(duration);
    recordVoiceDiagnostic("system-playing", "系统语音已经开始播放", { generation });
    settleStarted(true);
  };
  utterance.onend = () => {
    if (generation !== speechGeneration) return;
    speechState.systemSpeechActive = false;
    speechState.active = false;
    speechState.endsAt = Math.min(speechState.endsAt, performance.now() + 180);
    speakUntil = speechState.endsAt;
    protectCurrentExpressionThroughSpeech(performance.now());
    recordVoiceDiagnostic("ended", "系统语音播放完成", { generation });
    syncChatSubmitButton();
  };
  utterance.onerror = () => {
    if (generation !== speechGeneration) return settleStarted(null);
    speechState.systemSpeechActive = false;
    speechState.pending = false;
    speechState.textRevealPending = false;
    speechState.endsAt = performance.now() + Math.min(1400, duration * 0.35);
    speakUntil = speechState.endsAt;
    recordVoiceDiagnostic("system-error", "系统语音播放失败", { generation });
    syncChatSubmitButton();
    settleStarted(false);
  };
  speechState.utterance = utterance;
  startTimeout = window.setTimeout(() => {
    if (generation !== speechGeneration) return settleStarted(null);
    speechState.pending = false;
    speechState.textRevealPending = false;
    recordVoiceDiagnostic("system-timeout", "系统语音未能开始播放，文字已在失败确认后显示", { generation });
    syncChatSubmitButton();
    settleStarted(false);
  }, 12000);
  window.speechSynthesis.speak(utterance);
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
    // which already applies settings.speechRate, is not accelerated twice.
    speechSpeed: clamp(toNumber(speechSpeed, 1) * settings.speechRate, 0.62, 1.45),
    speechPace: normalizeSpeechPace(speechPace),
    speechLanguage: speechLanguageProfile().speechLanguage,
    speechProsody: normalizeSpeechProsody(settings.speechProsody),
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
    provider: String(settings.ttsProvider || "").trim().toLowerCase(),
    preset: String(settings.gptSovitsModelPreset || ""),
    endpoint: String(settings.ttsEndpoint || ""),
  });
}

async function requestPreparedTts(ttsRequest, generation = null, signal = null) {
  const prepared = await fetch("/api/tts-prepare", {
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
  if (!blob && String(settings.ttsProvider || "").toLowerCase() === "gpt-sovits") {
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
        const timed = await fetch("/api/tts-timed", {
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
    const response = await fetch("/api/tts", {
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
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
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
    speechState.endsAt = performance.now() + 180;
    speakUntil = speechState.endsAt;
    protectCurrentExpressionThroughSpeech(performance.now());
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
  const startedAt = performance.now();
  for (let attempt = 0; attempt < 100; attempt += 1) {
    assertCurrentSpeech(generation);
    const response = await fetch("/api/tts-alignment", {
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
        alignmentReadyMs: Math.round(performance.now() - startedAt),
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
    await new Promise(resolve => setTimeout(resolve, 120));
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
  URL.revokeObjectURL(ttsAudioUrl);
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
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
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
  return clamp((weighted * 105 * styleFactor) / Math.max(0.55, settings.speechRate * speechSpeed), 1000, 16000);
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

function isSpeaking(now = performance.now()) {
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

function speechMouthOpen(now = performance.now()) {
  if (!isSpeaking(now) || !speechState.text) return 0;
  const elapsed = now - speechState.startedAt;
  const timedShape = mouthShapeAt(now);
  const char = currentSpeechChar(now);
  const shape = timedShape || mouthShapeForChar(char);
  const pause = /[。！？.!?]/.test(char) ? 0.18 : /[，、；：,;:]/.test(char) ? 0.42 : 1;
  const styleEnergy = speechState.voiceStyle === "energetic" ? 1.12 : speechState.voiceStyle === "sharp" ? 1.05 : speechState.voiceStyle === "soft" ? 0.9 : 1.02;
  const mouthScale = speechState.voiceTone === "flustered" ? 1.48 : speechState.voiceTone === "comforting" ? 1.26 : 1.4;
  const beat = Math.pow(Math.sin(elapsed * 0.033 * settings.speechRate), 2);
  const secondBeat = Math.pow(Math.sin(elapsed * 0.019 + 0.8), 2) * 0.045;
  const boundary = animeSpeechBoundaryEnvelope(now);
  const audioDriven = audioMouthLevel > 0.015 ? audioMouthLevel : 0;
  const hasTimedShape = Boolean(timedShape);
  const phonemePulse = hasTimedShape ? 0.98 + beat * 0.035 : 0.78 + beat * 0.2;
  const phonemeDriven = (shape.open * mouthScale * phonemePulse + (hasTimedShape ? secondBeat * 0.32 : secondBeat)) * pause;
  const audioBlend = hasTimedShape ? audioDriven * 0.5 : audioDriven * (0.94 + shape.open * 0.7);
  return clamp(Math.max(audioBlend, phonemeDriven) * styleEnergy * boundary, 0, 0.96);
}

function speechMouthForm(now = performance.now()) {
  if (!isSpeaking(now) || !speechState.text) return 0;
  const shape = mouthShapeAt(now) || mouthShapeForChar(currentSpeechChar(now));
  const elapsed = now - speechState.startedAt;
  const wobble = speechState.mouthCues.length ? 0.006 : 0.028;
  return clamp((shape.form + Math.sin(elapsed * 0.012) * wobble) * animeSpeechBoundaryEnvelope(now), -0.62, 0.62);
}

function mouthCurveWithSpeech(emotionCurve, speechFormDelta) {
  const baseCurve = clamp(toNumber(emotionCurve, 0), -1, 1);
  let result = baseCurve + clamp(toNumber(speechFormDelta, 0), -0.7, 0.7) * 0.38;
  // Speech may widen/narrow a mouth, but it must not drag a deliberate smile
  // or frown through zero and briefly recreate the folded neutral wave.
  if (baseCurve >= 0.22) result = Math.max(result, baseCurve * 0.58);
  if (baseCurve <= -0.22) result = Math.min(result, baseCurve * 0.58);
  return clamp(result, -1, 1);
}

function mouthTemplateCurveForEmotion(emotion, fallbackCurve = 0, expressionPlan = null) {
  if (Number.isFinite(expressionPlan?.mouthForm)) {
    return clamp(expressionPlan.mouthForm, -1, 1);
  }
  const family = MOUTH_TEMPLATE_FAMILY_BY_EMOTION[emotion] || "";
  if (family === "smile") return clamp(Math.max(0.82, fallbackCurve), -1, 1);
  if (family === "awkward") return 0;
  // Do not use the numeric endpoint as a semantic anger maximum. On the
  // annotated AiPoet rig the clearest angry mouth is around -0.34.
  if (family === "angry") return clamp(fallbackCurve, -0.48, -0.24);
  return clamp(toNumber(fallbackCurve, 0), -1, 1);
}

function mouthShapeAt(now = performance.now()) {
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

function createAnimeMouthState() {
  return {
    group: "closed",
    shape: { open: 0.01, form: 0 },
    lastSwitchAt: 0,
    holdUntil: 0,
    lastUpdateAt: 0,
    cachedAt: 0,
    cachedShape: null,
  };
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

function animeSpeechBoundaryEnvelope(now = performance.now()) {
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

function animeVisemeGroupForCue(value) {
  switch (value) {
    case "A":
    case "X":
      return "closed";
    case "D":
      return "open";
    case "E":
      return "round";
    case "F":
      return "pucker";
    case "C":
    case "H":
      return "wide";
    case "G":
      return "medium";
    case "B":
    default:
      return "small";
  }
}

function animeShapeForGroup(group) {
  switch (group) {
    case "open":
      return { open: 0.72, form: 0.03 };
    case "round":
      return { open: 0.5, form: -0.34 };
    case "pucker":
      return { open: 0.34, form: -0.54 };
    case "wide":
      return { open: 0.42, form: 0.36 };
    case "medium":
      return { open: 0.48, form: 0.08 };
    case "small":
      return { open: 0.3, form: -0.02 };
    case "closed":
    default:
      return { open: 0.01, form: 0 };
  }
}

function mouthShapeDistance(a, b) {
  return Math.abs(a.open - b.open) + Math.abs(a.form - b.form) * 0.55;
}

function lerpMouthShape(from, to, amount) {
  return {
    open: from.open + (to.open - from.open) * amount,
    form: from.form + (to.form - from.form) * amount,
  };
}

function smoothstep(value) {
  const x = clamp(value, 0, 1);
  return x * x * (3 - 2 * x);
}

function mouthShapeForCue(value) {
  switch (value) {
    case "A":
      return { open: 0.04, form: 0 };
    case "B":
      return { open: 0.12, form: -0.08 };
    case "C":
      return { open: 0.34, form: 0.34 };
    case "D":
      return { open: 0.66, form: 0.06 };
    case "E":
      return { open: 0.48, form: -0.26 };
    case "F":
      return { open: 0.26, form: -0.5 };
    case "G":
      return { open: 0.18, form: 0.16 };
    case "H":
      return { open: 0.38, form: 0.26 };
    case "X":
    default:
      return { open: 0.01, form: 0 };
  }
}

function currentSpeechChar(now = performance.now()) {
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

function mouthShapeForChar(char) {
  if (!char || /[\s]/.test(char)) return { open: 0.02, form: 0 };
  if (/[。！？.!?]/.test(char)) return { open: 0.04, form: 0 };
  if (/[，、；：,;:]/.test(char)) return { open: 0.1, form: 0 };
  if (/[あアぁァかカがガさサざザたタだダなナはハばバぱパまマやヤゃャらラわワ啊阿呀哈]/.test(char)) return { open: 0.62, form: 0.02 };
  if (/[いイぃィきキぎギしシじジちチぢヂにニひヒびビぴピみミりリ一衣已]/.test(char)) return { open: 0.2, form: 0.42 };
  if (/[うウぅゥくクぐグすスずズつツっッづヅぬヌふフぶブぷプむムゆユゅュるル呜唔]/.test(char)) return { open: 0.24, form: -0.46 };
  if (/[えエぇェけケげゲせセぜゼてテでデねネへヘべベぺペめメれレ诶欸]/.test(char)) return { open: 0.38, form: 0.22 };
  if (/[おオぉォこコごゴそソぞゾとトどドのノほホぼボぽポもモよヨょョろロをヲ哦喔]/.test(char)) return { open: 0.46, form: -0.28 };
  if (/[んン嗯唔mMnN]/.test(char)) return { open: 0.08, form: -0.08 };
  return { open: 0.34, form: 0.02 };
}

function updateConnectionStatus(data = {}) {
  const source = String(data.source || "").toLowerCase();
  const memoryCount = Array.isArray(data.memories) ? data.memories.length : state.memories.length;
  if (data.connectionState === "startup-greeting-fallback") {
    connectionStatus = `STARTUP GREETING LOCAL / MEM ${memoryCount}`;
  } else if (data.connectionState === "backend-reconnecting") {
    connectionStatus = `BACKEND RECONNECT ${data.attempt || 1}/${data.attempts || 3} / MEM ${memoryCount}`;
  } else if (data.connectionState === "backend-failed") {
    connectionStatus = `LOCAL BACKEND LOST / MEM ${memoryCount}`;
  } else if (data.connectionState === "backend-restored") {
    connectionStatus = `${settings.modelConfigured ? "API READY" : "LOCAL"} / MEM ${memoryCount}`;
  } else if (data.connectionState === "reconnecting") {
    connectionStatus = `API RECONNECT ${data.attempt || 1}/${data.attempts || 3} / MEM ${memoryCount}`;
  } else if (data.connectionState === "failed") {
    const code = String(data.connectionCode || "CONNECTION").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 24);
    connectionStatus = `API ERROR ${code || "CONNECTION"} / MEM ${memoryCount}`;
  } else if (data.toolUsed) {
    const toolState = data.toolStatus === "failed" ? "FAILED" : "OK";
    connectionStatus = `TOOL ${String(data.toolUsed).toUpperCase()} ${toolState} / MEM ${memoryCount}`;
  } else if (source === "api") {
    connectionStatus = `API ${data.latencyMs ? `${data.latencyMs}MS` : "OK"} / MEM ${memoryCount}`;
  } else if (source === "tool") {
    connectionStatus = `TOOL ${data.toolUsed || "LOCAL"} / MEM ${memoryCount}`;
  } else if (settings.modelConfigured) {
    connectionStatus = `API READY / MEM ${memoryCount}`;
  } else {
    connectionStatus = `LOCAL / MEM ${memoryCount}`;
  }
  updateModelStateLabel();
}

function connectionErrorSummary(error) {
  const code = String(error?.code || "").trim();
  let message = String(error?.message || "未知连接错误").trim();
  message = message
    .replace(/\bsk-[A-Za-z0-9_-]+\b/gi, "[API KEY 已隐藏]")
    .replace(/\bBearer\s+[A-Za-z0-9._-]+\b/gi, "Bearer [已隐藏]")
    .replace(/\s+/g, " ")
    .slice(0, 180);
  if (/abort|timeout|timed out|超时/i.test(`${code} ${message}`)) message = `请求超时；${message}`;
  return `${code ? `${code}: ` : ""}${message}`;
}

function chatRequestId(group, generation, inputCount) {
  const safeGroup = String(group || "turn").replace(/[^A-Za-z0-9._:-]/g, "").slice(0, 80);
  return `${safeGroup}:g${generation}:n${inputCount}`;
}

function retryableChatStatus(status) {
  return [408, 425, 429, 500, 502, 503, 504].includes(Number(status));
}

async function requestNativeBackendRecovery() {
  if (!window.pywebview?.api?.ensureBackend) return false;
  try {
    const result = await Promise.race([
      window.pywebview.api.ensureBackend(),
      delay(5000).then(() => ({ ok: false, error: "native_recovery_timeout" })),
    ]);
    return Boolean(result?.ok);
  } catch (error) {
    console.warn("native backend recovery failed:", error);
    return false;
  }
}

async function probeLocalBackend() {
  if (chatRequestInFlight) return true;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch("/api/health", { cache: "no-store", signal: controller.signal });
    const data = response.ok ? await response.json() : null;
    if (!data?.ok || data?.service !== "amadeus-local") throw new Error(`HTTP ${response.status}`);
    if (/^(LOCAL BACKEND LOST|BACKEND RECONNECT)/.test(connectionStatus)) {
      updateConnectionStatus({ connectionState: "backend-restored" });
    }
    return true;
  } catch {
    updateConnectionStatus({ connectionState: "backend-reconnecting" });
    const recovered = await requestNativeBackendRecovery();
    if (!recovered) updateConnectionStatus({ connectionState: "backend-failed" });
    return recovered;
  } finally {
    window.clearTimeout(timeout);
  }
}

function startConnectionWatchdog() {
  if (connectionWatchdogTimer) window.clearInterval(connectionWatchdogTimer);
  connectionWatchdogTimer = window.setInterval(probeLocalBackend, 12000);
}

function validateFinalTurnEnvelope(result, requestId) {
  const envelope = result?.finalTurnEnvelope;
  const expectedTurnId = `${requestId}:turn`;
  const required = Array.isArray(envelope?.required_acts) ? envelope.required_acts.map(String) : [];
  const completed = new Set(
    Array.isArray(envelope?.completed_acts) ? envelope.completed_acts.map(String) : [],
  );
  const valid = Boolean(
    envelope
    && typeof envelope === "object"
    && String(envelope.turn_id || "") === expectedTurnId
    && envelope.turn_complete === true
    && envelope.standalone === true
    && envelope.depends_on_undelivered_prefix === false
    && required.every(item => completed.has(item))
  );
  if (!valid) {
    const error = new Error("最终回答信封与当前请求或必答合同不一致");
    error.streamFallbackAllowed = false;
    error.streamErrorType = "invalid_final_turn_envelope";
    error.streamErrorStage = "client_delivery_validation";
    throw error;
  }
  return envelope;
}

function createChatUtterancePlayback(generation, requestStartedAt, requestId = "") {
  speechPipelineAbortController?.abort();
  speechPipelineAbortController = new AbortController();
  const controller = speechPipelineAbortController;
  const pipelineToken = ++speechPipelineGeneration;
  speechPipelineActive = true;
  activeAnswerRequestId = String(requestId || activeAnswerRequestId || "");
  syncChatSubmitButton();
  const queue = [];
  let cursor = 0;
  let closed = false;
  let running = false;
  let wake = null;
  let progressiveMessage = null;
  let finalResult = null;
  let toolGateReleased = false;
  let turnCompleted = false;
  let textFallbackAfterVoiceFailure = false;
  let pipelineCancelled = false;
  const deliveryContextEpoch = Math.max(0, Number(state.contextEpoch) || 0);
  const deliveryTurnId = `${requestId}:turn`;

  const recordDelivery = (stage, item = null) => {
    storageScopedFetch("/api/conversation/delivery", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requestId,
        turnId: String(finalResult?.finalTurnEnvelope?.turn_id || deliveryTurnId),
        utteranceId: String(item?.utteranceId || ""),
        stage,
        contextEpoch: deliveryContextEpoch,
      }),
    }).catch(error => {
      recordVoiceDiagnostic(
        "delivered-turn-event-failed",
        `交付回执写入失败：${error.message}`,
        { requestId, stage, utteranceId: String(item?.utteranceId || "") },
      );
    });
  };

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
      if (timer) window.clearTimeout(timer);
      resolve();
    };
    wake = settle;
    if (pollMs > 0) timer = window.setTimeout(settle, pollMs);
  });
  const settlePipeline = () => {
    if (pipelineToken !== speechPipelineGeneration) return;
    speechPipelineActive = false;
    speechPipelineAbortController = null;
    if (activeAnswerRequestId === requestId) activeAnswerRequestId = "";
    syncChatSubmitButton();
    // The request owns the thinking indicator. A stream may settle because it
    // is handing off to the complete-response fallback, so playback cleanup
    // must not make an unfinished turn appear idle.
  };
  const releaseToolGate = (item, voiceStarted = false) => {
    if (item.utteranceId !== "tool-preamble" || toolGateReleased) return;
    toolGateReleased = true;
    storageScopedFetch("/api/chat-stream/continue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requestId, voiceStarted: Boolean(voiceStarted) }),
    }).then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).then(data => {
      recordVoiceDiagnostic(
        "tool-gate-released",
        voiceStarted
          ? "复杂任务过渡语已开始播放，后端现在可以调用工具"
          : "复杂任务过渡语未能播放，已明确记录语音失败并继续工具流程",
        { requestId, voiceStarted: Boolean(voiceStarted), released: Boolean(data?.released) },
      );
    }).catch(error => {
      recordVoiceDiagnostic(
        "tool-gate-release-failed",
        `无法确认复杂任务过渡语的播放状态：${error.message}`,
        { requestId },
      );
    });
  };
  const markDeliveryPartial = (item, failedIndex) => {
    if (progressiveMessage?.message?.meta) {
      progressiveMessage.message.meta.voicePartialFailure = true;
      progressiveMessage.message.meta.voiceFailedSegment = failedIndex + 1;
      progressiveMessage.message.meta.deliveryPartial = true;
      persist();
    }
    releaseToolGate(item, false);
  };
  const reveal = (item, voiceStarted = false) => {
    if (item.revealed) return;
    item.revealed = true;
    const messageMeta = {
      source: finalResult?.source || "api-stream",
      latencyMs: finalResult?.latencyMs,
      memoryCount: state.memories.length,
      spokenTextJa: item.spokenTextJa,
      speechText: item.speechText,
      speechLanguage: item.speechLanguage,
      speechUnitId: item.speechUnitId,
      boundaryAfter: item.boundaryAfter,
      kind: "chat",
      sources: Array.isArray(finalResult?.sources) ? finalResult.sources : [],
      files: Array.isArray(finalResult?.files) ? finalResult.files : [],
      streamedUtterance: true,
      utteranceId: item.utteranceId,
      progressOnly: Boolean(item.progressOnly),
      progressKind: String(item.progressKind || ""),
      dialoguePolicy: finalResult?.dialoguePolicy || null,
      dialogueValidation: finalResult?.dialogueValidation || null,
      toolUsed: String(finalResult?.toolUsed || ""),
      toolStatus: String(finalResult?.toolStatus || ""),
      actionReceipt: finalResult?.actionReceipt && typeof finalResult.actionReceipt === "object"
        ? { ...finalResult.actionReceipt }
        : null,
      todoContext: finalResult?.todoContext && typeof finalResult.todoContext === "object"
        ? { ...finalResult.todoContext }
        : null,
      ...messageIdentityMeta(finalResult, "assistant"),
      requestId,
      turnId: String(finalResult?.turnIdentity?.turnId || `${requestId}:turn`),
    };
    if (!progressiveMessage) {
      progressiveMessage = beginProgressiveAssistantMessage(item.textZh, messageMeta);
      progressiveMessage.spokenParts = [item.speechText];
      progressiveMessage.message.meta.progressiveComplete = false;
      activeAssistantMessage = progressiveMessage.message;
    } else {
      appendProgressiveAssistantSegment(
        progressiveMessage,
        item.textZh,
        item.speechText,
        item.displayIndex,
        Math.max(queue.length, item.displayIndex + 1),
      );
    }
    if (item.utteranceId === "tool-preamble") {
      releaseToolGate(item, voiceStarted);
      showThinkingIndicator(generation, "tool-executing", "开场语已开始播放，正在执行所需工具");
    } else {
      hideThinkingIndicator(generation);
    }
    recordDelivery("text_revealed", item);
  };
  const prepare = item => {
    if (item.preparationStarted) return item.promise;
    item.preparationStarted = true;
    const provider = String(settings.ttsProvider || "").trim().toLowerCase();
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
    if (!settings.voiceEnabled || provider !== "gpt-sovits") {
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
            && !isSpeaking(performance.now())
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
        speechLanguage: normalizeSpeechLanguage(raw?.speechLanguage || settings.speechLanguage),
        speechProsody: normalizeSpeechProsody(raw?.speechProsody || settings.speechProsody),
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
        speechLanguage: normalizeSpeechLanguage(result?.speechLanguage || settings.speechLanguage),
        speechPace: resolveSpeechPace(result || {}, ""),
        speechSpeed: resolveSpeechSpeed(result || {}, "", resolveSpeechPace(result || {}, "")),
        textOnly: Boolean(result?.textOnlyDelivery) && !chooseSpeechText(
          unit.textZh,
          unit.spokenTextJa,
          result?.speechLanguage || settings.speechLanguage,
        ),
      }));
    },
    setFinalResult(result) {
      finalResult = result;
      recordDelivery("turn_envelope_validated");
      bindCompletedTurnIdentity(result, progressiveMessage?.message || null);
      if (progressiveMessage?.message?.meta) {
        Object.assign(progressiveMessage.message.meta, {
          source: result?.source || "api-stream",
          latencyMs: result?.latencyMs,
          toolUsed: String(result?.toolUsed || ""),
          toolStatus: String(result?.toolStatus || ""),
          actionReceipt: result?.actionReceipt && typeof result.actionReceipt === "object"
            ? { ...result.actionReceipt }
            : null,
          todoContext: result?.todoContext && typeof result.todoContext === "object"
            ? { ...result.todoContext }
            : null,
          sources: Array.isArray(result?.sources) ? result.sources : [],
          files: Array.isArray(result?.files) ? result.files : [],
          dialoguePolicy: result?.dialoguePolicy || null,
          dialogueValidation: result?.dialogueValidation || null,
          progressiveComplete: true,
          ...messageIdentityMeta(result, "assistant"),
        });
        const metaNode = progressiveMessage.bubble?.querySelector(".meta");
        if (metaNode) metaNode.textContent = assistantMetaLabel(progressiveMessage.message.meta);
        if (progressiveMessage.bubble) {
          progressiveMessage.bubble.dataset.messageId = String(progressiveMessage.message.meta.messageId || "");
          progressiveMessage.bubble.dataset.turnId = String(progressiveMessage.message.meta.turnId || "");
        }
        persist();
      }
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
      return progressiveMessage?.message || null;
    },
    get queuedCount() {
      return queue.length;
    },
  };
}

async function fetchChatUtteranceStream(payload, controller, playback) {
  const response = await storageScopedFetch("/api/chat-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal: controller.signal,
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) {
    const error = new Error(await response.text() || `HTTP ${response.status}`);
    error.retryable = true;
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResult = null;
  let finalEnvelopeValidated = false;
  const streamRequestId = String(payload?.requestId || "");
  let route = "";
  let hadUtterance = false;
  let hadAnswerUtterance = false;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split(/\r?\n/);
    buffer = done ? "" : lines.pop() || "";
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch (parseError) {
        const error = new Error("流式接口返回了无法解析的 NDJSON 事件");
        error.cause = parseError;
        error.streamFallbackAllowed = !hadUtterance;
        error.streamErrorType = "stream_event_parse_failed";
        error.streamErrorStage = "client_ndjson_parse";
        throw error;
      }
      const eventType = String(event.type || "");
      const eventPayload = event.payload && typeof event.payload === "object" ? event.payload : {};
      if (eventType === "turn_header") {
        route = String(eventPayload.route || route);
        updateThinkingIndicatorStage(
          route === "fast_chat" ? "model-generating" : "context",
          route === "fast_chat" ? "快路径已建立，等待第一条完整语句" : "已完成请求分流",
        );
      } else if (eventType === "utterance") {
        hadUtterance = true;
        hadAnswerUtterance = true;
        playback.enqueue(eventPayload);
      } else if (eventType === "progress_utterance") {
        hadUtterance = true;
        playback.enqueue({ ...eventPayload, progressOnly: true });
      } else if (eventType === "tool_status") {
        updateThinkingIndicatorStage(
          String(eventPayload.stage || "tool-executing"),
          String(eventPayload.displayText || "正在执行工具"),
        );
      } else if (eventType === "turn_trailer") {
        finalResult = eventPayload.finalResult || finalResult;
        route = String(eventPayload.route || route);
        // Route describes execution policy, not whether answer speech was
        // already delivered. A tool-gated turn may still stream its complete
        // answer, so replay only when no answer utterance was observed.
        // Progress utterances do not count as the final answer.
        if (finalResult) {
          // Validate the terminal contract before enqueueing its TTS. A bad
          // envelope must terminate the turn without starting disposable work.
          validateFinalTurnEnvelope(finalResult, streamRequestId);
          finalEnvelopeValidated = true;
          if (!hadAnswerUtterance) playback.enqueueFinal(finalResult);
        }
      } else if (eventType === "turn_abort") {
        const error = new Error(String(eventPayload.message || eventPayload.reason || "流式回答中断"));
        error.streamFallbackAllowed = Boolean(eventPayload.fallbackAllowed) && !hadUtterance;
        error.streamErrorType = String(eventPayload.errorType || eventPayload.reason || "stream_failed");
        error.streamErrorStage = String(eventPayload.stage || "stream");
        error.connectionFailure = Boolean(eventPayload.connectionFailure);
        error.completedTools = Array.isArray(eventPayload.completedTools) ? eventPayload.completedTools : [];
        error.streamCancelled = Boolean(eventPayload.cancelled) || isStreamCancellation(error);
        throw error;
      }
    }
    if (done) break;
  }
  if (!finalResult) {
    const error = new Error("流式接口没有返回完整收尾");
    error.streamFallbackAllowed = !hadUtterance;
    throw error;
  }
  if (!finalEnvelopeValidated) {
    validateFinalTurnEnvelope(finalResult, String(payload?.requestId || ""));
  }
  playback.setFinalResult(finalResult);
  playback.close();
  return { data: finalResult, route, hadUtterance };
}

async function fetchChatWithReconnect(payload, controller) {
  const attempts = 3;
  const retryDelays = [650, 1800];
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    if (controller.signal.aborted) throw new DOMException("Request aborted", "AbortError");
    try {
      const response = await storageScopedFetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify(payload),
      });
      const responseText = await response.text();
      let data = {};
      try {
        data = responseText ? JSON.parse(responseText) : {};
      } catch {
        const parseError = new Error(`本地服务返回了无法解析的响应（HTTP ${response.status}）`);
        parseError.retryable = response.ok || retryableChatStatus(response.status);
        throw parseError;
      }
      if (!response.ok || data.ok === false) {
        const error = new Error(data.error || `HTTP ${response.status}`);
        error.code = String(data.code || "");
        error.retryable = typeof data.retryable === "boolean"
          ? data.retryable
          : retryableChatStatus(response.status);
        throw error;
      }
      if (attempt > 1) toast("连接已恢复，刚才的问题没有丢失");
      return data;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") throw error;
      lastError = error;
      const retryable = error?.retryable !== false;
      if (!retryable || attempt >= attempts) break;
      const localFailure = !error?.code && (error instanceof TypeError || /fetch|网络|解析/.test(String(error?.message || "")));
      updateConnectionStatus({
        connectionState: localFailure ? "backend-reconnecting" : "reconnecting",
        attempt,
        attempts,
      });
      if (localFailure) await requestNativeBackendRecovery();
      await delay(retryDelays[attempt - 1] || 1200);
    }
  }
  throw lastError || new Error("聊天连接未返回结果");
}

function updateModelStateLabel() {
  if (!dom.modelState) return;
  dom.modelState.textContent = runtimeModeStateLabel(`${currentEmotion.toUpperCase()} / ${activeGesture || "IDLE"} / ${connectionStatus}`);
}

const MAX_CHAT_ATTACHMENTS = 6;
const MAX_CHAT_IMAGE_BYTES = 32 * 1024 * 1024;
const MAX_CHAT_FILE_BYTES = 24 * 1024 * 1024;
const MAX_CHAT_ATTACHMENTS_TOTAL_BYTES = 64 * 1024 * 1024;
const IMAGE_UPLOAD_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp", "gif", "bmp"]);

function isImageUpload(file) {
  const extension = String(file?.name || "").split(".").pop().toLowerCase();
  return String(file?.type || "").startsWith("image/") || IMAGE_UPLOAD_EXTENSIONS.has(extension);
}

function attachmentId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `file-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function formatFileSize(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function addPendingAttachments(files) {
  const incoming = Array.isArray(files) ? files : [];
  let total = pendingAttachments.reduce((sum, item) => sum + item.file.size, 0);
  let rejected = "";
  for (const file of incoming) {
    if (!(file instanceof File) || !file.size) {
      rejected = "不能添加空文件";
      continue;
    }
    if (pendingAttachments.length >= MAX_CHAT_ATTACHMENTS) {
      rejected = `一次最多添加 ${MAX_CHAT_ATTACHMENTS} 个附件`;
      break;
    }
    const fileLimit = isImageUpload(file) ? MAX_CHAT_IMAGE_BYTES : MAX_CHAT_FILE_BYTES;
    if (file.size > fileLimit) {
      rejected = `${file.name} 超过 ${fileLimit / 1024 / 1024} MB`;
      continue;
    }
    if (total + file.size > MAX_CHAT_ATTACHMENTS_TOTAL_BYTES) {
      rejected = "本次附件总大小不能超过 64 MB";
      continue;
    }
    const duplicate = pendingAttachments.some(item =>
      item.file.name === file.name && item.file.size === file.size && item.file.lastModified === file.lastModified
    );
    if (duplicate) continue;
    const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : "";
    pendingAttachments.push({ id: attachmentId(), file, previewUrl });
    total += file.size;
  }
  renderPendingAttachments();
  if (rejected) toast(rejected);
}

function removePendingAttachment(id) {
  const target = pendingAttachments.find(item => item.id === id);
  if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
  pendingAttachments = pendingAttachments.filter(item => item.id !== id);
  renderPendingAttachments();
}

function clearPendingAttachments() {
  pendingAttachments.forEach(item => {
    if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  });
  pendingAttachments = [];
  renderPendingAttachments();
}

function renderPendingAttachments() {
  if (!dom.attachmentTray) return;
  dom.attachmentTray.innerHTML = "";
  dom.attachmentTray.classList.toggle("hidden", !pendingAttachments.length);
  pendingAttachments.forEach(item => {
    const card = document.createElement("div");
    card.className = "pending-attachment";
    if (item.previewUrl) {
      const image = document.createElement("img");
      image.src = item.previewUrl;
      image.alt = "";
      card.appendChild(image);
    } else {
      const icon = document.createElement("span");
      icon.className = "attachment-icon";
      icon.textContent = "FILE";
      card.appendChild(icon);
    }
    const label = document.createElement("span");
    label.className = "attachment-label";
    label.textContent = item.file.name;
    label.title = `${item.file.name} · ${formatFileSize(item.file.size)}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove-attachment-btn";
    remove.textContent = "×";
    remove.title = `移除 ${item.file.name}`;
    remove.setAttribute("aria-label", `移除 ${item.file.name}`);
    remove.addEventListener("click", () => removePendingAttachment(item.id));
    card.append(label, remove);
    dom.attachmentTray.appendChild(card);
  });
  syncChatSubmitButton();
}

function bindAttachmentDropEvents() {
  if (!dom.chatWrap) return;
  let dragDepth = 0;
  const hasFiles = event => [...(event.dataTransfer?.types || [])].includes("Files");
  dom.chatWrap.addEventListener("dragenter", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    dom.chatWrap.classList.add("drag-active");
  });
  dom.chatWrap.addEventListener("dragover", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
  dom.chatWrap.addEventListener("dragleave", event => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dom.chatWrap.classList.remove("drag-active");
  });
  dom.chatWrap.addEventListener("drop", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    dom.chatWrap.classList.remove("drag-active");
    addPendingAttachments([...(event.dataTransfer?.files || [])]);
  });
}

function resizeChatInput() {
  if (!dom.userInput) return;
  dom.userInput.style.height = "auto";
  dom.userInput.style.height = `${Math.min(108, Math.max(38, dom.userInput.scrollHeight))}px`;
  if (dom.hud) applyChatWindowHeight(state.chatWindowHeight, { persist: false });
}

function chatComposerHasDraft() {
  return Boolean(String(dom.userInput?.value || "").trim() || pendingAttachments.length);
}

function answerIsBusy() {
  return Boolean(
    chatRequestInFlight
    || speechPipelineActive
    || speechState.pending
    || isSpeaking(performance.now())
  );
}

function shouldStopCurrentAnswer() {
  return answerIsBusy() && !chatComposerHasDraft();
}

function syncChatSubmitButton() {
  if (!dom.chatSubmitBtn) return;
  const stopping = shouldStopCurrentAnswer();
  dom.chatSubmitBtn.classList.toggle("is-stop", stopping);
  dom.chatSubmitBtn.title = stopping ? "终止当前回答" : "发送";
  dom.chatSubmitBtn.setAttribute("aria-label", stopping ? "终止当前回答" : "发送");
}

function notifyBackendAnswerCancellation(requestId) {
  const normalized = String(requestId || "").trim();
  if (!normalized) return;
  storageScopedFetch("/api/chat/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestId: normalized }),
  }).catch(error => {
    console.warn("backend answer cancellation failed:", error);
  });
}

function stopCurrentAnswer() {
  if (!answerIsBusy()) return;
  const requestId = String(activeChatRequestId || activeAnswerRequestId || "").trim();
  const stoppedGeneration = chatRequestGeneration;
  chatAbortController?.abort();
  chatAbortController = null;
  chatRequestGeneration += 1;
  chatRequestInFlight = false;
  stopThinkingProgressPolling(requestId);
  hideThinkingIndicator(stoppedGeneration);
  resetActiveConversationRequestState();
  supersededChatRequestId = "";
  interruptCurrentSpeech();
  activeChatRequestId = "";
  activeAnswerRequestId = "";
  syncChatSubmitButton();
  setEmotion("neutral", "idle", { restart: true });
  notifyBackendAnswerCancellation(requestId);
  toast("已终止当前回答");
}

function chatWindowHeightBounds() {
  const viewportMax = Math.max(140, Math.floor(window.innerHeight - 24));
  const titleHeight = dom.hud?.querySelector(".titlebar")?.getBoundingClientRect().height || 40;
  const formHeight = dom.chatForm?.getBoundingClientRect().height || 54;
  const quickActions = dom.hud?.querySelector(".quick-actions");
  const quickHeight = quickActions && getComputedStyle(quickActions).display !== "none"
    ? quickActions.getBoundingClientRect().height
    : 0;
  const readableMinimum = Math.ceil(titleHeight + quickHeight + formHeight + 156);
  return {
    min: Math.min(viewportMax, Math.max(220, readableMinimum)),
    max: viewportMax,
  };
}

function applyChatWindowHeight(value, { persist: shouldPersist = false } = {}) {
  if (!dom.hud) return 330;
  const bounds = chatWindowHeightBounds();
  const height = Math.round(clamp(toNumber(value, 330), bounds.min, bounds.max));
  state.chatWindowHeight = height;
  dom.hud.style.setProperty("--chat-window-height", `${height}px`);
  if (dom.chatResizeHandle) {
    dom.chatResizeHandle.setAttribute("aria-valuemin", String(bounds.min));
    dom.chatResizeHandle.setAttribute("aria-valuemax", String(bounds.max));
    dom.chatResizeHandle.setAttribute("aria-valuenow", String(height));
    dom.chatResizeHandle.title = `拖动对话面板上边缘以调整高度（当前 ${height}px，双击恢复）`;
  }
  if (shouldPersist) persist();
  return height;
}

function bindChatResizeHandle() {
  const handle = dom.chatResizeHandle;
  if (!handle) return;
  applyChatWindowHeight(state.chatWindowHeight, { persist: false });
  let drag = null;

  const finish = () => {
    if (!drag) return;
    drag = null;
    handle.classList.remove("is-resizing");
    document.body.classList.remove("chat-resizing");
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    persist();
    scrollChatToLatest();
  };
  const move = event => {
    if (!drag) return;
    event.preventDefault();
    applyChatWindowHeight(drag.startHeight + drag.startY - event.clientY, { persist: false });
  };

  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0 || document.body.classList.contains("full-layout")) return;
    event.preventDefault();
    event.stopPropagation();
    drag = {
      startY: event.clientY,
      startHeight: applyChatWindowHeight(state.chatWindowHeight, { persist: false }),
    };
    handle.classList.add("is-resizing");
    document.body.classList.add("chat-resizing");
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  });
  handle.addEventListener("dblclick", () => {
    applyChatWindowHeight(330, { persist: true });
    scrollChatToLatest();
  });
  handle.addEventListener("keydown", event => {
    const bounds = chatWindowHeightBounds();
    const step = event.shiftKey ? 48 : 16;
    let next = null;
    if (event.key === "ArrowUp") next = state.chatWindowHeight + step;
    if (event.key === "ArrowDown") next = state.chatWindowHeight - step;
    if (event.key === "Home") next = bounds.min;
    if (event.key === "End") next = bounds.max;
    if (next === null) return;
    event.preventDefault();
    applyChatWindowHeight(next, { persist: true });
    scrollChatToLatest();
  });
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunks = [];
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + chunkSize)));
  }
  return btoa(chunks.join(""));
}

async function serializeAttachments(items) {
  return Promise.all(items.map(async item => ({
    id: item.id,
    name: item.file.name,
    mimeType: item.file.type || "application/octet-stream",
    size: item.file.size,
    dataBase64: arrayBufferToBase64(await item.file.arrayBuffer()),
  })));
}

function attachmentMessageMetadata(items) {
  return items.map(item => ({
    id: item.id,
    name: item.name,
    mimeType: item.mimeType,
    size: item.size,
    kind: item.mimeType?.startsWith("image/") ? "image" : "file",
  }));
}

function conversationFocusForMessages(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role !== "assistant") continue;
    // The stored progressive content contains only segments already revealed.
    // Never replace it with finalResult or a queued, unplayed segment.
    const text = String(message.content || "").trim();
    if (!text) return null;
    const user = messages.slice(0, index).reverse().find(item => item?.role === "user");
    return {
      conversationId: String(message.meta?.conversationId || state.conversationId || ""),
      messageId: String(message.meta?.messageId || ""),
      turnId: String(message.meta?.turnId || ""),
      requestId: String(message.meta?.requestId || ""),
      utteranceId: String(message.meta?.utteranceId || ""),
      text: text.slice(-3000),
      userText: String(user?.content || "").slice(-1000),
      partial: message.meta?.progressiveComplete === false,
      progressOnly: Boolean(message.meta?.progressOnly),
      followsLatestUser: index === messages.length - 1,
    };
  }
  return null;
}

async function sendMessage() {
  const text = dom.userInput.value.trim();
  if ((!text && !pendingAttachments.length) || attachmentSerializationInFlight) return;
  const visibleFocus = conversationFocusForMessages(cleanHistoryForModel());
  attachmentSerializationInFlight = true;
  dom.attachFileBtn.disabled = true;
  let serializedAttachments = [];
  try {
    serializedAttachments = await serializeAttachments(pendingAttachments.slice());
  } catch (error) {
    toast(`附件读取失败：${error.message}`);
    return;
  } finally {
    attachmentSerializationInFlight = false;
    dom.attachFileBtn.disabled = false;
  }
  const displayText = text || "请查看这些附件。";
  updateInteractionCoordinator(performance.now());
  noteUserActivity();
  dom.userInput.value = "";
  resizeChatInput();
  clearPendingAttachments();
  syncChatSubmitButton();
  // Once the assistant has spoken, a new input replies to that visible speech.
  // Only consecutive user additions before any response belong to one input.
  if (activeConversationGroup && (visibleFocus?.followsLatestUser || pendingReplyAnchor)) {
    activeConversationGroup = "";
    activeConversationInputs = [];
    activeConversationAttachments = [];
    activeConversationReplyAnchor = null;
  }
  if (!activeConversationGroup) {
    activeConversationGroup = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    activeConversationReplyAnchor = pendingReplyAnchor ? { ...pendingReplyAnchor } : null;
    clearPendingReplyAnchor();
  }
  activeConversationFocus = visibleFocus;
  activeConversationUserMessageId = createClientMessageId();
  activeConversationInputs.push(displayText);
  activeConversationAttachments.push(...serializedAttachments);
  addMessage("user", displayText, {
    conversationGroup: activeConversationGroup,
    messageId: activeConversationUserMessageId,
    replyAnchor: activeConversationReplyAnchor ? { ...activeConversationReplyAnchor } : null,
    attachments: attachmentMessageMetadata(serializedAttachments),
  });

  if (touchDebounceTimer) {
    clearTimeout(touchDebounceTimer);
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
    hideThinkingIndicator(-interruptedTouchGeneration);
  }

  const priorAnswerRequestId = String(activeAnswerRequestId || activeChatRequestId || "");
  const now = performance.now();
  interruptReplyForNewConversation(now);
  notifyBackendAnswerCancellation(priorAnswerRequestId);
  if (chatRequestInFlight) {
    supersededChatRequestId = activeChatRequestId;
    chatAbortController?.abort();
    chatRequestGeneration += 1;
    chatRequestInFlight = false;
  }
  requestConversationReply();
}

async function requestConversationReply() {
  if (!activeConversationInputs.length || chatRequestInFlight) return;
  const now = performance.now();
  // Retry paths may enter here without going through sendMessage(). A real
  // user turn still takes priority over any unfinished assistant audio.
  interruptReplyForNewConversation(now);
  const inputs = activeConversationInputs.slice();
  const proactiveContext = proactiveState.returnContext;
  const generation = ++chatRequestGeneration;
  const requestId = chatRequestId(activeConversationGroup, generation, inputs.length);
  const supersedesRequestId = supersededChatRequestId;
  supersededChatRequestId = "";
  const controller = new AbortController();
  chatAbortController = controller;
  activeChatRequestId = requestId;
  activeAnswerRequestId = requestId;
  chatRequestInFlight = true;
  syncChatSubmitButton();
  setEmotion("thinking", "nod", { restart: true });
  showThinkingIndicator(generation, "context", "正在整理当前对话与记忆");
  startThinkingProgressPolling(requestId, generation);

  if (settings.debugDirectSpeech) {
    try {
      const directText = inputs.join("\n");
      const applied = await applyAssistant({
        reply: directText,
        spokenTextJa: directText,
        emotion: "neutral",
        gesture: "idle",
        voiceStyle: "calm",
        memoryUpdates: [],
        source: "debug",
        debugDirectSpeech: true,
        clientChatGeneration: generation,
      }, directText);
      if (applied && proactiveContext === proactiveState.returnContext) proactiveState.returnContext = "";
    } finally {
      if (generation === chatRequestGeneration) {
        chatRequestInFlight = false;
        if (!speechPipelineActive && activeAnswerRequestId === requestId) activeAnswerRequestId = "";
        syncChatSubmitButton();
      }
    }
    return;
  }

  const history = cleanHistoryForModel()
    .filter(message => message.meta?.conversationGroup !== activeConversationGroup)
    .slice(-MODEL_HISTORY_LIMIT);
  try {
    const payload = {
      requestId,
      clientMessageId: String(activeConversationGroup || requestId),
      conversationId: String(state.conversationId || `conv_${Math.max(0, Number(state.contextEpoch) || 0)}`),
      userMessageId: String(activeConversationUserMessageId || createClientMessageId()),
      conversationFocus: activeConversationFocus ? { ...activeConversationFocus } : null,
      replyAnchor: activeConversationReplyAnchor ? {
        anchorId: activeConversationReplyAnchor.anchorId,
        type: activeConversationReplyAnchor.type,
        targetMessageIds: [...activeConversationReplyAnchor.targetMessageIds],
        targetContentHashes: [...(activeConversationReplyAnchor.targetContentHashes || [])],
        quotedPreview: activeConversationReplyAnchor.quotedPreview,
      } : null,
      supersedesRequestId,
      userMessage: inputs[inputs.length - 1],
      userMessages: inputs,
      interruptedPreviousReply: conversationWasInterrupted,
      proactiveContext,
      messages: history,
      attachments: activeConversationAttachments,
      contextEpoch: Math.max(0, Number(state.contextEpoch) || 0),
    };
    let playback = createChatUtterancePlayback(generation, now, requestId);
    let streamed = null;
    try {
      streamed = await fetchChatUtteranceStream(payload, controller, playback);
    } catch (streamError) {
      if (isStreamCancellation(streamError)) {
        playback.abort("stream_cancelled");
        if (generation === chatRequestGeneration) {
          recordVoiceDiagnostic(
            "stream-cancelled",
            "本轮流式回答已取消，未将取消误记为语音合成失败",
            { requestId, errorType: String(streamError?.streamErrorType || streamError?.name || "cancelled") },
          );
          resetActiveConversationRequestState();
        }
        return;
      }
      if (streamError?.connectionFailure && streamError?.streamFallbackAllowed) {
        playback.abort();
        updateConnectionStatus({ connectionState: "reconnecting", attempt: 1, attempts: 1 });
        updateThinkingIndicatorStage("backend-reconnecting", "上游暂时不可达，问题已保留，正在自动恢复一次");
        await delay(2500);
        if (controller.signal.aborted) throw new DOMException("Request aborted", "AbortError");
        playback = createChatUtterancePlayback(generation, now, requestId);
        try {
          streamed = await fetchChatUtteranceStream(payload, controller, playback);
          toast("连接已恢复，刚才的问题没有丢失");
        } catch (recoveryError) {
          streamError = recoveryError;
        }
      }
      if (streamed) {
        // Same request id is replay-safe: a completed backend result is reused,
        // while a failed claim is reopened without adding another user turn.
      } else if (!streamError?.streamFallbackAllowed) {
        playback.abort();
        throw streamError;
      } else if (settings.architectureFeatures?.frontendFullRetryDisabled) {
        playback.abort();
        streamError.streamFallbackAllowed = false;
        streamError.streamErrorType = String(
          streamError.streamErrorType || "backend_single_turn_recovery_failed",
        );
        throw streamError;
      } else {
        playback.abort();
        updateThinkingIndicatorStage("fallback-legacy", "流式协议不可用，正在切回完整回答路径");
        const legacyData = await fetchChatWithReconnect(payload, controller);
        streamed = { data: legacyData, route: "fallback_legacy", hadUtterance: false, legacy: true };
      }
    }
    if (generation !== chatRequestGeneration) return;
    const data = streamed.data;
    const assistantData = {
      ...data,
      source: data.source || "api",
      clientRequestStartedAt: now,
      clientChatGeneration: generation,
    };
    const applied = streamed.legacy
      ? await applyAssistant(assistantData, inputs.join("\n"))
      : commitStreamedAssistantResult(assistantData, playback, inputs.join("\n"));
    if (applied && generation === chatRequestGeneration && proactiveContext === proactiveState.returnContext) {
      proactiveState.returnContext = "";
    }
  } catch (error) {
    if (controller.signal.aborted || error?.name === "AbortError" || generation !== chatRequestGeneration) return;
    const detail = connectionErrorSummary(error);
    const completedTools = Array.isArray(error?.completedTools) ? error.completedTools : [];
    const typedPipelineFailure = completedTools.length > 0 || (Boolean(error?.streamErrorType) && !error?.connectionFailure);
    if (typedPipelineFailure) {
      const errorType = String(error.streamErrorType || "internal_pipeline_failed");
      const stage = String(error.streamErrorStage || "pipeline");
      const message = completedTools.includes("open_browser")
        ? "网页已打开，但后续回复生成失败。"
        : completedTools.length > 0
        ? "已有操作成功完成，但后续回复生成失败。"
        : errorType === "cognitive_revision_failed"
        ? "本次回复没有生成成功。"
        : errorType === "invalid_reply_anchor"
        ? "刚才引用的消息没有找到。可以把那句话直接引用过来，我再接着回答。"
        : errorType === "grounding_validation_failed"
        ? "我刚才没能确认自己的理解，所以先没有乱说。你愿意的话，我可以换个方式再试一次。"
        : errorType === "upstream_http_failed"
          ? "我刚才没能把这件事处理完。你要我再试一次吗？"
        : errorType === "upstream_protocol_failed"
          ? "我刚才的话没能完整接上。你可以让我再试一次。"
          : "我刚才没处理好这件事。你可以让我再试一次，或者再告诉我一下你想要的结果。";
      toast(message);
      addMessage("assistant", message, {
        source: "local-recovery",
        errorType,
        errorStage: stage,
        completedTools,
        noSpeech: true,
      });
      if (generation === chatRequestGeneration) {
        recordVoiceDiagnostic(
          "stream-terminal-failure",
          "本轮已终止，等待用户明确重新发送",
          { requestId, errorType, errorStage: stage },
        );
        resetActiveConversationRequestState();
      }
      return;
    }
    updateConnectionStatus({ connectionState: "failed", connectionCode: error?.code || "CONNECTION" });
    toast(`DeepSeek 连接失败：${detail}`);
    const applied = await applyAssistant({
      reply: `DeepSeek 连接失败，已自动重试但仍未收到回答。错误：${detail}。请检查网络、API Key、接口地址或额度后再试。`,
      spokenTextJa: "接続が一度途切れて、完全な返事を受け取れなかったみたい。質問は分かってるから、もう一度送って。",
      emotion: "concerned",
      gesture: "glitch",
      voiceStyle: "soft",
      memoryUpdates: [],
      source: "connection-error",
      connectionState: "failed",
      connectionCode: error?.code || "CONNECTION",
      clientChatGeneration: generation,
    }, inputs.join("\n"));
    if (applied && generation === chatRequestGeneration && proactiveContext === proactiveState.returnContext) {
      proactiveState.returnContext = "";
    }
  } finally {
    if (generation === chatRequestGeneration) {
      stopThinkingProgressPolling(requestId);
      if (!speechPipelineActive) hideThinkingIndicator(generation);
      chatRequestInFlight = false;
      chatAbortController = null;
      activeChatRequestId = "";
      if (!speechPipelineActive && activeAnswerRequestId === requestId) activeAnswerRequestId = "";
      syncChatSubmitButton();
    }
  }
}

function commitStreamedAssistantResult(data, playback, userMessage = "") {
  if (data.proactiveControl && typeof data.proactiveControl === "object") {
    applyProactiveControl(data.proactiveControl, { persistState: false });
  }
  if (Array.isArray(data.memories)) {
    state.memories = unique(data.memories).slice(0, MAX_MEMORIES);
  } else if (Array.isArray(data.memoryUpdates)) {
    state.memories = unique([...data.memoryUpdates.map(String), ...state.memories]).slice(0, MAX_MEMORIES);
  }
  refreshMemoryEditor();
  if (typeof data.coldShoulder === "boolean") {
    persistentColdShoulder = data.coldShoulder;
    if (persistentColdShoulder) cancelTouchInteractionForColdShoulder();
  }
  if (data.relationshipTone) relationshipTone = String(data.relationshipTone);
  updateConnectionStatus(data);
  updateDeveloperExchange(
    String(data.reply || ""),
    String(data.spokenTextJa || ""),
    { ...data, utteranceStreamCommitted: true },
  );
  playback?.setFinalResult(data);
  refreshTodosAfterAssistantAction(data);
  refreshBookmarksAfterAssistantAction(data);
  if (playback?.message) activeAssistantMessage = playback.message;
  if (
    Number(data.clientChatGeneration || 0) === chatRequestGeneration
  ) {
    resetActiveConversationRequestState();
  }
  persist();
  return true;
}

function proactiveDeliveryStillCurrent(data) {
  const isProactive = data?.interactionKind === "proactive"
    || data?.source === "proactive"
    || data?.source === "api-proactive"
    || data?.source === "todo-reminder";
  if (!isProactive) return true;
  const generation = Number(data.proactiveRequestGeneration || 0);
  const sequenceId = Number(data.proactiveSequenceId ?? -1);
  return !proactiveOutreachPaused()
    && !chatRequestInFlight
    && (!generation || generation === proactiveRequestGeneration)
    && (sequenceId < 0 || sequenceId === proactiveState.sequenceId);
}

async function applyAssistant(data, userMessage = "") {
  if (data.source === "touch" && data.turnIdentity
      && (String(data.turnIdentity.conversationId) !== String(state.conversationId || "")
          || Number(data.contextEpoch) !== (Number(state.contextEpoch) || 0))) return false;
  if (!proactiveDeliveryStillCurrent(data)) return false;
  if (data.proactiveControl && typeof data.proactiveControl === "object") {
    applyProactiveControl(data.proactiveControl, { persistState: false });
  }
  refreshTodosAfterAssistantAction(data);
  refreshBookmarksAfterAssistantAction(data);
  const incomingSource = String(data.source || "");
  const touchCannotOverrideColdShoulder = incomingSource === "touch" && persistentColdShoulder;
  if (touchCannotOverrideColdShoulder) {
    preservePersistentFuriousPose();
    hideThinkingIndicator(Number(data.clientChatGeneration || 0));
    return true;
  }
  if (typeof data.coldShoulder === "boolean") {
    persistentColdShoulder = data.coldShoulder;
    if (persistentColdShoulder) cancelTouchInteractionForColdShoulder();
  }
  if (data.silentLocked || data.silentInteraction) {
    const mood = resolveAssistantMood(data, userMessage, "");
    if (data.relationshipTone) relationshipTone = String(data.relationshipTone);
    updateConnectionStatus(data);
    setEmotion(mood.emotion, mood.gesture, mood);
    updateDeveloperExchange("", "", data);
    hideThinkingIndicator(Number(data.clientChatGeneration || 0));
    return true;
  }
  const reply = String(data.reply || "……你这个问题，倒也不是不能回答。");
  const spokenTextJa = String(data.spokenTextJa || data.spoken_text_ja || "").trim();
  const speechText = chooseSpeechText(reply, spokenTextJa);
  const mood = resolveAssistantMood(data, userMessage, reply);
  if (data.relationshipTone) relationshipTone = String(data.relationshipTone);
  const speechPace = resolveSpeechPace(data, userMessage);
  const speechSpeed = resolveSpeechSpeed(data, userMessage, speechPace);
  if (Array.isArray(data.memories)) {
    state.memories = unique(data.memories).slice(0, MAX_MEMORIES);
  } else if (Array.isArray(data.memoryUpdates)) {
    state.memories = unique([...data.memoryUpdates.map(String), ...state.memories]).slice(0, MAX_MEMORIES);
  }
  refreshMemoryEditor();
  updateConnectionStatus(data);
  const messageMeta = {
    source: data.source || (settings.modelConfigured ? "api" : "local"),
    latencyMs: data.latencyMs,
    memoryCount: state.memories.length,
    spokenTextJa,
    speechText,
    speechLanguage: settings.speechLanguage,
    spokenTextJaOriginal: data.spokenTextJaOriginal || "",
    spokenTextJaRepaired: Boolean(data.spokenTextJaRepaired),
    kind: data.interactionKind === "proactive" || data.source === "proactive"
      ? "proactive"
      : data.source === "touch" ? "touch" : "chat",
    proactiveEventId: String(data.proactiveEventId || ""),
    proactiveKind: String(data.proactiveKind || ""),
    proactiveSubject: String(data.proactiveSubject || ""),
    proactivePhase: String(data.proactivePhase || ""),
    proactiveUnansweredCount: Math.max(0, Number(data.proactiveUnansweredCount) || 0),
    proactiveSilenceLimit: Math.max(0, Number(data.proactiveSilenceLimit) || 0),
    proactiveDirective: String(data.proactiveDirective || "unchanged"),
    proactiveScreenObserved: Boolean(data.proactiveScreenObserved),
    proactiveObservation: data.proactiveObservation && typeof data.proactiveObservation === "object"
      ? data.proactiveObservation
      : null,
    proactivePersisted: false,
    sources: Array.isArray(data.sources) ? data.sources : [],
    files: Array.isArray(data.files) ? data.files : [],
    dialoguePolicy: data.dialoguePolicy || data.pipelineMetrics?.turnPolicy || null,
    dialogueValidation: data.dialogueValidation || null,
    toolUsed: String(data.toolUsed || ""),
    toolStatus: String(data.toolStatus || ""),
    actionReceipt: data.actionReceipt && typeof data.actionReceipt === "object"
      ? { ...data.actionReceipt }
      : null,
    todoContext: data.todoContext && typeof data.todoContext === "object"
      ? { ...data.todoContext }
      : null,
    startupTtsFailure: data.startupTtsFailure || null,
    originalClipId: String(data.originalAudio?.id || ""),
    ...messageIdentityMeta(data, "assistant"),
  };
  const provider = String(settings.ttsProvider || "").trim().toLowerCase();
  const contractedSpeechUnits = Array.isArray(data.speechUnits)
    ? data.speechUnits.filter(unit => unit && unit.deliveryRequired !== false).map(unit => ({
      ...unit,
      textZh: String(unit.textZh || "").trim(),
      spokenTextJa: String(unit.spokenTextJa || "").trim(),
    })).filter(unit => unit.textZh && chooseSpeechText(unit.textZh, unit.spokenTextJa, data.speechLanguage))
    : [];
  const progressivePlan = contractedSpeechUnits.length > 1
    ? {
      progressive: true,
      displaySegments: contractedSpeechUnits.map(unit => unit.textZh),
      speechSegments: contractedSpeechUnits.map(unit => chooseSpeechText(unit.textZh, unit.spokenTextJa)),
    }
    : {
      progressive: false,
      displaySegments: [reply],
      speechSegments: [speechText],
    };
  let progressiveMessage = null;
  const revealPipelineSegment = (index, displaySegment, spokenSegment, total) => {
    if (!proactiveDeliveryStillCurrent(data)) return;
    if (!progressivePlan.progressive) return;
    const visibleSegment = String(displaySegment || "").trim();
    if (!visibleSegment) return;
    if (!progressiveMessage) {
      progressiveMessage = beginProgressiveAssistantMessage(visibleSegment, {
        ...messageMeta,
        speechText: String(spokenSegment || "").trim(),
        spokenTextJa: normalizeSpeechLanguage(settings.speechLanguage) === "ja-JP"
          ? String(spokenSegment || "").trim()
          : spokenTextJa,
      });
      progressiveMessage.spokenParts = [String(spokenSegment || "").trim()].filter(Boolean);
      progressiveMessage.message.meta.progressiveComplete = Number(total) <= 1;
      activeAssistantMessage = progressiveMessage.message;
    } else {
      appendProgressiveAssistantSegment(
        progressiveMessage,
        visibleSegment,
        spokenSegment,
        index,
        total,
      );
    }
    hideThinkingIndicator(Number(data.clientChatGeneration || 0));
  };
  const revealFailedPipelineSegment = (index, displaySegment, spokenSegment, total) => {
    if (progressiveMessage?.message?.meta) {
      progressiveMessage.message.meta.voicePartialFailure = true;
      progressiveMessage.message.meta.voiceFailedSegment = index + 1;
      progressiveMessage.message.meta.deliveryPartial = true;
      persist();
    }
  };
  const deterministicStartupTtsFailure = messageMeta.kind === "proactive"
    && Boolean(data.startupTtsFailure?.deterministic);
  if (deterministicStartupTtsFailure) {
    recordVoiceDiagnostic(
      "startup-audio-suppressed",
      "开机问候预合成已经确定性失败，播放阶段不会重复提交；文字将正常显示",
      { code: String(data.startupTtsFailure?.code || "tts_incomplete_audio") },
    );
  }
  const speechReady = deterministicStartupTtsFailure
    ? Promise.resolve(false)
    : startSegmentedSpeech(speechText, mood.voiceStyle, {
    displayText: reply,
    spokenTextJa: speechText,
    speechPace,
    speechSpeed,
    voiceTone: mood.voiceTone,
    voiceIntensity: mood.voiceIntensity,
    mood,
    preparedTts: data.preparedTts || null,
    originalAudio: data.originalAudio || null,
    kind: messageMeta.kind,
    requestStartedAt: Number(data.clientRequestStartedAt || 0),
    pipelineSpeechSegments: progressivePlan.speechSegments,
    pipelineDisplaySegments: progressivePlan.displaySegments,
    pipelineBoundaries: contractedSpeechUnits.map((unit, index) => normalizeSpeechBoundary(
      unit.boundaryAfter,
      index === contractedSpeechUnits.length - 1 ? "turn_pause" : "phrase_pause",
    )),
    semanticUnits: contractedSpeechUnits.length
      ? contractedSpeechUnits.map(unit => chooseSpeechText(unit.textZh, unit.spokenTextJa))
      : semanticSpeechUnits(speechText),
    onPipelineSegmentStarted: revealPipelineSegment,
    onPipelineSegmentFailed: revealFailedPipelineSegment,
    });
  const startedVoice = await speechReady;
  if (!proactiveDeliveryStillCurrent(data)) {
    if (speechState.kind === "proactive") interruptCurrentSpeech();
    return false;
  }
  const supersededByNewChat = messageMeta.kind === "chat"
    && Number(data.clientChatGeneration || 0) > 0
    && Number(data.clientChatGeneration) !== chatRequestGeneration;
  if (startedVoice === null && supersededByNewChat) return false;
  if (!startedVoice) {
    setEmotion(mood.emotion, mood.gesture, mood);
  }
  updateDeveloperExchange(reply, speechText, data);
  hideThinkingIndicator(Number(data.clientChatGeneration || 0));
  const voiceGatedText = (
    settings.voiceEnabled
    && provider === "gpt-sovits"
    && !deterministicStartupTtsFailure
  );
  if (!progressiveMessage && voiceGatedText && startedVoice !== true) {
    messageMeta.voiceFailed = true;
    messageMeta.speechUnavailableReason = String(
      data.speechUnavailableReason || data.ttsUnavailableReason || "audio_start_failed"
    );
    recordVoiceDiagnostic(
      "assistant-text-revealed-after-tts-failure",
      "首段语音已确认失败，现在提交对应文字",
      {
        kind: messageMeta.kind,
        textLength: reply.length,
        speechUnavailableReason: messageMeta.speechUnavailableReason,
      },
    );
  }
  if (!progressiveMessage) {
    activeAssistantMessage = addMessage("assistant", reply, messageMeta);
  }
  const committedAssistantMessage = progressiveMessage?.message || activeAssistantMessage;
  bindCompletedTurnIdentity(data, committedAssistantMessage);
  if (messageMeta.kind === "proactive" && committedAssistantMessage) {
    recordProactiveConversationEvent(committedAssistantMessage, mood);
  }
  if (
    messageMeta.kind === "chat"
    && Number(data.clientChatGeneration || 0) === chatRequestGeneration
  ) {
    // The written answer is complete as soon as it is committed to the chat.
    // A later user message may stop only its audio; it must begin a fresh turn
    // instead of merging the previous prompt back into the new request.
    activeConversationInputs = [];
    activeConversationAttachments = [];
    activeConversationGroup = "";
    activeConversationReplyAnchor = null;
    activeConversationUserMessageId = "";
    conversationWasInterrupted = false;
  }
  persist();
  return true;
}

function updateInteractionCoordinator(now = performance.now()) {
  if (speechPipelineActive || speechState.pending || isSpeaking(now)) return;
  const completedKind = speechState.kind;
  if (completedKind !== "idle") {
    speechState.kind = "idle";
    activeAssistantMessage = null;
    if (completedKind === "chat") {
      resetActiveConversationRequestState();
    }
  }
  if (activeConversationInputs.length && !chatRequestInFlight) {
    requestConversationReply();
    return;
  }
  if (chatRequestInFlight || proactiveInteractionInFlight) return;
  if (deferredTouchAssistant) {
    const pending = deferredTouchAssistant;
    deferredTouchAssistant = null;
    touchState.pendingAfterChat = false;
    applyAssistant(pending, "");
    return;
  }
  if (touchState.pendingAfterChat && !touchState.requestInFlight && !touchDebounceTimer) {
    touchState.pendingAfterChat = false;
    scheduleTouchInteraction(120);
  }
}

function markActiveTouchInterrupted() {
  if (!activeAssistantMessage || activeAssistantMessage.meta?.kind !== "touch") return;
  activeAssistantMessage.meta.interrupted = true;
  renderMessages();
  persist();
}

function shouldSynchronizeTextWithVoice() {
  const provider = String(settings.ttsProvider || "").toLowerCase();
  return Boolean(settings.voiceEnabled && provider && provider !== "system");
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function resolveSpeechPace(data, userMessage = "") {
  if (/慢慢念|慢慢说|慢点|慢一点|说慢|放慢|ゆっくり|slow/i.test(userMessage)) return /慢慢|ゆっくり/i.test(userMessage) ? "very_slow" : "slow";
  if (/快点|快一点|说快|加快|很快|超快|早口|fast/i.test(userMessage)) return /很快|超快|早口/i.test(userMessage) ? "very_fast" : "fast";
  const requested = normalizeSpeechPace(data.speechPace || data.speech_pace || "");
  if (requested !== "normal") return requested;
  return "normal";
}

function explicitlyRequestedSpeechPace(userMessage = "") {
  return /慢慢念|慢慢说|慢点|慢一点|说慢|放慢|快点|快一点|说快|加快|很快|超快|早口|ゆっくり|slow|fast/i.test(userMessage);
}

function resolveSpeechSpeed(data, userMessage = "", speechPace = "normal") {
  const paceSpeed = speedForSpeechPace(speechPace);
  if (explicitlyRequestedSpeechPace(userMessage) || String(data.speechPaceSource || "") === "user_request") {
    return clamp(paceSpeed, 0.62, 1.45);
  }
  const requested = toNumber(data.speechSpeed, paceSpeed);
  const paceBounds = {
    very_slow: [0.66, 0.78],
    slow: [0.82, 0.92],
    normal: [0.94, 1.08],
    fast: [1.08, 1.24],
    very_fast: [1.22, 1.40],
  }[normalizeSpeechPace(speechPace)] || [0.94, 1.08];
  return clamp(requested, paceBounds[0], paceBounds[1]);
}

function chooseSpeechText(reply, spokenTextJa, speechLanguage = settings.speechLanguage) {
  const provider = String(settings.ttsProvider || "").toLowerCase();
  if (provider === "gpt-sovits") {
    if (speechLanguageProfile(speechLanguage).speechLanguage === "zh-CN") {
      return String(reply || "").trim();
    }
    // Japanese mode consumes the semantically aligned Japanese script. If
    // alignment failed, keep it silent rather than reading unrelated text.
    return String(spokenTextJa || "").trim();
  }
  return reply;
}

function resolveAssistantMood(data, userMessage, reply) {
  const hasExplicitEmotionLabel = typeof data.emotion === "string" && Boolean(emotionTargets[data.emotion]);
  const incoming = normalizeEmotion(data.emotion);
  let emotion = incoming;
  let gesture = normalizeGesture(data.gesture);
  let voiceStyle = String(data.voiceStyle || "calm");
  let voiceTone = normalizeVoiceTone(data.voiceTone || data.voice_tone || voiceStyle);
  const requestedVoiceIntensity = data.voiceIntensity ?? data.voice_intensity;
  const deliveryMode = String(data.deliveryMode || data.delivery_mode || "normal").toLowerCase() === "outburst"
    ? "outburst" : "normal";
  let emotionIntensityValue = clamp(
    toNumber(data.emotionIntensity, 1),
    0.2,
    deliveryMode === "outburst" ? 2.5 : 1.25
  );
  let emotionDeltaValue = clamp(toNumber(data.emotionDelta ?? data.emotion_delta, 0), -0.45, 0.45);
  const defaults = expressionDefaultsFor(emotion);
  let blushIntensity = clamp(toNumber(data.blushIntensity, defaults.blushIntensity), 0, 1);
  let eyeContact = clamp(toNumber(data.eyeContact, defaults.eyeContact), 0, 1);
  let gazeX = clamp(toNumber(data.gazeX, defaults.gazeX), -0.6, 0.6);
  let gazeY = clamp(toNumber(data.gazeY, defaults.gazeY), -0.6, 0.6);
  let tension = clamp(toNumber(data.tension, defaults.tension), 0, 1);
  const text = `${userMessage}\n${reply}`;

  if (!hasExplicitEmotionLabel && isShyTrigger(text) && ["neutral", "amused", "thinking"].includes(incoming)) {
    emotion = /爱你|喜欢你|表白|亲|恋|好き|愛して/.test(text) ? "flustered" : "shy";
    voiceStyle = voiceStyle === "calm" ? "teasing" : voiceStyle;
    voiceTone = emotion === "flustered" ? "flustered" : "shy";
    emotionIntensityValue = Math.max(emotionIntensityValue, emotion === "flustered" ? 1.08 : 0.96);
    blushIntensity = Math.max(blushIntensity, emotion === "flustered" ? 0.88 : 0.78);
    eyeContact = Math.min(eyeContact, emotion === "flustered" ? 0.31 : 0.42);
    gazeX = gazeX || -0.22;
    gazeY = Math.max(gazeY, 0.16);
  } else if (!hasExplicitEmotionLabel && isConcernTrigger(userMessage) && incoming === "neutral") {
    emotion = /危险|伤害|别管|撑不住|自杀|死|崩溃/.test(userMessage) ? "protective" : "concerned";
    gesture = "soften";
    voiceStyle = "soft";
    voiceTone = "comforting";
    emotionIntensityValue = Math.max(emotionIntensityValue, 0.9);
    eyeContact = Math.max(eyeContact, 0.78);
  } else if (!hasExplicitEmotionLabel && isAnnoyedTrigger(userMessage) && incoming === "neutral") {
    emotion = /突然|吓|惊|变态/.test(userMessage) ? "startled" : "annoyed";
    if (/变态|笨蛋|吓/.test(userMessage)) gesture = "recoil";
    voiceStyle = "sharp";
    voiceTone = "dry";
    tension = Math.max(tension, 0.55);
  } else if (!hasExplicitEmotionLabel && isThinkingTrigger(userMessage) && incoming === "neutral") {
    emotion = /证据|严谨|排查|复现|定位|变量|指标|标准|bug|错误|失败|问题/.test(userMessage) ? "focused" : "thinking";
    gesture = /解释|讲|怎么|为什么|原理|流程|方案|代码|算法|模型|api/i.test(userMessage) ? "explain" : "nod";
    voiceTone = "serious";
  } else if (incoming === "excited") {
    gesture = gesture === "idle" ? "perk" : gesture;
  } else if (incoming === "laughing") {
    gesture = gesture === "idle" ? "laugh" : gesture;
    voiceStyle = voiceStyle === "calm" ? "energetic" : voiceStyle;
    voiceTone = "laughing";
    emotionIntensityValue = Math.max(emotionIntensityValue, 1.08);
    eyeContact = Math.max(eyeContact, 0.76);
  } else if (incoming === "furious") {
    gesture = "look_away";
    voiceStyle = "sharp";
    voiceTone = "cold";
    tension = Math.max(tension, 0.92);
    eyeContact = Math.min(eyeContact, 0.2);
    gazeX = Math.abs(gazeX) >= 0.3 ? gazeX : 0.5;
  }

  const moderateFaceTouch = (
    String(data.source || "") === "touch"
    && String(data.touchZone || "") === "face"
    && String(data.touchBoundaryLevel || "normal") === "normal"
    && String(data.touchExpressionBand || "") === "middle"
  );
  if (moderateFaceTouch) {
    if (["flustered", "startled"].includes(emotion)) emotion = "shy";
    emotionIntensityValue = clamp(emotionIntensityValue, 0.82, 0.88);
    emotionDeltaValue = clamp(emotionDeltaValue, -0.02, 0.02);
    blushIntensity = clamp(blushIntensity, 0.22, 0.38);
    tension = clamp(tension, 0.12, 0.3);
    eyeContact = clamp(eyeContact, 0.46, 0.6);
    gazeX = clamp(gazeX, -0.22, 0.22);
    gazeY = clamp(gazeY, 0.04, 0.14);
    if (voiceTone === "flustered") voiceTone = "shy";
  }

  gesture = contextualGestureForEmotion(emotion, gesture);

  if (emotion === "shy" && !["shy", "shy_annoyed"].includes(voiceTone)) {
    voiceTone = "shy";
    if (voiceStyle === "calm") voiceStyle = "soft";
  } else if (emotion === "flustered" && voiceTone !== "flustered") {
    voiceTone = "flustered";
    if (voiceStyle === "calm") voiceStyle = "energetic";
  } else if (emotion === "startled") {
    voiceTone = "startled";
    if (voiceStyle === "calm") voiceStyle = "energetic";
  } else if (emotion === "laughing") {
    voiceTone = "laughing";
  }

  const voiceIntensity = resolveVoiceIntensity(requestedVoiceIntensity, emotionIntensityValue, emotionDeltaValue);

  return {
    emotion,
    gesture,
    voiceStyle,
    voiceTone,
    voiceIntensity,
    emotionIntensity: emotionIntensityValue,
    emotionDelta: emotionDeltaValue,
    blushIntensity,
    eyeContact,
    gazeX,
    gazeY,
    tension,
    deliveryMode,
    outburst: deliveryMode === "outburst",
    holdMs: toNumber(data.holdMs, emotion === "neutral" ? 0 : 45000),
    fadeMs: toNumber(data.fadeMs, emotion === "neutral" ? 1200 : 10000),
    annotatedVariantUnit: undefined,
    accumulateExpression: moderateFaceTouch
      ? false
      : Boolean(data.accumulateExpression || data.accumulate_expression),
    smoothEmotionTransition: moderateFaceTouch
      ? false
      : Boolean(data.smoothEmotionTransition || data.source === "touch"),
  };
}

function normalizeEmotion(value) {
  return emotionTargets[value] ? value : "neutral";
}

function normalizeGesture(value) {
  const allowed = new Set(["idle", "nod", "tilt", "look_away", "glitch", "lean", "soften", "smile", "laugh", "tap", "boot", "recoil", "explain", "perk"]);
  return allowed.has(value) ? value : "idle";
}

function contextualGestureForEmotion(emotion, gesture) {
  const normalized = normalizeGesture(gesture);
  if (normalized !== "idle") return normalized;
  const cycle = EMOTION_GESTURE_CYCLES[emotion];
  if (!cycle?.length) return normalized;
  const step = emotionGestureSteps.get(emotion) || 0;
  emotionGestureSteps.set(emotion, step + 1);
  return cycle[step % cycle.length];
}

function normalizeVoiceTone(value) {
  const tone = String(value || "").toLowerCase();
  const allowed = new Set(["neutral", "dry", "gentle", "shy", "shy_annoyed", "flustered", "startled", "serious", "playful", "laughing", "comforting", "urgent", "warm", "cold"]);
  const aliases = {
    calm: "neutral",
    soft: "gentle",
    sharp: "dry",
    teasing: "playful",
    energetic: "urgent",
    embarrassed: "shy",
    surprised: "startled",
    shocked: "startled",
    laugh: "laughing",
    laughter: "laughing",
    joyful: "laughing",
  };
  return allowed.has(tone) ? tone : aliases[tone] || "neutral";
}

function normalizeVoiceIntensity(value) {
  const raw = String(value || "normal").toLowerCase();
  const aliases = { low: "subtle", mild: "subtle", medium: "normal", moderate: "normal", high: "strong", intense: "strong" };
  const normalized = aliases[raw] || raw;
  return ["subtle", "normal", "strong"].includes(normalized) ? normalized : "normal";
}

function resolveVoiceIntensity(value, emotionIntensityValue, emotionDeltaValue = 0) {
  const effective = clamp(toNumber(emotionIntensityValue, 1) + toNumber(emotionDeltaValue, 0) * 0.65, 0.2, 2.5);
  const inferred = effective <= 0.76 ? "subtle" : effective >= 1.08 ? "strong" : "normal";
  const raw = String(value ?? "").trim().toLowerCase();
  const aliases = { low: "subtle", mild: "subtle", medium: "normal", moderate: "normal", high: "strong", intense: "strong" };
  const requested = aliases[raw] || raw;
  if (!["subtle", "normal", "strong"].includes(requested)) return inferred;
  if (requested === "normal" && inferred !== "normal") return inferred;
  if (new Set([requested, inferred]).size === 2 && requested !== "normal" && inferred !== "normal") return "normal";
  return requested;
}

function normalizeSpeechPace(value) {
  const pace = String(value || "").toLowerCase();
  const aliases = {
    slower: "slow",
    veryslow: "very_slow",
    very_slowly: "very_slow",
    normal_speed: "normal",
    faster: "fast",
    veryfast: "very_fast",
  };
  const normalized = aliases[pace] || pace;
  return ["very_slow", "slow", "normal", "fast", "very_fast"].includes(normalized) ? normalized : "normal";
}

function speedForSpeechPace(pace) {
  return {
    very_slow: 0.66,
    slow: 0.8,
    normal: 1,
    fast: 1.16,
    very_fast: 1.32,
  }[normalizeSpeechPace(pace)] || 1;
}

function isShyTrigger(text) {
  if (/不喜欢你|不可爱|讨厌你|别自作多情/.test(text)) return false;
  return /喜欢你|可爱|漂亮|美|天才少女|老婆|女友|抱|亲|摸头|害羞|脸红|傲娇|辛苦了|想你|厉害|谢谢你|陪我|红莉栖/.test(text);
}

function isConcernTrigger(text) {
  return /难受|崩溃|焦虑|害怕|孤独|睡不着|痛苦|不开心|撑不住|累了|失败/.test(text);
}

function isAnnoyedTrigger(text) {
  return /克里斯蒂娜|变态|笨蛋|助手|别扭|傲娇/.test(text);
}

function isThinkingTrigger(text) {
  return /代码|bug|模型|api|算法|论文|时间|机器|神经|量子|实验|世界线|部署|接口/.test(text.toLowerCase());
}

function speakPulse(length) {
  const now = performance.now();
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

const VISIBLE_STAGE_DIRECTION_TERMS = /(?:眯(?:起)?眼|睁(?:开)?眼|闭(?:上)?眼|眼神|视线|目光|嘴角|偏(?:过)?头|歪(?:了)?头|低头|抬头|转头|别开(?:脸|视线)|移开视线|看向|望向|盯着|注视|微笑|苦笑|冷笑|笑意|笑容|轻笑|叹气|脸红|红晕|皱眉|挑眉|耸肩|抱臂|点头|摇头|后退|前倾|轻哼|沉默|语气|声音|表情|神情|动作|姿势|心想|心里|心中|脑海|内心|心理|暗自|假装|故作|装作|目を|目線|視線|顔を|首を|俯く|笑み|ため息|頬|赤ら|そっぽ|smiles?|sighs?|looks?\s+away|tilts?\s+(?:her\s+)?head|blush(?:es|ed)?)/i;
const VISIBLE_STAGE_BLOCK_PATTERNS = [
  /（([^（）\r\n]{1,220})）/g,
  /\(([^()\r\n]{1,220})\)/g,
  /【([^【】\r\n]{1,220})】/g,
  /\[([^\[\]\r\n]{1,220})\]/g,
  /\*([^*\r\n]{1,220})\*/g,
];

function stripVisibleStageDirections(value) {
  let text = String(value || "").trim();
  for (const pattern of VISIBLE_STAGE_BLOCK_PATTERNS) {
    text = text.replace(pattern, (whole, inner) => (
      VISIBLE_STAGE_DIRECTION_TERMS.test(String(inner || "")) ? "" : whole
    ));
  }
  text = text.replace(/[ \t]+\n/g, "\n").replace(/\n[ \t]+/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  const quotePairs = [["\"", "\""], ["“", "”"], ["‘", "’"], ["「", "」"], ["『", "』"]];
  let unwrapped = true;
  while (unwrapped && text.length >= 2) {
    unwrapped = false;
    for (const [opening, closing] of quotePairs) {
      if (text.startsWith(opening) && text.endsWith(closing)) {
        text = text.slice(opening.length, -closing.length).trim();
        unwrapped = true;
        break;
      }
    }
  }
  return text;
}

function visibleMessageContent(role, content) {
  const text = String(content || "");
  return role === "assistant" ? stripVisibleStageDirections(text) : text;
}

function beginProgressiveAssistantMessage(content, meta = {}) {
  const visibleContent = visibleMessageContent("assistant", content);
  const message = {
    role: "assistant",
    content: visibleContent,
    meta: {
      contextEpoch: Math.max(0, Number(state.contextEpoch) || 0),
      conversationId: String(state.conversationId || ""),
      ...meta,
      progressiveDisplay: true,
      progressiveComplete: false,
      progressiveDisplayedSegments: 1,
    },
  };
  state.messages.push(message);
  state.messages = state.messages.slice(-MAX_MESSAGES);
  persist();
  const bubble = appendBubble("assistant", visibleContent, message.meta);
  return { message, bubble, displayParts: [visibleContent], spokenParts: [] };
}

function appendProgressiveAssistantSegment(progress, content, spokenText, index, total) {
  if (!progress?.message || !progress?.bubble) return;
  const visibleContent = visibleMessageContent("assistant", content);
  if (visibleContent) progress.displayParts.push(visibleContent);
  if (String(spokenText || "").trim()) progress.spokenParts.push(String(spokenText).trim());
  progress.message.content = progress.displayParts.filter(Boolean).join("\n\n");
  progress.message.meta.progressiveDisplayedSegments = Math.max(1, Number(index) + 1);
  progress.message.meta.progressiveComplete = Number(index) + 1 >= Number(total);
  if (progress.spokenParts.length) {
    progress.message.meta.speechText = progress.spokenParts.join("\n\n");
    if (normalizeSpeechLanguage(progress.message.meta.speechLanguage) === "ja-JP") {
      progress.message.meta.spokenTextJa = progress.message.meta.speechText;
    }
  }
  const body = progress.bubble.querySelector(".message-body");
  if (body) body.textContent = progress.message.content;
  const ja = progress.bubble.querySelector(".dev-ja-line");
  if (ja && progress.spokenParts.length) {
    const label = normalizeSpeechLanguage(progress.message.meta.speechLanguage) === "zh-CN" ? "ZH" : "JA";
    ja.textContent = `${label}: ${stripVisibleStageDirections(progress.spokenParts.join("\n\n"))}`;
  }
  persist();
  scrollChatToLatest();
}

function addMessage(role, content, meta = {}) {
  const visibleContent = visibleMessageContent(role, content);
  const message = {
    role,
    content: visibleContent,
    meta: {
      contextEpoch: Math.max(0, Number(state.contextEpoch) || 0),
      conversationId: String(state.conversationId || ""),
      ...meta,
    },
  };
  state.messages.push(message);
  state.messages = state.messages.slice(-MAX_MESSAGES);
  persist();
  appendBubble(role, visibleContent, message.meta);
  return message;
}

function renderMessages() {
  dom.chatLog.innerHTML = "";
  state.messages.slice(-MAX_MESSAGES).forEach(message => appendBubble(message.role, message.content, message.meta || {}));
  scrollChatToLatest();
}

function scrollChatToLatest() {
  if (!dom.chatLog) return;
  const scroll = () => { dom.chatLog.scrollTop = dom.chatLog.scrollHeight; };
  scroll();
  requestAnimationFrame(() => {
    scroll();
    requestAnimationFrame(scroll);
  });
  window.setTimeout(scroll, 120);
}

function openChatHistorySearch() {
  if (!dom.settingsModal?.classList.contains("hidden")) closeSettings();
  setChatCollapsed(false, { scroll: false });
  closeConversationPicker();
  closeBookmarkPanel();
  dom.chatHistorySearchPanel?.classList.remove("hidden");
  dom.chatHistorySearchBtn?.setAttribute("aria-expanded", "true");
  chatHistorySearchState.offset = 0;
  updateChatHistorySearchRoleButtons();
  performChatHistorySearch();
  window.setTimeout(() => dom.chatHistorySearchInput?.focus(), 0);
}

function closeChatHistorySearch() {
  window.clearTimeout(chatHistorySearchState.timer);
  chatHistorySearchState.controller?.abort();
  chatHistorySearchState.controller = null;
  dom.chatHistorySearchPanel?.classList.add("hidden");
  dom.chatHistorySearchBtn?.setAttribute("aria-expanded", "false");
}

function scheduleChatHistorySearch() {
  window.clearTimeout(chatHistorySearchState.timer);
  chatHistorySearchState.query = String(dom.chatHistorySearchInput?.value || "").trim();
  chatHistorySearchState.offset = 0;
  chatHistorySearchState.timer = window.setTimeout(performChatHistorySearch, 180);
}

function updateChatHistorySearchRoleButtons() {
  dom.chatHistorySearchRoleButtons?.forEach(button => {
    const active = String(button.dataset.historyRole || "all") === chatHistorySearchState.role;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

function formatHistorySearchTime(value) {
  const parsed = new Date(String(value || ""));
  if (!Number.isFinite(parsed.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function appendHighlightedHistoryText(node, text, query) {
  const source = String(text || "");
  const needle = String(query || "").trim();
  if (!needle) {
    node.textContent = source;
    return;
  }
  const sourceFolded = source.normalize("NFKC").toLocaleLowerCase();
  const needleFolded = needle.normalize("NFKC").toLocaleLowerCase();
  const index = sourceFolded.indexOf(needleFolded);
  if (index < 0) {
    node.textContent = source;
    return;
  }
  node.append(
    document.createTextNode(source.slice(0, index)),
    Object.assign(document.createElement("mark"), {
      textContent: source.slice(index, index + needle.length),
    }),
    document.createTextNode(source.slice(index + needle.length)),
  );
}

function findLoadedHistoryBubble(result) {
  const candidates = [
    ...(result.matchRoles || []).map(role => ({
      role,
      content: role === "user" ? result.user : result.assistant,
    })),
  ];
  return [...(dom.chatLog?.querySelectorAll(".msg") || [])].find(bubble => {
    const role = bubble.classList.contains("assistant") ? "assistant" : "user";
    const body = bubble.querySelector(".message-body");
    return candidates.some(candidate =>
      candidate.role === role
      && String(body?.textContent || "").trim() === String(candidate.content || "").trim()
      && (
        !result.at
        || !bubble.dataset.historyAt
        || bubble.dataset.historyAt === String(result.at)
      )
    );
  }) || null;
}

function activateHistorySearchResult(result, card) {
  const loaded = findLoadedHistoryBubble(result);
  if (loaded) {
    closeChatHistorySearch();
    loaded.classList.add("history-search-hit");
    loaded.scrollIntoView({ block: "center", behavior: "smooth" });
    window.setTimeout(() => loaded.classList.remove("history-search-hit"), 2600);
    return;
  }
  card.classList.toggle("expanded");
  card.querySelectorAll(".chat-history-result-copy").forEach(node => {
    node.style.whiteSpace = card.classList.contains("expanded") ? "pre-wrap" : "";
  });
}

function renderChatHistorySearchResults() {
  if (!dom.chatHistorySearchResults) return;
  dom.chatHistorySearchResults.replaceChildren();
  if (!chatHistorySearchState.results.length) {
    const empty = document.createElement("div");
    empty.className = "chat-history-search-empty";
    empty.textContent = chatHistorySearchState.query
      ? "没有找到相关聊天记录"
      : "还没有可显示的聊天记录";
    dom.chatHistorySearchResults.appendChild(empty);
  }
  chatHistorySearchState.results.forEach(result => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "chat-history-result";
    card.dataset.historyResultId = String(result.id || "");
    const head = document.createElement("span");
    head.className = "chat-history-result-head";
    const actor = document.createElement("strong");
    actor.textContent = (result.matchRoles || []).includes("user")
      && !(result.matchRoles || []).includes("assistant")
      ? "我"
      : (result.matchRoles || []).includes("assistant")
        && !(result.matchRoles || []).includes("user")
        ? "AMADEUS"
        : "本轮对话";
    const at = document.createElement("span");
    at.textContent = formatHistorySearchTime(result.at);
    head.append(actor, at);
    card.appendChild(head);
    for (const [role, content] of [
      ["user", result.user],
      ["assistant", result.assistant],
    ]) {
      if (!String(content || "").trim()) continue;
      const copy = document.createElement("span");
      copy.className = "chat-history-result-copy";
      copy.dataset.role = role;
      appendHighlightedHistoryText(
        copy,
        `${role === "user" ? "我" : "AMADEUS"}：${String(content).trim()}`,
        chatHistorySearchState.query,
      );
      card.appendChild(copy);
    }
    card.addEventListener("click", () => activateHistorySearchResult(result, card));
    dom.chatHistorySearchResults.appendChild(card);
  });
  const page = Math.floor(chatHistorySearchState.offset / chatHistorySearchState.limit) + 1;
  if (dom.chatHistorySearchPage) dom.chatHistorySearchPage.textContent = `第 ${page} 页`;
  if (dom.chatHistorySearchPrevBtn) dom.chatHistorySearchPrevBtn.disabled = chatHistorySearchState.offset <= 0;
  if (dom.chatHistorySearchNextBtn) dom.chatHistorySearchNextBtn.disabled = !chatHistorySearchState.hasMore;
}

async function performChatHistorySearch() {
  if (dom.chatHistorySearchPanel?.classList.contains("hidden")) return;
  window.clearTimeout(chatHistorySearchState.timer);
  chatHistorySearchState.query = String(dom.chatHistorySearchInput?.value || "").trim();
  chatHistorySearchState.controller?.abort();
  const controller = new AbortController();
  chatHistorySearchState.controller = controller;
  if (dom.chatHistorySearchStatus) dom.chatHistorySearchStatus.textContent = "正在查找…";
  const params = new URLSearchParams({
    q: chatHistorySearchState.query,
    role: chatHistorySearchState.role,
    limit: String(chatHistorySearchState.limit),
    offset: String(chatHistorySearchState.offset),
  });
  try {
    const response = await storageScopedFetch(
      `/api/conversation/search?${params.toString()}`,
      { cache: "no-store", signal: controller.signal },
    );
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    if (controller !== chatHistorySearchState.controller) return;
    chatHistorySearchState.total = Math.max(0, Number(data.total) || 0);
    chatHistorySearchState.hasMore = Boolean(data.hasMore);
    chatHistorySearchState.results = Array.isArray(data.results) ? data.results : [];
    if (dom.chatHistorySearchStatus) {
      dom.chatHistorySearchStatus.textContent = chatHistorySearchState.query
        ? `找到 ${chatHistorySearchState.total} 条相关记录`
        : `最近 ${chatHistorySearchState.total} 条记录`;
    }
    renderChatHistorySearchResults();
  } catch (error) {
    if (error?.name === "AbortError") return;
    if (dom.chatHistorySearchStatus) dom.chatHistorySearchStatus.textContent = "查找失败";
    chatHistorySearchState.results = [];
    renderChatHistorySearchResults();
    toast(`聊天记录查找失败：${error.message}`);
  }
}

function hideThinkingIndicator(generation = 0) {
  if (!thinkingIndicator) return;
  const indicatorGeneration = Number(thinkingIndicator.dataset.generation || 0);
  if (generation && indicatorGeneration && generation !== indicatorGeneration) return;
  thinkingIndicator.remove();
  thinkingIndicator = null;
}

const THINKING_PIPELINE_STAGES = {
  received: ["已接收请求", "消息已经进入处理队列", 0],
  attachments: ["读取输入", "正在读取消息与本轮附件", 0],
  context: ["整理上下文", "正在组合对话、记忆与人格信息", 1],
  "tool-executing": ["执行工具", "正在获取回答所需的视觉、文件或联网证据", 2],
  researching: ["补充证据", "现有证据不足，正在继续检索", 2],
  "model-generating": ["生成回答", "模型正在形成第一版回答", 3],
  "fallback-legacy": ["继续生成", "快速回复没有完整收尾，正在继续组织回答", 3],
  "model-synthesizing": ["综合证据", "模型正在结合工具结果形成回答", 3],
  validating: ["检查回答", "正在检查完整性、上下文与证据", 4],
  repairing: ["修正回答", "正在根据校验结果定向修正", 4],
  finalizing: ["整理输出", "正在选择完整回答并整理情绪参数", 4],
  postprocessing: ["准备交付", "正在校准日语发声稿与记忆更新", 4],
  "speech-segmenting": ["切分语音", "正在按自然语义安排分段播放", 5],
  "voice-first": ["合成首段", "正在优先生成可以立即播放的第一段", 5],
  "voice-prefetch": ["预合成后续", "前一段播放期间正在准备下一段", 5],
  "voice-wait": ["衔接语音", "下一段尚未准备完成，正在保持自然衔接", 5],
  "voice-playing": ["播放语音", "语音已经开始播放，后续段在后台准备", 5],
  complete: ["处理完成", "回答已经交付", 5],
  failed: ["处理失败", "处理链路发生错误", 5],
};

function thinkingStageDefinition(stage) {
  return THINKING_PIPELINE_STAGES[String(stage || "").toLowerCase()] || ["处理中", "正在完成当前步骤", 1];
}

function updateThinkingIndicatorStage(stage, detail = "", elapsedMs = null) {
  if (!thinkingIndicator) return;
  const [labelText, defaultDetail, step] = thinkingStageDefinition(stage);
  thinkingIndicator.dataset.stage = String(stage || "working");
  const label = thinkingIndicator.querySelector(".thinking-label");
  if (label) label.textContent = DEV_MODE ? labelText : "正在思考";
  const detailNode = thinkingIndicator.querySelector(".thinking-stage-detail");
  if (detailNode) detailNode.textContent = String(detail || defaultDetail);
  const elapsedNode = thinkingIndicator.querySelector(".thinking-elapsed");
  if (elapsedNode && Number.isFinite(Number(elapsedMs))) {
    elapsedNode.textContent = `${(Number(elapsedMs) / 1000).toFixed(1)}s`;
  }
  thinkingIndicator.querySelectorAll(".thinking-stage-track i").forEach((node, index) => {
    node.classList.toggle("done", index < step);
    node.classList.toggle("active", index === step);
  });
}

function stopThinkingProgressPolling(requestId = "") {
  if (requestId && thinkingProgressRequestId && requestId !== thinkingProgressRequestId) return;
  if (thinkingProgressTimer) window.clearInterval(thinkingProgressTimer);
  thinkingProgressTimer = 0;
  thinkingProgressRequestId = "";
}

function startThinkingProgressPolling(requestId, generation) {
  stopThinkingProgressPolling();
  if (!DEV_MODE || !requestId) return;
  thinkingProgressRequestId = requestId;
  const poll = async () => {
    if (requestId !== thinkingProgressRequestId || generation !== chatRequestGeneration) return;
    try {
      const response = await fetch(`/api/developer/chat-progress?requestId=${encodeURIComponent(requestId)}`, {
        cache: "no-store",
      });
      if (!response.ok) return;
      const progress = await response.json();
      if (!progress.ok || requestId !== thinkingProgressRequestId) return;
      updateThinkingIndicatorStage(progress.stage, progress.detail, progress.elapsedMs);
    } catch {
      // Progress is diagnostic-only and must never disturb the chat request.
    }
  };
  poll();
  thinkingProgressTimer = window.setInterval(poll, 420);
}

function showThinkingIndicator(generation, stage = "context", detail = "") {
  if (!dom.chatLog) return;
  hideThinkingIndicator();
  const bubble = document.createElement("article");
  bubble.className = "msg assistant thinking-indicator";
  bubble.classList.toggle("developer-pipeline", DEV_MODE);
  bubble.dataset.generation = String(generation || 0);
  bubble.setAttribute("role", "status");
  bubble.setAttribute("aria-live", "polite");
  bubble.setAttribute("aria-label", "AMADEUS 正在思考");

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = DEV_MODE ? "AMADEUS · PIPELINE" : "AMADEUS · THINKING";
  const body = document.createElement("span");
  body.className = "message-body thinking-body";
  const label = document.createElement("span");
  label.className = "thinking-label";
  label.textContent = "正在思考";
  const dots = document.createElement("span");
  dots.className = "thinking-dots";
  dots.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) {
    const dot = document.createElement("i");
    dot.style.setProperty("--thinking-dot-index", String(index));
    dots.appendChild(dot);
  }
  body.append(label, dots);
  if (DEV_MODE) {
    const stagePanel = document.createElement("span");
    stagePanel.className = "thinking-stage-panel";
    const track = document.createElement("span");
    track.className = "thinking-stage-track";
    track.setAttribute("aria-hidden", "true");
    for (let index = 0; index < 6; index += 1) track.appendChild(document.createElement("i"));
    const stageDetail = document.createElement("span");
    stageDetail.className = "thinking-stage-detail";
    const elapsed = document.createElement("span");
    elapsed.className = "thinking-elapsed";
    elapsed.textContent = "0.0s";
    stagePanel.append(track, stageDetail, elapsed);
    body.appendChild(stagePanel);
  }
  bubble.append(meta, body);
  dom.chatLog.appendChild(bubble);
  thinkingIndicator = bubble;
  updateThinkingIndicatorStage(stage, detail, 0);
  scrollChatToLatest();
  requestAnimationFrame(() => bubble.scrollIntoView({ block: "end", inline: "nearest" }));
}

function appendBubble(role, content, metaData = {}) {
  const bubble = document.createElement("article");
  bubble.className = `msg ${role === "assistant" ? "assistant" : "user"}`;
  bubble.dataset.historyAt = String(metaData.at || "");
  bubble.dataset.historyRole = role === "assistant" ? "assistant" : "user";
  bubble.dataset.messageId = String(metaData.messageId || "");
  bubble.dataset.turnId = String(metaData.turnId || "");
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = role === "assistant" ? assistantMetaLabel(metaData) : "YOU";
  const body = document.createElement("span");
  body.className = "message-body";
  body.textContent = visibleMessageContent(role, content);
  const actions = document.createElement("span");
  actions.className = "message-actions";
  const replyButton = document.createElement("button");
  replyButton.type = "button";
  replyButton.className = "reply-message-btn";
  replyButton.textContent = "回复";
  replyButton.title = "回复这条消息";
  replyButton.setAttribute("aria-label", "回复这条消息");
  replyButton.addEventListener("click", () => replyToMessage(role, body.textContent, metaData));
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "copy-message-btn";
  copyButton.textContent = "复制";
  copyButton.title = "复制这条对话文字";
  copyButton.setAttribute("aria-label", "复制这条对话文字");
  copyButton.addEventListener("click", () => copyMessageText(body.textContent));
  actions.append(replyButton, copyButton);
  const replyAnchor = metaData.replyAnchor && typeof metaData.replyAnchor === "object"
    ? metaData.replyAnchor : null;
  let replySource = null;
  if (replyAnchor?.targetMessageIds?.length) {
    replySource = document.createElement("button");
    replySource.type = "button";
    replySource.className = "message-reply-source";
    replySource.textContent = `回复自：${String(replyAnchor.quotedPreview || "原消息").slice(0, 180)}`;
    replySource.title = "跳到被回复的消息";
    replySource.addEventListener("click", () => scrollToQuotedMessage(replyAnchor.targetMessageIds[0]));
  }
  bubble.append(meta, actions);
  if (replySource) bubble.appendChild(replySource);
  bubble.appendChild(body);
  appendAttachmentCards(bubble, metaData.attachments);
  const diagnosticSpeechText = metaData.speechText || metaData.spokenTextJa;
  if (DEV_MODE && role === "assistant" && diagnosticSpeechText) {
    const ja = document.createElement("span");
    ja.className = "dev-ja-line";
    const label = normalizeSpeechLanguage(metaData.speechLanguage) === "zh-CN" ? "ZH" : "JA";
    ja.textContent = `${label}: ${stripVisibleStageDirections(diagnosticSpeechText)}`;
    bubble.appendChild(ja);
  }
  if (role === "assistant" && Array.isArray(metaData.sources) && metaData.sources.length) {
    const sourceList = document.createElement("details");
    sourceList.className = "message-sources";
    const summary = document.createElement("summary");
    summary.textContent = `参考来源 · ${Math.min(metaData.sources.length, 8)}`;
    summary.title = "展开查看来源";
    const sourceDetails = document.createElement("div");
    sourceDetails.className = "message-source-details";
    sourceList.append(summary, sourceDetails);
    metaData.sources.slice(0, 8).forEach(source => {
      const safeUrl = safeExternalSourceUrl(source.url);
      const link = document.createElement("a");
      link.href = safeUrl || "#";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = `[${source.id || "S?"}] ${source.title || source.domain || "网页来源"}`;
      link.title = String(source.excerpt || source.url || "");
      link.addEventListener("click", async event => {
        if (!safeUrl) {
          event.preventDefault();
          toast("来源 URL 未通过安全校验");
          return;
        }
        event.preventDefault();
        try {
          const response = await fetch("/api/actions/open-browser", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              url: safeUrl,
              actionId: `source_${String(source.sourceUid || source.id || Date.now())}`,
            }),
          });
          const result = await response.json();
          if (!response.ok || !result.ok) {
            toast(result.message || "浏览器没有接受打开请求");
          }
        } catch (_error) {
          toast("打开请求未能交给本地浏览器网关");
        }
      });
      sourceDetails.appendChild(link);
    });
    bubble.appendChild(sourceList);
  }
  appendGeneratedFileCards(bubble, metaData.files);
  dom.chatLog.appendChild(bubble);
  dom.chatLog.scrollTop = dom.chatLog.scrollHeight;
  return bubble;
}

async function copyMessageText(value) {
  const text = String(value || "");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
  toast("对话文字已复制");
}

function appendAttachmentCards(bubble, attachments) {
  if (!Array.isArray(attachments) || !attachments.length) return;
  const list = document.createElement("div");
  list.className = "message-attachments";
  attachments.slice(0, MAX_CHAT_ATTACHMENTS).forEach(item => {
    if (!item || typeof item !== "object") return;
    const card = document.createElement("div");
    card.className = "message-attachment-card";
    const icon = document.createElement("span");
    icon.className = "message-file-icon";
    icon.textContent = String(item.mimeType || "").startsWith("image/") || item.kind === "image" ? "IMG" : "FILE";
    const label = document.createElement("span");
    label.className = "message-file-label";
    const name = document.createElement("strong");
    name.textContent = String(item.name || "附件");
    const size = document.createElement("small");
    size.textContent = formatFileSize(item.size);
    label.append(name, size);
    card.append(icon, label);
    list.appendChild(card);
  });
  bubble.appendChild(list);
}

function appendGeneratedFileCards(bubble, files) {
  if (!Array.isArray(files) || !files.length) return;
  const list = document.createElement("div");
  list.className = "generated-files";
  files.forEach(item => {
    if (!item || typeof item !== "object" || !item.id) return;
    const card = document.createElement("button");
    card.type = "button";
    card.className = "generated-file-card";
    card.title = `保存 ${item.name || "生成文件"}`;
    const icon = document.createElement("span");
    icon.className = "message-file-icon";
    icon.textContent = "SAVE";
    const label = document.createElement("span");
    label.className = "message-file-label";
    const name = document.createElement("strong");
    name.textContent = String(item.name || "生成文件");
    const size = document.createElement("small");
    size.textContent = `${formatFileSize(item.size)} · 点击保存`;
    label.append(name, size);
    card.append(icon, label);
    card.addEventListener("click", () => saveGeneratedFile(item));
    list.appendChild(card);
  });
  bubble.appendChild(list);
}

async function saveGeneratedFile(item) {
  if (window.pywebview?.api?.saveGeneratedFile) {
    const result = await window.pywebview.api.saveGeneratedFile(String(item.id), String(item.name || "output.txt"));
    if (result?.ok) toast(`文件已保存：${result.path}`);
    else if (!result?.cancelled) toast("文件保存失败");
    return;
  }
  const link = document.createElement("a");
  link.href = String(item.url || `/api/files/${encodeURIComponent(item.id)}`);
  link.download = String(item.name || "output.txt");
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function safeExternalSourceUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (!host || host === "localhost" || host.endsWith(".local") || host.endsWith(".internal")) return "";
    if (/^(127\.|10\.|169\.254\.|192\.168\.)/.test(host)) return "";
    const private172 = host.match(/^172\.(\d+)\./);
    if (private172 && Number(private172[1]) >= 16 && Number(private172[1]) <= 31) return "";
    if (host === "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80:")) return "";
    return parsed.href;
  } catch {
    return "";
  }
}

function assistantMetaLabel(metaData = {}) {
  const bits = ["AMADEUS"];
  const source = String(metaData.source || "");
  const internalRecoverySources = new Set([
    "pipeline-error",
    "local-recovery",
    "local-action-state-recovery",
    "model-pipeline-rescue",
  ]);
  if (source && !internalRecoverySources.has(source)) bits.push(source.toUpperCase());
  if (metaData.latencyMs) bits.push(`${metaData.latencyMs}MS`);
  if (typeof metaData.memoryCount === "number") bits.push(`MEM ${metaData.memoryCount}`);
  if (DEV_MODE && metaData.dialoguePolicy?.reply_mode) {
    bits.push(`MODE ${String(metaData.dialoguePolicy.reply_mode).toUpperCase()}`);
  }
  if (DEV_MODE && Number.isFinite(Number(metaData.dialogueValidation?.sentence_count))) {
    bits.push(`${Number(metaData.dialogueValidation.sentence_count)}S`);
  }
  if (metaData.spokenTextJaRepaired) bits.push("JA_REPAIRED");
  return bits.join(" · ");
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
      || (!REQUESTED_DEV_MODE ? localStorage.getItem("amadeus-live2d-chat-v1") : null);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        messages: Array.isArray(parsed.messages)
          ? parsed.messages.map(message => ({
              ...message,
              content: visibleMessageContent(message?.role, message?.content),
              meta: message?.meta && message.role === "assistant"
                ? { ...message.meta, spokenTextJa: stripVisibleStageDirections(message.meta.spokenTextJa) }
                : message?.meta,
            }))
          : [],
        memories: Array.isArray(parsed.memories) ? parsed.memories : DEFAULT_MEMORIES.slice(),
        memoryRecords: [],
        contextEpoch: Math.max(0, Number(parsed.contextEpoch) || 0),
        conversationId: String(parsed.conversationId || `conv_${Math.max(0, Number(parsed.contextEpoch) || 0)}`),
        chatWindowHeight: clamp(toNumber(parsed.chatWindowHeight, 330), 220, 1200),
        proactiveControl: normalizeProactiveControl(parsed.proactiveControl),
      };
    }
  } catch {
    // Rebuild damaged local state below.
  }
  return {
    messages: [],
    memories: DEFAULT_MEMORIES.slice(),
    memoryRecords: [],
    contextEpoch: 0,
    conversationId: "conv_0",
    chatWindowHeight: 330,
    proactiveControl: normalizeProactiveControl(null),
  };
}

function messageHistorySignature(message) {
  const role = message?.role === "assistant" ? "assistant" : "user";
  const messageId = String(message?.meta?.messageId || "").trim();
  if (messageId) return `id\u0000${messageId}`;
  return `${role}\u0000${visibleMessageContent(role, message?.content).trim()}`;
}

async function hydrateConversationHistory() {
  try {
    const response = await storageScopedFetch("/api/conversation/history", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
      if (String(data.code || "").startsWith("context_")) toast(data.error || "旧记录恢复失败，原文件已保留");
      return;
    }
    if (data.compatibilityRecovery?.status === "recovered") {
      toast(data.compatibilityRecovery.notice || "旧格式记录已恢复，原文件备份已保留");
    }
    if (data.recoveryRequired) {
      toast("检测到会话索引不一致；已从只读归档恢复显示，原始文件没有被覆盖");
      console.warn("conversation recovery is active:", data.storageHealth || {});
    }
    state.contextEpoch = Math.max(
      0,
      Number(data.conversationEpoch ?? state.contextEpoch) || 0,
    );
    state.conversationId = String(data.conversationId || `conv_${state.contextEpoch}`);
    if (data.proactiveControl && typeof data.proactiveControl === "object") {
      applyProactiveControl(data.proactiveControl, { persistState: false });
    }
    state.messages = (Array.isArray(state.messages) ? state.messages : []).filter(message => {
      const messageConversationId = String(message?.meta?.conversationId || "");
      return messageConversationId
        ? messageConversationId === state.conversationId
        : Math.max(0, Number(message?.meta?.contextEpoch) || 0) === state.contextEpoch;
    });
    if (!Array.isArray(data.messages) || !data.messages.length) {
      renderMessages();
      persist();
      return;
    }
    const localMessages = Array.isArray(state.messages) ? state.messages : [];
    const localBySignature = new Map();
    localMessages.forEach(message => {
      const key = messageHistorySignature(message);
      if (!localBySignature.has(key)) localBySignature.set(key, []);
      localBySignature.get(key).push(message);
    });
    const restored = data.messages.map(message => {
      const role = message?.role === "assistant" ? "assistant" : "user";
      const content = visibleMessageContent(role, message?.content);
      const key = messageHistorySignature({ role, content });
      const richerLocal = localBySignature.get(key)?.shift();
      return {
        role,
        content,
        meta: {
          ...(message?.meta || {}),
          ...(richerLocal?.meta || {}),
          actionReceipt: message?.meta?.actionReceipt || richerLocal?.meta?.actionReceipt || null,
          todoContext: message?.meta?.todoContext || richerLocal?.meta?.todoContext || null,
          toolUsed: message?.meta?.toolUsed || richerLocal?.meta?.toolUsed || "",
          toolStatus: message?.meta?.toolStatus || richerLocal?.meta?.toolStatus || "",
          contextEpoch: Math.max(
            0,
            Number(message?.meta?.contextEpoch ?? richerLocal?.meta?.contextEpoch ?? 0) || 0,
          ),
          restored: true,
        },
      };
    });
    let sharedPrefix = 0;
    const prefixLimit = Math.min(restored.length, localMessages.length);
    while (
      sharedPrefix < prefixLimit
      && messageHistorySignature(restored[sharedPrefix]) === messageHistorySignature(localMessages[sharedPrefix])
    ) sharedPrefix += 1;
    const localOnlyTail = sharedPrefix === prefixLimit ? localMessages.slice(sharedPrefix) : [];
    state.messages = [...restored, ...localOnlyTail].slice(-MAX_MESSAGES);
    persist();
  } catch (error) {
    console.warn("conversation history restore failed:", error);
  }
}

async function recordProactiveConversationEvent(message, mood = {}) {
  if (!message || message.role !== "assistant" || message.meta?.kind !== "proactive") return;
  const eventId = String(
    message.meta.proactiveEventId
    || `proactive-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  );
  message.meta.proactiveEventId = eventId;
  if (message.meta.proactivePersisted || proactivePersistenceInFlight.has(eventId)) return;
  proactivePersistenceInFlight.add(eventId);
  persist();
  try {
    const response = await storageScopedFetch("/api/conversation/proactive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        eventId,
        reply: String(message.content || ""),
        emotion: String(mood.emotion || message.meta.emotion || currentEmotion || "neutral"),
        proactiveKind: String(message.meta.proactiveKind || ""),
        proactiveSubject: String(message.meta.proactiveSubject || ""),
        proactivePhase: String(message.meta.proactivePhase || ""),
        unansweredCount: Math.max(0, Number(message.meta.proactiveUnansweredCount) || 0),
        silenceLimit: Math.max(0, Number(message.meta.proactiveSilenceLimit) || 0),
        proactiveDirective: String(message.meta.proactiveDirective || "unchanged"),
        proactiveObservation: message.meta.proactiveObservation || null,
        contextEpoch: Math.max(0, Number(state.contextEpoch) || 0),
      }),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || "proactive persistence failed");
    if (data.proactiveControl && typeof data.proactiveControl === "object") {
      applyProactiveControl(data.proactiveControl, { persistState: false });
    }
    message.meta.proactivePersisted = true;
    delete message.meta.proactiveObservation;
    persist();
  } catch (error) {
    console.warn("proactive conversation persistence failed:", error);
  } finally {
    proactivePersistenceInFlight.delete(eventId);
  }
}

function syncPendingProactiveConversationEvents() {
  state.messages
    .filter(message =>
      message?.role === "assistant"
      && message.meta?.kind === "proactive"
      && !message.meta?.proactivePersisted
    )
    .forEach(message => recordProactiveConversationEvent(message));
}

function cleanHistoryForModel() {
  const currentEpoch = Math.max(0, Number(state.contextEpoch) || 0);
  const currentConversationId = String(state.conversationId || "");
  return state.messages.filter(message => {
    const messageConversationId = String(message.meta?.conversationId || "");
    const belongsToConversation = messageConversationId
      ? messageConversationId === currentConversationId
      : Math.max(0, Number(message.meta?.contextEpoch) || 0) === currentEpoch;
    return belongsToConversation
      && message.meta?.source !== "connection-error"
      && !String(message.content || "").startsWith("接口没有正常回应。")
      && !STALE_FALLBACK_REPLIES.has(String(message.content || "").trim());
  });
}

function pruneStaleFallbacks() {
  const cleaned = state.messages.filter(message =>
    !STALE_FALLBACK_REPLIES.has(String(message.content || "").trim())
  );
  if (cleaned.length !== state.messages.length) {
    state.messages = cleaned.slice(-MAX_MESSAGES);
    persist();
  }
}

function persist() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    messages: state.messages.slice(-MAX_MESSAGES),
    memories: state.memories.slice(0, MAX_MEMORIES),
    contextEpoch: Math.max(0, Number(state.contextEpoch) || 0),
    conversationId: String(state.conversationId || `conv_${Math.max(0, Number(state.contextEpoch) || 0)}`),
    chatWindowHeight: clamp(toNumber(state.chatWindowHeight, 330), 220, 1200),
    proactiveControl: normalizeProactiveControl(state.proactiveControl),
  }));
}

function toast(message) {
  dom.toast.textContent = message;
  dom.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => dom.toast.classList.remove("show"), 1900);
}

function showVoiceFailure(title, error) {
  if (!dom.voiceFailureBanner) return;
  const raw = error instanceof Error ? error.message : String(error || "未知错误");
  const compact = raw.replace(/\s+/g, " ").trim().slice(0, 280);
  dom.voiceFailureTitle.textContent = title || "语音合成失败";
  dom.voiceFailureMessage.textContent = `${compact || "本地语音没有生成可播放音频"}。本次未播放系统音。`;
  dom.voiceFailureBanner.hidden = false;
}

function clearVoiceFailure() {
  if (!dom.voiceFailureBanner) return;
  dom.voiceFailureBanner.hidden = true;
}

function failLocalSpeech(error, generation = speechGeneration, title = "本地语音合成失败") {
  if (generation !== speechGeneration) return null;
  console.warn(`${title}:`, error);
  speechState.pending = false;
  speechState.active = false;
  speechState.textRevealPending = false;
  speechState.systemSpeechActive = false;
  speechState.endsAt = performance.now();
  speakUntil = speechState.endsAt;
  resetAnimeMouthState();
  const message = error instanceof Error ? error.message : String(error || "未知错误");
  recordVoiceDiagnostic("local-failed", `${title}，本次未播放系统音：${message}`, { generation });
  showVoiceFailure(title, error);
  return false;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function toNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function unique(items) {
  const seen = new Set();
  return (items || []).filter(item => {
    const key = String(item).trim();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

window.amadeusLive2D = {
  setEmotion,
  speakPulse,
  pulseInteraction,
};
