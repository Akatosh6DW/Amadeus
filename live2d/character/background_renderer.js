// 背景渲染器唯一持有背景图像的 PIXI 子节点、贴图 fit 回调和缓存代次。
// 设置页仍拥有草稿/保存事务；控制器只接收已生效背景快照并把它绘制到场景层。
(function attachBackgroundRenderer(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AmadeusBackgroundRenderer = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function createBackgroundRendererApi() {
  "use strict";

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function createBackgroundRenderer(options = {}) {
    const bootBackgroundUrl = String(options.bootBackgroundUrl || "");
    const getPixi = typeof options.getPixi === "function"
      ? options.getPixi
      : () => (typeof window !== "undefined" ? window.PIXI : globalThis.PIXI);
    const getWindow = typeof options.getWindow === "function"
      ? options.getWindow
      : () => (typeof window !== "undefined" ? window : globalThis);
    const getApp = typeof options.getApp === "function" ? options.getApp : () => null;
    const getLayer = typeof options.getLayer === "function" ? options.getLayer : () => null;
    const getPresetUrl = typeof options.getPresetUrl === "function" ? options.getPresetUrl : () => "";
    const isKnownPreset = typeof options.isKnownPreset === "function"
      ? options.isKnownPreset
      : () => true;
    const getView = typeof options.getView === "function" ? options.getView : () => ({});
    const state = {
      revision: 0,
      image: null,
    };

    function imageUrl(source = {}) {
      const preset = String(source.backgroundPreset || "");
      if (preset === "boot") return bootBackgroundUrl;
      if (preset === "custom" && source.customBackgroundPath) {
        return source.previewUrl || `/api/background-image?v=${state.revision}`;
      }
      return String(getPresetUrl(preset) || "");
    }

    function bumpRevision() {
      state.revision += 1;
      return state.revision;
    }

    function resetMotion() {
      const layer = getLayer();
      if (!layer) return;
      layer.x = 0;
      layer.y = 0;
      layer.rotation = 0;
    }

    function drawCoverImage(url, width, height, alpha = 1) {
      const PIXI = getPixi();
      const layer = getLayer();
      if (!PIXI || !layer || !url) return;
      const texture = PIXI.Texture.from(url);
      const sprite = new PIXI.Sprite(texture);
      const base = layer.children?.[0];
      const shade = new PIXI.Graphics();
      sprite.anchor.set(0.5);
      sprite.alpha = alpha;
      const fit = () => {
        if (sprite.destroyed) return;
        // 图片可能在窗口改变尺寸后才完成加载，因此每次 fit 都读取最新视口。
        const view = getWindow();
        const currentWidth = number(view.innerWidth, width);
        const currentHeight = number(view.innerHeight, height);
        const textureWidth = texture.orig?.width
          || texture.baseTexture?.realWidth
          || texture.width
          || currentWidth;
        const textureHeight = texture.orig?.height
          || texture.baseTexture?.realHeight
          || texture.height
          || currentHeight;
        const position = typeof options.layout === "function"
          ? options.layout(textureWidth, textureHeight, currentWidth, currentHeight, getView())
          : { width: currentWidth, height: currentHeight, x: 0, y: 0 };
        sprite.width = position.width;
        sprite.height = position.height;
        sprite.x = position.x + position.width * 0.5;
        sprite.y = position.y + position.height * 0.5;
        if (base?.clear && base?.beginFill && base?.drawRect && base?.endFill) {
          base.clear();
          base.beginFill(0x071011, 1);
          base.drawRect(0, 0, currentWidth, currentHeight);
          base.endFill();
        }
        shade.clear();
        shade.beginFill(0x020606, 0.18);
        shade.drawRect(0, 0, currentWidth, currentHeight);
        shade.endFill();
      };
      state.image = { url, fit };
      fit();
      if (!texture.valid && texture.baseTexture && typeof texture.baseTexture.once === "function") {
        texture.baseTexture.once("loaded", fit);
      }
      layer.addChild(sprite);
      layer.addChild(shade);
    }

    function drawContainedImage(url, width, height, crop, alpha = 1) {
      const PIXI = getPixi();
      const layer = getLayer();
      if (!PIXI || !layer || !url) return;
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
        const scale = Math.min(width / cropWidth, height / cropHeight);
        const viewportWidth = cropWidth * scale;
        const viewportHeight = cropHeight * scale;
        const viewportX = (width - viewportWidth) * 0.5;
        const viewportY = (height - viewportHeight) * 0.5;
        sprite.width = sourceWidth * scale;
        sprite.height = sourceHeight * scale;
        sprite.x = viewportX - cropX * scale;
        sprite.y = viewportY - cropY * scale;
        sprite.alpha = alpha;
        matte.clear();
        matte.beginFill(0x071011, 1);
        if (viewportY > 0) matte.drawRect(0, 0, width, viewportY + 1);
        if (viewportY + viewportHeight < height) {
          matte.drawRect(0, viewportY + viewportHeight - 1, width, height - viewportY - viewportHeight + 1);
        }
        if (viewportX > 0) matte.drawRect(0, viewportY, viewportX + 1, viewportHeight);
        if (viewportX + viewportWidth < width) {
          matte.drawRect(viewportX + viewportWidth - 1, viewportY, width - viewportX - viewportWidth + 1, viewportHeight);
        }
        matte.endFill();
      };
      fit();
      if (!texture.valid && texture.baseTexture && typeof texture.baseTexture.once === "function") {
        texture.baseTexture.once("loaded", fit);
      }
      layer.addChild(sprite);
      layer.addChild(matte);
    }

    function drawResearchGrid(width, height) {
      const PIXI = getPixi();
      const layer = getLayer();
      if (!PIXI || !layer) return;
      const grid = new PIXI.Graphics();
      grid.lineStyle(1, 0xec922d, 0.08);
      const step = 36;
      for (let x = (width * 0.5) % step; x <= width; x += step) {
        grid.moveTo(x, 0);
        grid.lineTo(x, height);
      }
      for (let y = (height * 0.5) % step; y <= height; y += step) {
        grid.moveTo(0, y);
        grid.lineTo(width, y);
      }
      layer.addChild(grid);
    }

    function draw(source = {}) {
      const PIXI = getPixi();
      const app = getApp();
      const layer = getLayer();
      if (!app || !layer || !PIXI) return;
      const view = getWindow();
      const width = number(view.innerWidth, 1);
      const height = number(view.innerHeight, 1);
      const requestedPreset = String(source.backgroundPreset || "grid");
      const preset = isKnownPreset(requestedPreset) ? requestedPreset : "grid";
      const url = imageUrl(source);
      resetMotion();
      // 同一张图只需重新 fit，避免拖动窗口时重建贴图和遮罩。
      if (url && state.image?.url === url) {
        state.image.fit();
        return;
      }
      state.image = null;
      for (const child of layer.removeChildren()) child.destroy?.({ children: true });
      const base = new PIXI.Graphics();
      base.beginFill(preset === "solid" ? 0x020505 : 0x071011, 1);
      base.drawRect(0, 0, width, height);
      base.endFill();
      layer.addChild(base);
      if (preset === "boot") {
        drawCoverImage(bootBackgroundUrl, width, height, 0.9);
      } else if (preset === "custom" && source.customBackgroundPath) {
        drawCoverImage(url, width, height, 1);
      } else if (url) {
        drawCoverImage(url, width, height, 1);
      } else if (preset === "grid") {
        drawResearchGrid(width, height);
      }
      resetMotion();
    }

    return Object.freeze({
      bumpRevision,
      draw,
      drawContainedImage,
      getState: () => ({ revision: state.revision, imageUrl: state.image?.url || "" }),
      imageUrl,
      resetMotion,
    });
  }

  return Object.freeze({ createBackgroundRenderer });
}));
