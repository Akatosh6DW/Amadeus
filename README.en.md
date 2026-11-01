# Amadeus System

[中文](README.md)

Chat with Amadeus on your Windows desktop.

Amadeus System is a *Steins;Gate* fan project. A Live2D character modeled on Kurisu Makise lives on your desktop: she chats with you, remembers what you tell her, keeps track of your to-dos and reminders, and speaks Chinese or Japanese with synchronized mouth movement.

## What she can do

- **Chat and memory**: she remembers your preferences and ongoing plans, even across conversations.
- **To-dos and reminders**: add items in the to-do panel or just say "remind me about the meeting at 9 tomorrow"; she can also read something aloud to you a little later. Reminders and delayed tasks need the program to stay running.
- **Reminder completion**: a one-time notification can complete after successful delivery. Ordinary to-dos still need your completion confirmation, and recurring tasks keep their next occurrence.
- **Recurring tasks**: ask her to recommend papers based on your current interests every Monday at 9. Edit instructions, pause, and resume through conversation or the to-do editor. Tasks execute while the backend is running and save results for desktop delivery. After shutdown or sleep, missed occurrences are coalesced into one catch-up run.
- **Voice and expressions**: desktop speech supports Chinese and Japanese. QQ text/voice mode, language and voice frequency can be configured separately.
- **Images and research**: with your permission she can look at images, screenshots and camera frames, read and edit text files, and search the web with sources.
- **QQ delegation**: configure recipients separately, including group triggers, reply identity, persona, text/voice, short message bubbles and timing. Account profiles keep settings and conversation contexts separate. Owner commands can use the tools enabled locally.
- **Personas**: the local conversation uses Kurisu; individual QQ conversations can select Kurisu, Sunba or Kangba. Forum modes read only locally installed corpora; Sunba/Kangba corpora are not distributed with the public source or Release, so the persona remains selectable without forum examples when they are absent.
- **Web research**: choose DeepSeek, Bing or DuckDuckGo and a fallback provider in browser settings. Search results can be followed by page/PDF text reading and continuation.
- **Appearance**: a floating desktop layout or a video-call layout, two outfits, and backgrounds you can replace with your own pictures.

## Install

1. [Download AMADEUS-v1.1.1-setup.zip](https://github.com/Akatosh6DW/Amadeus/releases/download/v1.1.1/AMADEUS-v1.1.1-setup.zip) (about 17 KB; the installer downloads the full program).
2. Extract it and double-click `install-release.cmd`. The installer downloads the program, runtimes and voice assets, and installs to `%USERPROFILE%\Apps\Amadeus` by default.
3. Run `AMADEUS.exe` and enter your API key under Settings → API.

DeepSeek is the default (endpoint `https://api.deepseek.com/chat/completions`, model `deepseek-flash`). Other OpenAI-compatible services can be configured; image features require a model that accepts image input.

v1.1.1 includes QQ delegation and independent personas, reminder, memory, history and speech fixes, and repairs the update process handoff. Use this installer to upgrade older versions; subsequent updates are available through Settings → Updates while retaining the same profile and conversations. See the [release notes](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.1.1) for installation and upgrade details.

## Run from source

```powershell
git clone --depth 1 https://github.com/Akatosh6DW/Amadeus.git
cd Amadeus
.\start.cmd
```

The first run sets up the desktop and speech environments and downloads about 614 MB of the published voice/context resources plus several GB of GPT-SoVITS, Python and PyTorch dependencies. You must still enter your own API key under Settings → API. Keep the project caches after an interrupted or failed download and run the same `start.cmd` again to continue.

Sunba/Kangba corpora are optional local data and are not distributed with the public source or Release. The [QQ setup and usage guide](integrations/qq/README.md) describes the QQ/persona boundary and local-data requirements.

## Requirements

### QQ delegation

QQ delegation is off by default. Source setup downloads the pinned NapCat component; release builds bundle it. Amadeus does not bundle or download QQ itself. Install QQ NT first.

1. Open **Settings → QQ → Account and login (账号与登录)**, then **Connect local QQ (连接本机 QQ)**. Exit ordinary QQ first if requested; specify QQ.exe only when automatic detection fails.
2. Scan the QR code with mobile QQ and check the actual account number. Follow the displayed **Switch to logged-in account (改用已登录账号)** or **Connect this account (连接这个账号)** action. Logging in does not automatically enable replies.
3. Under **Owner permissions (主人权限)**, enter your own QQ number, separately from the assistant's logged-in account.
4. Add allowed friends/groups under **Reply to whom (回复谁)**. Each recipient's **Settings (设置)** button opens a separate page for identity, persona, group triggers, speech and timing.
5. Choose automatic sending or draft review, save, then press **▶**. **⏸** pauses replies. **Exit QQ background (退出 QQ 后台)** closes the connection while keeping saved settings.

Saved profiles and new account entries are under **Account and login**. Switching a profile pauses replies and requires the matching QQ account to log in; entering a number alone is not a login. Contacts, settings and conversation contexts are account-scoped. Background operation after desktop exit must be explicitly enabled.

See the [QQ setup and usage guide (Chinese)](integrations/qq/README.md) for switching accounts, remote owner commands, group triggers and troubleshooting. The NapCat archive is about 34.2 MiB (109.3 MiB extracted), reusing Amadeus's runtime without AstrBot. The v1.1.1 installer includes the QQ adapter; you still choose an account and scan its QR code before use.

### System requirements

- Windows 10/11 x64, 16 GB RAM recommended.
- About 30 GB of disk space for the Release, about 40 GB for a source setup.
- An NVIDIA GPU makes speech synthesis faster; CPU works too.

## Data and privacy

Chats, memories, to-dos and settings are stored on your computer. Features such as chat, image recognition, automatic summaries and expression detection send the content they need to the model service you configured; web tools visit the corresponding websites.

## Known issue

A saved setting, a scheduled task and a delivered QQ message are distinct outcomes. Successful QQ sends are quiet by default; failures and uncertain results are reported, with detailed receipts in QQ activity. Reminders need the backend running, and delivery can be delayed by shutdown, sleep or an offline QQ account.

The current version removes wording-based authorization guesses and duplicate task registration. Configured permissions, account/recipient scope, pause/review mode and duplicate-send protection remain effective.

Bug reports and feature suggestions are welcome in Issues or by email at [akatoshll6dw@gmail.com](mailto:akatoshll6dw@gmail.com). For bug reports, please include the app version, steps to reproduce the issue, and screenshots when possible.

## Credits and license

- The project's original code is released under the [MIT License](LICENSE).
- See [asset terms](ASSETS_LICENSE.md) and [third-party notices](THIRD_PARTY_NOTICES.md) for bundled resources and components.
- The Live2D model comes from ai-poet's [amadeus-system-new](https://github.com/ai-poet/amadeus-system-new); the "daily outfit" is a skin drawn on top of it.
- The voice model was trained by the author with [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS).
- The interface uses [PixiJS](https://github.com/pixijs/pixijs), [pixi-live2d-display](https://github.com/guansss/pixi-live2d-display) and Live2D Cubism Core, and lip sync uses [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync), each under its own license.

Kurisu Makise and the characters, voices and trademarks of *Steins;Gate* belong to their respective owners. The Live2D models, voice model and reference audio are not covered by the MIT License; please don't extract them for separate use. For any questions about these assets, feel free to reach out through Issues.
