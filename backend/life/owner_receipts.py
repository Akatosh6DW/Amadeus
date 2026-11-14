"""Read-only access to authenticated QQ owner delivery receipts.

This module intentionally has no QQ client dependency.  The desktop process
can use it to inspect the gateway-owned receipt database without importing the
optional ``aiohttp`` transport stack.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def read_owner_delivery_state(runtime: Path, *, account: str, owner: str,
                              request_id: str) -> dict[str, Any]:
    """Read one owner's private QQ delivery receipt without writing.

    ``request_id`` is only a lookup locator.  The matching row must still
    carry the requested account, owner sender, private kind, and owner target.
    SQLite read-only URI mode ensures a missing or malformed receipt database
    cannot be created or changed by this lookup.
    """

    receipt_path = Path(runtime) / "owner-receipts.sqlite3"
    if not receipt_path.is_file() or not str(request_id):
        return {}

    expected_account = str(account)
    expected_owner = str(owner)
    expected_request_id = str(request_id)
    try:
        readonly_uri = receipt_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(readonly_uri, uri=True, timeout=2.0) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(
                """SELECT account, sender, kind, target, request_id, state,
                          send_message_id
                     FROM owner_receipts
                    WHERE request_id=?
                    ORDER BY updated DESC""",
                (expected_request_id,),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}

    for row in rows:
        if (str(row["account"]) != expected_account
                or str(row["sender"]) != expected_owner
                or str(row["kind"]) != "private"
                or str(row["target"]) != expected_owner
                or str(row["request_id"]) != expected_request_id):
            continue
        result: dict[str, Any] = {"state": str(row["state"])}
        raw_message_ids = str(row["send_message_id"] or "").strip()
        if not raw_message_ids:
            return result
        try:
            parsed = json.loads(raw_message_ids)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            message_ids = [str(value) for value in parsed if str(value)]
            if message_ids:
                result["messageIds"] = message_ids
        else:
            result["messageId"] = raw_message_ids
        return result
    return {}


__all__ = ["read_owner_delivery_state"]
