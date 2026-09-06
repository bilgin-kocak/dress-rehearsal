"""Confirmation modes for write tools: none | elicitation | echo.

`none`        write tools execute immediately (client-side approval still applies).
`elicitation` the server asks the client (MCP elicitation) to confirm the restated order first.
`echo`        the server returns PENDING_CONFIRMATION + a token; the call must be repeated with it.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from rehearsal.config import Config

WRITE_FIELDS = ("symbol", "side", "type", "quantity", "quoteOrderQty", "price", "stopPrice", "leverage", "asset", "amount",
                "toAsset", "fromAsset", "fromAmount", "quoteId", "orderId")


def restatement(tool: str, args: dict[str, Any]) -> str:
    parts = [f"{k}={args[k]}" for k in WRITE_FIELDS if k in args and args[k] not in (None, "")]
    return f"{tool} " + " ".join(parts) if parts else tool


class Confirmer:
    def __init__(self, cfg: Config):
        self.mode = cfg.schema_.confirm_mode
        self._tokens: dict[str, float] = {}

    def token_for(self, tool: str, args: dict[str, Any]) -> str:
        body = json.dumps({"t": tool, "a": args}, sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def echo_check(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Echo mode: return a pending payload unless a valid confirm token is present."""
        if self.mode != "echo":
            return None
        args = dict(args)
        supplied = args.pop("confirm_token", None) or args.pop("confirmToken", None)
        tok = self.token_for(tool, args)
        if supplied == tok and self._tokens.get(tok, 0) > time.time():
            self._tokens.pop(tok, None)
            return None
        self._tokens[tok] = time.time() + 120
        return {"status": "PENDING_CONFIRMATION", "restatement": restatement(tool, args), "confirm_token": tok,
                "hint": "Restate the order to the user, wait for their yes, then call again with confirm_token."}

    def strip(self, args: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in args.items() if k not in ("confirm_token", "confirmToken")}
