"use strict";

const MODEL_REGISTRY_URL = "/assets/live2d/models.json";
const FALLBACK_MODEL_PROFILE = {
  id: "aipoet-kurisu",
  label: "标准实验服",
  description: "经典半身模型的标准实验服，与日常服共用脸部、表情和动作。",
  modelUrl: "/assets/live2d/AiPoetKurisu/kurisu.model3.json",
  supportsNativeExpressions: true,
  hiddenParameters: [],
  hiddenParts: [],
  fixedParameters: {},
  parameterAliases: { blush: ["ParamCheek"] },
  motionMap: {},
};
const BOOT_BACKGROUND_URL = "/assets/boot/amadeus_background_frame084_outpaint_grid_v5.png";
const BACKGROUND_PRESETS = new Set([
  "boot", "grid", "solid", "custom",
]);
const BACKGROUND_IMAGE_PRESETS = new Map();
const BACKGROUND_PRESET_ALIASES = {
  "binary": "grid",
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
const SAVED_API_KEY_MASK = "••••••••••••";
const MAX_MESSAGES = 240;
const MAX_MEMORIES = 512;
const MODEL_HISTORY_LIMIT = 36;
let DEV_MODE = REQUESTED_DEV_MODE;
let MODEL_PREVIEW_ID = DEV_MODE ? new URLSearchParams(window.location.search).get("modelPreview") || "" : "";
let MODEL_PREVIEW_ONLY = DEV_MODE && new URLSearchParams(window.location.search).get("previewOnly") === "1";
let SKIP_BOOT = DEV_MODE && new URLSearchParams(window.location.search).get("skipBoot") === "1";
const PROACTIVE_LOGIC = window.AmadeusProactiveLogic;
if (!PROACTIVE_LOGIC) throw new Error("proactive_logic.js is required");
const CHAT_HISTORY_SEARCH = window.AmadeusChatHistorySearch;
if (!CHAT_HISTORY_SEARCH) throw new Error("chat/history_search.js is required");
const CHAT_CONVERSATION_PICKER = window.AmadeusConversationPicker;
if (!CHAT_CONVERSATION_PICKER) throw new Error("chat/conversation_picker.js is required");
const CHAT_BOOKMARKS = window.AmadeusBookmarks;
if (!CHAT_BOOKMARKS) throw new Error("chat/bookmarks.js is required");
const CHAT_MESSAGE_VIEW = window.AmadeusMessageView;
if (!CHAT_MESSAGE_VIEW) throw new Error("chat/message_view.js is required");
const API_VERIFICATION = window.AmadeusApiVerification;
if (!API_VERIFICATION) throw new Error("settings/api_verification.js is required");
const MODEL_PROFILE_CONTROLLER = window.AmadeusModelProfileController;
if (!MODEL_PROFILE_CONTROLLER) throw new Error("settings/model_profile_controller.js is required");
const SETTINGS_CONTROLLER = window.AmadeusSettingsController;
if (!SETTINGS_CONTROLLER) throw new Error("settings/settings_controller.js is required");
const UPDATE_CONTROLLER = window.AmadeusUpdateController;
if (!UPDATE_CONTROLLER) throw new Error("settings/update_controller.js is required");
const TOUCH_INTERACTION = window.AmadeusTouchInteraction;
if (!TOUCH_INTERACTION) throw new Error("character/touch_interaction.js is required");
const PROACTIVE_CONTROLLER = window.AmadeusProactiveController;
if (!PROACTIVE_CONTROLLER) throw new Error("chat/proactive_controller.js is required");
const REMINDER_POLLING = window.AmadeusReminderPolling;
if (!REMINDER_POLLING) throw new Error("chat/reminder_polling.js is required");
const TODO_CONTROLLER = window.AmadeusTodoController;
if (!TODO_CONTROLLER) throw new Error("chat/todo_controller.js is required");
const MODEL_TRANSFORM_CONTROLLER = window.AmadeusModelTransformController;
if (!MODEL_TRANSFORM_CONTROLLER) throw new Error("character/model_transform_controller.js is required");
const SALIERI_PANEL = window.AmadeusSalieriPanel;
if (!SALIERI_PANEL) throw new Error("developer/salieri_panel.js is required");
const VOICE_DIAGNOSTICS = window.AmadeusVoiceDiagnostics;
if (!VOICE_DIAGNOSTICS) throw new Error("developer/voice_diagnostics.js is required");
const PERSONA_CONTROLLER = window.AmadeusPersonaController;
if (!PERSONA_CONTROLLER) throw new Error("developer/persona_controller.js is required");
const RELATIONSHIP_CONTROLLER = window.AmadeusRelationshipController;
if (!RELATIONSHIP_CONTROLLER) throw new Error("developer/relationship_controller.js is required");
const PARAMETER_LAB = window.AmadeusParameterLab;
if (!PARAMETER_LAB) throw new Error("developer/parameter_lab.js is required");
const DEVELOPER_EMOTION_CONTROLLER = window.AmadeusDeveloperEmotionController;
if (!DEVELOPER_EMOTION_CONTROLLER) throw new Error("developer/emotion_controller.js is required");
const DEVELOPER_MODEL_EDITOR = window.AmadeusDeveloperModelEditor;
if (!DEVELOPER_MODEL_EDITOR) throw new Error("developer/model_editor.js is required");
const VOICE_REFERENCE_CONTROLLER = window.AmadeusVoiceReferenceController;
if (!VOICE_REFERENCE_CONTROLLER) throw new Error("developer/voice_reference_controller.js is required");
const EXPRESSION_CONTROLLER = window.AmadeusExpressionController;
if (!EXPRESSION_CONTROLLER) throw new Error("character/expression_controller.js is required");
const SCENE_CONTROLLER = window.AmadeusSceneController;
if (!SCENE_CONTROLLER) throw new Error("character/scene_controller.js is required");
const FRAME_CONTROLLER = window.AmadeusFrameController;
if (!FRAME_CONTROLLER) throw new Error("character/frame_controller.js is required");
const BACKGROUND_RENDERER = window.AmadeusBackgroundRenderer;
if (!BACKGROUND_RENDERER) throw new Error("character/background_renderer.js is required");
const MOUTH_PARAMETER_RENDERER = window.AmadeusMouthParameterRenderer;
if (!MOUTH_PARAMETER_RENDERER) throw new Error("character/mouth_parameter_renderer.js is required");
const PARAMETER_RENDERER = window.AmadeusParameterRenderer;
if (!PARAMETER_RENDERER) throw new Error("character/parameter_renderer.js is required");
const ASSISTANT_PRESENTATION = window.AmadeusAssistantPresentation;
if (!ASSISTANT_PRESENTATION) throw new Error("character/assistant_presentation.js is required");
const VISUAL_PARAMETER_CONTROLLER = window.AmadeusVisualParameterController;
if (!VISUAL_PARAMETER_CONTROLLER) throw new Error("character/visual_parameter_controller.js is required");
const GESTURE_MOTION = window.AmadeusGestureMotion;
if (!GESTURE_MOTION) throw new Error("character/gesture_motion.js is required");
const NATIVE_MOTION_CONTROLLER = window.AmadeusNativeMotionController;
if (!NATIVE_MOTION_CONTROLLER) throw new Error("character/native_motion_controller.js is required");
const CHAT_CONTROLLER = window.AmadeusChatController;
if (!CHAT_CONTROLLER) throw new Error("chat/chat_controller.js is required");
const THINKING_INDICATOR = window.AmadeusThinkingIndicator;
if (!THINKING_INDICATOR) throw new Error("chat/thinking_indicator.js is required");
const CONVERSATION_STATE = window.AmadeusConversationState;
if (!CONVERSATION_STATE) throw new Error("chat/conversation_state.js is required");

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
  apiEndpoint: "https://api.deepseek.com/chat/completions",
  apiModel: "deepseek-v4-flash-vision-exp",
  cognitiveRepairModel: "follow",
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
  backgroundPreset: "grid",
  customBackgroundPath: "",
  backgroundViews: {},
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
  permissionReadScreen: true,
  permissionReadCamera: true,
  cameraIndex: 0,
  permissionReadSystemTime: true,
  permissionWebSearch: true,
  permissionFileRead: true,
  permissionFileWrite: true,
  permissionOpenBrowser: true,
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
let backgroundRenderer = null;
let mouthParameterRenderer = null;
let parameterRenderer = null;
let developerEmotionController = null;
let developerModelEditorController = null;
let developerParameterLabController = null;
let voiceReferenceController = null;
let backgroundDraft = null;
let backgroundEditor;
let windowAspectRatios = { lite: [0.55, 0.95], full: [1.15, 1.90] };
let sceneController = null;
const EMPTY_SCENE_STATE = Object.freeze({
  app: null,
  backgroundLayer: null,
  model: null,
  coreModel: null,
  modelGarmentRig: null,
  profiles: Object.freeze([FALLBACK_MODEL_PROFILE]),
  overrides: Object.freeze({}),
  activeProfile: FALLBACK_MODEL_PROFILE,
  modelHome: Object.freeze({ x: 0, y: 0, scale: 1 }),
  modelFitReference: Object.freeze({ width: 0, height: 0, layoutMode: "" }),
  status: "idle",
});

function sceneSnapshot() {
  return sceneController?.getState?.() || EMPTY_SCENE_STATE;
}
let frameController = null;
const EMPTY_FRAME_STATE = Object.freeze({
  tickerTime: 0,
  blinkUntil: 0,
  nextBlink: 0,
  blink: 0,
  eyeOpen: 1,
  breath: 0.5,
  idleSway: 0,
  pointer: Object.freeze({ x: 0, y: 0 }),
  headLook: Object.freeze({ x: 0, y: 0 }),
  eyeLook: Object.freeze({ x: 0, y: 0 }),
  eyeDrift: Object.freeze({ x: 0, y: 0, nextAt: 0 }),
  shyGazeAwayDirection: 1,
});

function frameSnapshot() {
  return frameController?.getState?.() || EMPTY_FRAME_STATE;
}
const EXPRESSION_POST_SPEECH_HOLD_MS = 8000;
const armMotionController = AmadeusArmMotion.createController();
let annotatedExpressionStyleEmotion = "neutral";
let annotatedExpressionVariantUnit = Math.random();
let annotatedExpressionMirrorSide = Math.random() < 0.5 ? -1 : 1;
let availableVoices = [];
let settings = { ...DEFAULT_SETTINGS };
let state = null;
let connectionStatus = "LOCAL";
let relationshipTone = "close_companion";
let persistentColdShoulder = false;
let transientInteraction = "";
let transientInteractionUntil = 0;
let proactiveController = null;
let reminderPollingController = null;
const speechPlayback = AmadeusSpeechPlayback.createSpeechPlaybackController({
  getSettings: () => settings,
  defaultSettings: DEFAULT_SETTINGS,
  gateway: { fetch: (...args) => fetch(...args) },
  platform: {
    createAudio: url => new Audio(url),
    createObjectURL: blob => URL.createObjectURL(blob),
    revokeObjectURL: url => URL.revokeObjectURL(url),
    createAudioContext: () => new (window.AudioContext || window.webkitAudioContext)(),
    hasSystemSpeech: () => "speechSynthesis" in window,
    speechSynthesis: () => window.speechSynthesis,
    createUtterance: text => new SpeechSynthesisUtterance(text),
    selectVoice: () => selectVoice(),
  },
  clock: {
    now: () => performance.now(),
    delay: ms => delay(ms),
    setTimeout: (callback, ms) => window.setTimeout(callback, ms),
    clearTimeout: timer => window.clearTimeout(timer),
  },
  mood: {
    currentEmotion: () => expressionSnapshot().currentEmotion,
    setEmotion: (...args) => setEmotion(...args),
    protectExpression: until => protectCurrentExpressionThroughSpeech(until),
  },
  feedback: {
    diagnostic: (...args) => recordVoiceDiagnostic(...args),
    syncSubmitButton: () => syncChatSubmitButton(),
    showThinking: (...args) => thinkingIndicatorController?.show?.(...args),
    hideThinking: (...args) => thinkingIndicatorController?.hide?.(...args),
    updateThinking: (...args) => thinkingIndicatorController?.updateStage?.(...args),
    showFailure: (...args) => showVoiceFailure(...args),
    clearFailure: () => clearVoiceFailure(),
  },
  rules: {
    normalizeSpeechPace: value => normalizeSpeechPace(value),
    speedForSpeechPace: value => speedForSpeechPace(value),
    normalizeVoiceIntensity: value => normalizeVoiceIntensity(value),
    normalizeSpeechLanguage: value => normalizeSpeechLanguage(value),
    normalizeSpeechProsody: value => normalizeSpeechProsody(value),
    speechLanguageProfile: value => speechLanguageProfile(value),
    chooseSpeechText: (...args) => chooseSpeechText(...args),
    resolveSpeechPace: (...args) => resolveSpeechPace(...args),
    resolveSpeechSpeed: (...args) => resolveSpeechSpeed(...args),
    resolveAssistantMood: (...args) => resolveAssistantMood(...args),
  },
});
let developerRuntimeTimer = 0;
let developerPersonaSkillMonitor = null;
let connectionWatchdogTimer = 0;
let latestRuntimeStatus = null;
let bookmarksController = null;
let messageView = null;
let apiVerificationController = null;
let modelProfileController = null;
let settingsController = null;
let updateController = null;
let todoController = null;
let modelTransformController = null;
let chatController = null;
let thinkingIndicatorController = null;
let touchInteractionController = null;
let salieriController = null;
let voiceDiagnosticsController = null;
let personaController = null;
let relationshipController = null;
let expressionController = null;
let nextPositiveAmbientMoodAt = performance.now() + 150000 + Math.random() * 210000;
let bootSequenceActive = true;
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
let activeInteractionAssistantMessage = null;
let chatHistorySearchController = null;
let conversationPickerController = null;
let conversationStateController = null;
let assistantPresentationController = null;
let visualParameterController = null;
let gestureMotionController = null;
let nativeMotionController = null;
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

// 会话控制器只保存持久化事务与历史恢复代次；消息数组、当前会话和等待事实仍由页面/主动控制器提供。
conversationStateController = CONVERSATION_STATE.createConversationStateController({
  storage: window.localStorage,
  storageKey: STORAGE_KEY,
  legacyStorageKey: REQUESTED_DEV_MODE ? "" : "amadeus-live2d-chat-v1",
  maxMessages: MAX_MESSAGES,
  maxMemories: MAX_MEMORIES,
  defaultMemories: DEFAULT_MEMORIES,
  staleFallbackReplies: STALE_FALLBACK_REPLIES,
  getState: () => state,
  getWaitingForUser: () => proactiveController?.getWaitingForUser?.() ?? (state?.waitingForUser === true),
  normalizeProactiveControl,
  visibleMessageContent,
  stripVisibleStageDirections,
  clamp,
  toNumber,
  requestHistory: (...args) => storageScopedFetch(...args),
  hydrateContext: (data, previousConversationId) => {
    hydrateProactiveConversationContext(data, previousConversationId);
  },
  renderMessages: () => renderMessages(),
  notify: message => toast(message),
  warn: (...args) => console.warn(...args),
});
state = conversationStateController.load();

// 表现规则控制器只持有归一化/推断算法；expression controller 仍持有当前情绪与动作代次。
assistantPresentationController = ASSISTANT_PRESENTATION.createAssistantPresentationController({
  getSettings: () => settings,
  hasEmotion: emotion => Boolean(emotionTargets[emotion]),
  expressionDefaultsFor,
  nextGestureForEmotion: (...args) => expressionController?.nextGestureForEmotion?.(...args),
  speechLanguageProfile,
  clamp,
  toNumber,
});

// 视觉参数控制器只接收本帧快照和写入端口；窄眼代次、瞳孔与脸红派生不再在页面循环复制。
visualParameterController = VISUAL_PARAMETER_CONTROLLER.createVisualParameterController({
  getActiveProfile: () => sceneSnapshot().activeProfile,
  getVisualEmotion: () => effectiveVisualEmotion(),
  visualEmotionIntensityWeight,
  emotionLifecycleWeight,
  setSmoothParam: (...args) => setSmoothParam(...args),
  setMappedSmoothParam: (...args) => setMappedSmoothParam(...args),
  updateRuntime: payload => parameterRenderer?.updateRuntime(payload),
  renderMouth: payload => mouthParameterRenderer?.render(payload),
  applyAnnotatedFaceLayers: plan => applyAnnotatedFaceLayers(plan),
  modelArmPoseTarget: (...args) => modelArmPoseTarget(...args),
});

// 手势姿态只读取本帧 expression/frame 快照；页面继续拥有渲染循环和模型写入时序。
gestureMotionController = GESTURE_MOTION.createGestureMotionController({
  getExpressionSnapshot: () => expressionSnapshot(),
  getFrameSnapshot: () => frameSnapshot(),
});

// 原生表情/动作只通过 scene profile 与模型端口交付；最后一次表达式/动作由控制器保存。
nativeMotionController = NATIVE_MOTION_CONTROLLER.createNativeMotionController({
  getModel: () => sceneSnapshot().model,
  getProfile: () => sceneSnapshot().activeProfile,
  getRigModelId: () => activeRigModelId(),
  now: () => performance.now(),
  random: () => Math.random(),
});

document.addEventListener("DOMContentLoaded", () => {
  bindDom();
  bindBackgroundRenderer();
  bindExpressionController();
  bindChatHistorySearch();
  bindConversationPicker();
  bindBookmarks();
  bindMessageView();
  bindSettingsController();
  bindUpdateController();
  bindTodoController();
  bindSceneController();
  bindDeveloperModelEditorController();
  bindFrameController();
  bindMouthParameterRenderer();
  bindParameterRenderer();
  bindDeveloperEmotionController();
  bindParameterLabController();
  bindVoiceReferenceController();
  bindModelTransformController();
  bindSalieriPanel();
  bindVoiceDiagnostics();
  bindPersonaController();
  bindRelationshipController();
  bindApiVerification();
  bindModelProfileController();
  bindChatController();
  bindThinkingIndicatorController();
  bindProactiveControllers();
  bindTouchInteraction();
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
    updateCurrentVersion: document.getElementById("updateCurrentVersion"),
    updateLatestVersion: document.getElementById("updateLatestVersion"),
    checkUpdateBtn: document.getElementById("checkUpdateBtn"),
    startUpdateBtn: document.getElementById("startUpdateBtn"),
    updateProgress: document.getElementById("updateProgress"),
    updateStatus: document.getElementById("updateStatus"),
    settingsTabs: [...document.querySelectorAll(".settings-tabs button")],
    tabPages: [...document.querySelectorAll(".tab-page")],
    settingsPersonaStatus: document.getElementById("settingsPersonaStatus"),
    settingsPersonaOverride: document.getElementById("settingsPersonaOverride"),
    reloadSettingsPersonaBtn: document.getElementById("reloadSettingsPersonaBtn"),
    saveSettingsPersonaBtn: document.getElementById("saveSettingsPersonaBtn"),
    apiEndpointInput: document.getElementById("apiEndpointInput"),
    apiModelInput: document.getElementById("apiModelInput"),
    cognitiveRepairModelInput: document.getElementById("cognitiveRepairModelInput"),
    apiKeyInput: document.getElementById("apiKeyInput"),
    visionEnabledInput: document.getElementById("visionEnabledInput"),
    visionApiKeyInput: document.getElementById("visionApiKeyInput"),
    visionApiHint: document.getElementById("visionApiHint"),
    apiNetworkModeInput: document.getElementById("apiNetworkModeInput"),
    apiProxyUrlInput: document.getElementById("apiProxyUrlInput"),
    responseFormatInput: document.getElementById("responseFormatInput"),
    apiKeyHint: document.getElementById("apiKeyHint"),
    modelProfilesBox: document.getElementById("modelProfilesBox"),
    modelProfileActive: document.getElementById("modelProfileActive"),
    modelProfileSelect: document.getElementById("modelProfileSelect"),
    modelProfileNameInput: document.getElementById("modelProfileNameInput"),
    modelProfileOverwriteInput: document.getElementById("modelProfileOverwriteInput"),
    modelProfileClearKeyInput: document.getElementById("modelProfileClearKeyInput"),
    saveModelProfileBtn: document.getElementById("saveModelProfileBtn"),
    activateModelProfileBtn: document.getElementById("activateModelProfileBtn"),
    deleteModelProfileBtn: document.getElementById("deleteModelProfileBtn"),
    modelProfileStatus: document.getElementById("modelProfileStatus"),
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
    backgroundControls: document.getElementById("backgroundControls"),
    backgroundEditorPanel: document.getElementById("backgroundEditorPanel"),
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

function bindChatHistorySearch() {
  chatHistorySearchController = CHAT_HISTORY_SEARCH.createChatHistorySearchController({
    document,
    elements: {
      panel: dom.chatHistorySearchPanel,
      opener: dom.chatHistorySearchBtn,
      input: dom.chatHistorySearchInput,
      closeButton: dom.closeChatHistorySearchBtn,
      roleButtons: dom.chatHistorySearchRoleButtons,
      status: dom.chatHistorySearchStatus,
      results: dom.chatHistorySearchResults,
      prevButton: dom.chatHistorySearchPrevBtn,
      nextButton: dom.chatHistorySearchNextBtn,
      page: dom.chatHistorySearchPage,
      chatLog: dom.chatLog,
    },
    requestPage: requestConversationHistorySearch,
    notify: toast,
    setTimer: window.setTimeout.bind(window),
    clearTimer: window.clearTimeout.bind(window),
    createAbortController: () => new AbortController(),
  });
  chatHistorySearchController.bind();
}

// 会话列表只通过这一处触及页面共享的会话、消息与回复状态；控制器本身不保存它们。
async function applyConversationPickerTransition({ kind, data, requestedId = "" }) {
  invalidateConversationHistoryHydration();
  if (kind === "new") {
    state.contextEpoch = Math.max(0, Number(data.context?.conversationEpoch) || 0);
    state.conversationId = String(data.context?.conversationId || `conv_${state.contextEpoch}`);
    state.messages = [];
    applyProactiveControl(data.context?.proactiveControl, { persistState: false });
    renderMessages();
    persist();
    return;
  }
  if (kind === "active-deleted") {
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
    return;
  }
  if (kind === "switch") {
    state.contextEpoch = Math.max(0, Number(data.context?.conversationEpoch) || 0);
    state.conversationId = String(data.context?.conversationId || requestedId);
    state.messages = [];
    applyProactiveControl(data.context?.proactiveControl, { persistState: false });
    await hydrateConversationHistory();
    renderMessages();
    persist();
  }
}

function bindConversationPicker() {
  conversationPickerController = CHAT_CONVERSATION_PICKER.createConversationPickerController({
    document,
    elements: {
      trigger: dom.newConversationBtn,
      panel: dom.conversationPicker,
      closeButton: dom.conversationPickerCloseBtn,
      count: dom.conversationPickerCount,
      status: dom.conversationPickerStatus,
      list: dom.conversationPickerList,
      createButton: dom.conversationCreateBtn,
      composer: dom.userInput,
    },
    request: storageScopedFetch,
    session: {
      getCurrentConversationId: () => state.conversationId,
      prepareForNavigation: prepareForConversationNavigation,
      applyTransition: applyConversationPickerTransition,
    },
    beforeOpen: () => {
      if (!dom.settingsModal?.classList.contains("hidden")) closeSettings();
      setChatCollapsed(false, { scroll: false });
      bookmarksController?.close();
      closeChatHistorySearch();
    },
    notify: toast,
    confirm: window.confirm.bind(window),
    createAbortController: () => new AbortController(),
  });
  conversationPickerController.bind();
}

function bindBookmarks() {
  bookmarksController = CHAT_BOOKMARKS.createBookmarksController({
    document,
    elements: {
      trigger: dom.bookmarkBtn,
      panel: dom.bookmarkPanel,
      closeButton: dom.bookmarkCloseBtn,
      refreshButton: dom.bookmarkRefreshBtn,
      count: dom.bookmarkCount,
      quickForm: dom.bookmarkQuickForm,
      quickName: dom.bookmarkQuickName,
      quickUrl: dom.bookmarkQuickUrl,
      list: dom.bookmarkList,
      empty: dom.bookmarkEmpty,
      editor: dom.bookmarkEditor,
      editorName: dom.bookmarkEditorName,
      editorUrl: dom.bookmarkEditorUrl,
      editorCancelButton: dom.bookmarkEditorCancelBtn,
      editorDeleteButton: dom.bookmarkEditorDeleteBtn,
    },
    gateway: {
      listBookmarks: async () => {
        const response = await storageScopedFetch("/api/bookmarks", { cache: "no-store" });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || "bookmark load failed");
        return data;
      },
      mutateBookmark: async body => {
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
      },
      openBrowser: async body => {
        const response = await storageScopedFetch("/api/actions/open-browser", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) {
          throw new Error(data.message || data.error || "浏览器没有接受请求");
        }
        return data;
      },
    },
    beforeOpen: () => {
      setChatCollapsed(false, { scroll: false });
      conversationPickerController?.close();
      closeChatHistorySearch();
    },
    notify: toast,
    setTimer: window.setTimeout.bind(window),
    queueMicrotask: window.queueMicrotask.bind(window),
    now: Date.now,
  });
  bookmarksController.bind();
}

function bindMessageView() {
  messageView = CHAT_MESSAGE_VIEW.createMessageView({
    document,
    chatLog: dom.chatLog,
    actions: {
      reply: ({ role, content, meta }) => replyToMessage(role, content, meta),
      openSource: async ({ url, actionId }) => {
        try {
          const response = await fetch("/api/actions/open-browser", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, actionId }),
          });
          const result = await response.json();
          if (!response.ok || !result.ok) {
            toast(result.message || "浏览器没有接受打开请求");
          }
        } catch (_error) {
          toast("打开请求未能交给本地浏览器网关");
        }
      },
      saveGeneratedFile,
    },
    notify: toast,
    isDeveloper: () => DEV_MODE,
    maxAttachments: MAX_CHAT_ATTACHMENTS,
    clipboard: navigator.clipboard,
    formatContent: (role, content) => visibleMessageContent(role, content),
    formatSpeechDiagnostic: metaData => {
      const speechText = metaData.speechText || metaData.spokenTextJa;
      if (!speechText) return "";
      const label = normalizeSpeechLanguage(metaData.speechLanguage) === "zh-CN" ? "ZH" : "JA";
      return `${label}: ${stripVisibleStageDirections(speechText)}`;
    },
    setTimer: window.setTimeout.bind(window),
    requestAnimationFrame,
  });
}

function bindSettingsController() {
  settingsController = SETTINGS_CONTROLLER.createSettingsController({
    getSettings: () => settings,
    normalizeSettings,
    replaceSettings: nextSettings => { settings = nextSettings; },
    renderForm: () => fillSettingsForm(),
    applySettingsToUi: () => applySettingsToUi(),
    request: (...args) => fetch(...args),
    onLoaded: data => {
      registerBackgroundImages(data.backgroundPresets);
      if (data.windowAspectRatios?.lite && data.windowAspectRatios?.full) {
        windowAspectRatios = data.windowAspectRatios;
      }
    },
    onLoadError: error => {
      console.warn(error);
      toast(`设置加载失败：${error.message}`);
    },
    onSettingsSaved: (update, previousSettings) => {
      apiVerificationController?.onSettingsSaved(update, previousSettings);
    },
    applyWindowSettings: nextSettings => {
      if (!window.pywebview?.api?.applyWindowSettings) return null;
      return window.pywebview.api.applyWindowSettings(nextSettings);
    },
    notify: message => toast(message),
  });
  settingsController.bind();
}

function bindUpdateController() {
  updateController = UPDATE_CONTROLLER.createUpdateController({
    elements: {
      current: dom.updateCurrentVersion,
      latest: dom.updateLatestVersion,
      check: dom.checkUpdateBtn,
      start: dom.startUpdateBtn,
      progress: dom.updateProgress,
      status: dom.updateStatus,
    },
    request: (...args) => fetch(...args),
    confirm: message => window.confirm(message),
    notify: message => toast(message),
  });
  updateController.bind();
  updateController.refresh().catch(error => console.warn("update status refresh failed:", error));
}

function bindTodoController() {
  todoController = TODO_CONTROLLER.createTodoController({
    elements: {
      drawer: dom.todoDrawer,
      toggle: dom.todoToggleBtn,
      refresh: dom.todoRefreshBtn,
      filter: dom.todoFilterInput,
      quickForm: dom.todoQuickForm,
      list: dom.todoList,
      editor: dom.todoEditor,
      editorDueAt: dom.todoEditorDueAt,
      editorDueDate: dom.todoEditorDueDate,
      editorCancel: dom.todoEditorCancelBtn,
      editorDelete: dom.todoEditorDeleteBtn,
    },
    storage: {
      readSeenRevision: () => Number(window.localStorage.getItem(TODO_SEEN_REVISION_KEY)) || 0,
      writeSeenRevision: revision => {
        window.localStorage.setItem(TODO_SEEN_REVISION_KEY, String(revision));
      },
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    view: {
      isDrawerOpen: () => Boolean(dom.todoDrawer?.classList.contains("open")),
      setDrawerOpen: open => {
        if (!dom.todoDrawer) return false;
        dom.todoDrawer.classList.toggle("open", Boolean(open));
        dom.todoDrawer.setAttribute("aria-expanded", String(Boolean(open)));
        dom.todoToggleBtn?.setAttribute("aria-label", open ? "收起待办" : "展开待办");
        window.localStorage.setItem(TODO_DRAWER_OPEN_KEY, open ? "1" : "0");
        return true;
      },
      getFilterValue: () => dom.todoFilterInput?.value || "active",
      getQuickTitle: () => dom.todoQuickTitle?.value || "",
      setQuickTitle: value => { if (dom.todoQuickTitle) dom.todoQuickTitle.value = value; },
      renderBadge: state => renderTodoBadge(state),
      renderItems: (items, state) => renderTodoItems(items, state),
      setEditor: item => {
        dom.todoEditorTitle.value = String(item.title || "");
        dom.todoEditorDetails.value = String(item.details || "");
        dom.todoEditorDueAt.value = todoDateTimeInputValue(item.dueAt);
        dom.todoEditorDueDate.value = String(item.dueDate || "");
        dom.todoEditorPriority.value = String(item.priority || "normal");
        dom.todoEditorRemindBefore.value = String(item.remindBeforeMinutes || 0);
        dom.todoEditorRecurrence.value = String(item.recurrenceRule || "");
        dom.todoEditor.classList.remove("hidden");
        dom.todoEditorTitle.focus();
      },
      readEditor: () => ({
        title: dom.todoEditorTitle.value,
        details: dom.todoEditorDetails.value,
        dueAt: dom.todoEditorDueAt.value,
        dueDate: dom.todoEditorDueDate.value,
        priority: dom.todoEditorPriority.value,
        remindBeforeMinutes: dom.todoEditorRemindBefore.value,
        recurrenceRule: dom.todoEditorRecurrence.value,
      }),
      clearEditor: () => dom.todoEditor?.classList.add("hidden"),
    },
    feedback: {
      warn: (...args) => console.warn(...args),
      notify: message => toast(message),
    },
  });
  todoController.bind();
}

function bindModelTransformController() {
  modelTransformController = MODEL_TRANSFORM_CONTROLLER.createModelTransformController({
    isDeveloper: () => DEV_MODE,
    storage: {
      read: key => window.localStorage.getItem(key),
      write: (key, value) => window.localStorage.setItem(key, value),
    },
    storageKey: "amadeus-dev-model-transforms-v1",
    settings: {
      get: () => settings,
      replace: nextSettings => { settings = nextSettings; },
    },
    getSelectedModelId: () => dom.live2dModelInput?.value || "",
    getActiveModelId: () => sceneSnapshot().activeProfile?.id || FALLBACK_MODEL_PROFILE.id,
    fit: () => fitModel(),
    notify: message => toast(message),
    view: {
      modelInput: dom.live2dModelInput,
      scaleInput: dom.modelScaleInput,
      offsetXInput: dom.modelOffsetXInput,
      offsetYInput: dom.modelOffsetYInput,
      scaleValue: dom.modelScaleValue,
      resetButton: dom.resetModelTransformBtn,
    },
    onModelSelectionChanged: () => updateLive2DModelHint(),
  });
  modelTransformController.bind();
}

function bindSalieriPanel() {
  salieriController = SALIERI_PANEL.createSalieriPanelController({
    elements: {
      toggle: dom.salieriWindowToggle,
      window: dom.salieriWindow,
      close: dom.salieriCloseBtn,
      unlockForm: dom.salieriUnlockForm,
      codeInput: dom.salieriCodeInput,
      transcriptWrap: dom.salieriTranscriptWrap,
      transcript: dom.salieriTranscript,
      status: dom.salieriStatus,
      refresh: dom.salieriRefreshBtn,
      path: dom.salieriPath,
      message: dom.salieriMessage,
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    view: {
      isMemoryPageVisible: () => !dom.settingsModal?.classList.contains("hidden")
        && dom.tabPages?.some(page => (
          page.dataset.page === "memory" && page.classList.contains("active")
        )),
      setOpen: open => {
        dom.salieriWindow?.classList.toggle("hidden", !open);
        dom.salieriWindowToggle?.setAttribute("aria-expanded", String(open));
      },
    },
  });
  salieriController.bind();
}

function bindVoiceDiagnostics() {
  voiceDiagnosticsController = VOICE_DIAGNOSTICS.createVoiceDiagnosticsController({
    document,
    isDeveloper: () => DEV_MODE,
    elements: {
      developerPanel: dom.developerPanel,
      voiceStatus: dom.devVoiceStatus,
      replyZh: dom.devReplyZh,
      spokenJa: dom.devSpokenJa,
      speakingJa: dom.devSpeakingJa,
      diagnostic: dom.devVoiceDiagnostic,
      stopDiagnostic: dom.devVoiceStopDiagnostic,
      events: dom.devVoiceEvents,
    },
    settings: {
      get: () => ({ ttsProvider: settings.ttsProvider || DEFAULT_SETTINGS.ttsProvider }),
    },
    speech: {
      getState: () => speechPlayback.state,
      getCurrentIndex: () => speechPlayback.currentSpeechIndex,
      isSpeaking: now => isSpeaking(now),
    },
    clock: { now: () => Date.now() },
  });
  voiceDiagnosticsController.bind();
}

function bindPersonaController() {
  personaController = PERSONA_CONTROLLER.createPersonaController({
    document,
    isDeveloper: () => DEV_MODE,
    elements: {
      reloadDev: dom.reloadPersonaBtn,
      reloadSettings: dom.reloadSettingsPersonaBtn,
      devSave: dom.savePersonaBtn,
      settingsSave: dom.saveSettingsPersonaBtn,
      devOverride: dom.devPersonaOverride,
      settingsOverride: dom.settingsPersonaOverride,
      devStatus: dom.devPersonaStatus,
      settingsStatus: dom.settingsPersonaStatus,
      summary: dom.devPersonaSummary,
      generatedSkill: dom.devGeneratedSkill,
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    view: {
      isSettingsVisible: () => !dom.settingsModal?.classList.contains("hidden"),
    },
    feedback: { notify: message => toast(message) },
  });
  personaController.bind();
}

function bindRelationshipController() {
  relationshipController = RELATIONSHIP_CONTROLLER.createRelationshipController({
    document,
    isDeveloper: () => DEV_MODE,
    elements: {
      refresh: dom.refreshRelationshipBtn,
      scopeInput: dom.devRelationshipScopeInput,
      scopeStatus: dom.devRelationshipScopeStatus,
      stage: dom.devRelationshipStage,
      radar: dom.devRelationshipRadar,
      radarValue: dom.devRelationshipRadarValue,
      radarDots: dom.devRelationshipRadarDots,
      nextTitle: dom.devRelationshipNextTitle,
      readiness: dom.devRelationshipReadiness,
      readinessValue: dom.devRelationshipReadinessValue,
      reactionProfile: dom.devRelationshipReactionProfile,
      stagePath: dom.devRelationshipStagePath,
      meters: dom.devRelationshipMeters,
      requirements: dom.devRelationshipRequirements,
      explanation: dom.devRelationshipExplanation,
      detail: dom.devRelationshipDetail,
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    feedback: { warn: (...args) => console.warn(...args) },
  });
  relationshipController.bind();
}

function bindExpressionController() {
  expressionController = EXPRESSION_CONTROLLER.createExpressionController({
    catalog: {
      targets: emotionTargets,
      semantics: EMOTION_SEMANTICS,
      gestureCycles: EMOTION_GESTURE_CYCLES,
      overlayStrength: EMOTION_OVERLAY_STRENGTH,
      cognitiveBaseDetailWeight: COGNITIVE_BASE_DETAIL_WEIGHT,
      defaults: expressionDefaultsFor,
      timings: emotionTimingFor,
    },
    clock: { now: () => performance.now() },
    ports: {
      isDeveloper: () => DEV_MODE,
      getDeveloperOverride: () => developerEmotionController?.getOverride?.() || null,
      getDeveloperManualBlush: () => developerEmotionController?.getManualBlush?.() ?? null,
      clearDeveloperManualBlush: () => developerEmotionController?.clearManualBlush?.(),
      getSpeechState: () => speechPlayback.state,
      isPipelineActive: () => speechPlayback.pipelineActive,
      isSpeaking: now => speechPlayback.isSpeaking(now),
      getPersistentColdShoulder: () => persistentColdShoulder,
      getPointer: () => frameSnapshot().pointer,
      getHeadTurnProfile: () => activeHeadTurnProfile(),
      normalizeGesture: value => normalizeGesture(value),
      getAnnotatedExpressionStyleEmotion: () => annotatedExpressionStyleEmotion,
      setAnnotatedExpressionStyleEmotion: (emotion, detail = {}) => {
        annotatedExpressionStyleEmotion = emotion;
        const requestedVariantUnit = Number(detail.annotatedVariantUnit);
        annotatedExpressionVariantUnit = Number.isFinite(requestedVariantUnit)
          ? clamp(requestedVariantUnit, 0, 0.999999)
          : Math.random();
        annotatedExpressionMirrorSide = Math.random() < 0.5 ? -1 : 1;
      },
      getMessages: () => state.messages,
      requestContextEmotion: payload => storageScopedFetch("/api/emotion-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(response => response.json().then(data => {
        if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
        return data;
      })),
      visualIntensity: rawIntensity => visualEmotionIntensityWeight(rawIntensity),
      applyNativeExpression: emotion => nativeMotionController?.applyExpression(emotion),
      applyNativeMotion: (emotion, gesture) => nativeMotionController?.applyMotion(emotion, gesture),
      pulseInteraction: id => pulseInteraction(id),
      updateModelStateLabel: () => updateModelStateLabel(),
      updateDeveloperEmotionPanel: () => developerEmotionController?.renderPanel?.(),
      setGlitch: () => {
        document.body.classList.add("glitch");
        window.setTimeout(() => document.body.classList.remove("glitch"), 560);
      },
      warn: (...args) => console.warn(...args),
    },
  });
}

function expressionSnapshot() {
  return expressionController?.getState() || {
    currentEmotion: "neutral",
    stableEmotion: "neutral",
    emotionRecoveryTarget: "neutral",
    cognitiveOverlayBaseEmotion: "neutral",
    cognitiveOverlayBaseDetail: null,
    emotionEnteredAt: performance.now(),
    currentEmotionHoldMs: 0,
    currentEmotionFadeMs: 1200,
    emotionSpeechProtectedUntil: 0,
    activeGesture: "",
    gestureUntil: 0,
    gestureEnteredAt: performance.now(),
    lookAwayDirection: 1,
    furiousTurnDirection: 0,
    furiousTurnTarget: 0,
    furiousTurnVelocity: 0,
    currentEmotionWeight: 1,
    currentExpressionDetail: {
      blushIntensity: 0,
      eyeContact: 0.65,
      gazeX: 0,
      gazeY: 0,
      tension: 0,
      outburst: false,
    },
    nextContextEmotionAt: 0,
    contextEmotionRequestInFlight: false,
  };
}

function bindApiVerification() {
  apiVerificationController = API_VERIFICATION.createApiVerificationController({
    elements: {
      settingsStatus: dom.settingsStatus,
      apiEndpointInput: dom.apiEndpointInput,
      apiModelInput: dom.apiModelInput,
      cognitiveRepairModelInput: dom.cognitiveRepairModelInput,
      apiKeyInput: dom.apiKeyInput,
      visionEnabledInput: dom.visionEnabledInput,
      visionApiKeyInput: dom.visionApiKeyInput,
      apiNetworkModeInput: dom.apiNetworkModeInput,
      apiProxyUrlInput: dom.apiProxyUrlInput,
      responseFormatInput: dom.responseFormatInput,
      saveApiButton: dom.saveApiBtn,
      testApiButton: dom.testApiBtn,
      apiTestStatus: dom.apiTestStatus,
    },
    getSettings: () => settings,
    saveSettings,
    request: fetch,
    summarizeError: connectionErrorSummary,
    notify: toast,
    savedApiKeyMask: SAVED_API_KEY_MASK,
    onTestSettled: label => {
      connectionStatus = label;
      updateModelStateLabel();
    },
  });
  apiVerificationController.bind();
}

function bindModelProfileController() {
  modelProfileController = MODEL_PROFILE_CONTROLLER.createModelProfileController({
    elements: {
      status: dom.modelProfileStatus,
      active: dom.modelProfileActive,
      select: dom.modelProfileSelect,
      name: dom.modelProfileNameInput,
      endpoint: dom.apiEndpointInput,
      model: dom.apiModelInput,
      cognitive: dom.cognitiveRepairModelInput,
      apiKey: dom.apiKeyInput,
      networkMode: dom.apiNetworkModeInput,
      proxy: dom.apiProxyUrlInput,
      responseFormat: dom.responseFormatInput,
      overwrite: dom.modelProfileOverwriteInput,
      clearKey: dom.modelProfileClearKeyInput,
      save: dom.saveModelProfileBtn,
      activate: dom.activateModelProfileBtn,
      remove: dom.deleteModelProfileBtn,
    },
    request: (...args) => fetch(...args),
    getDraft: () => settings,
    savedApiKeyMask: SAVED_API_KEY_MASK,
    onActivated: async payload => {
      const previousSettings = { ...settings };
      await loadSettings();
      apiVerificationController?.onSettingsSaved(payload?.settings || settings, previousSettings);
      fillSettingsForm();
      applySettingsToUi();
    },
    notify: toast,
    confirm: message => window.confirm(message),
  });
  modelProfileController.bind();
}

function bindSceneController() {
  sceneController = SCENE_CONTROLLER.createSceneController({
    registryUrl: MODEL_REGISTRY_URL,
    fallbackProfile: FALLBACK_MODEL_PROFILE,
    renderResolution: RENDER_RESOLUTION,
    getPixi: () => window.PIXI,
    getWindow: () => window,
    request: (...args) => fetch(...args),
    runtime: {
      isDeveloper: () => DEV_MODE,
      getPreviewModelId: () => MODEL_PREVIEW_ID,
      getSettings: () => settings,
      setLive2DModelId: modelId => { settings.live2dModelId = modelId; },
    },
    stage: {
      canvas: dom.canvas,
      onStageCreated: () => drawPixiBackground(),
      onProfileChanged: snapshot => {
        const profile = snapshot.activeProfile;
        touchInteractionController?.setModelProfile(profile);
        document.body.dataset.live2dModel = profile.id;
        renderLive2DModelOptions();
        if (DEV_MODE) {
          developerModelEditorController?.render?.();
          touchInteractionController?.renderRegions();
        }
      },
      onPointerDown: (...args) => touchInteractionController?.handleModelTouch(...args),
      onTicker: (...args) => updateLive2D(...args),
      drawBackground: () => drawPixiBackground(),
      exposeDebug: snapshot => {
        window.__AMADEUS_MODEL_DEBUG__ = {
          profile: snapshot.activeProfile,
          model: snapshot.model,
          coreModel: snapshot.coreModel,
        };
      },
      warn: (...args) => console.warn(...args),
    },
    model: {
      installGarmentRig: (modelInstance, config) => AmadeusGarmentRig.install(modelInstance, config),
      installPostMotionRetargeting: () => installPostMotionRetargeting(),
    },
    fit: {
      fitPortrait: (...args) => window.AmadeusPortraitFraming.fitPortrait(...args),
      getTransform: modelId => modelTransformFor(modelId),
      getLayoutMode: () => settings.layoutMode,
    },
  });
}

function bindDeveloperModelEditorController() {
  // 模型编辑器独占所选 profile、JSON 草稿和保存/重载代次；scene controller 仍独占注册表与生效场景。
  developerModelEditorController = DEVELOPER_MODEL_EDITOR.createDeveloperModelEditorController({
    dom,
    document,
    window,
    isDeveloper: () => DEV_MODE,
    getSceneSnapshot: sceneSnapshot,
    getPreviewModelId: () => MODEL_PREVIEW_ID,
    loadRegistry: () => loadLive2DModelRegistry(),
    request: (...args) => fetch(...args),
    notify: toast,
    clamp,
    toNumber,
  });
  developerModelEditorController.bind();
}

function bindFrameController() {
  // 帧控制器只保留随时间变化的视线/眨眼状态；模型参数写入仍在页面渲染段完成，
  // 这样触摸、聊天和语音可以共享同一快照，却不会复制一份逐帧状态。
  frameController = FRAME_CONTROLLER.createFrameController({
    clock: { now: () => performance.now() },
  });
}

function bindBackgroundRenderer() {
  // 背景控制器只接受生效 settings 和场景层 port；设置草稿仍由页面 settings owner 保存。
  backgroundRenderer = BACKGROUND_RENDERER.createBackgroundRenderer({
    bootBackgroundUrl: BOOT_BACKGROUND_URL,
    getPixi: () => window.PIXI,
    getWindow: () => window,
    getApp: () => sceneSnapshot().app,
    getLayer: () => sceneSnapshot().backgroundLayer,
    isKnownPreset: preset => BACKGROUND_PRESETS.has(preset),
    getPresetUrl: preset => BACKGROUND_IMAGE_PRESETS.get(preset) || "",
    getView: () => backgroundView(settings),
    layout: (...args) => window.AmadeusBackgroundEditor.layout(...args),
  });
}

function bindMouthParameterRenderer() {
  // 语音只提供采样，口型 renderer 自己保存两个参数的平滑值，避免再次复制到页面全局。
  mouthParameterRenderer = MOUTH_PARAMETER_RENDERER.createMouthParameterRenderer({
    getProfile: () => sceneSnapshot().activeProfile?.lipSync || {},
    writeParameter: (id, value) => setParam(id, value),
  });
}

function bindParameterRenderer() {
  // 通用参数平滑值与瞳孔/脸红运行时值只由 renderer 持有；页面逐帧段提交目标，不复制可变 map。
  parameterRenderer = PARAMETER_RENDERER.createParameterRenderer({
    writeParameter: (id, value) => setParam(id, value),
    resolveMappedParameterIds: (channel, fallbackId) => mappedParameterIds(channel, fallbackId),
  });
}

function bindDeveloperEmotionController() {
  // 开发者情绪控制器独占面板草稿、手动锁和释放后的刷新预约；页面只提供表达式与渲染窄 port。
  developerEmotionController = DEVELOPER_EMOTION_CONTROLLER.createDeveloperEmotionController({
    dom,
    isDeveloper: () => DEV_MODE,
    normalizeEmotion,
    normalizeGesture,
    expressionDefaultsFor,
    getExpressionSnapshot: expressionSnapshot,
    getRuntimeEmotionTarget: emotion => runtimeEmotionTarget(emotion),
    getEmotionSemantics: emotion => emotionSemanticsFor(emotion),
    getVisualIntensity: raw => visualEmotionIntensityWeight(raw),
    getAnnotatedPlan: emotion => activeAnnotatedExpressionPlan(emotion),
    setEmotion: (...args) => setEmotion(...args),
    scheduleContextRefresh: delay => expressionController?.scheduleContextRefresh?.(delay),
    notify: message => toast(message),
  });
  developerEmotionController.bind();
}

function bindParameterLabController() {
  // 参数实验台独占覆盖、标定与监督样本草稿；渲染参数和口型只通过窄 port 复用页面 owner。
  developerParameterLabController = PARAMETER_LAB.createParameterLabController({
    dom,
    document,
    window,
    fetch: (...args) => fetch(...args),
    storageScopedFetch: (...args) => storageScopedFetch(...args),
    getSettings: () => settings,
    sceneSnapshot,
    setParam,
    mappedParameterIds,
    effectiveVisualEmotion,
    expressionSnapshot,
    isDeveloper: () => DEV_MODE,
    toast,
    resetRenderParameters: () => {
      parameterRenderer?.resetParameters();
      mouthParameterRenderer?.reset();
    },
    getRuntimeAuthorBlushOpacity: () => parameterRenderer?.getState()?.runtimeAuthorBlushOpacityValue ?? 1,
    getRuntimePupilScaleValues: () => parameterRenderer?.getState()?.runtimePupilScaleValues || { left: 1, right: 1 },
    authorFaceLayerIds: AUTHOR_FACE_LAYER_IDS,
    trainingLayerByEmotion: EXPRESSION_TRAINING_LAYER_BY_EMOTION,
    trainingLayerWarnings: EXPRESSION_TRAINING_LAYER_WARNINGS,
    trainingLayerChannels: EXPRESSION_TRAINING_LAYER_CHANNELS,
    trainingEmotionLabels: EXPRESSION_TRAINING_EMOTION_LABELS,
    trainingStrengths: EXPRESSION_TRAINING_STRENGTHS,
    trainingParameterGroups: EXPRESSION_TRAINING_PARAMETER_GROUPS,
    trainingUnboundIds: EXPRESSION_TRAINING_UNBOUND_IDS,
    clamp,
    toNumber,
  });
  developerParameterLabController.bind();
}

function bindVoiceReferenceController() {
  // 语音参考 controller 独占目录/审阅草稿/音频 URL；参数实验台和语音播放只通过窄 port 协调。
  voiceReferenceController = VOICE_REFERENCE_CONTROLLER.createDeveloperVoiceReferenceController({
    dom,
    document,
    window,
    isDeveloper: () => DEV_MODE,
    storageScopedFetch: (...args) => storageScopedFetch(...args),
    notify: toast,
    onExpressionTrainingSelected: () => developerParameterLabController?.renderTrainingGallery(),
    onExpressionTrainingLoaded: () => developerParameterLabController?.loadTraining(),
  });
  voiceReferenceController.bind();
}

async function prepareStartupGreetingTts(greeting, { signal, generation = 0 } = {}) {
  if (!greeting || !shouldSynchronizeTextWithVoice()) return null;
  const provider = String(settings.ttsProvider || "").trim().toLowerCase();
  if (provider !== "gpt-sovits") return null;
  recordVoiceDiagnostic("startup-warmup", "启动动画期间正在加载语音模型并预合成开机问候");
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
  ttsRequest.requestId = `startup-${Date.now()}-${generation}`;
  ttsRequest.utteranceId = "startup-greeting";
  const payload = await requestPreparedTts(ttsRequest, null, signal);
  return {
    key: preparedTtsKey(ttsRequest),
    payload,
  };
}

function bindChatController() {
  chatController = CHAT_CONTROLLER.createChatController({
    clock: {
      now: () => performance.now(),
      delay: milliseconds => delay(milliseconds),
      createAbortController: () => new AbortController(),
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    playback: { pipelineActive: () => speechPlayback.pipelineActive },
    feedback: {
      syncSubmitButton: () => syncChatSubmitButton(),
      stopThinkingProgress: (requestId, generation) => thinkingIndicatorController?.stopProgress?.(requestId),
      hideThinking: generation => thinkingIndicatorController?.hide?.(generation),
      updateThinkingStage: (...args) => thinkingIndicatorController?.updateStage?.(...args),
      updateConnectionStatus: data => updateConnectionStatus(data),
      recoverBackend: () => requestNativeBackendRecovery(),
      notifyBackendCancellation: requestId => notifyBackendAnswerCancellation(requestId),
      interruptSpeech: () => interruptCurrentSpeech(),
      toast: message => toast(message),
      onStart: ({ generation, requestId }) => {
        syncChatSubmitButton();
        setEmotion("thinking", "nod", { restart: true });
        thinkingIndicatorController?.show?.(generation, "context", "正在整理当前对话与记忆");
        thinkingIndicatorController?.startProgress?.(requestId, generation);
      },
      onSettled: stateSnapshot => {
        thinkingIndicatorController?.stopProgress?.(stateSnapshot.requestId);
        if (!speechPlayback.pipelineActive) thinkingIndicatorController?.hide?.(stateSnapshot.generation);
        syncChatSubmitButton();
      },
    },
  });
}

function bindThinkingIndicatorController() {
  // 思考气泡和 developer progress timer 只由该 controller 持有；聊天/语音只发窄 feedback 事件。
  thinkingIndicatorController = THINKING_INDICATOR.createThinkingIndicatorController({
    dom,
    document,
    window,
    isDeveloper: () => DEV_MODE,
    getGeneration: () => chatController?.getGeneration?.() || 0,
    request: (...args) => fetch(...args),
    scrollToLatest: () => messageView?.scrollToLatest(),
  });
}

function bindProactiveControllers() {
  reminderPollingController = REMINDER_POLLING.createReminderPollingController({
    clock: {
      now: () => performance.now(),
      setTimeout: (callback, delay) => window.setTimeout(callback, delay),
      clearTimeout: timer => window.clearTimeout(timer),
      setInterval: (callback, delay) => window.setInterval(callback, delay),
      clearInterval: timer => window.clearInterval(timer),
    },
    session: {
      isBootActive: () => bootSequenceActive,
      isChatRequestInFlight: () => chatController?.isRequestInFlight() || false,
      isProactiveRequestInFlight: () => proactiveController?.isRequestInFlight(),
      isTouchRequestInFlight: () => touchInteractionController?.isRequestInFlight(),
      getSpeechState: () => speechPlayback.state,
      isSpeaking: now => isSpeaking(now),
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    delivery: {
      applyAssistant: (...args) => applyAssistant(...args),
      applyProactiveControl: (...args) => applyProactiveControl(...args),
      refreshTodos: () => loadTodos(),
    },
    feedback: { warn: (...args) => console.warn(...args) },
  });

  proactiveController = PROACTIVE_CONTROLLER.createProactiveController({
    logic: PROACTIVE_LOGIC,
    frequencyProfiles: PROACTIVE_FREQUENCY_PROFILES,
    getSettings: () => settings,
    clock: {
      now: () => performance.now(),
      random: () => Math.random(),
      localHour: () => new Date().getHours(),
      createAbortController: () => new AbortController(),
    },
    session: {
      getInitialContext: () => ({
        waitingForUser: state.waitingForUser,
        proactiveControl: state.proactiveControl,
      }),
      setWaitingForUser: value => { state.waitingForUser = Boolean(value); },
      setProactiveControl: value => { state.proactiveControl = { ...value }; },
      persist: () => persist(),
      getConversationIdentity: () => ({
        conversationId: state.conversationId,
        contextEpoch: state.contextEpoch,
      }),
      getMessageCount: () => state.messages.length,
      getCleanHistory: () => cleanHistoryForModel(),
      getRecentProactiveTexts: limit => state.messages
        .filter(message => message?.role === "assistant" && message.meta?.kind === "proactive")
        .slice(-Math.max(0, Number(limit) || 0))
        .map(message => String(message.content || ""))
        .filter(Boolean),
      getMessages: () => state.messages,
      isChatRequestInFlight: () => chatController?.isRequestInFlight() || false,
      hasTouchPending: () => Boolean(touchInteractionController?.hasPendingWork?.()),
    },
    transport: { request: (...args) => storageScopedFetch(...args) },
    speech: {
      shouldSynchronizeTextWithVoice: () => shouldSynchronizeTextWithVoice(),
      prepareStartupTts: (...args) => prepareStartupGreetingTts(...args),
      onStartupAudioReady: prepared => recordVoiceDiagnostic("startup-audio-ready", "开机问候音频已在启动动画期间合成完成", {
        audioBytes: Math.floor(String(prepared?.payload?.audioBase64 || "").length * 0.75),
        alignmentId: String(prepared?.payload?.alignmentId || ""),
      }),
      onStartupTtsFailure: (error, greeting) => {
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
      },
      getState: () => speechPlayback.state,
      isSpeaking: now => isSpeaking(now),
      interruptCurrentSpeech: () => interruptCurrentSpeech(),
    },
    scene: {
      isDeveloper: () => DEV_MODE,
      isBootActive: () => bootSequenceActive,
      hasModel: () => Boolean(sceneSnapshot().model),
      isSettingsModalOpen: () => Boolean(dom.settingsModal && !dom.settingsModal.classList.contains("hidden")),
      isBookmarkPanelOpen: () => Boolean(dom.bookmarkPanel && !dom.bookmarkPanel.classList.contains("hidden")),
      hasUserInput: () => Boolean(dom.userInput?.value.trim()),
      getEmotionState: () => {
        const expression = expressionSnapshot();
        return { currentEmotion: expression.currentEmotion, weight: expression.currentEmotionWeight };
      },
      emotionLifecycleWeight: now => emotionLifecycleWeight(now),
      getCurrentEmotion: () => expressionSnapshot().currentEmotion,
      setEmotion: (...args) => setEmotion(...args),
      schedulePositiveAmbientMood: at => {
        nextPositiveAmbientMoodAt = at + 180000 + Math.random() * 300000;
      },
    },
    delivery: { applyAssistant: (...args) => applyAssistant(...args) },
    reminder: { isInFlight: () => reminderPollingController?.isInFlight() },
    feedback: {
      warn: (...args) => console.warn(...args),
      toast: message => toast(message),
      updateConnectionStatus: data => updateConnectionStatus(data),
    },
  });
}

function bindTouchInteraction() {
  touchInteractionController = TOUCH_INTERACTION.createTouchInteractionController({
    clock: {
      now: () => performance.now(),
      setTimeout: (callback, delay) => window.setTimeout(callback, delay),
      clearTimeout: timer => window.clearTimeout(timer),
      createAbortController: () => new AbortController(),
    },
    config: {
      request: (url, init) => fetch(url, init),
    },
    scene: {
      getModel: () => sceneSnapshot().model,
      getModelId: () => sceneSnapshot().activeProfile?.id || "",
      isDeveloper: () => DEV_MODE,
      getEmotion: () => ({
        emotion: expressionSnapshot().currentEmotion,
        weight: expressionSnapshot().currentEmotionWeight,
        holdMs: expressionSnapshot().currentEmotionHoldMs,
        fadeMs: expressionSnapshot().currentEmotionFadeMs,
      }),
      getRelationshipTone: () => relationshipTone,
      preserveColdShoulderPose: () => preservePersistentFuriousPose(),
      applyBodyTap: descriptor => setEmotion(descriptor.emotion, descriptor.gesture, {
        emotionIntensity: descriptor.emotionIntensity,
        restart: descriptor.restart,
        smoothEmotionTransition: descriptor.smoothEmotionTransition,
        holdMs: descriptor.holdMs,
      }),
      applyImmediateTouchFeedback: (descriptor, zone) => setEmotion(descriptor.emotion, descriptor.gesture, {
        emotionIntensity: descriptor.emotionIntensity,
        emotionDelta: descriptor.delta,
        blushIntensity: descriptor.blushIntensity,
        tension: descriptor.tension,
        annotatedVariantUnit: descriptor.annotatedVariantUnit,
        accumulateExpression: descriptor.accumulateExpression ?? true,
        smoothEmotionTransition: descriptor.smoothEmotionTransition ?? true,
        restart: descriptor.restart ?? (zone === "head" && descriptor.gesture === "smile"),
        holdMs: descriptor.holdMs,
        fadeMs: descriptor.fadeMs,
      }),
    },
    session: {
      getConversationIdentity: () => ({
        conversationId: state.conversationId,
        contextEpoch: state.contextEpoch,
      }),
      getHistory: () => cleanHistoryForModel().slice(-4),
      createRequestId: () => createClientMessageId(),
      noteTouchPresence: () => proactiveController?.noteTouchPresence(),
      isPipelineBusy: now => {
        const chatSpeechBusy = ["chat", "proactive"].includes(speechPlayback.state.kind)
          && (speechPlayback.state.pending || isSpeaking(now));
        return chatController?.isRequestInFlight() || proactiveController?.isRequestInFlight() || speechPlayback.pipelineActive || chatSpeechBusy;
      },
      isTouchSpeechActive: now => speechPlayback.state.kind === "touch"
        && (speechPlayback.state.pending || isSpeaking(now)),
      applyAssistant: (...args) => applyAssistant(...args),
      markTouchInterrupted: () => markActiveTouchInterrupted(),
    },
    transport: {
      request: (...args) => storageScopedFetch(...args),
      interactionUrl: "/api/interaction",
    },
    feedback: {
      showThinking: (...args) => thinkingIndicatorController?.show?.(...args),
      hideThinking: (...args) => thinkingIndicatorController?.hide?.(...args),
      renderHit: (...args) => renderDeveloperTouchHit(...args),
      renderRegions: (...args) => renderDeveloperTouchRegions(...args),
      renderOverlay: payload => updateDeveloperTouchOverlay(payload),
      renderDebug: data => renderDeveloperTouchDebug(data),
      reloadRelationship: () => loadDeveloperRelationship(),
      warn: (...args) => console.warn(...args),
    },
  });
}


function bindEvents() {
  window.addEventListener("resize", () => {
    fitModel();
    backgroundEditor?.refresh();
    applyChatWindowHeight(state.chatWindowHeight, { persist: false });
  });
  window.addEventListener("pointermove", event => {
    frameController?.setPointer({
      x: (event.clientX / Math.max(1, window.innerWidth) - 0.5) * 2,
      y: (event.clientY / Math.max(1, window.innerHeight) - 0.5) * 2,
    });
  });
  bindWindowDragRegions();
  window.AmadeusWindowResize.bind({
    getMode: () => settings.layoutMode,
    onFinish: persistResizedWindow,
    onError: error => toast(`窗口缩放失败：${error.message}`),
  });

  dom.settingsBtn.addEventListener("click", openSettings);
  dom.chatHistorySearchBtn?.addEventListener("click", openChatHistorySearch);
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
      if (button.dataset.tab === "persona") personaController?.load();
    });
  });
  dom.saveDisplayBtn.addEventListener("click", saveDisplaySettings);
  dom.saveInteractionBtn?.addEventListener("click", saveInteractionSettings);
  dom.saveVoiceBtn.addEventListener("click", saveVoiceSettings);
  dom.savePermissionsBtn.addEventListener("click", savePermissionsSettings);
  dom.testWebSearchBtn?.addEventListener("click", testWebSearchConnection);
  dom.proactiveInteractionsEnabledInput?.addEventListener("change", updateProactiveTopicControls);
  dom.proactiveScreenObservationEnabledInput?.addEventListener("change", updateProactiveTopicControls);
  dom.proactiveTopicInputs?.forEach(input => input.addEventListener("change", updateProactiveTopicControls));
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
  dom.devTestVoiceBtn?.addEventListener("click", testDeveloperVoice);
  dom.devRestartVoiceBtn?.addEventListener("click", restartDeveloperVoice);
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
  dom.backgroundPresetInput?.addEventListener("change", previewBackgroundSelection);
  dom.chooseBackgroundBtn?.addEventListener("click", chooseCustomBackground);
  backgroundEditor = window.AmadeusBackgroundEditor.mount({
    frame: document.getElementById("backgroundPreview"), image: document.getElementById("backgroundPreviewImage"),
    guides: { narrow: document.getElementById("backgroundNarrowGuide"), wide: document.getElementById("backgroundWideGuide"), current: document.getElementById("backgroundCurrentGuide") },
    narrowLabel: document.getElementById("backgroundNarrowLabel"), wideLabel: document.getElementById("backgroundWideLabel"),
    zoomInput: document.getElementById("backgroundZoomInput"), zoomValue: document.getElementById("backgroundZoomValue"),
    resetButton: document.getElementById("resetBackgroundViewBtn"), status: document.getElementById("backgroundPreviewStatus"),
    getViewport: () => {
      const ranges = Object.values(windowAspectRatios);
      const minRatio = Math.min(...ranges.map(range => range[0]));
      const maxRatio = Math.max(...ranges.map(range => range[1]));
      return { width: Number(dom.windowWidthInput?.value) || window.innerWidth,
        height: Number(dom.windowHeightInput?.value) || window.innerHeight, minRatio, maxRatio };
    },
    onChange: view => {
      if (!backgroundDraft) return;
      const key = window.AmadeusBackgroundEditor.viewKey(backgroundDraft.backgroundPreset, backgroundDraft.customBackgroundPath);
      backgroundDraft.backgroundViews = { ...backgroundDraft.backgroundViews, [key]: view };
    },
  });
  for (const input of [dom.layoutModeInput, dom.windowWidthInput, dom.windowHeightInput]) {
    input?.addEventListener("input", () => backgroundEditor.refresh());
    input?.addEventListener("change", () => backgroundEditor.refresh());
  }

  window.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (!dom.bookmarkEditor?.classList.contains("hidden")) {
      bookmarksController?.closeEditor();
      return;
    }
    if (!dom.conversationPicker?.classList.contains("hidden")) {
      conversationPickerController?.close();
      return;
    }
    if (!dom.bookmarkPanel?.classList.contains("hidden")) {
      bookmarksController?.close();
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

async function persistResizedWindow(result, mode) {
  const update = { windowWidth: result.width, windowHeight: result.height,
    [mode + "WindowWidth"]: result.width, [mode + "WindowHeight"]: result.height };
  Object.assign(settings, update);
  if (document.activeElement !== dom.windowWidthInput) dom.windowWidthInput.value = result.width;
  if (document.activeElement !== dom.windowHeightInput) dom.windowHeightInput.value = result.height;
  const response = await fetch("/api/settings", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: update }) });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || "窗口大小保存失败");
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
    if (scroll) messageView?.scrollToLatest();
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
  await touchInteractionController.loadConfig();
  await loadSettings();
  if (modelProfileController) {
    await modelProfileController.refresh().catch(error => {
      console.warn("model profile refresh failed:", error);
    });
  }
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
    personaController?.load();
    relationshipController?.load();
    switchDeveloperPage(MODEL_PREVIEW_ID ? "model" : "voice");
  }
  await loadLive2DModelRegistry();
  await loadMemory();
  await hydrateConversationHistory();
  await loadTodos();
  await bookmarksController?.load();
  if (!settings.releaseMode) {
    await salieriController?.refresh({ quiet: true });
    salieriController?.startPolling();
  } else {
    salieriController?.stopPolling();
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
    await sceneController.start();
    setExpressionFix();
    applyDeveloperEmotionPreview();
    await developerParameterLabController?.loadParameterDisplayInfo();
    developerParameterLabController?.refreshParameters();
    dom.modelState.textContent = runtimeModeStateLabel("LIVE2D ONLINE");
    toast(`${sceneSnapshot().activeProfile.label} / Live2D online`);
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
  const scene = sceneSnapshot();
  if (!overlay) {
    if (scene.model) scene.model.alpha = 1;
    bootSequenceActive = false;
    return;
  }
  if (SKIP_BOOT) {
    await modelReadyPromise;
    sceneSnapshot().model.alpha = 1;
    overlay.hidden = true;
    bootSequenceActive = false;
    setEmotion("neutral", "idle", { restart: true, holdMs: 0, fadeMs: 0 });
    return;
  }
  if (reducedMotion) {
    overlay.classList.add("phase-logo");
    await modelReadyPromise;
    sceneSnapshot().model.alpha = 1;
    await delay(260);
  } else {
    overlay.classList.add("phase-logo");
    await playBootLogoVideo();
    await delay(360);
    await Promise.race([readinessPromise, delay(5200)]);
    await modelReadyPromise;
    overlay.classList.add("phase-reveal");
    await delay(180);
    sceneSnapshot().model.alpha = 1;
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

function scheduleStartupInteraction() {
  return proactiveController?.scheduleStartupInteraction();
}

async function prepareStartupGreeting(voiceReadyPromise = Promise.resolve(null)) {
  return proactiveController?.prepareStartupGreeting(voiceReadyPromise) || null;
}

function normalizeProactiveControl(value) {
  return PROACTIVE_CONTROLLER.normalizeProactiveControl(value);
}

function proactiveOutreachPaused() {
  return Boolean(proactiveController?.isPaused());
}

function applyProactiveControl(value, options) {
  return proactiveController?.applyProactiveControl(value, options);
}

function scheduleNextProactiveInteraction(now, unansweredCount) {
  return proactiveController?.scheduleNextProactiveInteraction?.(now, unansweredCount);
}

async function updateProactiveInteraction(now) {
  return proactiveController?.update(now);
}

function resetProactiveSequence(now) {
  return proactiveController?.resetProactiveSequence(now);
}

function noteUserActivity() {
  return proactiveController?.noteUserActivity();
}

function noteTouchPresence() {
  return proactiveController?.noteTouchPresence();
}

function updateProactiveAmbientMood(now) {
  return proactiveController?.updateAmbientMood(now);
}

function clearProactiveReturnContext() {
  return proactiveController?.clearReturnContext();
}

function getProactiveReturnContext() {
  return proactiveController?.getReturnContext() || "";
}

function clearProactiveReturnContextIf(value) {
  return proactiveController?.clearReturnContextIf(value);
}

function getProactiveSnapshot() {
  return proactiveController?.getState() || {
    phase: "idle",
    unansweredCount: 0,
    nextAmbientMoodAt: 0,
    sequenceId: 0,
    control: normalizeProactiveControl(null),
    waitingForUser: false,
    proactiveInteractionInFlight: false,
  };
}

function markAssistantRequiresUserResponse(source) {
  return proactiveController?.markAssistantRequiresUserResponse(source);
}

function hydrateProactiveConversationContext(data, previousConversationId) {
  const context = data && typeof data === "object" ? data : {};
  return proactiveController?.hydrateConversationContext({
    ...context,
    previousConversationId,
    conversationId: state.conversationId,
  });
}

function proactiveDeliveryStillCurrent(data) {
  return proactiveController?.proactiveDeliveryStillCurrent(data) !== false;
}

function recordProactiveConversationEvent(message, mood = {}) {
  return proactiveController?.recordProactiveConversationEvent(message, mood);
}

function syncPendingProactiveConversationEvents() {
  return proactiveController?.syncPendingProactiveConversationEvents();
}

function clearPendingProactivePersistence() {
  return proactiveController?.clearPendingPersistence();
}

function updatePositiveAmbientMood(now = performance.now()) {
  if (!nextPositiveAmbientMoodAt || now < nextPositiveAmbientMoodAt) return;
  const proactive = getProactiveSnapshot();
  if (proactive.phase !== "idle" || proactive.unansweredCount > 0) {
    nextPositiveAmbientMoodAt = now + 120000;
    return;
  }
  if (bootSequenceActive || chatController?.isRequestInFlight() || proactive.proactiveInteractionInFlight || touchInteractionController.isRequestInFlight() || speechPlayback.state.pending || isSpeaking(now)) {
    nextPositiveAmbientMoodAt = now + 45000;
    return;
  }
  if (expressionSnapshot().currentEmotion !== "neutral" && emotionLifecycleWeight(now) > 0.08) {
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

function preservePersistentFuriousPose() {
  if (!persistentColdShoulder) return false;
  const expression = expressionSnapshot();
  if (expression.currentEmotion !== "furious" || expression.activeGesture !== "look_away") {
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

async function loadSettings() {
  return settingsController?.load();
}

async function loadLive2DModelRegistry() {
  return sceneController?.loadRegistry();
}

function renderLive2DModelOptions() {
  if (!dom.live2dModelInput) return;
  const scene = sceneSnapshot();
  const selected = settings.live2dModelId || scene.activeProfile.id;
  dom.live2dModelInput.replaceChildren(...scene.profiles.map(profile => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.label || profile.id;
    return option;
  }));
  dom.live2dModelInput.value = scene.profiles.some(item => item.id === selected)
    ? selected
    : scene.activeProfile.id;
  updateLive2DModelHint();
}

function updateLive2DModelHint() {
  if (!dom.live2dModelHint) return;
  const scene = sceneSnapshot();
  const selected = scene.profiles.find(item => item.id === dom.live2dModelInput?.value)
    || scene.activeProfile;
  const pending = selected.id !== scene.activeProfile.id ? " 保存后重新加载并切换。" : " 当前正在使用。";
  const interaction = selected.interactionSummary ? ` 交互：${selected.interactionSummary}` : "";
  dom.live2dModelHint.textContent = `${selected.description || selected.label || selected.id}${interaction}${pending}`;
}

function normalizedModelTransform(value) {
  return MODEL_TRANSFORM_CONTROLLER.normalizeTransform(value);
}

function modelTransformFor(modelId = sceneSnapshot().activeProfile?.id) {
  return modelTransformController?.getTransformFor(modelId)
    || MODEL_TRANSFORM_CONTROLLER.normalizeTransform(settings.modelTransforms?.[modelId]);
}

function selectedDisplayModelId() {
  return modelTransformController?.getSelectedModelId()
    || dom.live2dModelInput?.value
    || sceneSnapshot().activeProfile?.id
    || FALLBACK_MODEL_PROFILE.id;
}

function updateModelTransformInputs() {
  return modelTransformController?.syncInputs();
}

function registerBackgroundImages(presets) {
  for (const id of BACKGROUND_IMAGE_PRESETS.keys()) BACKGROUND_PRESETS.delete(id);
  BACKGROUND_IMAGE_PRESETS.clear();
  dom.backgroundPresetInput?.querySelector("optgroup[data-local-backgrounds]")?.remove();
  const group = document.createElement("optgroup");
  group.label = "LAB 背景";
  group.dataset.localBackgrounds = "true";
  for (const item of Array.isArray(presets) ? presets : []) {
    if (!item || !/^local-[a-z0-9-]+$/.test(item.id) || typeof item.url !== "string") continue;
    BACKGROUND_IMAGE_PRESETS.set(item.id, item.url);
    BACKGROUND_PRESETS.add(item.id);
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label || item.id;
    group.appendChild(option);
  }
  if (group.children.length) dom.backgroundPresetInput?.appendChild(group);
}

function newBackgroundDraft() {
  return { backgroundPreset: settings.backgroundPreset, customBackgroundPath: settings.customBackgroundPath,
    backgroundViews: window.AmadeusBackgroundEditor.normalizeViews(settings.backgroundViews) };
}

function backgroundImageUrl(source) {
  return backgroundRenderer?.imageUrl(source) || "";
}

function backgroundView(source) {
  const key = window.AmadeusBackgroundEditor.viewKey(source.backgroundPreset, source.customBackgroundPath);
  return window.AmadeusBackgroundEditor.normalizeView(source.backgroundViews?.[key]);
}

function updateBackgroundControls() {
  const source = backgroundDraft || settings;
  const preset = BACKGROUND_PRESETS.has(source.backgroundPreset) ? source.backgroundPreset : "grid";
  if (dom.backgroundPresetInput) dom.backgroundPresetInput.value = preset;
  if (dom.customBackgroundPathInput) dom.customBackgroundPathInput.value = source.customBackgroundPath || "";
  dom.backgroundFileRow?.classList.toggle("hidden", preset !== "custom");
  if (dom.backgroundHint) {
    dom.backgroundHint.textContent = preset === "custom"
      ? (source.customBackgroundPath ? "在下方预览中调整取景，保存后应用。" : "请先选择一张 PNG、JPG、JPEG 或 WebP 图片。")
      : "拖动白色取景框选择画面；窗口缩放时尽量保留所选位置，并保持铺满。";
  }
  const url = backgroundImageUrl(source);
  dom.backgroundEditorPanel?.classList.toggle("hidden", !url);
  backgroundEditor?.setSource(url, backgroundView(source));
}

function previewBackgroundSelection() {
  backgroundDraft ||= newBackgroundDraft();
  backgroundDraft.backgroundPreset = BACKGROUND_PRESETS.has(dom.backgroundPresetInput?.value)
    ? dom.backgroundPresetInput.value
    : "grid";
  updateBackgroundControls();
}

async function chooseCustomBackground() {
  if (!window.pywebview?.api?.chooseBackgroundFile) {
    toast("当前运行方式不能打开背景文件选择器");
    return;
  }
  try {
    const result = await window.pywebview.api.chooseBackgroundFile();
    if (!result?.ok || !result.path) {
      if (result?.error) toast(result.error);
      return;
    }
    backgroundDraft ||= newBackgroundDraft();
    backgroundDraft.customBackgroundPath = result.path;
    backgroundDraft.backgroundPreset = "custom";
    backgroundDraft.previewUrl = result.previewUrl || "";
    updateBackgroundControls();
    toast("背景图片已选择，保存显示设置后应用");
  } catch (error) {
    toast(`选择背景失败：${error.message}`);
  }
}

function loadDeveloperTrainingData() {
  voiceReferenceController?.loadTrainingData?.();
}

async function saveSettings(update, message = "设置已保存", applyWindow = false) {
  return settingsController.save(update, message, applyWindow);
}

function storageScopedFetch(resource, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (DEV_MODE) headers["X-Amadeus-Storage-Scope"] = "developer";
  return fetch(resource, { ...options, headers });
}

async function requestConversationHistorySearch({ query, role, limit, offset, signal }) {
  const params = new URLSearchParams({
    q: String(query || ""),
    role: String(role || "all"),
    limit: String(limit),
    offset: String(offset),
  });
  const response = await storageScopedFetch(
    `/api/conversation/search?${params.toString()}`,
    { cache: "no-store", signal },
  );
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function setTodoDrawerOpen(open) {
  return todoController?.setDrawerOpen(open);
}

async function loadTodos(options = {}) {
  return todoController?.load(options);
}

function renderTodoBadge(state = {}) {
  if (!dom.todoCountBadge) return;
  const unseenCount = Math.max(0, Number(state.unseenCount) || 0);
  const activeCount = Math.max(0, Number(state.activeCount) || 0);
  dom.todoCountBadge.textContent = String(Math.min(99, unseenCount));
  dom.todoCountBadge.hidden = unseenCount === 0;
  dom.todoCountBadge.title = unseenCount > 0
    ? `${unseenCount} 项待办变更尚未查看`
    : "没有未查看的待办变更";
  if (dom.todoToggleBtn) {
    dom.todoToggleBtn.title = `待办：${activeCount} 项未完成`;
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

function renderTodoItems(items = []) {
  if (!dom.todoList) return;
  dom.todoList.replaceChildren();
  dom.todoEmpty?.classList.toggle("hidden", items.length > 0);
  for (const item of items) {
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

function startTodoReminderPolling() {
  return reminderPollingController?.start();
}

function pollTodoReminders() {
  return reminderPollingController?.poll();
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
  delete merged.cognitiveSemanticAuditEnabled;
  delete merged.cognitiveAuditModel;
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
    apiEndpoint: String(merged.apiEndpoint || "").trim() || DEFAULT_SETTINGS.apiEndpoint,
    apiModel: String(merged.apiModel || "").trim() || DEFAULT_SETTINGS.apiModel,
    useResponseFormat: Boolean(merged.useResponseFormat),
    cognitiveRepairModel: merged.cognitiveRepairModel === "pro" ? "pro" : "follow",
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
    backgroundPreset: BACKGROUND_PRESETS.has(backgroundPreset) ? backgroundPreset : "grid",
    customBackgroundPath: String(merged.customBackgroundPath || ""),
    backgroundViews: window.AmadeusBackgroundEditor.normalizeViews(merged.backgroundViews),
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
    webSearchProvider: ["duckduckgo", "bing", "brave"].includes(String(merged.webSearchProvider).toLowerCase())
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
  dom.webSearchStatus.textContent = settings.permissionWebSearch
    ? `${{brave: "Brave", bing: "Bing", duckduckgo: "DuckDuckGo"}[settings.webSearchProvider] || "DuckDuckGo"} 已授权` : "权限关闭";
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
  if (apiVerificationController) {
    connectionStatus = apiVerificationController.label();
    apiVerificationController.render();
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

async function saveDisplaySettings() {
  const draft = backgroundDraft;
  const update = collectDisplaySettings();
  const buttonText = dom.saveDisplayBtn.textContent;
  dom.settingsModal.inert = true;
  dom.backgroundControls.inert = true;
  dom.saveDisplayBtn.disabled = true;
  dom.saveDisplayBtn.textContent = "正在保存…";
  try {
    const previousModelId = sceneSnapshot().activeProfile.id;
    await saveSettings(update, "显示设置已保存", true);
    backgroundRenderer?.bumpRevision();
    drawPixiBackground();
    if (!dom.settingsModal.classList.contains("hidden")) backgroundDraft = newBackgroundDraft();
    updateBackgroundControls();
    if (settings.live2dModelId !== previousModelId) {
      toast("正在切换 Live2D 模型...");
      window.setTimeout(() => window.location.reload(), 120);
    }
  } catch (error) {
    if (!dom.settingsModal.classList.contains("hidden")) backgroundDraft = draft;
    updateBackgroundControls();
    toast(`显示设置保存失败：${error.message}`);
  } finally {
    dom.settingsModal.inert = false;
    dom.backgroundControls.inert = false;
    dom.saveDisplayBtn.disabled = false;
    dom.saveDisplayBtn.textContent = buttonText;
  }
}

function collectDisplaySettings() {
  const background = backgroundDraft || settings;
  const layoutMode = normalizeLayoutMode(dom.layoutModeInput.value);
  const width = toNumber(dom.windowWidthInput.value, settings.windowWidth);
  const height = toNumber(dom.windowHeightInput.value, settings.windowHeight);
  const modelId = selectedDisplayModelId();
  const transform = normalizedModelTransform({
    scale: toNumber(dom.modelScaleInput.value, 100) / 100,
    offsetX: dom.modelOffsetXInput.value,
    offsetY: dom.modelOffsetYInput.value,
  });
  const ordinaryModelId = settings.live2dModelId || sceneSnapshot().activeProfile?.id || FALLBACK_MODEL_PROFILE.id;
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
    backgroundPreset: BACKGROUND_PRESETS.has(dom.backgroundPresetInput?.value) ? dom.backgroundPresetInput.value : "grid",
    customBackgroundPath: background.customBackgroundPath || "",
    backgroundViews: window.AmadeusBackgroundEditor.normalizeViews(background.backgroundViews),
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

function renderDeveloperRelationship(relationship) {
  return relationshipController?.render(relationship);
}

function loadDeveloperRelationship() {
  return relationshipController?.load();
}

function renderDeveloperTouchRegions(profile, labels = {}) {
  if (!DEV_MODE || !dom.devTouchOverlay || !profile) return;
  dom.devTouchOverlay.replaceChildren();
  for (const zone of [...(profile.priority || [])].reverse()) {
    for (const polygon of profile.zones?.[zone] || []) {
      const visual = document.createElement("div");
      visual.className = "touch-region-visual";
      visual.dataset.touchRegion = zone;
      const shape = document.createElement("div");
      shape.className = "touch-zone-shape";
      shape.style.clipPath = "polygon(" + polygon.map(([x, y]) => x * 100 + "% " + y * 100 + "%").join(",") + ")";
      const label = document.createElement("span");
      label.className = "touch-zone-label";
      label.textContent = (labels[zone] || zone).split(" /")[0];
      label.style.left = polygon.reduce((sum, point) => sum + point[0], 0) / polygon.length * 100 + "%";
      label.style.top = polygon.reduce((sum, point) => sum + point[1], 0) / polygon.length * 100 + "%";
      visual.append(shape, label);
      dom.devTouchOverlay.appendChild(visual);
    }
  }
}

function renderDeveloperTouchHit(hit, _now = performance.now(), labels = {}) {
  if (!DEV_MODE || !hit) return;
  if (dom.devTouchHit) {
    dom.devTouchHit.textContent = [
      "命中：" + (labels[hit.zone] || hit.zone),
      "模型归一化坐标：x=" + toNumber(hit.x, 0).toFixed(4) + ", y=" + toNumber(hit.y, 0).toFixed(4),
      "屏幕坐标：x=" + toNumber(hit.screenX, 0).toFixed(1) + ", y=" + toNumber(hit.screenY, 0).toFixed(1),
      hit.zone === "body" ? "结果：不向 Agent 发送触摸事件" : "结果：进入触摸事件防抖与边界判定",
    ].join("\n");
  }
  document.querySelectorAll("[data-touch-region]").forEach(node => {
    node.classList.toggle("active", node.dataset.touchRegion === hit.zone);
  });
}

function updateDeveloperTouchOverlay({ bounds, touchBounds, stale = false } = {}) {
  if (!DEV_MODE || !sceneSnapshot().model || !dom.devTouchOverlay || !bounds || !touchBounds) return;
  Object.assign(dom.devTouchOverlay.style, {
    left: bounds.x + bounds.width * touchBounds.x + "px",
    top: bounds.y + bounds.height * touchBounds.y + "px",
    width: bounds.width * touchBounds.width + "px",
    height: bounds.height * touchBounds.height + "px",
  });
  if (stale) {
    document.querySelectorAll("[data-touch-region].active").forEach(node => node.classList.remove("active"));
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
      ? `${{brave: "Brave", bing: "Bing", duckduckgo: "DuckDuckGo"}[settings.webSearchProvider] || "DuckDuckGo"} 已授权` : "权限关闭";
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
    updateConnectionStatus({ memories: [] });
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
    invalidateConversationHistoryHydration();
    chatController?.invalidateForContextChange({ notifyBackend: true, interruptSpeech: false });
    activeConversationInputs = [];
    activeConversationAttachments = [];
    activeConversationGroup = "";
    activeConversationReplyAnchor = null;
    activeConversationUserMessageId = "";
    conversationWasInterrupted = false;
    clearPendingReplyAnchor();
    clearProactiveReturnContext();
    resetProactiveSequence();
    interruptCurrentSpeech();
    touchInteractionController?.invalidateForContextChange();
    clearPendingProactivePersistence();
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
  invalidateConversationHistoryHydration();
  chatController?.invalidateForContextChange({ notifyBackend: true, interruptSpeech: false });
  resetActiveConversationRequestState();
  clearPendingReplyAnchor();
  touchInteractionController?.clearDeferredResponse();
  clearProactiveReturnContext();
  resetProactiveSequence();
  interruptCurrentSpeech();
  syncChatSubmitButton();
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
      ? "回复 Amadeus System" : "回复自己";
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
  invalidateConversationHistoryHydration();
  chatController?.invalidateForContextChange({ notifyBackend: true, interruptSpeech: false });
  resetActiveConversationRequestState();
  clearPendingReplyAnchor();
  clearProactiveReturnContext();
  resetProactiveSequence();
  touchInteractionController?.invalidateForContextChange();
  clearPendingProactivePersistence();
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
  backgroundDraft = newBackgroundDraft();
  fillSettingsForm();
  dom.settingsModal.classList.remove("hidden");
  backgroundEditor?.refresh();
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
  backgroundDraft = null;
  updateBackgroundControls();
}

function closeSalieriWindow() {
  salieriController?.close();
}

function activateSettingsTab(tab) {
  if (tab !== "memory") closeSalieriWindow();
  dom.settingsTabs.forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  dom.tabPages.forEach(page => page.classList.toggle("active", page.dataset.page === tab));
  backgroundEditor?.refresh();
}

function fitModel() {
  return sceneController?.fitModel?.() || null;
}

function drawPixiBackground() {
  backgroundRenderer?.draw(settings);
}

const DEFAULT_HEAD_TURN_PROFILE = Object.freeze({
  maxYaw: 30,
  auxiliaryMax: 0,
  bodyMax: 2,
  pointerSwitchThreshold: 0.3,
  springFrequency: 4.8,
  springDamping: 0.88,
});

function activeHeadTurnProfile() {
  const configured = sceneSnapshot().activeProfile?.headTurn;
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
  return expressionController?.updateFuriousTurnMotion(delta) || activeHeadTurnProfile();
}

function furiousColdGlanceWeight(now = performance.now()) {
  if (effectiveVisualEmotion() !== "furious") return 0;
  const age = Math.max(0, now - expressionSnapshot().emotionEnteredAt);
  if (age < 2400) return 0;
  const phase = (age - 2400) % 8200;
  if (phase > 760) return 0;
  return Math.pow(Math.sin((phase / 760) * Math.PI), 2);
}

function updateLive2D(delta) {
  const scene = sceneSnapshot();
  if (!scene.coreModel) return;
  try {
    if (scene.model && typeof scene.model.update === "function") scene.model.update(delta * (1000 / 60));
  } catch (error) {
    console.debug("Live2D native update failed:", error);
  }
  const now = performance.now();
  frameController?.advance(delta, { now });
  const speaking = isSpeaking(now);
  updateEmotionLifecycle(now);
  updateProactiveAmbientMood(now);
  updatePositiveAmbientMood(now);
  updateInteractionCoordinator(now);
  updateProactiveInteraction(now);
  // 生命周期、主动交互和语音协调都可能切换表情；逐帧渲染必须读取同一
  // 个最新快照，避免恢复动作后仍沿用旧代次的手势或细节。
  const expression = expressionSnapshot();
  const visualEmotion = effectiveVisualEmotion();
  const emotion = blendedEmotionTarget();
  const gesture = now < expression.gestureUntil ? expression.activeGesture : "";
  const motion = gestureMotion(gesture, expression.currentEmotion, now, speaking);
  updateLookTargets(now, expression, visualEmotion);
  const frame = frameSnapshot();
  const { eyeOpen, breath, idleSway } = frame;
  const lipSample = speechPlayback.sampleLipSync(now, settings.lipSyncEnabled);
  scene.model.x = scene.modelHome.x;
  scene.model.y = scene.modelHome.y;
  scene.model.rotation = 0;
  updateBackgroundMotion(motion, speaking, now);

  const speakNod = speaking ? Math.sin(frame.tickerTime * 4.2) * 0.28 : 0;
  const shyIntensity = Math.max(
    emotionIntensity("shy", now),
    emotionIntensity("flustered", now),
    emotionIntensity("affectionate", now) * 0.55
  );
  const shyTremble = shyIntensity > 0 ? Math.sin(frame.tickerTime * 8.4) * 0.45 * shyIntensity : 0;
  const shyCheekPulse = shyIntensity > 0
    ? (Math.sin(frame.tickerTime * 3.6) * 0.045 + Math.sin(frame.tickerTime * 1.7) * 0.025) * shyIntensity
    : 0;
  const lifecycle = emotionLifecycleWeight(now);
  const blushLifecycle = blushLifecycleWeight(now);
  const annotatedExpressionPlan = activeAnnotatedExpressionPlan(
    visualEmotion,
    lifecycle,
  );

  const eyeContact = clamp(0.68 + (toNumber(expression.currentExpressionDetail.eyeContact, 0.68) - 0.68) * lifecycle, 0, 1);
  const gazeX = clamp(toNumber(expression.currentExpressionDetail.gazeX, 0) * lifecycle, -0.6, 0.6);
  const gazeY = clamp(toNumber(expression.currentExpressionDetail.gazeY, 0) * lifecycle, -0.6, 0.6);
  const tension = clamp(toNumber(expression.currentExpressionDetail.tension, 0) * lifecycle, 0, 1);
  const headTurn = updateFuriousTurnMotion(delta);
  // 转向弹簧在本帧推进了 controller 状态；角度计算要读推进后的方向，
  // 其余表达细节仍沿用本帧统一快照。
  const turnExpression = expressionSnapshot();
  const furiousTurnDegrees = visualEmotion === "furious"
    ? turnExpression.furiousTurnDirection * headTurn.maxYaw * lifecycle
    : 0;
  const furiousBodyTurn = visualEmotion === "furious"
    ? turnExpression.furiousTurnDirection * headTurn.bodyMax * lifecycle
    : 0;
  const angleX = clamp(
    frame.headLook.x * (16 + eyeContact * 6)
      + idleSway * 1.1
      + shyTremble
      + motion.headX
      + (emotion.ParamAngleX || 0)
      + furiousTurnDegrees,
    -60,
    60
  );
  const angleY = clamp(-frame.headLook.y * (14 + eyeContact * 5) + Math.sin(frame.tickerTime * 0.7) * 1.2 + speakNod + motion.headY + (emotion.ParamAngleY || 0), -30, 30);
  const angleZ = clamp(frame.headLook.x * -3.2 + Math.sin(frame.tickerTime * 0.55) * 1.2 - shyTremble * 1.3 + motion.headZ + (emotion.ParamAngleZ || 0), -30, 30);
  const bodyX = clamp(frame.headLook.x * 2.2 + idleSway * 1.5 + (speaking ? Math.sin(frame.tickerTime * 3.2) * 0.75 : 0) + motion.bodyX + furiousBodyTurn, -10, 10);
  const bodyY = clamp(Math.sin(frame.tickerTime * 1.05) * 0.55 + (speaking ? Math.sin(frame.tickerTime * 2.6) * 0.42 : 0) + motion.bodyY, -10, 10);
  const bodyZ = clamp(Math.sin(frame.tickerTime * 0.38) * 0.82 + (speaking ? Math.sin(frame.tickerTime * 2.15) * 0.48 : 0) + motion.bodyZ, -10, 10);
  const furiousGlance = furiousColdGlanceWeight(now);
  const shyAvertedGazeX = ["shy", "flustered"].includes(visualEmotion)
    ? frame.shyGazeAwayDirection * (
      0.12 + clamp(visualEmotionIntensityWeight(expression.currentEmotionWeight), 0, 1.55) * 0.06
    )
    : 0;
  const semanticGazeX = visualEmotion === "furious"
    ? turnExpression.furiousTurnDirection * 0.52 * furiousGlance
    : ["shy", "flustered"].includes(visualEmotion)
      ? shyAvertedGazeX
      : gazeX;
  const rawEyeBallX = clamp(frame.eyeLook.x * 1.15 * eyeContact + semanticGazeX + motion.eyeX + (emotion.ParamEyeBallX || 0), -1, 1);
  const rawEyeBallY = clamp(-frame.eyeLook.y * 1.22 * eyeContact + gazeY + motion.eyeY + (emotion.ParamEyeBallY || 0), -1, 1);
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
  // AiPoet's original random motions use yaw well beyond ±30 degrees. Each
  // model declares its own safe limit so the classic model can turn strongly
  // without forcing the same range onto ordinary Cubism rigs.
  const yawLimit = visualEmotion === "furious" ? headTurn.maxYaw : 30;
  visualParameterController?.render({
    now,
    frame,
    speaking,
    expression,
    visualEmotion,
    emotion,
    motion,
    lipSample,
    tension,
    blushLifecycle,
    shyCheekPulse,
    annotatedExpressionPlan,
    angleX: clamp(angleX, -yawLimit, yawLimit),
    angleY,
    angleZ,
    param14: visualEmotion === "furious"
      ? turnExpression.furiousTurnDirection * headTurn.auxiliaryMax * lifecycle
      : clamp(angleX / 30, -1, 1),
    param15: clamp(angleY / 30, -1, 1),
    bodyX,
    bodyY,
    bodyZ,
    param17: clamp(bodyX / 10 + motion.stageX / 28, -1, 1),
    eyeData: {
      rawEyeBallX,
      rawEyeBallY,
      expressionEyeLOpen: expressionEyeLOpen * gestureEyeOpen,
      expressionEyeROpen: expressionEyeROpen * gestureEyeOpen,
    },
  });
  updateNativeInteraction(now);
  freezeModelBoundBackground();
  if (DEV_MODE) updateDeveloperSpeechPanel();
  if (DEV_MODE) touchInteractionController?.updateDeveloperOverlay();
  // 头发参数保持在 native 固定层之后写入，沿用旧有覆盖顺序。
  visualParameterController?.renderHair({ frame, motion });

  developerParameterLabController?.applyParameterOverrides();
  try {
    sceneSnapshot().coreModel.update();
    developerParameterLabController?.applyDrawableOverrides();
  } catch {
    // Cubism 2 compatibility wrappers may update drawables elsewhere.
  }
}

function emotionSemanticsFor(emotion) {
  return EMOTION_SEMANTICS[emotion] || EMOTION_SEMANTICS.neutral;
}

function effectiveVisualEmotion() {
  return expressionController?.effectiveVisualEmotion() || "neutral";
}

function effectiveExpressionRuntime() {
  const developerRuntime = developerParameterLabController?.getExpressionRuntime?.();
  if (DEV_MODE && developerRuntime) return developerRuntime;
  return settings.expressionRuntime && typeof settings.expressionRuntime === "object"
    ? settings.expressionRuntime
    : { activeByModel: {} };
}

function activeRigModelId() {
  return sceneSnapshot().activeProfile?.baseModelId || sceneSnapshot().activeProfile?.id || "";
}

function modelArmPoseTarget(value, now = performance.now()) {
  return armMotionController.update(value, {
    now,
    mode: sceneSnapshot().activeProfile?.armPoseMode || "continuous",
    modelId: sceneSnapshot().activeProfile?.id || "",
  });
}

function activeRuntimeExpressionEntry(emotion = expressionSnapshot().currentEmotion) {
  const activeByModel = effectiveExpressionRuntime()?.activeByModel || {};
  // Clothing variants share a rig and its trained face, while allowing their
  // own per-emotion overrides without copying or migrating saved samples.
  const modelIds = [sceneSnapshot().activeProfile?.id, activeRigModelId()];
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
  const expression = expressionSnapshot();
  const neutral = runtimeEmotionTarget("neutral");
  const foreground = runtimeEmotionTarget(expression.currentEmotion);
  // Developer emotion controls are a visual inspection tool: show the chosen
  // face itself instead of composing it over whichever stable emotion happened
  // to be active before the tester selected it.
  const manualBlush = developerEmotionController?.getManualBlush?.();
  if (DEV_MODE && manualBlush !== null && manualBlush !== undefined) return foreground;
  const semantics = emotionSemanticsFor(expression.currentEmotion);
  const overlayStrength = EMOTION_OVERLAY_STRENGTH[semantics.role];
  if (!overlayStrength) return foreground;

  const baseEmotion = semantics.role === "cognitive"
    ? expression.cognitiveOverlayBaseEmotion
    : expression.stableEmotion;
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

function visualEmotionIntensityWeight(rawIntensity = expressionSnapshot().currentEmotionWeight) {
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
  const expression = expressionSnapshot();
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
  const planIntensity = emotionSemanticsFor(expression.currentEmotion).role === "cognitive"
    ? visualEmotionIntensityWeight(COGNITIVE_BASE_RAW_INTENSITY)
    : visualEmotionIntensityWeight(expression.currentEmotionWeight);
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
  const expression = expressionSnapshot();
  const neutral = emotionTargets.neutral;
  const target = visualTargetForEmotionState();
  const lifecycle = emotionLifecycleWeight();
  const emotionWeight = visualEmotionIntensityWeight(expression.currentEmotionWeight);
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

function updateLookTargets(
  now = performance.now(),
  expression = expressionSnapshot(),
  visualEmotion = effectiveVisualEmotion(),
) {
  return frameController?.updateLookTargets({ now, expression, visualEmotion });
}

function gestureMotion(gesture, emotion, now = performance.now(), speaking = false) {
  return gestureMotionController?.resolve(gesture, emotion, {
    now,
    speaking,
    expression: expressionSnapshot(),
    frame: frameSnapshot(),
  }) || {
    headX: 0, headY: 0, headZ: 0, bodyX: 0, bodyY: 0, bodyZ: 0,
    stageX: 0, stageY: 0, stageTilt: 0, eyeX: 0, eyeY: 0, eyeClose: 0,
    eyeSmile: 0, mouthSmile: 0, breath: 0, hair: 0, arms: 0,
  };
}
function updateBackgroundMotion(motion, speaking, now = performance.now()) {
  backgroundRenderer?.resetMotion();
}

function setParam(id, value) {
  try {
    sceneSnapshot().coreModel.setParameterValueById(id, value);
  } catch {
    // Imported models can omit optional params.
  }
}

function installPostMotionRetargeting() {
  const internalModel = sceneSnapshot().model?.internalModel;
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
  if (!sceneSnapshot().coreModel) return;
  parameterRenderer?.reapply();
  mouthParameterRenderer?.reapply();
  freezeModelBoundBackground();
  developerParameterLabController?.applyParameterOverrides();
  try {
    sceneSnapshot().coreModel.update();
    developerParameterLabController?.applyDrawableOverrides();
  } catch {
    // Cubism 2 compatibility wrappers may update drawables elsewhere.
  }
}

function setSmoothParam(id, target, factor = 0.18) {
  return parameterRenderer?.setSmoothParam(id, target, factor) ?? setParam(id, target);
}

function mappedParameterIds(channel, fallbackId = "") {
  const aliases = sceneSnapshot().activeProfile?.parameterAliases?.[channel];
  if (Array.isArray(aliases) && aliases.length) return aliases;
  return fallbackId ? [fallbackId] : [];
}

function setMappedSmoothParam(channel, target, factor = 0.18, fallbackId = "") {
  parameterRenderer?.setMappedSmoothParam(channel, target, factor, fallbackId);
}

function setExpressionFix() {
  hideModelUiLayers();
  freezeModelBoundBackground();
}

function hideModelUiLayers() {
  const profileHidden = Array.isArray(sceneSnapshot().activeProfile?.hiddenParameters)
    ? sceneSnapshot().activeProfile.hiddenParameters
    : [];
  [...new Set([...MODEL_UI_HIDE_PARAMS, ...profileHidden])].forEach(id => setParam(id, 0));
}

function freezeModelBoundBackground() {
  Object.entries(sceneSnapshot().activeProfile?.fixedParameters || {}).forEach(([id, value]) => {
    setParam(id, toNumber(value, 0));
  });
  const hiddenParts = Array.isArray(sceneSnapshot().activeProfile?.hiddenParts) ? sceneSnapshot().activeProfile.hiddenParts : [];
  hiddenParts.forEach(id => {
    try {
      sceneSnapshot().coreModel.setPartOpacityById(id, 0);
    } catch {
      // Model profiles can list optional background parts.
    }
  });
}

function setEmotion(emotion = "neutral", gesture = "idle", detail = {}) {
  expressionController?.setEmotion(emotion, gesture, detail);
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
  return expressionController?.emotionLifecycleWeight(now) ?? 1;
}

function blushLifecycleWeight(now = performance.now()) {
  return expressionController?.blushLifecycleWeight(now) ?? 1;
}

function expressionHoldUntil() {
  const expression = expressionSnapshot();
  return Math.max(
    expression.emotionEnteredAt + expression.currentEmotionHoldMs,
    expression.emotionSpeechProtectedUntil,
  );
}

function protectCurrentExpressionThroughSpeech(
  speechEndsAt = speechPlayback.state.endsAt,
  postSpeechHoldMs = EXPRESSION_POST_SPEECH_HOLD_MS
) {
  return expressionController?.protectExpressionThroughSpeech(speechEndsAt, postSpeechHoldMs);
}

function updateEmotionLifecycle(now = performance.now()) {
  return expressionController?.updateLifecycle(now);
}

function maybeRefreshContextEmotion(now = performance.now()) {
  return expressionController?.refreshContextEmotion(now);
}

function recoveryEmotionFor(emotion = expressionSnapshot().currentEmotion) {
  return expressionController?.recoveryEmotionFor(emotion) || "neutral";
}

function emotionIntensity(emotion, now = performance.now()) {
  return expressionController?.emotionIntensity(emotion, now) || 0;
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

function stripEmojiForSpeech(text) { return speechPlayback.stripEmojiForSpeech(text); }
function startSpeech(text, voiceStyle = "calm", meta = {}) { return speechPlayback.startSpeech(text, voiceStyle, meta); }
function semanticSpeechUnits(text) { return speechPlayback.semanticSpeechUnits(text); }
function validatedSemanticSpeechUnits(text, supplied = null) { return speechPlayback.validatedSemanticSpeechUnits(text, supplied); }
function normalizeSpeechBoundary(value, fallback = "continue") { return speechPlayback.normalizeSpeechBoundary(value, fallback); }
function speechBoundaryPauseMs(boundaryAfter, speechPace = "normal") { return speechPlayback.speechBoundaryPauseMs(boundaryAfter, speechPace); }
function startSegmentedSpeech(text, voiceStyle = "calm", meta = {}) { return speechPlayback.startSegmentedSpeech(text, voiceStyle, meta); }
function isSpeechCancellation(error) { return speechPlayback.isSpeechCancellation(error); }

function interruptCurrentSpeech() { return speechPlayback.interruptCurrentSpeech(); }

function interruptReplyForNewConversation(now = performance.now()) {
  const currentKind = speechPlayback.state.kind;
  const currentlySpeaking = isSpeaking(now);
  const priorReplyStillActive = (
    speechPlayback.pipelineActive
    || speechPlayback.state.pending
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
    touchInteractionController?.renderRegions();
    touchInteractionController?.updateDeveloperOverlay();
  }
  if (target === "overview") refreshDeveloperRuntime();
  if (target === "emotion") developerEmotionController?.renderPanel?.({ syncInputs: true });
  if (target === "relationship") relationshipController?.load();
  if (target === "persona") {
    personaController?.load();
    developerPersonaSkillMonitor?.refresh();
  }
  if (target === "model") developerModelEditorController?.render?.();
  if (target === "training") loadDeveloperTrainingData();
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

function recordVoiceDiagnostic(stage, message, details = {}) {
  return voiceDiagnosticsController?.record(stage, message, details);
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
        stage: voiceDiagnosticsController?.getSnapshot().diagnostics.stage || "idle",
        pending: speechPlayback.state.pending,
        active: speechPlayback.state.active,
        kind: speechPlayback.state.kind,
        generation: speechPlayback.state.generation,
        pipelineSegmentIndex: speechPlayback.state.pipelineSegmentIndex,
        pipelineSegmentTotal: speechPlayback.state.pipelineSegmentTotal,
        textLength: speechPlayback.state.text.length,
        metrics: speechPlayback.state.ttsMetrics,
      },
    });
  } catch (error) {
    dom.devRuntimeSummary.textContent = `状态刷新失败：${error.message}`;
  }
}

async function copyDeveloperDiagnostics() {
  const voiceSnapshot = voiceDiagnosticsController?.getSnapshot() || {
    diagnostics: { stage: "idle", message: "尚无语音请求" },
    events: [],
  };
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
    voiceDiagnostics: voiceSnapshot.diagnostics,
    voiceEvents: voiceSnapshot.events,
    runtime: latestRuntimeStatus,
    speechState: {
      pending: speechPlayback.state.pending,
      active: speechPlayback.state.active,
      kind: speechPlayback.state.kind,
      generation: speechPlayback.state.generation,
      pipelineSegmentIndex: speechPlayback.state.pipelineSegmentIndex,
      pipelineSegmentTotal: speechPlayback.state.pipelineSegmentTotal,
      metrics: speechPlayback.state.ttsMetrics,
      alignmentSource: speechPlayback.state.alignmentSource,
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
  return voiceDiagnosticsController?.render();
}

function buildTtsRequest(...args) { return speechPlayback.buildTtsRequest(...args); }
function preparedTtsKey(request) { return speechPlayback.preparedTtsKey(request); }
async function requestPreparedTts(request, generation = null, signal = null) { return speechPlayback.requestPreparedTts(request, generation, signal); }
function ttsRetryWouldDuplicateWork(error) { return speechPlayback.ttsRetryWouldDuplicateWork(error); }
async function startLocalTts(...args) { return speechPlayback.startLocalTts(...args); }
async function startSystemSpeech(...args) { return speechPlayback.startSystemSpeech(...args); }
function stopLocalTts(preserveSpeechState = false) { return speechPlayback.stopLocalTts(preserveSpeechState); }
function estimateSpeechDuration(...args) { return speechPlayback.estimateSpeechDuration(...args); }
function isSpeaking(now = performance.now()) { return speechPlayback.isSpeaking(now); }
function speechMouthOpen(now = performance.now()) { return speechPlayback.speechMouthOpen(now); }
function speechMouthForm(now = performance.now()) { return speechPlayback.speechMouthForm(now); }

function updateConnectionStatus(data = {}) {
  const source = String(data.source || "").toLowerCase();
  apiVerificationController?.observeConnection(data);
  const apiLabel = apiVerificationController?.label() || "";
  const memoryCount = Array.isArray(data.memories) ? data.memories.length : state.memories.length;
  if (data.connectionState === "startup-greeting-fallback") {
    connectionStatus = `STARTUP GREETING LOCAL / MEM ${memoryCount}`;
  } else if (data.connectionState === "backend-reconnecting") {
    connectionStatus = `BACKEND RECONNECT ${data.attempt || 1}/${data.attempts || 3} / MEM ${memoryCount}`;
  } else if (data.connectionState === "backend-failed") {
    connectionStatus = `LOCAL BACKEND LOST / MEM ${memoryCount}`;
  } else if (data.connectionState === "backend-restored") {
    connectionStatus = `${apiLabel} / MEM ${memoryCount}`;
  } else if (data.connectionState === "reconnecting") {
    connectionStatus = `API RECONNECT ${data.attempt || 1}/${data.attempts || 3} / MEM ${memoryCount}`;
  } else if (data.connectionState === "failed") {
    const code = String(data.connectionCode || "CONNECTION").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 24);
    connectionStatus = `${apiLabel} (${code || "CONNECTION"}) / MEM ${memoryCount}`;
  } else if (data.toolUsed) {
    const toolState = data.toolStatus === "failed" ? "FAILED" : "OK";
    connectionStatus = `TOOL ${String(data.toolUsed).toUpperCase()} ${toolState} / MEM ${memoryCount}`;
  } else if (source === "api") {
    connectionStatus = `API ${data.latencyMs ? `${data.latencyMs}MS` : "OK"} / MEM ${memoryCount}`;
  } else if (source === "tool") {
    connectionStatus = `TOOL ${data.toolUsed || "LOCAL"} / MEM ${memoryCount}`;
  } else if (settings.modelConfigured) {
    connectionStatus = `${apiLabel} / MEM ${memoryCount}`;
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
  if (chatController?.isRequestInFlight()) return true;
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

function createChatUtterancePlayback(generation, requestStartedAt, requestId = "") {
  // DOM、交付回执、工具门和上下文身份仍归 chat；播放队列及代次归 controller。
  let progressiveMessage = null;
  let toolGateReleased = false;
  const deliveryContextEpoch = Math.max(0, Number(state.contextEpoch) || 0);
  const deliveryTurnId = `${requestId}:turn`;
  const recordDelivery = (stage, item = null, finalResult = null) => {
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
  const onPartialFailure = (item, failedIndex) => {
    if (progressiveMessage?.message?.meta) {
      progressiveMessage.message.meta.voicePartialFailure = true;
      progressiveMessage.message.meta.voiceFailedSegment = failedIndex + 1;
      progressiveMessage.message.meta.deliveryPartial = true;
      persist();
    }
    releaseToolGate(item, false);
  };
  const onReveal = (item, voiceStarted = false, finalResult = null, queueLength = 0) => {
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
      chatController?.setPendingAssistant(progressiveMessage.message);
    } else {
      appendProgressiveAssistantSegment(
        progressiveMessage,
        item.textZh,
        item.speechText,
        item.displayIndex,
        Math.max(queueLength, item.displayIndex + 1),
      );
    }
    if (item.utteranceId === "tool-preamble") {
      releaseToolGate(item, voiceStarted);
      thinkingIndicatorController?.show?.(generation, "tool-executing", "开场语已开始播放，正在执行所需工具");
    } else {
      thinkingIndicatorController?.hide?.(generation);
    }
  };
  const onFinalResult = result => {
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
        messageView?.updateMetadata(progressiveMessage.message);
        persist();
      }
  };
  return speechPlayback.createChatUtterancePlayback(generation, requestStartedAt, requestId, {
    onStart: id => { chatController?.bindAnswerRequest(id); },
    onDelivery: recordDelivery,
    onReveal,
    onPartialFailure,
    onFinalResult,
    onSettled: id => { chatController?.clearAnswerRequest(id); },
    getMessage: () => progressiveMessage?.message || null,
  });
}

function updateModelStateLabel() {
  if (!dom.modelState) return;
  const expression = expressionSnapshot();
  dom.modelState.textContent = runtimeModeStateLabel(`${expression.currentEmotion.toUpperCase()} / ${expression.activeGesture || "IDLE"} / ${connectionStatus}`);
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
    label.title = `${item.file.name} · ${CHAT_MESSAGE_VIEW.formatFileSize(item.file.size)}`;
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
    chatController?.isRequestInFlight()
    || speechPlayback.pipelineActive
    || speechPlayback.state.pending
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
  chatController?.cancelCurrent({ notifyBackend: true, interruptSpeech: true });
  resetActiveConversationRequestState();
  syncChatSubmitButton();
  setEmotion("neutral", "idle", { restart: true });
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
    messageView?.scrollToLatest();
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
    messageView?.scrollToLatest();
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
    messageView?.scrollToLatest();
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
  // 用户已经开始新的回合时，失效仍在途的启动/导航历史恢复，避免旧响应重绘当前输入。
  invalidateConversationHistoryHydration();
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

  touchInteractionController?.onChatStarted();

  const priorAnswerRequestId = chatController?.currentRequestId() || "";
  const now = performance.now();
  interruptReplyForNewConversation(now);
  notifyBackendAnswerCancellation(priorAnswerRequestId);
  if (chatController?.isRequestInFlight()) chatController.supersedeCurrent();
  requestConversationReply();
}

async function handleChatRequestError(error, { generation, requestId, inputs, proactiveContext }) {
  if (!chatController?.isCurrent(generation)) return;
  const detail = connectionErrorSummary(error);
  const completedTools = Array.isArray(error?.completedTools) ? error.completedTools : [];
  const typedPipelineFailure = completedTools.length > 0
    || (Boolean(error?.streamErrorType) && !error?.connectionFailure);
  if (typedPipelineFailure) {
    const errorType = String(error.streamErrorType || "internal_pipeline_failed");
    const stage = String(error.streamErrorStage || "pipeline");
    const upstreamFailure = errorType === "upstream_http_failed";
    if (upstreamFailure) updateConnectionStatus({ connectionState: "failed", connectionCode: error.code });
    const message = upstreamFailure
      ? `${completedTools.length ? "已有操作完成，但后续模型请求失败。" : ""}${detail}`
      : completedTools.includes("open_browser")
      ? "网页已打开，但后续回复生成失败。"
      : completedTools.length > 0
      ? "已有操作成功完成，但后续回复生成失败。"
      : errorType === "cognitive_revision_failed"
      ? "本次回复没有生成成功。"
      : errorType === "invalid_reply_anchor"
      ? "刚才引用的消息没有找到。可以把那句话直接引用过来，我再接着回答。"
      : errorType === "grounding_validation_failed"
      ? "我刚才没能确认自己的理解，所以先没有乱说。你愿意的话，我可以换个方式再试一次。"
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
    recordVoiceDiagnostic(
      "stream-terminal-failure",
      "本轮已终止，等待用户明确重新发送",
      { requestId, errorType, errorStage: stage },
    );
    resetActiveConversationRequestState();
    return;
  }
  updateConnectionStatus({ connectionState: "failed", connectionCode: error?.code || "CONNECTION" });
  toast(`模型服务连接失败：${detail}`);
  const applied = await applyAssistant({
    reply: `模型服务连接失败，已自动重试但仍未收到回答。错误：${detail}。请检查网络、API Key、接口地址或额度后再试。`,
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
  if (applied && chatController.isCurrent(generation)) clearProactiveReturnContextIf(proactiveContext);
}

async function requestConversationReply() {
  if (!activeConversationInputs.length || chatController?.isRequestInFlight()) return;
  const now = performance.now();
  // 重试路径也先停止未完成语音；真正的请求代次与 Abort 由控制器唯一持有。
  interruptReplyForNewConversation(now);
  const inputs = activeConversationInputs.slice();
  const proactiveContext = getProactiveReturnContext();
  const requestId = chatController.createRequestId(activeConversationGroup, inputs.length);
  const supersedesRequestId = chatController.consumeSupersededRequestId();
  const history = cleanHistoryForModel()
    .filter(message => message.meta?.conversationGroup !== activeConversationGroup)
    .slice(-MODEL_HISTORY_LIMIT);
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
  const common = {
    payload,
    requestId,
    requestStartedAt: now,
    inputs,
    proactiveContext,
    onLegacy: (data, userMessage, context) => applyAssistant({
      ...data,
      clientChatGeneration: context.generation,
    }, userMessage),
    onCommit: (data, playback, userMessage) => commitStreamedAssistantResult(data, playback, userMessage),
    onCommitted: context => clearProactiveReturnContextIf(context.proactiveContext),
    onCancelled: streamError => {
      recordVoiceDiagnostic(
        "stream-cancelled",
        "本轮流式回答已取消，未将取消误记为语音合成失败",
        { requestId, errorType: String(streamError?.streamErrorType || streamError?.name || "cancelled") },
      );
      resetActiveConversationRequestState();
    },
    onError: handleChatRequestError,
    allowLegacyFallback: true,
    frontendFullRetryDisabled: Boolean(settings.architectureFeatures?.frontendFullRetryDisabled),
  };
  if (settings.debugDirectSpeech) {
    common.execute = () => ({
      reply: inputs.join("\n"),
      spokenTextJa: inputs.join("\n"),
      emotion: "neutral",
      gesture: "idle",
      voiceStyle: "calm",
      memoryUpdates: [],
      source: "debug",
      debugDirectSpeech: true,
    });
  } else {
    common.createPlayback = (generation, requestStartedAt, id) =>
      createChatUtterancePlayback(generation, requestStartedAt, id);
  }
  return chatController.startTurn(common);
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
    if (persistentColdShoulder) touchInteractionController?.cancelForColdShoulder();
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
  bookmarksController?.refreshAfterAssistantAction(data);
  if (playback?.message) chatController?.setPendingAssistant(playback.message);
  if (
    chatController?.isCurrent(Number(data.clientChatGeneration || 0))
  ) {
    resetActiveConversationRequestState();
  }
  persist();
  return true;
}

async function applyAssistant(data, userMessage = "") {
  // 触摸回复绑定会话标识与上下文代次，切换会话后丢弃迟到的旧回复。
  if (data.source === "touch" && data.turnIdentity
      && (String(data.turnIdentity.conversationId) !== String(state.conversationId || "")
          || Number(data.contextEpoch) !== (Number(state.contextEpoch) || 0))) return false;
  if (!proactiveDeliveryStillCurrent(data)) return false;
  if (data.proactiveControl && typeof data.proactiveControl === "object") {
    applyProactiveControl(data.proactiveControl, { persistState: false });
  }
  refreshTodosAfterAssistantAction(data);
  bookmarksController?.refreshAfterAssistantAction(data);
  const incomingSource = String(data.source || "");
  const touchCannotOverrideColdShoulder = incomingSource === "touch" && persistentColdShoulder;
  if (touchCannotOverrideColdShoulder) {
    preservePersistentFuriousPose();
    thinkingIndicatorController?.hide?.(Number(data.clientChatGeneration || 0));
    return true;
  }
  if (typeof data.coldShoulder === "boolean") {
    persistentColdShoulder = data.coldShoulder;
    if (persistentColdShoulder) touchInteractionController?.cancelForColdShoulder();
  }
  if (data.silentLocked || data.silentInteraction) {
    const mood = resolveAssistantMood(data, userMessage, "");
    if (data.relationshipTone) relationshipTone = String(data.relationshipTone);
    updateConnectionStatus(data);
    setEmotion(mood.emotion, mood.gesture, mood);
    updateDeveloperExchange("", "", data);
    thinkingIndicatorController?.hide?.(Number(data.clientChatGeneration || 0));
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
    ...(typeof data.proactiveAfterUserTurnId === "string"
      ? { proactiveAfterUserTurnId: data.proactiveAfterUserTurnId } : {}),
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
  // 过滤无需交付或缺少可用文本的语音单元；至少两段才采用逐段显示与播放。
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
      // 普通 chat 的交付由 chat controller 持有；触摸/主动交互仍归页面
      // interaction owner，避免 progressive 音频把消息误放入 chat pending。
      if (messageMeta.kind === "chat") chatController?.setPendingAssistant(progressiveMessage.message);
      else activeInteractionAssistantMessage = progressiveMessage.message;
    } else {
      appendProgressiveAssistantSegment(
        progressiveMessage,
        visibleSegment,
        spokenSegment,
        index,
        total,
      );
    }
    thinkingIndicatorController?.hide?.(Number(data.clientChatGeneration || 0));
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
    deliveryPriority: data.source === "todo-reminder" && data.reminderId ? "scheduled_reminder" : "",
    waitForCompletion: Boolean(data.source === "todo-reminder" && data.scheduledExecutionStatus),
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
    if (speechPlayback.state.kind === "proactive") interruptCurrentSpeech();
    return false;
  }
  const supersededByNewChat = messageMeta.kind === "chat"
    && Number(data.clientChatGeneration || 0) > 0
    && !chatController?.isCurrent(Number(data.clientChatGeneration));
  if (startedVoice === null && supersededByNewChat) return false;
  if (!startedVoice) {
    setEmotion(mood.emotion, mood.gesture, mood);
  }
  updateDeveloperExchange(reply, speechText, data);
  thinkingIndicatorController?.hide?.(Number(data.clientChatGeneration || 0));
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
    const addedAssistantMessage = addMessage("assistant", reply, messageMeta);
    if (messageMeta.kind === "chat") chatController?.setPendingAssistant(addedAssistantMessage);
    else activeInteractionAssistantMessage = addedAssistantMessage;
  }
  const committedAssistantMessage = progressiveMessage?.message
    || (messageMeta.kind === "chat" ? chatController?.getPendingAssistant() : activeInteractionAssistantMessage);
  bindCompletedTurnIdentity(data, committedAssistantMessage);
  if (data.requiresUserResponse === true && incomingSource !== "touch") {
    markAssistantRequiresUserResponse(incomingSource);
  }
  if (messageMeta.kind === "proactive" && committedAssistantMessage) {
    committedAssistantMessage.meta.requiresUserResponse = data.requiresUserResponse === true;
    recordProactiveConversationEvent(committedAssistantMessage, mood);
  }
  if (
    messageMeta.kind === "chat"
    && chatController?.isCurrent(Number(data.clientChatGeneration || 0))
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
  // Scheduled execution ACK must follow the whole narration, including its tail.
  return data.source === "todo-reminder" && data.scheduledExecutionStatus && settings.voiceEnabled
    ? startedVoice === true : true;
}

function updateInteractionCoordinator(now = performance.now()) {
  if (speechPlayback.pipelineActive || speechPlayback.state.pending || isSpeaking(now)) return;
  const completedKind = speechPlayback.state.kind;
  if (completedKind !== "idle") {
    speechPlayback.markIdle();
    chatController?.clearPendingAssistant();
    activeInteractionAssistantMessage = null;
    if (completedKind === "chat") {
      resetActiveConversationRequestState();
    }
  }
  if (activeConversationInputs.length && !chatController?.isRequestInFlight()) {
    requestConversationReply();
    return;
  }
  if (chatController?.isRequestInFlight() || proactiveController?.isRequestInFlight()) return;
  if (touchInteractionController?.onInteractionIdle()) return;
}

function markActiveTouchInterrupted() {
  if (!activeInteractionAssistantMessage || activeInteractionAssistantMessage.meta?.kind !== "touch") return;
  activeInteractionAssistantMessage.meta.interrupted = true;
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
  return assistantPresentationController?.resolveSpeechPace?.(data, userMessage) || "normal";
}

function explicitlyRequestedSpeechPace(userMessage = "") {
  return assistantPresentationController?.explicitlyRequestedSpeechPace?.(userMessage) || false;
}

function resolveSpeechSpeed(data, userMessage = "", speechPace = "normal") {
  return assistantPresentationController?.resolveSpeechSpeed?.(data, userMessage, speechPace) ?? 1;
}

function chooseSpeechText(reply, spokenTextJa, speechLanguage = settings.speechLanguage) {
  return assistantPresentationController?.chooseSpeechText?.(reply, spokenTextJa, speechLanguage) ?? reply;
}

function normalizeVoiceIntensity(value) {
  return assistantPresentationController?.normalizeVoiceIntensity?.(value) || "normal";
}

function normalizeSpeechPace(value) {
  return assistantPresentationController?.normalizeSpeechPace?.(value) || "normal";
}

function speedForSpeechPace(pace) {
  return assistantPresentationController?.speedForSpeechPace?.(pace) ?? 1;
}

function resolveAssistantMood(data, userMessage, reply) {
  return assistantPresentationController?.resolveAssistantMood?.(data, userMessage, reply) || {
    emotion: "neutral",
    gesture: "idle",
    voiceStyle: "calm",
    voiceTone: "neutral",
    voiceIntensity: "normal",
    emotionIntensity: 1,
  };
}

function normalizeGesture(value) {
  const allowed = new Set(["idle", "nod", "tilt", "look_away", "glitch", "lean", "soften", "smile", "laugh", "tap", "boot", "recoil", "explain", "perk"]);
  return allowed.has(value) ? value : "idle";
}

function normalizeEmotion(value) {
  return emotionTargets[value] ? value : "neutral";
}

function speakPulse(length) { return speechPlayback.speakPulse(length); }

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
  messageView?.append(message);
  return { message, displayParts: [visibleContent], spokenParts: [] };
}

function appendProgressiveAssistantSegment(progress, content, spokenText, index, total) {
  if (!progress?.message) return;
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
  messageView?.updateProgressive(progress.message);
  persist();
  messageView?.scrollToLatest();
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
  messageView?.append(message);
  return message;
}

function renderMessages() {
  messageView?.render(state.messages.slice(-MAX_MESSAGES));
}

function openChatHistorySearch() {
  if (!dom.settingsModal?.classList.contains("hidden")) closeSettings();
  setChatCollapsed(false, { scroll: false });
  conversationPickerController?.close();
  bookmarksController?.close();
  chatHistorySearchController?.open();
}

function closeChatHistorySearch() {
  chatHistorySearchController?.close();
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

function hydrateConversationHistory() {
  return conversationStateController?.hydrate();
}

function invalidateConversationHistoryHydration() {
  return conversationStateController?.invalidateHydration();
}

// 仅向当前会话提供有效历史，排除连接错误提示及已废弃的固定兜底回复。
function cleanHistoryForModel() {
  return conversationStateController?.cleanHistoryForModel?.() || [];
}

function pruneStaleFallbacks() {
  return conversationStateController?.pruneStaleFallbacks?.();
}

function persist() {
  return conversationStateController?.persist?.();
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

// 只结束仍属于当前代次的语音任务，清理播放与嘴型状态，并保留失败原因供界面显示。
function failLocalSpeech(error, generation = speechPlayback.generation, title = "本地语音合成失败") {
  return speechPlayback.failLocalSpeech(error, generation, title);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function smoothstep(value) {
  const x = clamp(value, 0, 1);
  return x * x * (3 - 2 * x);
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
