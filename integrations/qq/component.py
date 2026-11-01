"""Bundled NapCat Shell and user-owned QQ discovery. Never downloads QQ."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile

REQUIRED = ('NapCatWinBootMain.exe', 'NapCatWinBootHook.dll', 'napcat.mjs', 'loadNapCat.js', 'qqnt.json')


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def shell_path(root: Path, runtime: Path) -> Path:
    stored = read_json(runtime / 'paths.json').get('napcat_shell')
    candidates = ([Path(stored)] if stored else []) + [root / 'qq-component/napcat-shell', root / '.runtime-assets/qq-agent/napcat-shell']
    return next((path for path in candidates if all((path / name).is_file() for name in REQUIRED)), candidates[-1])


def file_version(path: Path) -> tuple[int, ...]:
    if os.name != 'nt':
        return ()
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ()
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ()
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 24:
        return ()
    info = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
    high, low = info[2], info[3]
    return high >> 16, high & 65535, low >> 16, low & 65535


def valid_client(path: Path) -> bool:
    if path.name.lower() != 'qq.exe' or not path.is_file():
        return False
    version = file_version(path)
    # QQ 9.7 and older use a different architecture. Unknown files are not
    # accepted as QQ; use the NTQQ resource layout as an additional signal.
    nt_layout = ((path.parent / 'resources/app/app_launcher/index.js').is_file()
                 or any((path.parent / 'versions').glob('*/resources/app/package.json')))
    return bool((version and version[:2] >= (9, 9) and (version[-1] >= 40768 or version[0] > 9)) or (not version and nt_layout))


def registry_clients() -> list[Path]:
    if os.name != 'nt':
        return []
    import winreg
    found = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for prefix in ('SOFTWARE\\WOW6432Node', 'SOFTWARE'):
            try:
                with winreg.OpenKey(hive, prefix + r'\Microsoft\Windows\CurrentVersion\Uninstall\QQ') as key:
                    command = str(winreg.QueryValueEx(key, 'UninstallString')[0]).strip()
                    match = re.match(r'"([^"]+)"|(.+?\.exe)(?:\s|$)', command, re.I)
                    if match:
                        found.append(Path(match.group(1) or match.group(2)).parent / 'QQ.exe')
            except OSError:
                pass
    return found


def find_client(root: Path, runtime: Path, explicit: str = '') -> Path | None:
    saved = read_json(runtime / 'paths.json').get('qq_client')
    if explicit:
        path = Path(explicit.strip().strip('"')).resolve()
        return path if valid_client(path) else None
    candidates = ([Path(saved)] if saved else []) + registry_clients()
    # Retain the already-installed portable QQ on existing installations. No
    # installer in this module obtains or replaces a QQ executable.
    candidates.append(root / '.runtime-assets/qq-agent/qqnt/Files/QQ.exe')
    for base in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        if os.environ.get(base):
            candidates += [Path(os.environ[base]) / name for name in ('Tencent/QQNT/QQ.exe', 'Tencent/QQ/QQ.exe')]
    return next((path.resolve() for path in candidates if valid_client(path)), None)


def state(root: Path, runtime: Path) -> dict:
    shell = shell_path(root, runtime)
    client = find_client(root, runtime)
    return {'available': all((shell / name).is_file() for name in REQUIRED),
            'clientFound': client is not None, 'clientPath': str(client) if client else '',
            'version': read_json(root / 'configs/qq_component.json').get('version', ''),
            'message': '已找到本机 QQ' if client else '未找到兼容的 QQ NT，请自行安装或更新 QQ，也可以手动指定 QQ.exe'}


def prepare_amadeus(root: Path, runtime: Path, client: Path, *, python: str, frozen: bool = False) -> dict:
    from .policy import DEFAULT_POLICY
    shell = shell_path(root, runtime)
    if not all((shell / name).is_file() for name in REQUIRED):
        raise ValueError('接入组件不完整，请修复或重新安装 Amadeus')
    if not valid_client(client):
        raise ValueError('没有找到兼容的 QQ NT；请自行安装或更新 QQ，不会自动下载客户端')
    runtime.mkdir(parents=True, exist_ok=True)
    config_path = runtime / 'gateway.json'
    config = read_json(config_path)
    if not config:
        config = {'control_port': 6190, 'control_token': secrets.token_urlsafe(32),
                  'napcat_ws': 'ws://127.0.0.1:3001/', 'napcat_token': secrets.token_urlsafe(32),
                  'astrbot_ws': 'ws://127.0.0.1:6199/ws', 'astrbot_token': secrets.token_urlsafe(32),
                  'policy': dict(DEFAULT_POLICY), 'reply_agent': 'amadeus', 'resume_mode': 'review'}
    config.update(amadeus_python=python, amadeus_frozen=frozen)
    _write_json(config_path, config)
    paths = read_json(runtime / 'paths.json')
    paths.update(qq_client=str(client.resolve()), napcat_shell=str(shell.resolve()),
                 python=python, host_executable=python if frozen else '')
    _write_json(runtime / 'paths.json', paths)
    webui = runtime / 'napcat/config/webui.json'
    if not webui.exists():
        _write_json(webui, {'host':'127.0.0.1', 'port':6099, 'token':secrets.token_urlsafe(32),
                            'autoLoginAccount':'', 'disableWebUI':False})
    return config


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _bundle_notices(license_file: Path, target: Path, version: str) -> None:
    shutil.copyfile(license_file, target / 'LICENSE.txt')
    (target / 'AMADEUS-NOTICE.txt').write_text(
        'NapCat ' + version + '\nCopyright © 2024 Mlikiowa\n'
        'Source: https://github.com/NapNeko/NapCatQQ\n'
        'Unmodified official NapCat.Shell.zip; see LICENSE.txt for its separate terms.\n', encoding='utf-8')


def stage_bundle(root: Path, destination: Path) -> Path:
    """Build/install-time operation: pinned official Shell archive, no QQ."""
    manifest = read_json(root / 'configs/qq_component.json')
    if not re.fullmatch(r'https://github.com/NapNeko/NapCatQQ/releases/download/v[\d.]+/NapCat\.Shell\.zip', manifest.get('url', '')):
        raise ValueError('Invalid NapCat Shell manifest')
    license_file = root / 'assets/vendor/napcat/LICENSE.txt'
    if not license_file.is_file():
        raise ValueError('Missing NapCat redistribution license')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'napcat-shell'
    marker = target / '.amadeus-bundle-sha256'
    if marker.is_file() and marker.read_text(encoding='utf-8') == manifest['sha256'] and all((target / name).is_file() for name in REQUIRED):
        _bundle_notices(license_file, target, manifest['version'])
        return target
    with tempfile.TemporaryDirectory(prefix='napcat-stage-', dir=destination) as temporary:
        temporary = Path(temporary)
        archive = temporary / 'NapCat.Shell.zip'
        digest, total = hashlib.sha256(), 0
        with urlopen(Request(manifest['url'], headers={'User-Agent':'Amadeus-component-build'}), timeout=60) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > manifest['downloadBytes']:
                    raise ValueError('NapCat download size mismatch')
                output.write(chunk)
                digest.update(chunk)
        if total != manifest['downloadBytes'] or digest.hexdigest() != manifest['sha256']:
            raise ValueError('NapCat download integrity check failed')
        extracted = temporary / 'shell'
        extracted.mkdir()
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                path = (extracted / item.filename.replace('\\', '/')).resolve()
                if not path.is_relative_to(extracted.resolve()) or path.name.lower() == 'qq.exe':
                    raise ValueError('Unexpected QQ client or unsafe archive entry')
            package.extractall(extracted)
        if not all((extracted / name).is_file() for name in REQUIRED):
            raise ValueError('NapCat Shell archive is incomplete')
        target.mkdir(exist_ok=True)
        # Overlay code only; user config/logs always live in the separate data root.
        shutil.copytree(extracted, target, dirs_exist_ok=True)
        _bundle_notices(license_file, target, manifest['version'])
        marker.write_text(manifest['sha256'], encoding='utf-8')
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage-bundle', type=Path, required=True)
    args = parser.parse_args()
    stage_bundle(Path(__file__).resolve().parents[2], args.stage_bundle.resolve())


if __name__ == '__main__':
    main()
