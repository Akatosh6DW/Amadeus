// 原生 Live2D 表情/动作控制器唯一持有 profile 映射、重复动作冷却和最后一次交付状态。
// 页面只提供当前模型/profile 与时钟随机数；情绪与手势生命周期仍由 expression controller 拥有。
(function attachNativeMotionController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusNativeMotionController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createNativeMotionControllerApi() {
  "use strict";

  const DEFAULT_EXPRESSION_MAP = Object.freeze({
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
  });

  function createNativeMotionController(options = {}) {
    const getModel = typeof options.getModel === "function" ? options.getModel : () => null;
    const getProfile = typeof options.getProfile === "function" ? options.getProfile : () => null;
    const getRigModelId = typeof options.getRigModelId === "function" ? options.getRigModelId : () => "";
    const now = typeof options.now === "function" ? options.now : () => Date.now();
    const random = typeof options.random === "function" ? options.random : () => Math.random();
    const debug = typeof options.debug === "function" ? options.debug : (...args) => console.debug(...args);
    let lastExpression = "";
    let lastMotion = "";
    let lastMotionAt = 0;

    function applyExpression(emotion) {
      const model = getModel();
      const profile = getProfile() || {};
      if (!profile.supportsNativeExpressions) {
        lastExpression = "";
        return;
      }
      const profileExpressionMap = profile.expressionMap;
      const hasProfileExpression = profileExpressionMap
        && Object.prototype.hasOwnProperty.call(profileExpressionMap, emotion);
      const expressionName = hasProfileExpression
        ? profileExpressionMap[emotion]
        : DEFAULT_EXPRESSION_MAP[emotion] || "";
      if (!expressionName) {
        lastExpression = "";
        try {
          const manager = model?.internalModel?.motionManager?.expressionManager;
          if (manager && typeof manager.resetExpression === "function") manager.resetExpression();
        } catch {
          // Some Cubism runtimes do not expose expression reset.
        }
        return;
      }
      if (!model || lastExpression === expressionName) return;
      lastExpression = expressionName;
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
        debug("Live2D expression unavailable:", expressionName, error);
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
      else if (["shy", "flustered", "affectionate"].includes(emotion)) key = "shy";
      else if (["annoyed", "protective"].includes(emotion)) key = "angry";
      else if (emotion === "startled") key = "startled";
      else if (["concerned", "melancholic"].includes(emotion)) key = "sad";
      else if (["amused", "teasing", "laughing"].includes(emotion)) key = "happy";
      else if (["proud", "excited"].includes(emotion)) key = "proud";
      else if (["focused", "skeptical", "awkward"].includes(emotion)) key = "focused";
      const profile = getProfile() || {};
      const motionMap = profile.motionMap;
      const hasProfileMotion = Boolean(
        key && motionMap && Object.prototype.hasOwnProperty.call(motionMap, key)
      );
      if (hasProfileMotion) return motionMap[key] || "";
      if (getRigModelId() === "aipoet-kurisu" && random() < 0.35) {
        return `random${1 + Math.floor(random() * 5)}`;
      }
      return "";
    }

    function applyMotion(emotion, gesture) {
      const model = getModel();
      if (!model) return;
      const motionGroup = motionGroupForEmotion(emotion, gesture);
      if (!motionGroup) return;
      const currentTime = Number(now());
      if (lastMotion === motionGroup && currentTime - lastMotionAt < 1800) return;
      lastMotion = motionGroup;
      lastMotionAt = currentTime;
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
        debug("Live2D motion unavailable:", motionGroup, error);
      }
    }

    function getState() {
      return { lastExpression, lastMotion, lastMotionAt };
    }

    return Object.freeze({ applyExpression, applyMotion, motionGroupForEmotion, getState });
  }

  return Object.freeze({ DEFAULT_EXPRESSION_MAP, createNativeMotionController });
}));
