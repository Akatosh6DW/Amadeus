# AMADEUS

面向 Windows 的 Live2D 桌面助手，支持文字对话、长期记忆、Todo 与日程、主动提醒、日语情绪语音、口型同步，以及按需截图、摄像头和网页工具。

## 直接使用 Release

只想使用助手时，打开 [V1.0.0 试发版](https://github.com/Akatosh6DW/Amadeus/releases/tag/v1.0.0)，下载 `AMADEUS-v1.0.0-setup.zip`，解压后双击 `install-release.cmd`。安装器会自动下载、校验、合并安装包并配置 WebView2；完成后打开 `AMADEUS.exe`。Release 已包含 Python、声音权重和运行环境，没有开发者模式或测试模式入口。

默认安装到 `%USERPROFILE%\Apps\Amadeus`。首次使用在“设置 → API”填写自己的 Key；网址和模型已预填。仓库尚为私有时，需要具有仓库访问权并先登录 GitHub CLI 或 Git Credential Manager；仓库公开后不需要登录。需要修改代码或开发功能时，使用下面的源码部署流程。

## 拉取后启动

下载并解压源码，或用 Git 拉取后，双击根目录的 **`start.cmd`** 即可。首次启动会自动下载安装缺少的环境和声音资源，准备完成后打开界面；以后仍双击同一个文件。无需先运行其他安装脚本，也无需手动安装 Python。

```powershell
git clone --depth 1 https://github.com/Akatosh6DW/Amadeus.git
.\Amadeus\start.cmd
```

第一次需要联网等待下载：主要环境、基础模型和声音包合计超过 9 GB；没有微软 C++ 构建工具时还会自动安装该工具及 Windows SDK，下载量和占用空间会进一步增加，Windows 可能要求管理员授权。请保留启动窗口，完成后在“设置 → API”填写自己的 API Key。下载失败时，处理提示中的问题后重新双击 `start.cmd`，程序会检查并补齐缺失内容。完整的已下载资源会复用；未完成的文件可能需要重新下载。若微软安装器要求重启，程序会提示你重启后再运行，不会自动重启电脑。

上面的匿名 `git clone` 适用于主仓库公开的情况。仓库仍为私有时，先用 `gh auth login --hostname github.com` 登录或配置 Git Credential Manager，再执行拉取；资源安装器会复用已有的 GitHub 凭据。

## 尚未下载源码：一条命令安装并启动

在 **64 位 Windows 10/11 的 PowerShell** 中粘贴：

```powershell
irm https://raw.githubusercontent.com/Akatosh6DW/Amadeus/main/install.ps1 | iex
```

安装目录默认为 `%USERPROFILE%\Apps\Amadeus`。该命令自动下载源码、安装缺失环境，从 [AmadeusVoice](https://github.com/Akatosh6DW/AmadeusVoice/releases/tag/runtime-assets-20260910) 下载并校验资源，然后启动界面。首次启动只需在“设置 → API”填写自己的 API Key。

主仓库和 AmadeusVoice 的资源 Release 均公开后，这条命令可匿名使用。脚本需要访问 GitHub、Conda/Python 软件源、GPT-SoVITS 上游资源源；对话服务由用户自己的模型接口提供。

仓库仍为私有时，匿名 `irm` 会得到访问错误。用已登录的 GitHub CLI 先取出安装脚本，再以非交互方式执行：

```powershell
gh auth login --hostname github.com
$installer = Join-Path $env:TEMP "amadeus-install.ps1"
$scriptText = gh api --method GET "repos/Akatosh6DW/Amadeus/contents/install.ps1?ref=main" --header "Accept: application/vnd.github.raw+json"
[IO.File]::WriteAllText($installer, ($scriptText -join "`n"), [Text.UTF8Encoding]::new($false))
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installer -Backend auto
Remove-Item -LiteralPath $installer -Force
```

`install.ps1` 会依次读取当前进程的 `GH_TOKEN`/`GITHUB_TOKEN`、`gh auth token` 和 Git Credential Manager；凭据只在内存中使用，不写入源码、URL、日志或配置，也不会随 GitHub 重定向发往其他站点。资源 Release 公开后可继续匿名下载。

安装中途网络失败时，重新执行同一命令即可继续检查和补齐。已有用户数据不会随资源重装被删除。

自定义安装目录或使用 CPU：

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/Akatosh6DW/Amadeus/main/install.ps1 -OutFile install-amadeus.ps1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\install-amadeus.ps1 -InstallDirectory D:\Apps\Amadeus -Backend cpu
```

已经拿到源码时直接双击 `start.cmd`。原 `deploy.cmd` 保留为手动部署入口。

## 自动安装的内容

环境的“指针”在 `configs/deployment_environment.json` 和 `requirements*.txt` 中：只记录版本和来源，不上传本机 Python 环境。部署从 PyPI、PyTorch 官方软件源、Miniforge/GPT-SoVITS/Rhubarb 官方仓库和微软下载安装内容；GPT-SoVITS 基础模型使用其上游安装脚本指定的 Hugging Face 来源。

桌面使用 Python 3.11，语音使用 Python 3.10；PyTorch/TorchAudio 固定 2.7.0，Transformers 固定 4.50.0。GPT-SoVITS 固定到清单中的提交。Miniforge 安装器与 WebView2 使用官方维护的安装入口。依赖由安装脚本下载，单独执行 `git clone` 只会下载仓库文件。

- 桌面 Python 环境：`.conda/amadeus-desktop`，包含界面、附件处理、截图/摄像头采集及人格检索依赖。
- 语音 Python 环境：`.conda/gpt-sovits`，固定版本的 GPT-SoVITS 源码与上游基础资源。普通部署不应用声音训练补丁；上游安装脚本的 Windows 兼容修正仍由安装器自动处理。
- 缺失时补齐 Git、项目专用 Miniforge、Microsoft WebView2、Rhubarb 口型工具。
- 语音依赖需要源码构建时，使用已有 MSVC；缺失时从微软下载 Visual Studio Build Tools 和推荐的 C++/Windows SDK 组件。安装位置为项目 `.tools/vs-buildtools`，微软安装器仍可能在系统共享目录写入组件，因此需要 Windows 管理员授权。
- `AmadeusVoice` 提供 V7 默认/V6 回退权重、参考音频、原声反应及人格检索资源；下载有完整包和逐文件 SHA-256 校验。

自动检测可用 NVIDIA 驱动，满足 CUDA 12.8 条件时选择 GPU，否则选择 CPU。不要求另外安装 CUDA Toolkit。CPU 能运行语音，但通常更慢。建议至少 16 GB 内存并预留 40 GB 磁盘空间。

图像理解使用当前对话模型的原图输入能力。部署不下载 Florence-2 等旧版视觉模型，也不创建旧视觉模型环境。小型 BGE 模型用于人格文本检索，与图像识别无关。

部署环境和下载缓存默认都在项目目录内：`.tools/` 保存 Miniforge、Git、Rhubarb 等工具，`.conda/` 保存桌面与语音环境，`.sources/` 保存固定版本的 GPT-SoVITS，`.runtime-assets/` 保存已安装资源，`.cache/deployment-assets/` 保存可复用压缩包，`.tmp/bootstrap/` 保存安装临时文件。脚本会复用系统中已存在的 Conda；需要完全隔离时可让它使用项目内的 `.tools/miniforge`，不要把项目目录指向共享环境。

## 源码开发环境

源码运行使用项目环境中的解释器，不依赖系统 Python：桌面代码使用 `.\.conda\amadeus-desktop\python.exe`，语音服务使用 `.\.conda\gpt-sovits\python.exe`。可以直接调用解释器，或按需激活对应环境：

```powershell
conda activate .\.conda\amadeus-desktop
python -X utf8 server.py
conda deactivate
conda activate .\.conda\gpt-sovits
python -X utf8 tools\gpt_sovits_tts_server.py
conda deactivate
```

`server.py` 只启动后端服务。日常调试完整桌面界面用 `.\start.cmd`（首次也会安装环境）或 `.\run_amadeus.ps1`（环境已准备好）；在编辑器中选择上述项目 Python 解释器后即可修改和运行源码。

首次准备源码运行环境使用 `tools\bootstrap_deploy.ps1 -Backend auto -NoLaunch`；它创建可复用的项目环境并校验部署资源。开发时保持桌面和语音解释器分开，避免把语音依赖安装进桌面环境；`conda run --prefix .\.conda\amadeus-desktop ...` 可在脚本或编辑器中无需激活环境直接运行。

## 使用与检查

每次双击 `start.cmd` 启动即可：准备完整时直接启动，检测到缺失组件时自动补齐。API Key、模型和权限可在界面中设置；默认模型为支持图像输入的 DeepSeek 配置，使用其他接口前需确认对应能力。`run_amadeus.ps1` 是已有环境的底层启动入口，不负责首次安装。

仅检查安装，不打开窗口：

```powershell
.\tools\bootstrap_deploy.ps1 -VerifyOnly
```

完整部署但暂不启动：

```powershell
.\tools\bootstrap_deploy.ps1 -NoLaunch
```

检查器验证依赖导入、GPT-SoVITS 源码、资源完整性与必需路径；不代替真实对话、发声和摄像头体验测试。安装失败会显示具体原因，不会把未下载完整的资源标为成功。

排查 Python 包问题时使用项目解释器，例如 `.\.conda\amadeus-desktop\python.exe`。可用 `conda activate .\.conda\amadeus-desktop` 进入桌面环境，用 `conda deactivate` 退出；语音环境同理替换为 `gpt-sovits`。

## 仓库与私人数据

- **Amadeus**：部署必需的源码、界面、人格配置、资源清单和安装入口。
- **AmadeusVoice**：单独版本化的运行资源包。权重、参考音频及模型二进制不进入主仓库 Git 文件树。
- 开发测试、训练脚本、评估报告与维护者工作记录保留在本地。

设置、API Key、聊天、记忆、Todo、日程、关系状态、日志、上传附件和生成文件在本机 `data/` 下创建。公开源码与资源包不携带维护者的这些内容。下载资源只安装清单列出的运行文件，不写入 `data/`。

联网对话会把本轮所需上下文发送到用户配置的模型接口；屏幕、摄像头、文件和网页工具遵守对应权限。仓库下载功能不收集或上传本机记录。

## 版权与许可

- AMADEUS 中由项目维护者原创的代码、训练流程、配置、评估方法和界面实现，其原创贡献版权归项目维护者所有。
- 项目正在确认软件著作权申请主体及学校资助是否影响权属；在根目录正式加入 `LICENSE` 前，源码公开只用于查看和部署验证，不代表自动取得复制、修改、再分发或商业使用授权。
- 仓库中的 Live2D 形象已获得随 AMADEUS 项目使用和分发的授权，但原角色、模型、纹理和相关素材的版权仍属于各自权利人，不因进入本仓库而转移。
- 参考音频和原配音样本不是项目维护者创作，相关角色与录音版权不属于项目维护者。
- 由项目维护者完成的训练、筛选、配置和推理权重，其可受保护的原创贡献由项目维护者保留；这不改变基础模型、参考声音、角色和训练数据原有权利。
- GPT-SoVITS、PixiJS、pixi-live2d-display、Live2D Cubism Core、Rhubarb、Florence-2 及其他依赖继续适用各自许可证。
- 本项目不得用于冒充原配音者、原角色权利人或其他真实个人。

### 第三方组件摘要

- GPT-SoVITS：RVC-Boss 项目，固定上游提交 `bf81cdb14a38b674b6e9996dabc97340bc9978d2`，MIT License，Copyright 2024 RVC-Boss。开发脚本应用本仓库补丁时必须保留上游版权和许可证。
- PixiJS 6.5.10 与 pixi-live2d-display：MIT License；浏览器构建文件保留其文件头声明。
- Live2D Cubism Core：Copyright Live2D Inc.，适用 [Live2D Proprietary Software License Agreement](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html)，不适用 AMADEUS 原创代码的授权边界。
- Rhubarb Lip Sync 1.14.0：由部署脚本从官方 Release 获取，发布包必须保留其上游许可证和通知。
- Python 包、Florence-2、语音与视觉模型继续适用各自上游许可证；依赖清单见根目录的 `requirements*.txt`。正式二进制发布应附带按实际打包组件生成的许可证清单或 SBOM。

### 隐私与资产边界

应用不会把本地设置、聊天、记忆、Todo、日程、日志、附件、生成文件或 API Key 自动上传到源码仓库；联网模型、网页搜索、屏幕和摄像头功能只在相应配置与权限下运行。公开源码快照不包含项目维护者或测试用户的本地数据。角色模型、纹理、参考音频、原配音样本、基础模型和训练数据的权利仍属于各自权利人；进入本项目不代表权利转移，也不得用于冒充相关角色、配音者或真实个人。
