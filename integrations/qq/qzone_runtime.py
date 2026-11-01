"""Run one isolated QQ Space autonomous turn.

The ordinary QQ conversation runner deliberately owns a different scope and
tool set.  QQ Space visits therefore get their own short lived process and an
account-specific directory.  The control token is sent to that process over
stdin; it is never included in the returned result or diagnostics.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
_ACCOUNT_PATTERN = re.compile(r"^[0-9]{5,20}$")


def normalize_account(value: object) -> str:
    account = str(value or "").strip()
    return account if _ACCOUNT_PATTERN.fullmatch(account) else ""


def _loopback_port(control_url: str) -> int:
    parsed = urlsplit(str(control_url or "").strip())
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise ValueError("QQ 空间控制地址必须是本机 HTTP 服务")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("QQ 空间控制端口无效") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("QQ 空间控制端口无效")
    return port


class QzoneRunner:
    """Serialize visits per account while allowing different accounts to run."""

    def __init__(self, runtime: Path, *, python: str | None = None,
                 timeout: float = 240, frozen: bool = False):
        self.runtime = Path(runtime).resolve()
        desktop_python = ROOT / ".conda" / "amadeus-desktop" / (
            "python.exe" if os.name == "nt" else "bin/python"
        )
        self.python = python or (str(desktop_python) if desktop_python.exists() else sys.executable)
        self.timeout = timeout
        self.frozen = frozen
        self.locks: dict[str, asyncio.Lock] = {}

    def account_root(self, account: str) -> Path:
        account = normalize_account(account)
        if not account:
            raise ValueError("无效 QQ 空间账号")
        return self.runtime / "qzone" / account

    async def run(
        self,
        account: str,
        settings: dict,
        control_url: str,
        control_token: str,
        instructions: str = "",
        allow_write: bool = False,
        allow_publish: bool = False,
    ) -> dict:
        """Run one model turn in ``runtime/qzone/<account>/agent``.

        ``settings`` is copied into the child process and filtered there.  It
        may contain the current model/web configuration, but no desktop data
        paths are used by this runner.
        """

        account = normalize_account(account)
        if not account:
            raise ValueError("无效 QQ 空间账号")
        if not isinstance(settings, dict):
            raise ValueError("QQ 空间模型设置无效")
        if not str(control_token or "").strip():
            raise ValueError("QQ 空间控制凭证缺失")
        port = _loopback_port(control_url)
        lock = self.locks.setdefault(account, asyncio.Lock())
        async with lock:
            account_root = self.account_root(account)
            agent_root = account_root / "agent"
            payload = {
                "account": account,
                "settings": dict(settings),
                "control_url": f"http://127.0.0.1:{port}",
                "control_token": str(control_token),
                "instructions": str(instructions or "")[:2000],
                "allow_write": bool(allow_write),
                "allow_publish": bool(allow_write and allow_publish),
                "request_id": "",
            }
            return await self._invoke(account_root, agent_root, payload)

    async def _invoke(self, account_root: Path, agent_root: Path, payload: dict) -> dict:
        account_root.mkdir(parents=True, exist_ok=True)
        agent_root.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
        prefix = (
            [self.python, "--qq-helper", "qzone"]
            if self.frozen
            else [self.python, "-X", "utf8", "-m", "integrations.qq.qzone_worker"]
        )
        command = [
            *prefix,
            "--runtime-root", str(self.runtime),
            "--account-root", str(account_root),
            "--agent-root", str(agent_root),
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryDirectory(prefix="qzone-request-", dir=agent_root) as temporary, \
                (agent_root / "worker.stderr.log").open("ab") as errors:
            wire = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            result_path = Path(temporary) / "output.json"
            if self.frozen:
                request_path = Path(temporary) / "input.json"
                request_path.write_bytes(wire)
                command += ["--input-file", str(request_path), "--output-file", str(result_path)]
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=errors,
                creationflags=flags,
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(None if self.frozen else wire), self.timeout,
                )
                if self.frozen and process.returncode == 0:
                    output = result_path.read_bytes()
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
        if process.returncode:
            raise RuntimeError("QQ 空间自主进程失败；详见该账号的本机日志")
        try:
            result = json.loads(output)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("QQ 空间自主进程返回无效结果") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError(str(result.get("error") or "QQ 空间自主任务未完成") if isinstance(result, dict) else "QQ 空间自主任务未完成")
        return result


__all__ = ["QzoneRunner", "normalize_account"]
