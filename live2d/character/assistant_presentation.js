// 助手表现规则控制器唯一持有情绪推断、语音语气/强度与语速约束；请求、播放和模型参数仍归各自 owner。
// 规则通过 expression/settings 窄 port 读取当前事实，不在页面或语音播放器复制一份表现状态。
(function attachAssistantPresentationController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusAssistantPresentation = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createAssistantPresentationApi() {
  "use strict";

  function createAssistantPresentationController(options = {}) {
    const getSettings = typeof options.getSettings === "function"
      ? options.getSettings
      : () => ({});
    const hasEmotion = typeof options.hasEmotion === "function"
      ? options.hasEmotion
      : () => true;
    const expressionDefaultsFor = typeof options.expressionDefaultsFor === "function"
      ? options.expressionDefaultsFor
      : () => ({ blushIntensity: 0, eyeContact: 0.6, gazeX: 0, gazeY: 0, tension: 0 });
    const nextGestureForEmotion = typeof options.nextGestureForEmotion === "function"
      ? options.nextGestureForEmotion
      : () => "idle";
    const speechLanguageProfile = typeof options.speechLanguageProfile === "function"
      ? options.speechLanguageProfile
      : value => ({ speechLanguage: String(value || "ja-JP") });
    const clamp = typeof options.clamp === "function"
      ? options.clamp
      : (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
    const toNumber = typeof options.toNumber === "function"
      ? options.toNumber
      : (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;

    function normalizeEmotion(value) {
      return hasEmotion(value) ? value : "neutral";
    }

    function normalizeGesture(value) {
      const allowed = new Set([
        "idle", "nod", "tilt", "look_away", "glitch", "lean", "soften",
        "smile", "laugh", "tap", "boot", "recoil", "explain", "perk",
      ]);
      return allowed.has(value) ? value : "idle";
    }

    function contextualGestureForEmotion(emotion, gesture) {
      const normalized = normalizeGesture(gesture);
      if (normalized !== "idle") return normalized;
      return nextGestureForEmotion(emotion, normalized) || normalized;
    }

    function normalizeVoiceTone(value) {
      const tone = String(value || "").toLowerCase();
      const allowed = new Set([
        "neutral", "dry", "gentle", "shy", "shy_annoyed", "flustered", "startled",
        "serious", "playful", "laughing", "comforting", "urgent", "warm", "cold",
      ]);
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
      const aliases = {
        low: "subtle", mild: "subtle", medium: "normal", moderate: "normal",
        high: "strong", intense: "strong",
      };
      const normalized = aliases[raw] || raw;
      return ["subtle", "normal", "strong"].includes(normalized) ? normalized : "normal";
    }

    function resolveVoiceIntensity(value, emotionIntensityValue, emotionDeltaValue = 0) {
      const effective = clamp(
        toNumber(emotionIntensityValue, 1) + toNumber(emotionDeltaValue, 0) * 0.65,
        0.2,
        2.5,
      );
      const inferred = effective <= 0.76 ? "subtle" : effective >= 1.08 ? "strong" : "normal";
      const raw = String(value ?? "").trim().toLowerCase();
      const aliases = {
        low: "subtle", mild: "subtle", medium: "normal", moderate: "normal",
        high: "strong", intense: "strong",
      };
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
      return ["very_slow", "slow", "normal", "fast", "very_fast"].includes(normalized)
        ? normalized
        : "normal";
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

    function resolveSpeechPace(data = {}, userMessage = "") {
      if (/慢慢念|慢慢说|慢点|慢一点|说慢|放慢|ゆっくり|slow/i.test(userMessage)) {
        return /慢慢|ゆっくり/i.test(userMessage) ? "very_slow" : "slow";
      }
      if (/快点|快一点|说快|加快|很快|超快|早口|fast/i.test(userMessage)) {
        return /很快|超快|早口/i.test(userMessage) ? "very_fast" : "fast";
      }
      const requested = normalizeSpeechPace(data.speechPace || data.speech_pace || "");
      if (requested !== "normal") return requested;
      return "normal";
    }

    function explicitlyRequestedSpeechPace(userMessage = "") {
      return /慢慢念|慢慢说|慢点|慢一点|说慢|放慢|快点|快一点|说快|加快|很快|超快|早口|ゆっくり|slow|fast/i.test(userMessage);
    }

    function resolveSpeechSpeed(data = {}, userMessage = "", speechPace = "normal") {
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

    function chooseSpeechText(reply, spokenTextJa, speechLanguage = getSettings()?.speechLanguage) {
      const provider = String(getSettings()?.ttsProvider || "").toLowerCase();
      if (provider === "gpt-sovits") {
        if (speechLanguageProfile(speechLanguage).speechLanguage === "zh-CN") {
          return String(reply || "").trim();
        }
        // Japanese mode consumes the aligned Japanese script and remains silent when alignment is missing.
        return String(spokenTextJa || "").trim();
      }
      return reply;
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

    function resolveAssistantMood(data = {}, userMessage = "", reply = "") {
      // 后端给出合法情绪时保持其标签；只有缺失标签才使用文字特征补充表现。
      const hasExplicitEmotionLabel = typeof data.emotion === "string" && hasEmotion(data.emotion);
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
        deliveryMode === "outburst" ? 2.5 : 1.25,
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

      return {
        emotion,
        gesture,
        voiceStyle,
        voiceTone,
        voiceIntensity: resolveVoiceIntensity(requestedVoiceIntensity, emotionIntensityValue, emotionDeltaValue),
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

    return Object.freeze({
      resolveAssistantMood,
      resolveSpeechPace,
      resolveSpeechSpeed,
      explicitlyRequestedSpeechPace,
      chooseSpeechText,
      normalizeVoiceTone,
      normalizeVoiceIntensity,
      normalizeSpeechPace,
      speedForSpeechPace,
    });
  }

  return Object.freeze({ createAssistantPresentationController });
}));
