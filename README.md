# Amadeus System

[English](README.en.md)

在 Windows 桌面上和 Amadeus 聊天。

Amadeus System 是一个《命运石之门》同人项目。桌面上的 Live2D 角色以牧濑红莉栖为原型，能陪你聊天、记住你说过的事、帮你管理待办和提醒，还会用日语开口说话，口型跟着声音动。

## 能做什么

- **聊天和记忆**：她会记住你的偏好和长期在做的事，换个会话也还记得。
- **待办和提醒**：在待办面板里添加，或者直接说“明天上午九点提醒我开会”；也可以让她过一会儿再读一段话给你听。提醒和延时任务需要保持程序运行。
- **语音和表情**：回复会用日语逐句读出来，表情和动作跟着内容变化；点一点她也会有反应。
- **看图和查资料**：在你允许后，她可以看图片、截图和摄像头画面，读写文本文件，联网搜索并给出来源。
- **外观**：桌面悬浮和视频通话两种布局，两套服装，背景可以换成自己的图片。

## 安装

1. 从 [v1.0.1 Release](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.0.1) 下载 `AMADEUS-v1.0.1-setup.zip`。
2. 解压后双击 `install-release.cmd`，安装器会自动下载程序、运行环境和声音资源，默认装在 `%USERPROFILE%\Apps\Amadeus`。
3. 运行 `AMADEUS.exe`，在“设置 → API”里填入你的 API Key。

默认使用 DeepSeek（Endpoint `https://api.deepseek.com/chat/completions`，模型 `deepseek-v4-flash-vision-exp`）。也支持配置其他兼容 OpenAI 接口的服务；看图功能需要模型支持图片输入。

## 从源码运行

```powershell
git clone --depth 1 https://github.com/Akatosh6DW/Amadeus.git
cd Amadeus
.\start.cmd
```

第一次运行会自动安装环境，并下载声音和人格资源；之后双击 `start.cmd` 就能启动。下载中断时重新运行一次即可。

## 环境要求

- Windows 10/11 x64，建议 16 GB 内存。
- Release 安装约需 30 GB 空间，源码运行约需 40 GB。
- 有 NVIDIA 显卡时语音合成更快，没有也可以用 CPU 运行。

## 数据和隐私

聊天、记忆、待办和设置保存在本机。对话、识图、自动摘要和表情判断等功能会将所需内容发送到你配置的模型服务；联网工具会访问相应网站。

## 已知问题

她偶尔会说“已经安排好了”或“记住了”，实际却没有保存。重要的事项请在待办或记忆列表里确认一下。

使用中遇到错误或有功能建议，欢迎提 Issue，或发送邮件至 [akatoshll6dw@gmail.com](mailto:akatoshll6dw@gmail.com)。反馈错误时，请尽量附上软件版本、操作步骤和错误截图，方便定位问题。

## 致谢与许可

- 项目原创代码使用 [MIT 许可证](LICENSE)。
- 素材使用范围见[素材说明](ASSETS_LICENSE.md)，第三方组件及许可证见[第三方声明](THIRD_PARTY_NOTICES.md)。
- Live2D 模型来自 ai-poet 的 [amadeus-system-new](https://github.com/ai-poet/amadeus-system-new)，“日常服”是在它的基础上绘制的换装。
- 声音模型由作者基于 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 训练。
- 界面使用 [PixiJS](https://github.com/pixijs/pixijs)、[pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) 和 Live2D Cubism Core，口型分析使用 [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync)，它们沿用各自的许可证。

牧濑红莉栖与《命运石之门》的角色、配音和商标属于各自的权利人。Live2D 模型、声音模型和参考音频不在 MIT 许可范围内，请不要单独提取使用。素材方面有任何问题，欢迎通过 Issue 联系。
