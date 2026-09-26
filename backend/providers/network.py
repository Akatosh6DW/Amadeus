"""上游 API 的代理、TLS 和连接传输边界。

本模块不读取 server 的全局状态，也不负责熔断状态或并发闸门。入口层把
路由、时钟、重试策略、连接结果与日志动作作为回调传入，因此传输规则可以
用独立的 fake opener 测试，并继续由 server 保有应用状态。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from backend.providers.errors import (
    sanitize_provider_error_message,
    upstream_connection_error_category,
)


Environment = Mapping[str, str]
EndpointNormalizer = Callable[[str], str]
RoutesFactory = Callable[[str, dict[str, Any] | None], Sequence[tuple[str, Any]]]
RecordConnectivity = Callable[..., None]
LogWriter = Callable[..., None]
IncidentWriter = Callable[..., None]
NetworkModeReader = Callable[[str, dict[str, Any] | None], str]
TransportStatusReader = Callable[[str, dict[str, Any] | None], Mapping[str, Any]]
ErrorCategoryReader = Callable[[BaseException], str]
ErrorSanitizer = Callable[[Any], str]


def chat_endpoint(endpoint: str) -> str:
    """补齐 OpenAI 兼容路径并保留 endpoint 的其他部分。"""

    endpoint = str(endpoint or "").strip().rstrip("/")
    if not endpoint:
        return endpoint
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return endpoint
    if path in ("", "/v1", "/beta"):
        return endpoint + "/chat/completions"
    return endpoint


def _environment_flag(name: str, environment: Environment) -> bool | None:
    value = environment.get(name)
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def api_network_mode(
    endpoint: str = "",
    settings: dict[str, Any] | None = None,
    *,
    environment: Environment | None = None,
    endpoint_normalizer: EndpointNormalizer = chat_endpoint,
) -> str:
    """解析 API 传输策略；无效配置回到 auto，避免静默改变 TLS 信任。"""

    environment = os.environ if environment is None else environment
    configured = str((settings or {}).get("apiNetworkMode") or "").strip().lower().replace("-", "_")
    if configured in {"direct", "system_proxy", "explicit_proxy"}:
        return configured
    explicit = str(environment.get("AMADEUS_API_NETWORK_MODE") or "").strip().lower().replace("-", "_")
    aliases = {
        "proxy": "system_proxy",
        "system": "system_proxy",
        "system_proxy": "system_proxy",
        "explicit": "explicit_proxy",
        "explicit_proxy": "explicit_proxy",
        "direct": "direct",
        "auto": "auto",
    }
    if explicit in aliases:
        return aliases[explicit]
    legacy_use_proxy = _environment_flag("AMADEUS_API_USE_PROXY", environment)
    if legacy_use_proxy is True:
        return "system_proxy"
    if legacy_use_proxy is False:
        return "direct"
    if str(environment.get("AMADEUS_API_PROXY_URL") or "").strip():
        return "explicit_proxy"
    parsed = urllib.parse.urlparse(endpoint_normalizer(endpoint))
    host = (parsed.hostname or "").lower()
    raw_hosts = str(environment.get("AMADEUS_API_BYPASS_PROXY_HOSTS") or "")
    bypass_hosts = {item.strip().lower() for item in raw_hosts.split(",") if item.strip()}
    if host and host in bypass_hosts:
        return "direct"
    return "auto"


def api_bypasses_proxy(
    endpoint: str,
    settings: dict[str, Any] | None = None,
    *,
    environment: Environment | None = None,
    endpoint_normalizer: EndpointNormalizer = chat_endpoint,
) -> bool:
    """兼容辅助函数：只有明确 direct 策略才绕过代理。"""

    return api_network_mode(
        endpoint,
        settings,
        environment=environment,
        endpoint_normalizer=endpoint_normalizer,
    ) == "direct"


def _api_proxy_mapping(
    endpoint: str,
    mode: str,
    settings: dict[str, Any] | None = None,
    *,
    environment: Environment | None = None,
    endpoint_normalizer: EndpointNormalizer = chat_endpoint,
    get_proxies: Callable[[], Mapping[str, str]] = urllib.request.getproxies,
    proxy_bypass: Callable[[str], bool] = urllib.request.proxy_bypass,
) -> tuple[dict[str, str], str]:
    """按策略生成 urllib 代理映射和来源标签。"""

    environment = os.environ if environment is None else environment
    parsed = urllib.parse.urlparse(endpoint_normalizer(endpoint))
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    configured_proxy = str((settings or {}).get("apiProxyUrl") or "").strip()
    if mode == "explicit_proxy":
        proxy_url = configured_proxy or str(environment.get("AMADEUS_API_PROXY_URL") or "").strip()
        if not proxy_url:
            proxies = get_proxies()
            proxy_url = str(proxies.get(scheme) or proxies.get("all") or "").strip()
            return ({scheme: proxy_url} if proxy_url else {}), "system"
        return {"http": proxy_url, "https": proxy_url}, "settings" if configured_proxy else "explicit"
    if mode == "auto" and configured_proxy:
        return {"http": configured_proxy, "https": configured_proxy}, "settings"
    proxies = get_proxies()
    if host and proxy_bypass(host):
        return {}, "bypassed"
    proxy_url = str(proxies.get(scheme) or proxies.get("all") or "").strip()
    return ({scheme: proxy_url} if proxy_url else {}), "system"


def _api_ssl_context(
    *,
    environment: Environment | None = None,
    create_default_context: Callable[..., ssl.SSLContext] = ssl.create_default_context,
) -> ssl.SSLContext:
    """建立默认或显式 CA bundle 的 TLS 上下文，并拒绝不存在的路径。"""

    environment = os.environ if environment is None else environment
    ca_bundle = str(environment.get("AMADEUS_API_CA_BUNDLE") or "").strip()
    if not ca_bundle:
        return create_default_context()
    ca_path = Path(ca_bundle).expanduser()
    if not ca_path.is_file():
        raise FileNotFoundError(f"AMADEUS_API_CA_BUNDLE does not exist: {ca_path}")
    return create_default_context(cafile=str(ca_path))


def _api_transport_routes(
    endpoint: str,
    settings: dict[str, Any] | None = None,
    *,
    mode_reader: NetworkModeReader | None = None,
    proxy_mapping_reader: Callable[..., tuple[dict[str, str], str]] | None = None,
    ssl_context_factory: Callable[[], ssl.SSLContext] | None = None,
    build_opener: Callable[..., Any] = urllib.request.build_opener,
    proxy_handler: Callable[[Mapping[str, str]], Any] = urllib.request.ProxyHandler,
    https_handler: Callable[..., Any] = urllib.request.HTTPSHandler,
) -> list[Route]:
    """构造代理优先、direct 兜底的 opener 路由，不执行任何网络请求。"""

    mode_reader = api_network_mode if mode_reader is None else mode_reader
    proxy_mapping_reader = _api_proxy_mapping if proxy_mapping_reader is None else proxy_mapping_reader
    ssl_context_factory = _api_ssl_context if ssl_context_factory is None else ssl_context_factory
    mode = mode_reader(endpoint, settings)
    proxy_mapping, _ = proxy_mapping_reader(endpoint, mode, settings)
    context = ssl_context_factory()

    def opener(mapping: Mapping[str, str]) -> Any:
        return build_opener(
            proxy_handler(dict(mapping)),
            https_handler(context=context),
        )

    if mode == "direct":
        return [("direct", opener({}))]
    if mode in {"system_proxy", "explicit_proxy"}:
        return [("proxy", opener(proxy_mapping))] if proxy_mapping else [("direct", opener({}))]
    if proxy_mapping:
        return [("proxy", opener(proxy_mapping)), ("direct", opener({}))]
    return [("direct", opener({}))]


class TransportClock(Protocol):
    """传输所需的最小时钟接口，server 可直接传入其 time 模块。"""

    def monotonic(self) -> float: ...

    def perf_counter(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


def open_api_request(
    request: urllib.request.Request,
    endpoint: str,
    timeout: float = 90,
    *,
    connection_attempts: int = 1,
    request_id: str = "",
    purpose: str = "chat",
    settings: dict[str, Any] | None = None,
    routes_factory: RoutesFactory,
    record_connectivity: RecordConnectivity,
    write_log: LogWriter,
    write_incident: IncidentWriter,
    network_mode: NetworkModeReader,
    transport_status: TransportStatusReader,
    clock: TransportClock,
    retry_delays: Sequence[float],
    build: str,
    error_category: ErrorCategoryReader,
    sanitize_error: ErrorSanitizer,
) -> Any:
    """执行上游连接并保留旧路由/重试边界。

    ``connection_attempts`` 刻意只保留兼容签名：当前行为由 routes 数量决定，
    不能在迁移时悄悄把调用者传入的次数变成额外网络重试。HTTPError 证明
    DNS/TCP/TLS 已成功，所以记录连通性成功并直接交给上层状态处理；TLS
    证书不受信任按原有分类规则阻止重试。
    """

    del connection_attempts
    routes = list(routes_factory(endpoint, settings))

    # Routes already encode every materially different path. Replaying the same
    # dead direct route only multiplies timeout latency; higher-level recovery
    # owns any later replay.
    attempt_count = max(1, len(routes))
    deadline = clock.monotonic() + max(0.001, float(timeout))
    last_error: BaseException | None = None
    for attempt in range(1, attempt_count + 1):
        attempt_started = clock.perf_counter()
        route_name, opener = routes[min(attempt - 1, len(routes) - 1)]
        remaining = max(0.001, deadline - clock.monotonic())
        attempt_timeout = max(0.001, min(20.0, remaining))
        try:
            response = opener.open(request, timeout=attempt_timeout)
            record_connectivity(True, route=route_name)
            if attempt > 1:
                write_log(
                    "upstream_transport_recovered",
                    purpose=purpose,
                    requestId=request_id,
                    attempt=attempt,
                    route=route_name,
                )
            return response
        except urllib.error.HTTPError:
            # HTTP 错误证明传输已经成功；保留服务商响应供上层按状态处理。
            record_connectivity(True, route=route_name)
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            last_error = error
            category = error_category(error)
            record_connectivity(False, category=category, route=route_name)
            retryable = category != "tls_certificate_untrusted"
            write_log(
                "upstream_transport_failed",
                purpose=purpose,
                requestId=request_id,
                attempt=attempt,
                attempts=attempt_count,
                route=route_name,
                networkMode=network_mode(endpoint, settings),
                proxyConfigured=transport_status(endpoint, settings)["proxyConfigured"],
                category=category,
                retryable=retryable,
                errorType=type(error).__name__,
                error=sanitize_error(error)[:500],
            )
            parsed_target = urllib.parse.urlparse(request.full_url)
            write_incident(
                build=build,
                purpose=purpose,
                requestId=request_id,
                targetHost=parsed_target.hostname or "",
                targetPort=parsed_target.port or (443 if parsed_target.scheme == "https" else 80),
                route=route_name,
                networkMode=network_mode(endpoint, settings),
                proxyConfigured=transport_status(endpoint, settings)["proxyConfigured"],
                category=category,
                attempt=attempt,
                attempts=attempt_count,
                elapsedMs=round((clock.perf_counter() - attempt_started) * 1000),
                finalAttempt=bool(
                    not retryable or attempt >= attempt_count or clock.monotonic() >= deadline
                ),
                errorType=type(error).__name__,
                error=sanitize_error(error)[:500],
            )
            if not retryable or attempt >= attempt_count or clock.monotonic() >= deadline:
                raise
            clock.sleep(retry_delays[min(attempt - 1, len(retry_delays) - 1)])
    if last_error is not None:
        raise last_error
    raise ConnectionError("upstream transport unavailable")


__all__ = [
    "_api_proxy_mapping",
    "_api_ssl_context",
    "_api_transport_routes",
    "api_bypasses_proxy",
    "api_network_mode",
    "chat_endpoint",
    "open_api_request",
]
