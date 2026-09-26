"""语音相关 HTTP 分支。

请求入口只负责认证、作用域、发布限制和 JSON 解析；本模块保留原有
六个语音端点的精确路径、调用顺序以及 JSON/二进制响应契约。语音客户
端和本地模型服务通过窄操作端口延迟绑定，异常继续交给 Handler 处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class SpeechResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...

    def send_binary(self, data: bytes, content_type: str, status: int = 200) -> None: ...


@dataclass(frozen=True)
class SpeechOperations:
    read_alignment: Callable[[dict[str, Any]], dict[str, Any]]
    prepare: Callable[[dict[str, Any]], dict[str, Any]]
    synthesize_timed: Callable[[dict[str, Any]], dict[str, Any]]
    synthesize: Callable[[dict[str, Any]], tuple[str, bytes]]
    emotion_token: Callable[[dict[str, Any]], dict[str, Any]]
    startup_greeting: Callable[[dict[str, Any]], dict[str, Any]]


class SpeechRoutes:
    """保留语音响应类型和原始分支顺序的纯请求路由。"""

    def __init__(self, operations: SpeechOperations) -> None:
        self.operations = operations

    def post(self, response: SpeechResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/tts-alignment":
            response.send_json(self.operations.read_alignment(body))
            return True
        if path == "/api/tts-prepare":
            response.send_json(self.operations.prepare(body))
            return True
        if path == "/api/tts-timed":
            response.send_json(self.operations.synthesize_timed(body))
            return True
        if path == "/api/tts":
            content_type, audio = self.operations.synthesize(body)
            response.send_binary(audio, content_type)
            return True
        if path == "/api/emotion-token":
            response.send_json(self.operations.emotion_token(body))
            return True
        if path == "/api/startup-greeting":
            response.send_json(self.operations.startup_greeting(body))
            return True
        return False
