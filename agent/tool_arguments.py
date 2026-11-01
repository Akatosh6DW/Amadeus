"""File tool argument contracts shared by prompting and execution checks."""
from typing import Any, Mapping


TRANSPORT_ARGUMENT_FIELDS = frozenset({"actionId", "pendingActionId", "_actionGroupId", "_retryOfActionId"})
FILE_TOOL_INPUT_SCHEMAS = {
    "list_directory": {"path": "optional authorized directory, default '.'", "recursive": "optional boolean, default false",
                       "maxDepth": "optional integer, clamped to 1..4, default 2"},
    "read_text_file": {"path": "required nonempty authorized file path string"},
    "create_directory": {"path": "required nonempty authorized directory path string", "createParents": "optional boolean, default true"},
    "write_text_file": {"path": "required nonempty authorized file path string", "content": "required string; empty content is allowed",
                        "mode": "optional create|overwrite|append, default create", "createParents": "optional boolean, default false"},
}

# ``read_image`` has two deliberately separate source bindings.  An
# attachment ID is resolved through the upload store; a path is resolved
# through the configured Agent workspace.  Keeping both in one free-form path
# field would make it too easy for a model-selected local path to bypass the
# attachment/workspace boundary.
IMAGE_TOOL_INPUT_SCHEMAS = {
    "attachmentId": "optional exact uploaded image attachment ID; mutually exclusive with path",
    "id": "optional alias of attachmentId",
    "fileName": "optional exact uploaded image filename; used with attachmentId or as an attachment lookup",
    "name": "optional alias of fileName",
    "path": "optional relative or in-workspace image path; requires permissionFileRead; mutually exclusive with attachmentId",
}


def file_tool_argument_error(name: str, value: Any) -> dict[str, Any] | None:
    """Reject misspelled/missing fields before applying defaults or using paths."""
    schema = FILE_TOOL_INPUT_SCHEMAS.get(name)
    if schema is None:
        return None
    args = value if isinstance(value, Mapping) else {}
    missing = []
    invalid = [] if isinstance(value, Mapping) else ["args"]
    unknown = sorted(str(key) for key in args if key not in schema and key not in TRANSPORT_ARGUMENT_FIELDS)
    required = ({"path"} if name != "list_directory" else set()) | ({"content"} if name == "write_text_file" else set())
    for field in sorted(required):
        if field not in args:
            missing.append(field)
    for field, item in args.items():
        if field not in schema:
            continue
        if field not in required and item is None:
            continue
        if field in {"path", "content"}:
            if not isinstance(item, str) or (field == "path" and field in required and not item.strip()):
                invalid.append(field)
        elif field in {"recursive", "createParents"} and not isinstance(item, bool):
            invalid.append(field)
        elif field == "maxDepth" and (not isinstance(item, int) or isinstance(item, bool)):
            invalid.append(field)
        elif field == "mode" and (not isinstance(item, str) or item.strip().lower() not in {"create", "overwrite", "append"}):
            invalid.append(field)
    if not (missing or invalid or unknown):
        return None
    return {"code": "tool_arguments_invalid", "tool": name, "missingFields": missing,
            "invalidFields": sorted(set(invalid)), "unknownFields": unknown,
            "expectedArguments": dict(schema)}


def image_tool_argument_error(name: str, value: Any) -> dict[str, Any] | None:
    """Validate the structured source selector before any image is opened."""

    if name != "read_image":
        return None
    schema = IMAGE_TOOL_INPUT_SCHEMAS
    args = value if isinstance(value, Mapping) else {}
    invalid: list[str] = [] if isinstance(value, Mapping) else ["args"]
    unknown = sorted(
        str(key) for key in args
        if key not in schema and key not in TRANSPORT_ARGUMENT_FIELDS
    )
    for field in schema:
        if field not in args or args[field] is None:
            continue
        if not isinstance(args[field], str) or not args[field].strip():
            invalid.append(field)
    attachment_fields = ("attachmentId", "id", "fileName", "name")
    has_attachment = any(str(args.get(field) or "").strip() for field in attachment_fields)
    has_path = bool(str(args.get("path") or "").strip())
    if not has_attachment and not has_path:
        invalid.append("source")
    elif has_attachment and has_path:
        invalid.append("source")
    if not (invalid or unknown):
        return None
    return {
        "code": "tool_arguments_invalid",
        "tool": name,
        "missingFields": ["attachmentId or path"] if not has_attachment and not has_path else [],
        "invalidFields": sorted(set(invalid)),
        "unknownFields": unknown,
        "expectedArguments": dict(schema),
    }


def tool_argument_error(name: str, value: Any) -> dict[str, Any] | None:
    """Dispatch argument validation for tools with structured source fields."""

    return image_tool_argument_error(name, value) or file_tool_argument_error(name, value)
