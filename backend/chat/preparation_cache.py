"""Request-local reuse of pure preparation; never caches tool execution or authority."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Callable

from agent.fingerprints import runtime_fingerprint


def _signature(value: Any) -> str:
    return runtime_fingerprint(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


@dataclass
class PreparationCache:
    request_id: str
    entries: dict[str, tuple[str, Any]] = field(default_factory=dict)


def reuse_preparation(
    body: dict[str, Any], name: str, prepare: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Reuse identical inputs in this body only, returning isolated mutable results."""
    request_id = str(body.get("requestId") or "")
    cache = body.get("_requestPreparationCache")
    if not isinstance(cache, PreparationCache) or cache.request_id != request_id:
        cache = PreparationCache(request_id)
        body["_requestPreparationCache"] = cache
    signature = _signature((args, kwargs))
    previous = cache.entries.get(name)
    if previous is not None and previous[0] == signature:
        return deepcopy(previous[1])
    result = prepare(*args, **kwargs)
    cache.entries[name] = (signature, deepcopy(result))
    return result


def resolve_turn_references(
    body: dict[str, Any], resolver: Callable[..., Any], user_message: str,
    messages: list[dict[str, Any]], *, request_id: str = "",
    current_artifacts: Any = (), resolved_task: str = "",
    context_state: Any = None, model_owned: bool = False,
) -> Any:
    return reuse_preparation(
        body, "references", resolver, user_message, messages,
        request_id=request_id, current_artifacts=list(current_artifacts),
        resolved_task=resolved_task or user_message,
        context_state=context_state, model_owned=model_owned,
    )


def _input_signature(body: dict[str, Any], settings: dict[str, Any]) -> str:
    # Internal tool/cognitive state evolves during a turn; user input and settings
    # determine whether the initial context/control/artifact preparation still applies.
    return _signature((
        {key: value for key, value in body.items() if not key.startswith("_")},
        {key: value for key, value in settings.items() if not key.startswith("_")},
    ))


@dataclass(frozen=True)
class StreamPreparation:
    signature: str


def remember_stream_preparation(body: dict[str, Any], settings: dict[str, Any]) -> None:
    body["_streamPreparation"] = StreamPreparation(_input_signature(body, settings))


def resumes_stream_preparation(body: dict[str, Any], settings: dict[str, Any]) -> bool:
    prepared = body.get("_streamPreparation")
    return bool(
        isinstance(prepared, StreamPreparation)
        and (body.get("_toolEscalation") or body.get("_researchEscalation"))
        and prepared.signature == _input_signature(body, settings)
    )
