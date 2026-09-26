// Live2D 场景控制器唯一持有 PIXI/模型实例、模型注册表、当前 profile 与 fit 基准。
// 帧内表情、口型、背景和开发者参数写入仍由页面及各自控制器通过窄 port 驱动。
(function attachSceneController(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusSceneController = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createSceneControllerApi() {
  "use strict";

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function copyHome(value) {
    return {
      x: number(value?.x, 0),
      y: number(value?.y, 0),
      scale: Math.max(0.0001, number(value?.scale, 1)),
    };
  }

  function copyReference(value) {
    return {
      width: Math.max(0, number(value?.width, 0)),
      height: Math.max(0, number(value?.height, 0)),
      layoutMode: String(value?.layoutMode || ""),
    };
  }

  function validProfile(profile) {
    return Boolean(
      profile
      && typeof profile.id === "string"
      && profile.id
      && typeof profile.modelUrl === "string"
      && profile.modelUrl,
    );
  }

  function createSceneController(options = {}) {
    const runtime = options.runtime || {};
    const stage = options.stage || {};
    const modelPorts = options.model || {};
    const fitPorts = options.fit || {};
    const fallbackProfile = options.fallbackProfile || { id: "fallback", modelUrl: "" };
    const registryUrl = String(options.registryUrl || "/assets/live2d/models.json");
    const renderResolution = Math.max(1, number(options.renderResolution, 2));
    const getPixi = typeof options.getPixi === "function"
      ? options.getPixi
      : () => (typeof window !== "undefined" ? window.PIXI : globalThis.PIXI);
    const getWindow = typeof options.getWindow === "function"
      ? options.getWindow
      : () => (typeof window !== "undefined" ? window : globalThis);
    const request = typeof options.request === "function"
      ? options.request
      : (url, init) => {
          if (typeof fetch !== "function") throw new Error("Live2D scene transport is unavailable");
          return fetch(url, init);
        };
    const isDeveloper = typeof runtime.isDeveloper === "function"
      ? runtime.isDeveloper
      : () => false;
    const getPreviewModelId = typeof runtime.getPreviewModelId === "function"
      ? runtime.getPreviewModelId
      : () => "";
    const getSettings = typeof runtime.getSettings === "function"
      ? runtime.getSettings
      : () => ({});
    const setLive2DModelId = typeof runtime.setLive2DModelId === "function"
      ? runtime.setLive2DModelId
      : () => {};
    const getTransform = typeof fitPorts.getTransform === "function"
      ? fitPorts.getTransform
      : () => ({ scale: 1, offsetX: 0, offsetY: 0 });
    const getPortraitFraming = typeof fitPorts.fitPortrait === "function"
      ? fitPorts.fitPortrait
      : null;
    const getLayoutMode = typeof fitPorts.getLayoutMode === "function"
      ? fitPorts.getLayoutMode
      : () => String(getSettings()?.layoutMode || "lite");

    // Every mutable field below belongs to this controller. The page receives
    // snapshots so its render/update code cannot accidentally create a second owner.
    const state = {
      app: null,
      backgroundLayer: null,
      model: null,
      coreModel: null,
      modelGarmentRig: null,
      profiles: [fallbackProfile],
      overrides: {},
      activeProfile: fallbackProfile,
      modelHome: { x: 0, y: 0, scale: 1 },
      modelFitReference: { width: 0, height: 0, layoutMode: "" },
      status: "idle",
      registryGeneration: 0,
      startGeneration: 0,
    };

    function getState() {
      return {
        app: state.app,
        backgroundLayer: state.backgroundLayer,
        model: state.model,
        coreModel: state.coreModel,
        modelGarmentRig: state.modelGarmentRig,
        profiles: state.profiles.slice(),
        overrides: { ...state.overrides },
        activeProfile: state.activeProfile,
        modelHome: copyHome(state.modelHome),
        modelFitReference: copyReference(state.modelFitReference),
        status: state.status,
        registryGeneration: state.registryGeneration,
        startGeneration: state.startGeneration,
      };
    }

    function activeProfileFor(settings, previewModelId) {
      const profiles = state.profiles;
      return profiles.find(profile => profile.id === previewModelId)
        || profiles.find(profile => profile.id === settings?.live2dModelId)
        || profiles.find(profile => profile.id === fallbackProfile.id)
        || profiles[0]
        || fallbackProfile;
    }

    function notifyProfileChanged() {
      stage.onProfileChanged?.(getState());
    }

    async function loadRegistry() {
      const generation = state.registryGeneration + 1;
      state.registryGeneration = generation;
      let profiles = [fallbackProfile];
      let overrides = {};
      try {
        const response = await request(isDeveloper() ? "/api/developer/live2d-models" : registryUrl);
        const data = await response.json();
        const registry = isDeveloper() ? data?.registry : data;
        if (!response.ok || !Array.isArray(registry?.models)) {
          throw new Error(`HTTP ${response.status}`);
        }
        profiles = registry.models.filter(validProfile);
        if (!profiles.length) throw new Error("模型注册表为空");
        overrides = registry.overrides && typeof registry.overrides === "object"
          ? registry.overrides
          : {};
      } catch (error) {
        stage.warn?.("Live2D model registry unavailable:", error);
      }
      if (generation !== state.registryGeneration) return getState();
      state.profiles = profiles;
      state.overrides = overrides;
      const previewModelId = String(getPreviewModelId() || "");
      state.activeProfile = activeProfileFor(getSettings(), previewModelId);
      if (!previewModelId) setLive2DModelId(state.activeProfile.id);
      notifyProfileChanged();
      return getState();
    }

    function modelNaturalBounds() {
      const model = state.model;
      const view = getWindow();
      try {
        if (model && typeof model.getLocalBounds === "function") {
          const bounds = model.getLocalBounds();
          if (bounds.width > 0 && bounds.height > 0) {
            return {
              x: bounds.x,
              y: bounds.y,
              width: bounds.width,
              height: bounds.height,
            };
          }
        }
      } catch {
        // Fall back below when Cubism has not exposed bounds yet.
      }
      const scale = model?.scale?.x || 1;
      return {
        x: 0,
        y: 0,
        width: Math.max(1, (model?.width || view.innerWidth || 1) / scale),
        height: Math.max(1, (model?.height || view.innerHeight || 1) / scale),
      };
    }

    function modelDisplayBounds(natural) {
      const values = state.activeProfile?.displayBounds;
      if (!Array.isArray(values) || values.length !== 4) return natural;
      const x = clamp(number(values[0], 0), 0, 0.99);
      const y = clamp(number(values[1], 0), 0, 0.99);
      const width = clamp(number(values[2], 1), 0.01, 1 - x);
      const height = clamp(number(values[3], 1), 0.01, 1 - y);
      return {
        x: natural.x + natural.width * x,
        y: natural.y + natural.height * y,
        width: natural.width * width,
        height: natural.height * height,
      };
    }

    function fitModel() {
      stage.drawBackground?.();
      const model = state.model;
      if (!model) return null;
      const view = getWindow();
      const w = number(view.innerWidth, 1);
      const h = number(view.innerHeight, 1);
      const display = modelDisplayBounds(modelNaturalBounds());
      const settings = getSettings() || {};
      const transform = getTransform(state.activeProfile?.id) || {};
      const layoutMode = getLayoutMode();
      const full = layoutMode === "full";
      // Framing follows the window/layout only. The overlay conversation panel
      // can move, resize or collapse without shrinking or displacing the model.
      const portraitWidth = full ? Math.max(360, w - Math.min(460, Math.max(380, w * 0.38))) : w;
      const area = {
        x: 12,
        y: 12,
        width: Math.max(1, portraitWidth - 24),
        height: Math.max(1, h - 24),
      };
      if (
        state.modelFitReference.layoutMode !== layoutMode
        || state.modelFitReference.width <= 0
        || state.modelFitReference.height <= 0
      ) {
        state.modelFitReference = { width: w, height: h, layoutMode };
      }
      const offsetX = (number(transform.offsetX, 0) + number(state.activeProfile?.offsetX, 0))
        * w / state.modelFitReference.width;
      const offsetY = (number(transform.offsetY, 0) + number(state.activeProfile?.offsetY, 0))
        * h / state.modelFitReference.height;
      const profileScale = clamp(number(
        full ? state.activeProfile?.fullScaleMultiplier : state.activeProfile?.scaleMultiplier,
        full ? state.activeProfile?.scaleMultiplier || 1 : 1,
      ), 0.5, 2);
      const baseScale = Math.min(area.width / display.width, area.height / display.height);
      if (!getPortraitFraming) throw new Error("Portrait framing is unavailable");
      state.modelHome = copyHome(getPortraitFraming(
        display,
        area,
        baseScale * number(transform.scale, 1) * profileScale,
        offsetX,
        offsetY,
      ));
      model.scale?.set?.(state.modelHome.scale);
      model.x = state.modelHome.x;
      model.y = state.modelHome.y;
      return copyHome(state.modelHome);
    }

    async function start() {
      const generation = state.startGeneration + 1;
      state.startGeneration = generation;
      state.status = "loading";
      const PIXI = getPixi();
      if (!PIXI?.Application || !PIXI?.live2d?.Live2DModel?.from) {
        throw new Error("PIXI Live2D runtime is unavailable");
      }
      const view = getWindow();
      if (PIXI.settings) {
        PIXI.settings.RESOLUTION = renderResolution;
        PIXI.settings.ROUND_PIXELS = false;
        if (PIXI.SCALE_MODES) PIXI.settings.SCALE_MODE = PIXI.SCALE_MODES.LINEAR;
      }
      state.app = new PIXI.Application({
        view: stage.canvas,
        autoStart: true,
        resizeTo: view,
        backgroundColor: 0x091011,
        backgroundAlpha: 0,
        antialias: true,
        autoDensity: true,
        resolution: renderResolution,
      });
      state.backgroundLayer = new PIXI.Container();
      state.backgroundLayer.interactive = false;
      state.app.stage.addChild(state.backgroundLayer);
      stage.onStageCreated?.(getState());
      const profile = state.activeProfile;
      const loadedModel = await PIXI.live2d.Live2DModel.from(profile.modelUrl, { autoInteract: false });
      if (generation !== state.startGeneration) return getState();
      state.model = loadedModel;
      state.model.alpha = 0;
      if ("autoUpdate" in state.model) state.model.autoUpdate = false;
      state.coreModel = state.model.internalModel?.coreModel || null;
      state.modelGarmentRig = null;
      if (profile.garmentRigUrl && typeof modelPorts.installGarmentRig === "function") {
        const garmentResponse = await request(profile.garmentRigUrl);
        if (!garmentResponse.ok) throw new Error("Unable to load garment rig");
        state.modelGarmentRig = await modelPorts.installGarmentRig(
          state.model,
          await garmentResponse.json(),
        );
      }
      modelPorts.installPostMotionRetargeting?.(getState());
      if (getPreviewModelId()) {
        stage.exposeDebug?.(getState());
      }
      if (state.model.anchor && typeof state.model.anchor.set === "function") {
        state.model.anchor.set(0, 0);
      }
      state.model.interactive = true;
      state.model.buttonMode = true;
      state.app.stage.addChild(state.model);
      state.app.stage.interactive = true;
      state.app.stage.hitArea = state.app.screen;
      if (typeof stage.onPointerDown === "function") {
        state.app.stage.on("pointerdown", stage.onPointerDown);
      }
      fitModel();
      if (typeof stage.onTicker === "function") state.app.ticker.add(stage.onTicker);
      state.status = "ready";
      stage.onModelReady?.(getState());
      return getState();
    }

    function getProfileById(id) {
      return state.profiles.find(profile => profile.id === id) || null;
    }

    return Object.freeze({
      getState,
      getProfileById,
      loadRegistry,
      start,
      fitModel,
    });
  }

  return Object.freeze({ createSceneController });
}));
