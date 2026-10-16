# Amadeus System

[中文](README.md)

Chat with Amadeus on your Windows desktop.

Amadeus System is a *Steins;Gate* fan project. A Live2D character modeled on Kurisu Makise lives on your desktop: she chats with you, remembers what you tell her, keeps track of your to-dos and reminders, and speaks Japanese with her mouth moving in sync.

## What she can do

- **Chat and memory**: she remembers your preferences and ongoing plans, even across conversations.
- **To-dos and reminders**: add items in the to-do panel or just say "remind me about the meeting at 9 tomorrow"; she can also read something aloud to you a little later. Reminders and delayed tasks need the program to stay running.
- **Voice and expressions**: replies are read aloud in Japanese sentence by sentence, with expressions and motions that follow the mood; tap her and she reacts.
- **Images and research**: with your permission she can look at images, screenshots and camera frames, read and edit text files, and search the web with sources.
- **Appearance**: a floating desktop layout or a video-call layout, two outfits, and backgrounds you can replace with your own pictures.

## Install

1. Download `AMADEUS-v1.0.1-setup.zip` from the [v1.0.1 Release](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.0.1).
2. Extract it and double-click `install-release.cmd`. The installer downloads the program, runtimes and voice assets, and installs to `%USERPROFILE%\Apps\Amadeus` by default.
3. Run `AMADEUS.exe` and enter your API key under Settings → API.

DeepSeek is the default (endpoint `https://api.deepseek.com/chat/completions`, model `deepseek-v4-flash-vision-exp`). Other OpenAI-compatible services can be configured; image features require a model that accepts image input.

## Run from source

```powershell
git clone --depth 1 https://github.com/Akatosh6DW/Amadeus.git
cd Amadeus
.\start.cmd
```

The first run sets up the environments and downloads the voice and persona assets; after that, double-click `start.cmd` to launch. If a download is interrupted, just run it again.

## Requirements

- Windows 10/11 x64, 16 GB RAM recommended.
- About 30 GB of disk space for the Release, about 40 GB for a source setup.
- An NVIDIA GPU makes speech synthesis faster; CPU works too.

## Data and privacy

Chats, memories, to-dos and settings are stored on your computer. Features such as chat, image recognition, automatic summaries and expression detection send the content they need to the model service you configured; web tools visit the corresponding websites.

## Known issue

She sometimes says something has been scheduled or remembered when it was not actually saved. For anything important, check the to-do or memory list.

Bug reports and feature suggestions are welcome in Issues or by email at [akatoshll6dw@gmail.com](mailto:akatoshll6dw@gmail.com). For bug reports, please include the app version, steps to reproduce the issue, and screenshots when possible.

## Credits and license

- The project's original code is released under the [MIT License](LICENSE).
- See [asset terms](ASSETS_LICENSE.md) and [third-party notices](THIRD_PARTY_NOTICES.md) for bundled resources and components.
- The Live2D model comes from ai-poet's [amadeus-system-new](https://github.com/ai-poet/amadeus-system-new); the "daily outfit" is a skin drawn on top of it.
- The voice model was trained by the author with [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS).
- The interface uses [PixiJS](https://github.com/pixijs/pixijs), [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) and Live2D Cubism Core, and lip sync uses [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync), each under its own license.

Kurisu Makise and the characters, voices and trademarks of *Steins;Gate* belong to their respective owners. The Live2D models, voice model and reference audio are not covered by the MIT License; please don't extract them for separate use. For any questions about these assets, feel free to reach out through Issues.
