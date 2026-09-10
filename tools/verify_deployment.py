"""Read-only verification of an installed AMADEUS source deployment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
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


def verify(root: Path) -> dict:
    source = root / ".sources/GPT-SoVITS"
    patched = source / "GPT_SoVITS/AR/models/t2s_lightning_module.py"
    results = {
        "desktop": probe(root / ".conda/amadeus-desktop/python.exe", ["webview", "PIL", "pypdf", "cv2", "numpy", "onnxruntime", "tokenizers"]),
        "voice": probe(root / ".conda/gpt-sovits/python.exe", ["torch", "librosa", "soundfile", "transformers", "pyopenjtalk"]),
        "gptSovitsSource": {"ok": patched.is_file() and "AMADEUS_GPT_FREEZE_FIRST_LAYERS" in patched.read_text(encoding="utf-8")},
        "rhubarb": {"ok": (root / ".tools/rhubarb/Rhubarb-Lip-Sync-1.14.0-Windows/rhubarb.exe").is_file()},
    }
    voice_python = root / ".conda/gpt-sovits/python.exe"
    if voice_python.is_file():
        program = "import json,torch; d='cuda' if torch.cuda.is_available() else 'cpu'; x=torch.ones(2,device=d); assert x.sum().item()==2; print(json.dumps({'backend':d,'torch':torch.__version__,'device':torch.cuda.get_device_name(0) if d=='cuda' else 'CPU'}))"
        tensor = subprocess.run([str(voice_python), "-X", "utf8", "-c", program], capture_output=True, text=True, encoding="utf-8", timeout=120)
        results["voiceComputation"] = {"ok": tensor.returncode == 0, "detail": (tensor.stdout if tensor.returncode == 0 else tensor.stderr)[-1200:]}
    for name, bundle in json.loads((root / "configs/deployment_assets.json").read_text(encoding="utf-8"))["bundles"].items():
        problems = verify_bundle(root, bundle)
        results[name + "Assets"] = {"ok": not problems, "missingOrChanged": problems}
    base_models = source / "GPT_SoVITS/pretrained_models"
    missing = [name for name in ("chinese-hubert-base/pytorch_model.bin", "chinese-roberta-wwm-ext-large/pytorch_model.bin") if not (base_models / name).is_file()]
    results["voiceBaseModels"] = {"ok": not missing, "missing": missing}
    results["ok"] = all(value["ok"] for value in results.values())
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = verify(args.project_root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
