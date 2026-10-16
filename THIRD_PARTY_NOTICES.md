# Third-Party Notices

Amadeus由原创代码和多个独立第三方组件组成。根目录的 MIT 许可证（`LICENSE`）只适用于项目维护者有权授权的原创部分。

## DDGS

- Project: <https://github.com/deedy5/ddgs>
- Version: 9.16.0
- License: MIT

用于公开网页搜索；其依赖随 Python 环境安装，许可证保留在安装包中。

## GPT-SoVITS

- Project: <https://github.com/RVC-Boss/GPT-SoVITS>
- Pinned source commit: `bf81cdb14a38b674b6e9996dabc97340bc9978d2`
- License: MIT
- Copyright: 2024 RVC-Boss

开发脚本从上游仓库拉取源码，并应用本仓库中的独立补丁。上游版权声明和MIT许可证必须保留。

## PixiJS

- Project: <https://github.com/pixijs/pixijs>
- Bundled browser build: 6.5.10
- License: MIT

文件：`assets/vendor/live2d/pixi.min.js`。版本和许可证声明保留在文件头。
许可证全文：[`assets/vendor/live2d/PixiJS-LICENSE.txt`](assets/vendor/live2d/PixiJS-LICENSE.txt)。

## pixi-live2d-display

- Project: <https://github.com/guansss/pixi-live2d-display>
- License: MIT

文件：`assets/vendor/live2d/pixi-live2d-display-cubism4.min.js`。
许可证全文：[`assets/vendor/live2d/pixi-live2d-display-LICENSE.txt`](assets/vendor/live2d/pixi-live2d-display-LICENSE.txt)。

## AiPoet Kurisu Live2D model

- Project: <https://github.com/ai-poet/amadeus-system-new>
- Author: ai-poet
- Used in: `assets/live2d/AiPoetKurisu` (lab coat) and, as the base of an outfit skin drawn by this project, `assets/live2d/AiPoetKurisuDaily`

模型版权归原作者所有，不适用 Amadeus 的 MIT 许可证。

## Live2D Cubism Core

- Product: Live2D Cubism Core
- Rights holder: Live2D Inc.
- License: Live2D Proprietary Software License Agreement
- License link: <https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html>

文件：`assets/vendor/live2d/live2dcubismcore.min.js`。该文件头将其标记为协议中的Redistributable Code；它不适用Amadeus的代码许可证。

## Rhubarb Lip Sync

- Project: <https://github.com/DanielSWolf/rhubarb-lip-sync>
- Version used by bootstrap: 1.14.0

Rhubarb由开发脚本从官方Release下载，不提交到普通Git历史。发布包必须随附其上游许可证和通知。

## Python与模型依赖

Python依赖列在：

- `requirements.txt`
- `requirements-context-skill.txt`
- `requirements-voice.txt`
- `requirements-vision.txt`
- 固定GPT-SoVITS提交中的`requirements.txt`

各依赖继续适用其各自许可证。最终Release应由构建流程生成SBOM，并把实际打包组件的许可证文本放入`THIRD_PARTY_NOTICES/`。
