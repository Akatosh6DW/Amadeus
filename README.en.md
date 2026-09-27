# Amadeus System

[中文](README.md)

A fan project that attempts to recreate Amadeus from *Steins;Gate* on the Windows desktop. Built around Kurisu Makise's character, it brings together conversation, memory, Japanese speech, and Live2D interaction.

## What it does

- Displays a Live2D character in a desktop window with basic interaction.
- Supports text dialogue and stores local long-term memory for later conversations.
- Manages Todos and schedules, with reminders when needed.
- Provides optional screenshot, camera, file, and web tools.
- Produces emotional Japanese speech and synchronizes the character's mouth to the audio.

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

On the first run, `start.cmd` installs the required environments from their package sources and downloads voice and persona-retrieval assets from this repository's Releases, then starts the desktop interface. Live2D models are included with the source. Use the same file for later launches or to retry an interrupted download.

## Usage notes

Manage the API key, model, and tool permissions in the interface settings. Screen, camera, file, and web tools are used only when their corresponding features are enabled.

Settings, chats, memories, Todos, schedules, and generated files stay on the local machine. When network dialogue or another network feature is enabled, relevant content is sent to the model service or website you configure.

An NVIDIA GPU with a compatible CUDA driver can accelerate processing. CPU mode is also supported, but voice processing is usually slower.

## Requirements

- System: Windows 10/11 x64; at least 16 GB RAM is recommended.
- Disk space: reserve about 40 GiB for a source setup and about 30 GiB for the Release.

## License

The original Amadeus System source code is released under the [MIT License](LICENSE). The license covers only the parts the maintainer has the right to license: Live2D characters and textures, voice model weights, reference audio, character settings and related trademarks are not covered and may be used only as part of Amadeus System, not extracted and redistributed separately. Third-party components such as GPT-SoVITS, PixiJS, Live2D Cubism Core and Rhubarb remain under their own licenses.

Live2D model credit: the "lab coat" model comes from ai-poet's [amadeus-system-new](https://github.com/ai-poet/amadeus-system-new) project. The "daily outfit" is an outfit skin drawn by this project on top of that model and still uses its mesh, physics, expressions and motions. The model's copyright remains with its original author. Kurisu Makise and the Steins;Gate characters and trademarks belong to their respective rights holders.

The voice models were trained by the project maintainer on top of GPT-SoVITS. The character voice material used for training and the related voice rights belong to their original rights holders, so the model weights and reference audio are not covered by the MIT License and are provided only for use with Amadeus System.
