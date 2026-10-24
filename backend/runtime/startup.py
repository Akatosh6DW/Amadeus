"""源码后端启动与关闭顺序；共享对象由组装层创建，本模块不重复持有状态。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import threading
from agent.context_compatibility import ContextCompatibilityError
from backend.api.server import AmadeusHTTPServer

@dataclass(frozen=True)
class StartupServices:
    ensure_data: Callable[[], None]
    ensure_memory: Callable[[], Any]
    process_lock: Callable[[], Any]
    http_server: Callable[..., Any]
    handler: Callable[[], Any]
    host: Callable[[], str]
    port: Callable[[], int]
    settings: Callable[[], dict[str, Any]]
    reconcile_relationship: Callable[..., Any]
    storage_health: Callable[[], dict[str, Any]]
    log: Callable[..., None]
    mode: Callable[[], str]
    memory_path: Callable[..., Any]
    should_start_vision: Callable[..., bool]
    ensure_vision: Callable[..., Any]
    stop_tts: Callable[[], Any]
    stop_vision: Callable[[], Any]
    start_tasks: Callable[[], Any] = lambda: None
    stop_tasks: Callable[[], Any] = lambda: None

def serve(services: StartupServices) -> None:
    services.ensure_data()
    services.ensure_memory()
    runtime_lock = services.process_lock()
    try:
        runtime_lock.acquire()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    server: AmadeusHTTPServer | None = None
    try:
        server = services.http_server((services.host(), services.port()), services.handler())
        settings = services.settings()
        try:
            services.reconcile_relationship(settings)
            storage_health = services.storage_health()
        except ContextCompatibilityError as error:
            # Keep settings and the error-reporting HTTP interface available.
            # Chat/history remain blocked by the same protected storage reads.
            storage_health = {"ok": False, "code": error.code, "error": str(error), "recoveryRequired": True}
        services.log(
            "conversation_storage_health",
            runtimeMode=services.mode(),
            **storage_health,
        )
        print(f"Amadeus System local server: http://{services.host()}:{services.port()}")
        print(f"Model: {settings.get('apiModel') or 'local simulation'}")
        print(f"Memory: {services.memory_path(settings)}")
        if storage_health.get("recoveryRequired"):
            print(
                "Conversation storage warning: archive/index mismatch detected; "
                "history will use read-only recovery."
            )
        if services.should_start_vision(settings):
            threading.Thread(
                target=services.ensure_vision,
                args=(settings, 180),
                daemon=True,
                name="amadeus-vision-starter",
            ).start()
        if not storage_health.get("recoveryRequired"):
            services.start_tasks()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        services.stop_tasks()
        services.stop_tts()
        services.stop_vision()
        if server is not None:
            server.server_close()
        runtime_lock.release()
