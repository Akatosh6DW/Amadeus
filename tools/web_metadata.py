"""Small, lossless navigation hints retained through evidence compaction."""
from copy import deepcopy
from typing import Any, Mapping


def page_navigation(source: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(source[key]) for key in (
        "totalChars", "range", "rangeExact", "startChar", "endChar", "nextStartChar",
        "hasMore", "find", "matchCount", "truncated", "extractionTruncated",
        "projectionMode", "continuationStartChar", "continuationMaxChars",
    ) if key in source}
    if isinstance(source.get("matchPositions"), list):
        result["matchPositions"] = deepcopy(source["matchPositions"][:12])
    return result
