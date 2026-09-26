(function attachMouthShapes(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusMouthShapes = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createMouthShapes() {
  "use strict";

  // 口型的无状态映射和插值。完整保留原始调校值，不拥有音频、时钟或播放状态；
  // 实时采样、对齐接管与边界收口由播放控制器负责。

  // 每次返回独立缓存；播放控制器持有并在原有重置时机替换它。
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

  // 对齐标记映射到已调校的类别；不在这里推算时间或音量。
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

  // 保留旧对齐标记的形状映射，数值属于现有角色的调校结果。
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

  // 没有对齐数据时使用原来的中日文字符回退规则。
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

  return {
    createAnimeMouthState,
    animeVisemeGroupForCue,
    animeShapeForGroup,
    mouthShapeDistance,
    lerpMouthShape,
    mouthShapeForCue,
    mouthShapeForChar,
  };
}));
