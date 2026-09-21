# AMADEUS

[English](README.en.md)

一个尝试在 Windows 桌面上复刻《命运石之门》中 Amadeus 的同人项目。以牧濑红莉栖的人格设定为基础，结合对话、记忆、日语语音与 Live2D 互动。

## 能做什么

- 在桌面窗口中显示 Live2D 角色，并支持基础互动。
- 进行文字对话，保存可供后续对话使用的本地长期记忆。
- 管理 Todo 和日程，在需要时给出提醒。
- 按需使用截图、摄像头、文件和网页等工具。
- 生成带情绪的日语语音，并让角色口型跟随声音变化。

## 大致架构

- 界面层：Live2D 模型运行在 WebView2 中，由前端负责窗口、动作、表情和交互。
- 服务层：Python 负责对话流程，连接用户配置的大模型接口，并处理记忆、Todo、日程和按需工具。
- 语音层：GPT-SoVITS 负责语音合成，Rhubarb Lip Sync 根据语音生成口型时间轴。

## 直接使用 Release

1. 打开 [v1.0.1 试发版](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.0.1)，下载 `AMADEUS-v1.0.1-setup.zip`。
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

首次运行 `start.cmd` 会从依赖软件源安装所需环境，并从本仓库的 Releases 下载声音与人格检索资源，然后启动桌面界面。Live2D 模型随源码提供。以后双击同一个文件即可；下载中断时重新运行。开发时在编辑器中选择 `.conda/amadeus-desktop/python.exe` 作为 Python 解释器。

## 使用提示

API Key、模型和工具权限都在界面设置中管理。屏幕、摄像头、文件和网页工具只在相应功能启用时使用。

设置、聊天、记忆、Todo、日程和生成文件保存在本机；启用联网对话或其他联网功能时，相关内容会发送到你配置的模型服务或对应网站。

支持使用兼容 CUDA 的 NVIDIA 显卡加速；没有合适显卡时也可以使用 CPU，但语音处理通常会更慢。

## 环境要求

- 系统：Windows 10/11 x64；建议至少 16 GB 内存。
- 空间：源码部署预留约 40 GiB，Release 安装预留约 30 GiB。
