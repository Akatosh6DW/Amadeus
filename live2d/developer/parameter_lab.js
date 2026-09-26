// 开发者参数实验台唯一持有参数覆盖、标定草稿和监督样本编辑状态。
// 页面只提供模型/设置/存储等窄 port；release 门控与 storageScopedFetch 仍在此边界内执行，避免训练状态回流到整页全局。
(function attachParameterLab(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusParameterLab = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createParameterLabApi() {
  "use strict";

  function createParameterLabController(options = {}) {
    const dom = options.dom || {};
    const document = options.document || globalThis.document;
    const window = options.window || globalThis;
    const sceneSnapshot = typeof options.sceneSnapshot === "function"
      ? options.sceneSnapshot
      : () => ({});
    const setParam = typeof options.setParam === "function" ? options.setParam : () => {};
    const mappedParameterIds = typeof options.mappedParameterIds === "function"
      ? options.mappedParameterIds
      : () => [];
    const effectiveVisualEmotion = typeof options.effectiveVisualEmotion === "function"
      ? options.effectiveVisualEmotion
      : () => "neutral";
    const expressionSnapshot = typeof options.expressionSnapshot === "function"
      ? options.expressionSnapshot
      : () => ({ currentEmotion: "neutral", currentEmotionWeight: 0 });
    const isDeveloper = typeof options.isDeveloper === "function" ? options.isDeveloper : () => false;
    const storageScopedFetch = typeof options.storageScopedFetch === "function"
      ? options.storageScopedFetch
      : (...args) => (window.fetch ? window.fetch(...args) : Promise.reject(new Error("fetch is unavailable")));
    const fetch = typeof options.fetch === "function"
      ? options.fetch
      : (...args) => (window.fetch ? window.fetch(...args) : Promise.reject(new Error("fetch is unavailable")));
    const toast = typeof options.toast === "function" ? options.toast : () => {};
    const resetRenderParameters = typeof options.resetRenderParameters === "function"
      ? options.resetRenderParameters
      : () => {};
    const getRuntimeAuthorBlushOpacity = typeof options.getRuntimeAuthorBlushOpacity === "function"
      ? options.getRuntimeAuthorBlushOpacity
      : () => 1;
    const getRuntimePupilScaleValues = typeof options.getRuntimePupilScaleValues === "function"
      ? options.getRuntimePupilScaleValues
      : () => ({ left: 1, right: 1 });
    const authorFaceLayerIds = Array.isArray(options.authorFaceLayerIds)
      ? options.authorFaceLayerIds
      : ["Param3", "Param4", "Param5", "Param6", "Param7", "Param8"];
    const trainingLayerByEmotion = options.trainingLayerByEmotion || {};
    const trainingLayerWarnings = options.trainingLayerWarnings || {};
    const trainingLayerChannels = options.trainingLayerChannels || {};
    const trainingEmotionLabels = options.trainingEmotionLabels || {};
    const trainingStrengths = options.trainingStrengths || [];
    const trainingParameterGroups = options.trainingParameterGroups || [];
    const trainingUnboundIds = options.trainingUnboundIds instanceof Set
      ? options.trainingUnboundIds
      : new Set();
    const clamp = typeof options.clamp === "function"
      ? options.clamp
      : (value, min, max) => Math.max(min, Math.min(max, value));
    const toNumber = typeof options.toNumber === "function"
      ? options.toNumber
      : (value, fallback) => {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
      };
    const getSettings = typeof options.getSettings === "function"
      ? options.getSettings
      : () => options.settings || {};

    const AUTHOR_FACE_LAYER_IDS = authorFaceLayerIds;
    const EXPRESSION_TRAINING_LAYER_BY_EMOTION = trainingLayerByEmotion;
    const EXPRESSION_TRAINING_LAYER_WARNINGS = trainingLayerWarnings;
    const EXPRESSION_TRAINING_LAYER_CHANNELS = trainingLayerChannels;
    const EXPRESSION_TRAINING_EMOTION_LABELS = trainingEmotionLabels;
    const EXPRESSION_TRAINING_STRENGTHS = trainingStrengths;
    const EXPRESSION_TRAINING_PARAMETER_GROUPS = trainingParameterGroups;
    const EXPRESSION_TRAINING_UNBOUND_IDS = trainingUnboundIds;

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
    let developerAuthorBlushOpacityOverride = null;
    const developerPupilScaleOverrides = { left: null, right: null };

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
    sceneSnapshot().coreModel?.getDrawableIndex?.("ArtMesh60") >= 0
    && sceneSnapshot().coreModel?.getDrawableIndex?.("ArtMesh98") >= 0
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
  const ids = sceneSnapshot().coreModel?._parameterIds;
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
  if (!isDeveloper() || !sceneSnapshot().activeProfile?.modelUrl) return;
  try {
    const modelResponse = await fetch(sceneSnapshot().activeProfile.modelUrl, { cache: "no-store" });
    if (!modelResponse.ok) throw new Error(`model3 HTTP ${modelResponse.status}`);
    const modelJson = await modelResponse.json();
    const displayInfo = String(modelJson?.FileReferences?.DisplayInfo || "").trim();
    if (!displayInfo) return;
    const displayUrl = new URL(displayInfo, new URL(sceneSnapshot().activeProfile.modelUrl, window.location.href));
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
  if (!sceneSnapshot().coreModel || typeof sceneSnapshot().coreModel.getParameterCount !== "function") return [];
  const count = Math.max(0, Number(sceneSnapshot().coreModel.getParameterCount()) || 0);
  const parameters = [];
  for (let index = 0; index < count; index += 1) {
    try {
      const id = coreParameterIdAt(index);
      const minimum = Number(sceneSnapshot().coreModel.getParameterMinimumValue(index));
      const maximum = Number(sceneSnapshot().coreModel.getParameterMaximumValue(index));
      const defaultValue = Number(sceneSnapshot().coreModel.getParameterDefaultValue(index));
      const currentValue = Number(sceneSnapshot().coreModel.getParameterValueByIndex(index));
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
  if (!isDeveloper() || !dom.devParameterControls) return;
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
  if (!isDeveloper() || !sceneSnapshot().coreModel) {
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
    sceneSnapshot().coreModel.setParameterValueByIndex(parameter.index, clamped);
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
  resetRenderParameters();
  if (dom.devParameterStatus) dom.devParameterStatus.textContent = "已清除全部手动覆盖，自动情绪与动作重新接管模型。";
  window.setTimeout(refreshDeveloperParameterControls, 120);
}

function applyDeveloperParameterOverrides() {
  if (!isDeveloper() || !developerParameterLock || !sceneSnapshot().coreModel) return;
  developerParameterOverrides.forEach(({ index, value }, id) => {
    try {
      sceneSnapshot().coreModel.setParameterValueByIndex(index, value);
    } catch {
      setParam(id, value);
    }
  });
}

function applyDeveloperDrawableOverrides() {
  if (!sceneSnapshot().coreModel) return;
  const rawModel = typeof sceneSnapshot().coreModel.getModel === "function" ? sceneSnapshot().coreModel.getModel() : null;
  const drawables = rawModel?.drawables;
  if (!drawables) return;
  const authorBlushOpacity = isDeveloper() && developerAuthorBlushOpacityOverride !== null
    ? developerAuthorBlushOpacityOverride
    : getRuntimeAuthorBlushOpacity();
  if (authorBlushOpacity < 0.9999) {
    ["lianhong", "lianhong2"].forEach(id => {
      const index = sceneSnapshot().coreModel.getDrawableIndex(id);
      if (index >= 0) {
        drawables.opacities[index] *= clamp(toNumber(authorBlushOpacity, 1), 0, 1);
      }
    });
  }
  [
    [
      "ArtMesh60",
      isDeveloper() && developerPupilScaleOverrides.left !== null
        ? developerPupilScaleOverrides.left
        : getRuntimePupilScaleValues().left,
    ],
    [
      "ArtMesh98",
      isDeveloper() && developerPupilScaleOverrides.right !== null
        ? developerPupilScaleOverrides.right
        : getRuntimePupilScaleValues().right,
    ],
  ].forEach(([id, rawScale]) => {
    const index = sceneSnapshot().coreModel.getDrawableIndex(id);
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
  sceneSnapshot().modelGarmentRig?.update();
}

function developerCaptureCanvas() {
  const scene = sceneSnapshot();
  const extract = scene.app?.renderer?.plugins?.extract || scene.app?.renderer?.extract;
  if (extract && typeof extract.canvas === "function") {
    try {
      return extract.canvas(scene.app.stage);
    } catch (error) {
      console.debug("PIXI stage extraction failed:", error);
    }
  }
  return dom.canvas;
}

async function captureDeveloperParameterSnapshot() {
  if (!isDeveloper() || !sceneSnapshot().coreModel) {
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
        foregroundEmotion: expressionSnapshot().currentEmotion,
        emotionIntensity: expressionSnapshot().currentEmotionWeight,
        modelId: sceneSnapshot().activeProfile?.id || "",
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
  const modelId = sceneSnapshot().activeProfile?.id || "";
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
  getSettings().expressionRuntime = developerExpressionRuntime;
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
  if (!isDeveloper() || developerExpressionTrainingLoaded) {
    renderDeveloperExpressionTrainingGallery();
    return;
  }
  if (!sceneSnapshot().coreModel) {
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
  if (!isDeveloper() || !dom.devTrainingGallery) return;
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
  if (!isDeveloper() || !sceneSnapshot().coreModel) return;
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
    sceneSnapshot().coreModel.update();
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
    dom.devTrainingActivateBtn.disabled = !sample || sample.modelId !== (sceneSnapshot().activeProfile?.id || "");
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
      sceneSnapshot().coreModel.update();
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
    sceneSnapshot().coreModel?._drawableIds,
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
  const rawModel = typeof sceneSnapshot().coreModel?.getModel === "function" ? sceneSnapshot().coreModel.getModel() : null;
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
    return toNumber(sceneSnapshot().coreModel.getParameterValueByIndex(parameter.index), parameter.currentValue);
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
  if (!sample || !sceneSnapshot().coreModel || !dom.devTrainingEffectiveOutputs) return;
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
  sample.modelId = sceneSnapshot().activeProfile?.id || sample.modelId || "";
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
        modelId: sample.modelId || sceneSnapshot().activeProfile?.id || "",
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
  if (!sceneSnapshot().coreModel) {
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
    modelId: sceneSnapshot().activeProfile?.id || "",
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


    function bind() {
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
      [
        dom.devTrainingLabelInput,
        dom.devTrainingEmotionInput,
        dom.devTrainingStrengthInput,
        dom.devTrainingTagsInput,
        dom.devTrainingNotesInput,
      ].forEach(input => input?.addEventListener("input", markDeveloperExpressionTrainingDirty));
      dom.devTrainingIntensityInput?.addEventListener("input", updateDeveloperExpressionTrainingIntensity);
    }

    return Object.freeze({
      bind,
      loadParameterDisplayInfo: loadDeveloperParameterDisplayInfo,
      refreshParameters: refreshDeveloperParameterControls,
      applyParameterOverrides: applyDeveloperParameterOverrides,
      applyDrawableOverrides: applyDeveloperDrawableOverrides,
      reset: resetDeveloperParameterControls,
      captureSnapshot: captureDeveloperParameterSnapshot,
      openCaptureDirectory: openDeveloperParameterCaptureDirectory,
      loadTraining: loadDeveloperExpressionTraining,
      renderTrainingGallery: renderDeveloperExpressionTrainingGallery,
      closeEditor: closeDeveloperExpressionTrainingEditor,
      setTrainingEditorOpen: setDeveloperExpressionTrainingView,
      getExpressionRuntime: () => developerExpressionTrainingLoaded ? developerExpressionRuntime : null,
      isTrainingLoaded: () => developerExpressionTrainingLoaded,
    });
  }

  return { createParameterLabController };
}));
