"""Fee schedule. Fee asset is always the quote asset (BNB discount off)."""

from __future__ import annotations

from decimal import Decimal

from rehearsal.config import FeesConfig


class FeeSchedule:
    def __init__(self, cfg: FeesConfig):
        self.rates = {
            ("spot", True): Decimal(str(cfg.spot_maker)),
            ("spot", False): Decimal(str(cfg.spot_taker)),
            ("usdm", True): Decimal(str(cfg.usdm_maker)),
            ("usdm", False): Decimal(str(cfg.usdm_taker)),
        }

    def rate(self, market: str, is_maker: bool) -> Decimal:
        return self.rates[(market, is_maker)]

    def commission(self, market: str, is_maker: bool, quote_qty: Decimal) -> Decimal:
        return (quote_qty * self.rate(market, is_maker)).quantize(Decimal("0.00000001"))

    def as_bps(self, market: str, is_maker: bool) -> int:
        return int(self.rate(market, is_maker) * 10000)
