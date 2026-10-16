"""Read-only verification of an installed Amadeus source deployment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.install_deployment_assets import verify_bundle


def probe(python: Path, modules: list[str]) -> dict:
    if not python.is_file():
        return {"ok": False, "error": "python_missing", "path": str(python)}
    result = subprocess.run([str(python), "-X", "utf8", "-c", "import " + ", ".join(modules)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    return {"ok": result.returncode == 0, "error": result.stderr[-1200:] if result.returncode else ""}


def verify_source(root: Path) -> dict:
    source = root / ".sources/GPT-SoVITS"
    expected = json.loads((root / "configs/deployment_environment.json").read_text(encoding="utf-8"))["gptSovits"]["commit"]
    if not (source / "GPT_SoVITS/inference_webui.py").is_file():
        return {"ok": False, "error": "inference_source_missing"}
    git = shutil.which("git")
    if not git:
        git = next((str(path) for path in (
            root / ".tools/git/Library/bin/git.exe",
            root / ".tools/git/Library/usr/bin/git.exe",
        ) if path.is_file()), "git")
    try:
        result = subprocess.run([git, "-C", str(source), "rev-parse", "HEAD"],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)}
    actual = result.stdout.strip()
    return {"ok": result.returncode == 0 and actual == expected, "expected": expected, "actual": actual}


def voice_model_status(root: Path) -> dict:
    source = root / ".sources/GPT-SoVITS"
    base_models = source / "GPT_SoVITS/pretrained_models"
    required = (
        "chinese-hubert-base/pytorch_model.bin", "chinese-hubert-base/config.json",
        "chinese-roberta-wwm-ext-large/pytorch_model.bin", "chinese-roberta-wwm-ext-large/config.json",
        "gsv-v4-pretrained/s2Gv4.pth", "gsv-v4-pretrained/vocoder.pth",
    )
    missing = [name for name in required if not (base_models / name).is_file()]
    tokenizer = base_models / "chinese-roberta-wwm-ext-large"
    # The upstream bundle ships the fast tokenizer JSON; legacy bundles use vocab.txt.
    if not any((tokenizer / name).is_file() for name in ("tokenizer.json", "vocab.txt")):
        missing.append("chinese-roberta-wwm-ext-large/vocab.txt")
    return {"ok": not missing, "missing": missing}


def verify_voice(root: Path) -> dict:
    voice_python = root / ".conda/gpt-sovits/python.exe"
    results = {
        "voice": probe(voice_python, ["torch", "torchaudio", "librosa", "soundfile", "transformers", "pyopenjtalk", "gradio", "peft", "pytorch_lightning", "faster_whisper"]),
        "voiceBaseModels": voice_model_status(root),
        "voiceDictionary": {"ok": False, "error": "python_missing"},
    }
    if voice_python.is_file():
        dictionary_program = "import os,pyopenjtalk; from pathlib import Path; p=Path(os.fsdecode(pyopenjtalk.OPEN_JTALK_DICT_DIR)); assert (p/'sys.dic').is_file(), 'OpenJTalk dictionary missing'; print(p)"
        dictionary = subprocess.run([str(voice_python), "-X", "utf8", "-c", dictionary_program], capture_output=True, text=True, encoding="utf-8", timeout=30)
        results["voiceDictionary"] = {"ok": dictionary.returncode == 0, "detail": (dictionary.stdout if dictionary.returncode == 0 else dictionary.stderr)[-1200:]}
        program = "import json,torch; d='cuda' if torch.cuda.is_available() else 'cpu'; x=torch.ones(2,device=d); assert x.sum().item()==2; print(json.dumps({'backend':d,'torch':torch.__version__,'device':torch.cuda.get_device_name(0) if d=='cuda' else 'CPU'}))"
        tensor = subprocess.run([str(voice_python), "-X", "utf8", "-c", program], capture_output=True, text=True, encoding="utf-8", timeout=120)
        results["voiceComputation"] = {"ok": tensor.returncode == 0, "detail": (tensor.stdout if tensor.returncode == 0 else tensor.stderr)[-1200:]}
    return results


def verify(root: Path, *, voice_only: bool = False) -> dict:
    results = verify_voice(root)
    if voice_only:
        results["ok"] = all(value["ok"] for value in results.values())
        return results
    results.update({
        "desktop": probe(root / ".conda/amadeus-desktop/python.exe", ["webview", "PIL", "pypdf", "ddgs", "certifi", "primp", "lxml", "cv2", "numpy", "onnxruntime", "tokenizers"]),
        "gptSovitsSource": verify_source(root),
        "rhubarb": {"ok": (root / ".tools/rhubarb/Rhubarb-Lip-Sync-1.14.0-Windows/rhubarb.exe").is_file()},
    })
    for name, bundle in json.loads((root / "configs/deployment_assets.json").read_text(encoding="utf-8"))["bundles"].items():
        problems = verify_bundle(root, bundle)
        results[name + "Assets"] = {"ok": not problems, "missingOrChanged": problems}
    results["ok"] = all(value["ok"] for value in results.values())
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--voice-only", action="store_true")
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args()
    result = (voice_model_status(args.project_root.resolve()) if args.models_only
              else verify(args.project_root.resolve(), voice_only=args.voice_only))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
