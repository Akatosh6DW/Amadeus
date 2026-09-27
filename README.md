# Amadeus System

[English](README.en.md)

一个尝试在 Windows 桌面上复刻《命运石之门》中 Amadeus 的同人项目。以牧濑红莉栖的人格设定为基础，结合对话、记忆、日语语音与 Live2D 互动。

## 能做什么

- 在桌面窗口中显示 Live2D 角色，并支持基础互动。
- 进行文字对话，保存可供后续对话使用的本地长期记忆。
- 管理 Todo 和日程，在需要时给出提醒。
- 按需使用截图、摄像头、文件和网页等工具。
- 生成带情绪的日语语音，并让角色口型跟随声音变化。

## 直接使用 Release

1. 打开 [v1.0.0 发布版](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.0.0)，下载 `AMADEUS-v1.0.0-setup.zip`。
2. 将 ZIP 解压到一个文件夹，双击其中的 `install-release.cmd`。
3. 安装完成后运行生成的 `AMADEUS.exe`。
4. 首次打开后进入“设置 → API”，填入自己的 API Key。

默认 API Endpoint 为 `https://api.deepseek.com/chat/completions`，默认模型为 `deepseek-v4-flash-vision-exp`。如果使用其他服务，请按该服务的接口格式和模型能力填写。

仓库目前为私密状态；下载需使用有访问权限的账号，并通过 Git Credential Manager 或 GitHub CLI 登录。

## 从源码启动

需要修改代码或自行部署时，可以拉取主仓库：

```powershell
git clone --depth 1 https://github.com/Akatosh6DW/Amadeus.git
cd Amadeus
.\start.cmd
```

首次运行 `start.cmd` 会从依赖软件源安装所需环境，并从本仓库的 Releases 下载声音与人格检索资源，然后启动桌面界面。Live2D 模型随源码提供。以后双击同一个文件即可；下载中断时重新运行。

## 使用提示

API Key、模型和工具权限都在界面设置中管理。屏幕、摄像头、文件和网页工具只在相应功能启用时使用。

设置、聊天、记忆、Todo、日程和生成文件保存在本机；启用联网对话或其他联网功能时，相关内容会发送到你配置的模型服务或对应网站。

支持使用兼容 CUDA 的 NVIDIA 显卡加速；没有合适显卡时也可以使用 CPU，但语音处理通常会更慢。

## 环境要求

- 系统：Windows 10/11 x64；建议至少 16 GB 内存。
- 空间：源码部署预留约 40 GiB，Release 安装预留约 30 GiB。

## 许可证

本项目的原创代码采用 [MIT 许可证](LICENSE)。MIT 只覆盖项目维护者有权授权的原创部分：Live2D 形象与贴图、声音模型权重、参考音频、角色设定和相关商标不在其范围内，只能随 Amadeus System 使用，不得单独提取再发布。GPT-SoVITS、PixiJS、Live2D Cubism Core、Rhubarb 等第三方组件继续适用各自的原许可证。

Live2D 形象来源：“标准实验服”模型来自 ai-poet 的 [amadeus-system-new](https://github.com/ai-poet/amadeus-system-new) 项目；“日常服”是本项目在该模型基础上绘制的换装皮肤，仍使用原模型的网格、物理、表情和动作。模型版权归原作者所有。牧濑红莉栖及《命运石之门》相关角色和商标归其各自权利人所有。

声音模型由项目维护者基于 GPT-SoVITS 训练。训练所用的角色语音素材及声音相关权利归原权利人所有，因此模型权重和参考音频不适用 MIT 许可证，仅供随 Amadeus System 使用。
