import time


class MarketGuard:
    OPERATOR_REASONS = {
        "MARKET_NO_TICK": "NO TICK",
        "MARKET_INVALID_TICK_TIME": "BAD TICK",
        "MARKET_TICK_AGE_LIMIT": "STALE TICK",
        "MARKET_TICK_TIME_IN_FUTURE": "FUTURE TICK",
        "MARKET_INVALID_POINT": "BAD POINT",
        "MARKET_SPREAD_LIMIT": "SPREAD",
        "MARKET_STALE_PRICE": "STALE PRICE",
        "MARKET_TICK_JUMP_LIMIT": "PRICE JUMP",
    }

    def __init__(
        self,
        broker,
        max_spread_points,
        max_tick_age_sec,
        max_tick_jump_points,
        max_stale_price_sec=None,
        time_fn=time.time,
    ):
        self.broker = broker
        self.symbol = broker.symbol
        self.point = broker.get_symbol_info().point

        self.max_spread_points = max_spread_points
        self.max_tick_age_sec = max_tick_age_sec
        self.max_tick_jump_points = max_tick_jump_points
        self.max_stale_price_sec = max_stale_price_sec
        self._time = time_fn

        self.last_bid = None
        self.last_tick_time = None
        self.last_price_change_time = None
        self.jump_candidate_bid = None
        self.blocked_reason = None

    # =====================================
    # UPDATE GUARD STATE
    # =====================================
    def update(self, tick):
        self.blocked_reason = None

        if tick is None:
            self.blocked_reason = "MARKET_NO_TICK"
            return

        now = self._time()
        tick_timestamp = getattr(tick, "time", None)
        if tick_timestamp is not None:
            try:
                tick_age = now - float(tick_timestamp)
            except (TypeError, ValueError):
                self.blocked_reason = "MARKET_INVALID_TICK_TIME"
                return
            if tick_age > self.max_tick_age_sec:
                self.blocked_reason = "MARKET_TICK_AGE_LIMIT"
                return
            if tick_age < -5:
                self.blocked_reason = "MARKET_TICK_TIME_IN_FUTURE"
                return

        if self.point is None or self.point <= 0:
            self.blocked_reason = "MARKET_INVALID_POINT"
            return

        spread_points = (tick.ask - tick.bid) / self.point

        # =========================
        # SPREAD CHECK
        # =========================
        if spread_points > self.max_spread_points:
            self.blocked_reason = "MARKET_SPREAD_LIMIT"
            return

        # =========================
        # BAD TICK JUMP CHECK
        # =========================
        if self.last_bid is not None:
            jump_points = abs(tick.bid - self.last_bid) / self.point

            if jump_points > self.max_tick_jump_points:
                # One isolated outlier remains blocked.  A second tick close
                # to the same new level confirms a genuine market gap and
                # promotes that level to the new baseline, preventing a
                # permanent block after overnight/weekend repricing.
                if self.jump_candidate_bid is not None:
                    confirmation_jump = (
                        abs(tick.bid - self.jump_candidate_bid) / self.point
                    )
                    if confirmation_jump <= self.max_tick_jump_points:
                        self.last_bid = tick.bid
                        self.last_tick_time = now
                        self.last_price_change_time = now
                        self.jump_candidate_bid = None
                        return
                self.jump_candidate_bid = tick.bid
                self.blocked_reason = "MARKET_TICK_JUMP_LIMIT"
                return

        self.jump_candidate_bid = None

        # =========================
        # FROZEN FEED CHECK
        # =========================
        self.last_tick_time = now

        # =========================
        # PRICE STALE CHECK
        # =========================
        if self.last_bid is None or tick.bid != self.last_bid:
            self.last_price_change_time = now

        if (
            self.max_stale_price_sec is not None
            and self.last_price_change_time is not None
        ):
            stale_age = now - self.last_price_change_time

            if stale_age > self.max_stale_price_sec:
                self.blocked_reason = "MARKET_STALE_PRICE"
                return

        self.last_bid = tick.bid

    # =====================================
    # STATE
    # =====================================
    def can_trade(self):
        return self.blocked_reason is None

    def reason(self):
        return self.blocked_reason

    def operator_reason(self):
        if self.blocked_reason is None:
            return None
        return self.OPERATOR_REASONS.get(self.blocked_reason, "MARKET")

    def status_snapshot(self):
        return {
            "symbol": self.symbol,
            "can_trade": self.can_trade(),
            "reason": self.blocked_reason,
            "operator_reason": self.operator_reason(),
            "last_bid": self.last_bid,
            "last_tick_time": self.last_tick_time,
            "last_price_change_time": self.last_price_change_time,
            "jump_candidate_bid": self.jump_candidate_bid,
        }
