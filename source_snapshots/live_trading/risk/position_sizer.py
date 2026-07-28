import math


class PositionSizer:
    def __init__(
        self,
        broker,
        risk_buffer=0.98,
        allow_undersized_lot=False,
        max_lot=None,
        sanity_multiplier=20
    ):
        self.broker = broker
        self.risk_buffer = risk_buffer
        self.allow_undersized_lot = allow_undersized_lot
        # SAFETY NET : absolute per-symbol lot ceiling.
        # If None, no ceiling is enforced. Set via strategy config MAX_LOT.
        self.max_lot = max_lot
        # SAFETY NET: a lot is "anomalous" if its implied risk exceeds
        # risk_usd by more than this factor. Guards against any future
        # recurrence of the x100 sizing bug (bad broker spec, garbage
        # order_calc_profit, fallback formula gone wrong). 20x chosen so
        # legitimate configs never trip it while x100+ always does.
        self.sanity_multiplier = sanity_multiplier

    # =====================================
    # RISK PER 1 LOT - broker-aware 
    # =====================================
    def _risk_per_lot(self, entry_price, stop_price, tick_size,
                      tick_value, symbol_info):
        """
        Risk (in account currency) of holding 1 lot from entry to stop.

        Primary path: MT5 order_calc_profit via broker.estimate_profit_per_lot.
        It accounts for contract_size + currency conversion, so it is correct
        on ANY broker - including broker setups where trade_tick_value is
        returned "per unit" (e.g. per ounce for XAU) WITHOUT contract_size
        baked in. On such brokers the manual tick formula yields a value ~100x
        too small -> lot ~100x too large.

        Fallback: manual `(stop_distance / tick_size) * tick_value`, used when
        order_calc_profit is unavailable / returns None / gives a nonsensical
        value. Correct for standard brokers; may be wrong on some nonstandard symbol specifications.

        Returns:
            (risk_per_1_lot: float, source: str, fallback_value: float|None)
            source is "order_calc_profit" or "tick_formula".
            fallback_value is the manual-formula estimate (always computed)
            so the caller can detect a broker-spec desync.
        """
        # Manual tick-formula estimate - always computed for diagnostics
        # and as a fallback.
        stop_distance = abs(entry_price - stop_price)
        ticks_to_sl = stop_distance / tick_size
        fallback_risk = ticks_to_sl * tick_value

        # Primary path: let MT5 do the math for this account.
        mt5_risk = self.broker.estimate_profit_per_lot(
            open_price=entry_price,
            close_price=stop_price
        )

        if mt5_risk is not None and mt5_risk > 0:
            return mt5_risk, "order_calc_profit", fallback_risk

        # Fallback - manual formula. NOTE: may be wrong when
        # contract_size is not baked into tick_value.
        return fallback_risk, "tick_formula", fallback_risk

    # =====================================
    # ROUND DOWN TO BROKER LOT STEP
    # =====================================
    def _round_down_to_step(self, lot, step):
        if step <= 0:
            return lot

        rounded = math.floor(lot / step) * step
        return round(rounded, 8)

    # =====================================
    # POSITION SIZE CALCULATION
    # =====================================
    def calculate_lot(
        self,
        entry_price,
        stop_price,
        risk_usd
    ):
        symbol_info = self.broker.get_symbol_info()

        if symbol_info is None:
            return {
                "valid": False,
                "reason": "NO_SYMBOL_INFO"
            }

        tick_size = symbol_info.trade_tick_size
        tick_value = symbol_info.trade_tick_value

        volume_min = symbol_info.volume_min
        volume_step = symbol_info.volume_step
        volume_max = symbol_info.volume_max

        if tick_size <= 0:
            return {
                "valid": False,
                "reason": "INVALID_TICK_SIZE"
            }

        if tick_value <= 0:
            return {
                "valid": False,
                "reason": "INVALID_TICK_VALUE"
            }

        stop_distance = abs(entry_price - stop_price)

        if stop_distance <= 0:
            return {
                "valid": False,
                "reason": "INVALID_STOP_DISTANCE"
            }

        if risk_usd <= 0:
            return {
                "valid": False,
                "reason": "INVALID_RISK"
            }

        effective_risk = risk_usd * self.risk_buffer

        # Broker-aware risk per 1 lot. See _risk_per_lot docstring.
        risk_per_1_lot, risk_source, manual_risk = self._risk_per_lot(
            entry_price, stop_price, tick_size, tick_value, symbol_info
        )

        if risk_per_1_lot <= 0:
            return {
                "valid": False,
                "reason": "INVALID_RISK_PER_LOT"
            }

        raw_lot = effective_risk / risk_per_1_lot

        # =====================================
        # DIAGNOSTIC LOG  - permanent
        # Helps catch broker-spec desync (e.g. tick_value without
        # contract_size) at a glance. Emits on every calculate_lot call.
        # =====================================
        contract_size = getattr(
            symbol_info, "trade_contract_size", "?"
        )
        print(
            f"[LOTSIZE] symbol={self.broker.symbol} "
            f"tick_size={tick_size} tick_value={tick_value} "
            f"contract_size={contract_size} "
            f"stop_distance={stop_distance} "
            f"risk_per_1_lot={risk_per_1_lot:.4f} "
            f"(source={risk_source}) raw_lot={raw_lot:.6f}"
        )

        # =====================================
        # SAFETY CHECK 
        # If order_calc_profit disagrees with the manual tick formula by
        # more than 5x, the broker's symbol spec is likely desynced
        # (contract_size not baked into tick_value). Log a warning so it
        # is visible even when the primary path happens to be correct.
        # =====================================
        if (
            risk_source == "order_calc_profit"
            and manual_risk is not None
            and manual_risk > 0
        ):
            ratio = risk_per_1_lot / manual_risk
            if ratio > 5 or ratio < 0.2:
                print(
                    f"[LOTSIZE WARNING] symbol={self.broker.symbol} | "
                    f"broker-spec desync: order_calc_profit risk "
                    f"({risk_per_1_lot:.4f}) differs {ratio:.1f}x from "
                    f"manual tick formula ({manual_risk:.4f}) | "
                    f"using order_calc_profit"
                )

        # =====================================
        # HARD SAFETY NET #1 - sanity check on raw_lot 
        # Compare the chosen lot against what the independent manual tick
        # formula would have produced. If order_calc_profit yields a lot
        # more than `sanity_multiplier`x LARGER than the manual estimate,
        # the two methods radically disagree in the DANGEROUS direction
        # (bigger lot = more money at risk) and we refuse to send the
        # order rather than guess which method is right.
        #
        # Asymmetric on purpose: a lot SMALLER than the manual estimate is
        # harmless (we just risk less). Only an inflated lot can blow up
        # the account.
        #
        # NOTE: this only fires when the two methods disagree. If BOTH
        # return the same per-unit value, this check will not catch it - that case is covered
        # by the max_lot ceiling below. The two nets are complementary.
        # =====================================
        if manual_risk is not None and manual_risk > 0:
            manual_lot = effective_risk / manual_risk
            if (
                manual_lot > 0
                and raw_lot > manual_lot * self.sanity_multiplier
            ):
                ratio = raw_lot / manual_lot
                print(
                    f"[LOTSIZE BLOCK] symbol={self.broker.symbol} | "
                    f"ANOMALOUS LOT: raw_lot={raw_lot:.6f} is {ratio:.1f}x "
                    f"larger than manual-formula lot={manual_lot:.6f} "
                    f"(limit {self.sanity_multiplier}x) - methods disagree "
                    f"in dangerous direction | order BLOCKED"
                )
                return {
                    "valid": False,
                    "reason": "ANOMALOUS_LOT",
                    "raw_lot": round(raw_lot, 6),
                    "manual_lot": round(manual_lot, 6),
                    "ratio": round(ratio, 2),
                    "risk_source": risk_source
                }

        # =====================================
        # HARD SAFETY NET #2 - absolute max_lot ceiling 
        # The definitive backstop: never exceed the per-symbol configured
        # ceiling, no matter what any formula returned. This is the ONLY
        # net that catches the case where BOTH sizing methods return the
        # same wrong (per-unit) value. Blocks (does not silently cap) so
        # the misconfig is investigated rather than masked.
        # =====================================
        if self.max_lot is not None and raw_lot > self.max_lot:
            print(
                f"[LOTSIZE BLOCK] symbol={self.broker.symbol} | "
                f"raw_lot={raw_lot:.6f} exceeds MAX_LOT={self.max_lot} | "
                f"order BLOCKED"
            )
            return {
                "valid": False,
                "reason": "LOT_ABOVE_MAX",
                "raw_lot": round(raw_lot, 6),
                "max_lot": self.max_lot,
                "risk_source": risk_source
            }

        rounded_lot = self._round_down_to_step(raw_lot, volume_step)

        if rounded_lot < volume_min:
            if not self.allow_undersized_lot:
                return {
                    "valid": False,
                    "reason": "LOT_BELOW_MIN"
                }

            rounded_lot = volume_min

        rounded_lot = min(rounded_lot, volume_max)
        actual_risk = rounded_lot * risk_per_1_lot

        if actual_risk > risk_usd:
            smaller_lot = rounded_lot - volume_step

            if smaller_lot >= volume_min:
                rounded_lot = smaller_lot
                actual_risk = rounded_lot * risk_per_1_lot
            else:
                return {
                    "valid": False,
                    "reason": "CANNOT_FIT_RISK"
                }

        return {
            "valid": True,
            "lot": round(rounded_lot, 4),
            "actual_risk_usd": round(actual_risk, 2),
            "risk_per_1_lot": round(risk_per_1_lot, 2),
            "risk_source": risk_source,
            "stop_distance": stop_distance
        }