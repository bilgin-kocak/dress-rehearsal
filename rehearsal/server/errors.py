"""Binance-shaped error payloads.

Codes and messages follow the public Binance Spot / USDⓈ-M REST error docs so that an
agent hitting the twin sees exactly what it would see in production.
"""

from __future__ import annotations

from typing import Any


class BinanceError(Exception):
    """Raised by handlers; converted to `{"code": ..., "msg": ...}` at the MCP boundary."""

    def __init__(self, code: int, msg: str, extra: dict[str, Any] | None = None):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg
        self.extra = extra or {}

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "msg": self.msg}


# ---- Common (spot + futures) -------------------------------------------------------------
UNKNOWN = (-1000, "An unknown error occurred while processing the request.")
TOO_MANY_REQUESTS = (-1003, "Too many requests; current limit is %s requests per minute.")
BAD_SYMBOL = (-1121, "Invalid symbol.")
BAD_PRECISION = (-1111, "Precision is over the maximum defined for this asset.")
BAD_INTERVAL = (-1120, "Invalid interval.")
BAD_PARAM = (-1100, "Illegal characters found in parameter '%s'.")
MANDATORY_PARAM = (-1102, "Mandatory parameter '%s' was not sent, was empty/null, or malformed.")
BAD_ORDER_TYPE = (-1116, "Invalid orderType.")
BAD_SIDE = (-1117, "Invalid side.")
BAD_TIF = (-1115, "Invalid timeInForce.")
EMPTY_CLIENT_ORDER_ID = (-1118, "New client order ID was empty.")
BAD_LISTEN_KEY = (-1125, "This listenKey does not exist.")
FILTER_FAILURE = (-1013, "Filter failure: %s")
INVALID_QUANTITY = (-1013, "Invalid quantity.")
INVALID_PRICE = (-1013, "Invalid price.")
UNSUPPORTED_ORDER_COMBINATION = (-1106, "Parameter '%s' sent when not required.")

# ---- Spot trading (-2xxx) --------------------------------------------------------------
NEW_ORDER_REJECTED = (-2010, "NEW_ORDER_REJECTED")
INSUFFICIENT_BALANCE = (-2010, "Account has insufficient balance for requested action.")
WOULD_MATCH_AND_TAKE = (-2010, "Order would immediately match and take.")
MARKET_CLOSED = (-2010, "Market is closed.")
STOP_WOULD_TRIGGER = (-2010, "Order would trigger immediately.")
CANCEL_REJECTED = (-2011, "CANCEL_REJECTED")
UNKNOWN_ORDER = (-2011, "Unknown order sent.")
NO_SUCH_ORDER = (-2013, "Order does not exist.")
BAD_API_KEY_FMT = (-2014, "API-key format invalid.")
REJECTED_MBX_KEY = (-2015, "Invalid API-key, IP, or permissions for action.")
ORDER_ARCHIVED = (-2026, "Order was canceled or expired with no executed qty over 90 days ago and has been archived.")

# ---- USDⓈ-M futures (-2xxx / -4xxx) -----------------------------------------------------
FUT_MARGIN_INSUFFICIENT = (-2019, "Margin is insufficient.")
FUT_UNABLE_TO_FILL = (-2020, "Unable to fill.")
FUT_ORDER_WOULD_TRIGGER = (-2021, "Order would immediately trigger.")
FUT_REDUCE_ONLY_REJECTED = (-2022, "ReduceOnly Order is rejected.")
FUT_POSITION_NOT_EXIST = (-2023, "User in liquidation mode now.")
FUT_POSITION_NOT_SUFFICIENT = (-2024, "Position is not sufficient.")
FUT_MAX_OPEN_ORDER_EXCEEDED = (-2025, "Reach max open order limit.")
FUT_REDUCE_ONLY_ORDER_TYPE_NOT_SUPPORTED = (-2026, "This OrderType is not supported when reduceOnly.")
FUT_MAX_LEVERAGE_RATIO = (-2027, "Exceeded the maximum allowable position at current leverage.")
FUT_MIN_LEVERAGE_RATIO = (-2028, "Leverage is smaller than permitted: insufficient margin balance")
FUT_QTY_LESS_THAN_ZERO = (-4003, "Quantity less than zero.")
FUT_QTY_LESS_THAN_MIN = (-4004, "Quantity less than min quantity.")
FUT_QTY_GREATER_THAN_MAX = (-4005, "Quantity greater than max quantity.")
FUT_STOP_PRICE_LESS_THAN_ZERO = (-4006, "Stop price less than zero.")
FUT_STOP_PRICE_GREATER_THAN_MAX = (-4007, "Stop price greater than max price.")
FUT_TICK_SIZE_LESS_THAN_ZERO = (-4008, "Tick size less than zero.")
FUT_MAX_PRICE_LESS_THAN_MIN = (-4009, "Max price less than min price.")
FUT_PRICE_LESS_THAN_MIN = (-4016, "Limit price can't be higher than %s.")
FUT_INVALID_LEVERAGE = (-4028, "Leverage %s is not valid")
FUT_INVALID_TICK_SIZE_PRECISION = (-4029, "Tick size precision is invalid.")
FUT_INVALID_STEP_SIZE_PRECISION = (-4030, "Step size precision is invalid.")
FUT_INVALID_WORKING_TYPE = (-4031, "Invalid parameter working type: %s")
FUT_NO_NEED_TO_CHANGE_MARGIN_TYPE = (-4046, "No need to change margin type.")
FUT_MARGIN_TYPE_CANNOT_CHANGE = (-4047, "Margin type cannot be changed if there exists open orders.")
FUT_MARGIN_TYPE_POSITION = (-4048, "Margin type cannot be changed if there exists position.")
FUT_INVALID_SYMBOL_STATUS = (-4108, "Symbol is not trading or has been delisted.")
FUT_INSUFFICIENT_BALANCE = (-4118, "The transfer amount exceeds the maximum transferable amount.")
FUT_PERCENT_PRICE = (-4131, "The counterparty's best price does not meet the PERCENT_PRICE filter limit.")
FUT_NOTIONAL_TOO_SMALL = (-4164, "Order's notional must be no smaller than %s (unless you choose reduce only).")
FUT_INVALID_ORDER_TYPE_FOR_PRICE = (-4120, "Order type not supported.")

# ---- Wallet / transfer (-3xxx / -5xxx / -9xxx used by SAPI) -----------------------------
TRANSFER_INSUFFICIENT = (-3020, "Transfer out amount exceeds max amount.")
TRANSFER_BAD_ASSET = (-3022, "Asset not supported for transfer.")
TRANSFER_BAD_TYPE = (-1002, "You are not authorized to execute this request.")
SAPI_INSUFFICIENT = (-9000, "Insufficient balance.")

# ---- Twin-specific (never emitted by the real server; surfaced loudly) ------------------
TWIN_UNSUPPORTED = (-9001, "TWIN_UNSUPPORTED: %s")
TWIN_NO_MARKET_DATA = (-9002, "TWIN_NO_MARKET_DATA: %s")
TWIN_POLICY_BLOCK = (-9003, "TWIN_POLICY_BLOCK: %s")

# Convert
CONVERT_QUOTE_EXPIRED = (-4058, "Quote expired.")
CONVERT_BAD_PAIR = (-1121, "Invalid symbol.")


class ToolNotFound(Exception):
    """tool_execute of a name the real server does not have: surfaced verbatim as the JSON-RPC error message."""


def err(pair: tuple[int, str], *fmt: Any) -> BinanceError:
    code, msg = pair
    if fmt:
        msg = msg % fmt
    return BinanceError(code, msg)


def filter_failure(name: str) -> BinanceError:
    return err(FILTER_FAILURE, name)


def mandatory(param: str) -> BinanceError:
    return err(MANDATORY_PARAM, param)
