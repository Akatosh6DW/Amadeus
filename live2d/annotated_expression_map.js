(function exposeAnnotatedExpressionMap(globalObject) {
  "use strict";

  const LAYER_IDS = Object.freeze([
    "Param3", "Param4", "Param5", "Param6", "Param7", "Param8",
  ]);

  const MIRRORED_PAIRS = Object.freeze([
    ["ParamEyeLOpen", "ParamEyeROpen"],
    ["ParamEyeLSmile", "ParamEyeRSmile"],
    ["ParamBrowLY", "ParamBrowRY"],
    ["ParamBrowLX", "ParamBrowRX"],
    ["ParamBrowLAngle", "ParamBrowRAngle"],
    ["ParamBrowLForm", "ParamBrowRForm"],
  ]);

  const MIRRORED_SIGN_IDS = Object.freeze([
    "ParamEyeBallX",
    "ParamAngleX",
    "ParamAngleZ",
    "ParamBodyAngleX",
    "ParamBodyAngleZ",
    "Param14",
  ]);

  function clampValue(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function smoothstep(value) {
    const normalized = clampValue(Number(value) || 0, 0, 1);
    return normalized * normalized * (3 - 2 * normalized);
  }

  function sampleCurve(points, input, fallback = 0) {
    const valid = (Array.isArray(points) ? points : [])
      .filter(point => Array.isArray(point) && point.length >= 2)
      .map(point => [Number(point[0]), Number(point[1])])
      .filter(point => Number.isFinite(point[0]) && Number.isFinite(point[1]))
      .sort((left, right) => left[0] - right[0]);
    if (!valid.length) return fallback;
    if (input <= valid[0][0]) return valid[0][1];
    for (let index = 1; index < valid.length; index += 1) {
      const left = valid[index - 1];
      const right = valid[index];
      if (input > right[0]) continue;
      const progress = smoothstep(
        (input - left[0]) / Math.max(0.0001, right[0] - left[0])
      );
      return left[1] + (right[1] - left[1]) * progress;
    }
    return valid[valid.length - 1][1];
  }

  function freezeVariant(variant) {
    return Object.freeze({
      id: String(variant.id || "default"),
      weight: Math.max(0.0001, Number(variant.weight) || 1),
      layers: Object.freeze({ ...(variant.layers || {}) }),
      pupilScales: Array.isArray(variant.pupilScales)
        ? Object.freeze([
          clampValue(Number(variant.pupilScales[0]) || 1, 0.35, 1.35),
          clampValue(Number(variant.pupilScales[1]) || 1, 0.35, 1.35),
        ])
        : null,
      mouthForm: Number.isFinite(Number(variant.mouthForm))
        ? clampValue(Number(variant.mouthForm), -1, 1)
        : null,
    });
  }

  function freezeProfile(profile) {
    return Object.freeze({
      mirrorable: profile.mirrorable !== false,
      mouthCurve: Object.freeze((profile.mouthCurve || []).map(point => Object.freeze([...point]))),
      mouthOpenCurve: Object.freeze((profile.mouthOpenCurve || []).map(point => Object.freeze([...point]))),
      layerCurves: Object.freeze(Object.fromEntries(
        Object.entries(profile.layerCurves || {}).map(([id, points]) => [
          id,
          Object.freeze(points.map(point => Object.freeze([...point]))),
        ])
      )),
      pupilCurves: Object.freeze((profile.pupilCurves || []).map(
        points => Object.freeze(points.map(point => Object.freeze([...point])))
      )),
      authorBlushOpacityCurve: Object.freeze(
        (profile.authorBlushOpacityCurve || []).map(point => Object.freeze([...point]))
      ),
      variants: Object.freeze((profile.variants || []).map(freezeVariant)),
    });
  }

  // These values are distilled from the user's saved expression-training
  // revisions. The original subtle/typical/strong labels are deliberately not
  // treated as a single linear strength axis: several edited samples are style
  // variants, and the visually strongest angry mouth is near -0.34 rather than
  // the author's numeric minimum.
  const PROFILES = Object.freeze(Object.fromEntries(Object.entries({
    neutral: {
      mirrorable: false,
      mouthCurve: [[0.15, 0.18], [1.55, 0.18]],
    },
    affectionate: {
      mouthCurve: [[0.15, 0.18], [0.38, 0.82], [0.45, 1], [1.55, 1]],
      mouthOpenCurve: [[0.15, 0], [0.45, 0.2], [1.55, 0.52]],
      variants: [
        { id: "warm", layers: { Param7: 1 }, pupilScales: [1, 1], mouthForm: 1 },
      ],
    },
    amused: {
      mouthCurve: [[0.15, 0.18], [0.45, 0.91], [0.9, 1], [1.55, 1]],
      mouthOpenCurve: [[0.15, 0], [1.55, 0]],
      layerCurves: {
        Param4: [[0.15, 0], [0.38, 1], [1.55, 1]],
        Param7: [[0.15, 0], [0.9, 0], [1.55, 0.225]],
      },
      pupilCurves: [
        [[0.15, 1], [0.45, 0.88], [0.95, 0.815], [1.55, 0.94]],
        [[0.15, 1], [0.45, 0.88], [0.95, 0.815], [1.55, 0.94]],
      ],
    },
    laughing: {
      mouthCurve: [[0.15, 0.18], [0.38, 0.96], [0.55, 1], [1.55, 1]],
      mouthOpenCurve: [[0.15, 0], [0.45, 0.635], [0.9, 1], [1.55, 1]],
      layerCurves: {
        Param4: [[0.15, 0], [0.34, 1], [1.55, 1]],
        Param7: [[0.15, 0], [0.45, 0.335], [1.55, 0.43]],
      },
    },
    excited: {
      mouthCurve: [[0.15, 0.18], [0.45, 0.92], [0.9, 1], [1.55, 1]],
      mouthOpenCurve: [[0.15, 0], [0.45, 0.365], [0.9, 0.22], [1.55, 0.805]],
      layerCurves: {
        Param4: [[0.15, 0], [0.36, 1], [1.55, 1]],
        Param7: [[0.15, 0], [0.45, 0.26], [1.55, 0.215]],
        Param8: [[0.15, 0], [1.0, 0], [1.55, 0.325]],
      },
    },
    teasing: {
      variants: [
        {
          id: "dry-sly",
          weight: 0.48,
          layers: { Param4: 0.045, Param5: 0.385 },
          pupilScales: [0.9, 1],
          mouthForm: 0.89,
        },
        {
          id: "bright-sly",
          weight: 0.52,
          layers: { Param4: 0.52, Param5: 0.33 },
          pupilScales: [0.94, 1],
          mouthForm: 1,
        },
      ],
    },
    proud: {
      variants: [
        {
          id: "warm-pride",
          weight: 0.3,
          layers: { Param4: 0.895, Param5: 0.55 },
          pupilScales: [0.865, 1],
          mouthForm: 1,
        },
        {
          id: "smug-pride",
          weight: 0.44,
          layers: { Param4: 0.065, Param5: 1, Param7: 0.215 },
          pupilScales: [1, 0.865],
          mouthForm: 0.82,
        },
        {
          id: "firm-pride",
          weight: 0.26,
          layers: { Param5: 1 },
          pupilScales: [0.895, 1],
          mouthForm: 1,
        },
      ],
    },
    annoyed: {
      // Mild irritation and restrained strong annoyance use different portions
      // of the rig. The semantic peak stays around -0.34 instead of falling to
      // the visually misleading ParamMouthForm=-1 endpoint.
      mouthCurve: [[0.15, 0.18], [0.38, -0.48], [0.78, -0.34], [1.55, -0.34]],
      mouthOpenCurve: [[0.15, 0], [0.75, 0], [1.0, 0.14], [1.55, 0.08]],
      layerCurves: {
        Param5: [[0.15, 0], [0.36, 1], [1.55, 1]],
        Param6: [[0.15, 0], [1.05, 0], [1.55, 0.075]],
      },
    },
    furious: {
      mirrorable: false,
      mouthCurve: [[0.15, 0.18], [0.42, -0.245], [0.82, -0.34], [1.55, -0.35]],
      mouthOpenCurve: [[0.15, 0], [1.55, 0]],
      layerCurves: {
        Param5: [[0.15, 0], [0.32, 1], [1.55, 1]],
      },
      pupilCurves: [
        [[0.15, 1], [0.55, 0.935], [1.55, 1]],
        [[0.15, 1], [0.55, 0.935], [1.55, 1]],
      ],
    },
    shy: {
      // User-supervised shy samples are a non-linear progression rather than
      // interchangeable random styles. The re-edited typical sample is the
      // stable centre. Pupil contraction and the surprise layer are reserved
      // for the outburst-only extreme segment (for example, a direct proposal).
      mouthCurve: [
        [0.15, 0.18],
        [0.45, 0.1584],
        [1, 0.12],
        [1.28, 0.12],
        [1.55, 0.44],
      ],
      mouthOpenCurve: [
        [0.15, 0],
        [0.45, 0.009],
        [1, 0.025],
        [1.28, 0.025],
        [1.55, 0.028],
      ],
      layerCurves: {
        Param3: [[0.15, 0], [0.45, 0.635], [1, 0.5], [1.28, 0.5], [1.55, 1]],
        Param4: [[0.15, 0], [0.45, 0.585], [1, 0.605], [1.28, 0.605], [1.55, 0]],
        Param7: [[0.15, 0], [0.45, 0.28], [1, 0.315], [1.28, 0.315], [1.55, 0]],
        Param8: [[0.15, 0], [1.32, 0], [1.55, 0.08]],
      },
      pupilCurves: [
        [[0.15, 1], [1.32, 1], [1.55, 0.835]],
        [[0.15, 1], [1.32, 1], [1.55, 0.82]],
      ],
      authorBlushOpacityCurve: [
        [0.15, 1],
        [0.45, 1],
        [0.9, 0.18],
        [1, 0],
        [1.28, 0],
        [1.55, 1],
      ],
    },
    flustered: {
      mouthOpenCurve: [[0.15, 0], [0.72, 0.09], [1.55, 0.101]],
      pupilCurves: [
        [[0.15, 1], [1.32, 1], [1.55, 0.838]],
        [[0.15, 1], [1.32, 1], [1.55, 0.838]],
      ],
      variants: [
        {
          id: "rattled",
          weight: 0.56,
          layers: { Param3: 0.315, Param7: 0.555 },
          mouthForm: 0.34,
        },
        {
          id: "averted",
          weight: 0.44,
          layers: { Param3: 0.52, Param7: 0.345 },
          mouthForm: 0.33,
        },
      ],
    },
    concerned: {
      mouthCurve: [[0.15, 0.18], [0.45, 0.17], [0.82, -0.26], [1.55, -0.37]],
      mouthOpenCurve: [[0.15, 0], [1.55, 0]],
      layerCurves: {
        Param6: [[0.15, 0], [0.38, 1], [1.55, 1]],
      },
      pupilCurves: [
        [[0.15, 1], [0.9, 0.92], [1.55, 0.885]],
        [[0.15, 1], [0.9, 0.92], [1.55, 0.905]],
      ],
    },
    melancholic: {
      mouthCurve: [[0.15, 0.18], [0.42, -0.52], [0.9, -0.44], [1.55, -0.34]],
      mouthOpenCurve: [[0.15, 0], [1.55, 0]],
      layerCurves: {
        Param6: [[0.15, 0], [0.38, 1], [1.55, 1]],
      },
      pupilCurves: [
        [[0.15, 1], [0.75, 0.845], [1.55, 0.85]],
        [[0.15, 1], [0.75, 0.845], [1.55, 0.85]],
      ],
    },
    skeptical: {
      mouthCurve: [[0.15, 0.18], [0.7, -0.62], [1.55, -0.72]],
      variants: [
        { id: "side-eye", pupilScales: [0.865, 1] },
      ],
    },
    protective: {
      mouthCurve: [[0.15, 0.18], [0.72, -0.34], [1.55, -0.38]],
      layerCurves: {
        Param5: [[0.15, 0], [0.55, 0.22], [1.55, 0.42]],
      },
    },
    startled: {
      mouthCurve: [[0.15, 0.18], [0.48, -0.12], [1.55, -0.12]],
      mouthOpenCurve: [[0.15, 0], [0.48, 0.1], [1.55, 0.18]],
      layerCurves: {
        Param8: [[0.15, 0], [0.34, 1], [1.55, 1]],
      },
    },
  }).map(([emotion, profile]) => [emotion, freezeProfile(profile)])));

  function selectVariant(profile, variantUnit) {
    if (!profile?.variants?.length) return null;
    const total = profile.variants.reduce((sum, variant) => sum + variant.weight, 0);
    let cursor = clampValue(Number(variantUnit) || 0, 0, 0.999999) * total;
    for (const variant of profile.variants) {
      cursor -= variant.weight;
      if (cursor < 0) return variant;
    }
    return profile.variants[profile.variants.length - 1];
  }

  function plan(emotion, visualIntensity, lifecycle = 1, variantUnit = 0.5, mirrorSide = 1) {
    const profile = PROFILES[String(emotion || "")];
    const life = smoothstep(lifecycle);
    if (!profile) {
      return {
        emotion: String(emotion || "neutral"),
        styleId: "fallback",
        mirrorSide: 1,
        layers: Object.fromEntries(LAYER_IDS.map(id => [id, 0])),
        mouthForm: null,
        mouthOpenY: null,
        pupilScales: [1, 1],
        authorBlushOpacity: 1,
      };
    }

    const intensity = clampValue(Number(visualIntensity) || 0.7, 0.15, 1.55);
    // Classification layers need to become readable early; expressive strength
    // is then carried by the mouth, eyes, pose and motion rather than by forcing
    // every layer through the same linear multiplier.
    const layerActivation = smoothstep((intensity - 0.15) / 0.31) * life;
    const variant = selectVariant(profile, variantUnit);
    const layers = Object.fromEntries(LAYER_IDS.map(id => [id, 0]));
    for (const id of LAYER_IDS) {
      let target = sampleCurve(profile.layerCurves[id], intensity, 0);
      if (variant && Object.prototype.hasOwnProperty.call(variant.layers, id)) {
        target = Number(variant.layers[id]) || 0;
      }
      layers[id] = clampValue(target * layerActivation, 0, 1);
    }

    let mouthTarget = sampleCurve(profile.mouthCurve, intensity, Number.NaN);
    if (variant?.mouthForm !== null && !Number.isFinite(mouthTarget)) {
      mouthTarget = variant.mouthForm;
    }
    const mouthForm = Number.isFinite(mouthTarget)
      ? clampValue(0.18 + (mouthTarget - 0.18) * life, -1, 1)
      : null;
    const mouthOpenTarget = sampleCurve(profile.mouthOpenCurve, intensity, Number.NaN);
    const mouthOpenY = Number.isFinite(mouthOpenTarget)
      ? clampValue(mouthOpenTarget * life, 0, 1)
      : null;

    let pupilScales = [0, 1].map(index => {
      let target = sampleCurve(profile.pupilCurves[index], intensity, 1);
      if (variant?.pupilScales) target = variant.pupilScales[index];
      return clampValue(1 + (target - 1) * life, 0.35, 1.35);
    });
    const resolvedMirrorSide = profile.mirrorable && Number(mirrorSide) < 0 ? -1 : 1;
    if (resolvedMirrorSide < 0) pupilScales = [pupilScales[1], pupilScales[0]];
    const authorBlushOpacityTarget = sampleCurve(
      profile.authorBlushOpacityCurve,
      intensity,
      1,
    );
    const authorBlushOpacity = clampValue(
      1 + (authorBlushOpacityTarget - 1) * life,
      0,
      1,
    );

    return {
      emotion: String(emotion || "neutral"),
      styleId: variant?.id || "curve",
      mirrorSide: resolvedMirrorSide,
      layers,
      mouthForm,
      mouthOpenY,
      pupilScales,
      authorBlushOpacity,
    };
  }

  function mirrorParameters(parameters, emotion, mirrorSide) {
    const profile = PROFILES[String(emotion || "")];
    if (!profile?.mirrorable || Number(mirrorSide) >= 0) return { ...parameters };
    const mirrored = { ...parameters };
    for (const [leftId, rightId] of MIRRORED_PAIRS) {
      const left = mirrored[leftId];
      const right = mirrored[rightId];
      if (left !== undefined || right !== undefined) {
        mirrored[leftId] = right ?? left;
        mirrored[rightId] = left ?? right;
      }
    }
    for (const id of MIRRORED_SIGN_IDS) {
      if (mirrored[id] !== undefined) mirrored[id] = -Number(mirrored[id] || 0);
    }
    return mirrored;
  }

  const api = Object.freeze({
    LAYER_IDS,
    PROFILES,
    plan,
    mirrorParameters,
    sampleCurve,
  });

  globalObject.AmadeusAnnotatedExpressionMap = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : window);
