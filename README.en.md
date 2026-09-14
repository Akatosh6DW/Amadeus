# AMADEUS

[中文](README.md)

A fan project that attempts to recreate Amadeus from *Steins;Gate* on the Windows desktop. Built around Kurisu Makise's character, it brings together conversation, memory, Japanese speech, and Live2D interaction.

## What it does

- Displays a Live2D character in a desktop window with basic interaction.
- Supports text dialogue and stores local long-term memory for later conversations.
- Manages Todos and schedules, with reminders when needed.
- Provides optional screenshot, camera, file, and web tools.
- Produces emotional Japanese speech and synchronizes the character's mouth to the audio.

## High-level architecture

- UI layer: the Live2D model runs in WebView2; the frontend handles the window, motion, expressions, and interaction.
- Service layer: Python manages the conversation flow, connects to the model endpoint you configure, and handles memory, Todos, schedules, and optional tools.
- Voice layer: GPT-SoVITS synthesizes speech, while Rhubarb Lip Sync turns the audio into a mouth-timing track.

## Use the Release

1. Open the [v1.0.0 release](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.0.0) and download `AMADEUS-v1.0.0-setup.zip`.
2. Extract the ZIP to a folder and double-click `install-release.cmd`.
3. After installation, run the generated `AMADEUS.exe`.
4. On first launch, open Settings → API and enter your own API key.

The default API endpoint is `https://api.deepseek.com/chat/completions`, and the default model is `deepseek-v4-flash-vision-exp`. If you use another service, enter the endpoint and model supported by that service.

The repositories are currently private. Downloads require an account with access, signed in through Git Credential Manager or GitHub CLI.

## Run from source

To change the code or set up the project yourself, clone the main repository:

```powershell
git clone --depth 1 https://github.com/Akatosh6DW/Amadeus.git
cd Amadeus
.\start.cmd
```

On the first run, `start.cmd` automatically downloads and installs the required environments and voice assets, then starts the desktop interface. Use the same file for later launches or to retry an interrupted download. For development, select `.conda/amadeus-desktop/python.exe` as the Python interpreter in your editor.

## Usage notes

Manage the API key, model, and tool permissions in the interface settings. Screen, camera, file, and web tools are used only when their corresponding features are enabled.

Settings, chats, memories, Todos, schedules, and generated files stay on the local machine. When network dialogue or another network feature is enabled, relevant content is sent to the model service or website you configure.

An NVIDIA GPU with a compatible CUDA driver can accelerate processing. CPU mode is also supported, but voice processing is usually slower.

## Requirements

- System: Windows 10/11 x64; at least 16 GB RAM is recommended.
- Disk space: reserve about 40 GiB for a source setup and about 30 GiB for the Release.
