# Future extensions can add a news blackout and holiday calendar.

from zoneinfo import ZoneInfo


class SessionGuard:
    def __init__(self, broker, config):
        self.broker = broker
        self.portfolio_config = config
        self.timezone = ZoneInfo(config.SESSION_TIMEZONE)

    # =====================================
    # TIME HELPERS
    # =====================================
    def now(self):
        # Session rules must advance even while a symbol has no fresh tick.
        # Broker.utc_now() is canonical system UTC and never interprets a
        # broker wall-clock epoch as UTC.
        now_method = getattr(self.broker, "utc_now", None)
        if not callable(now_method):
            now_method = self.broker.broker_now
        utc_now = now_method()
        if utc_now.tzinfo is None:
            return utc_now.replace(tzinfo=self.timezone)
        return utc_now.astimezone(self.timezone)

    def weekday(self):
        return self.now().weekday()
        # Monday=0 ... Friday=4 Sunday=6

    # =====================================
    # NO NEW TRADES WINDOW
    # =====================================
    def trading_allowed(self):
        return self.reason() is None

    def reason(self):
        now = self.now()
        weekday = now.weekday()

        # Saturday / Sunday
        if weekday >= 5:
            return "SESSION_WEEKEND"

        # Friday restrictions
        if weekday == 4:
            friday_cutoff = now.replace(
                hour=self.portfolio_config.FRIDAY_NO_TRADE_HOUR,
                minute=self.portfolio_config.FRIDAY_NO_TRADE_MINUTE,
                second=0,
                microsecond=0
            )

            if now >= friday_cutoff:
                return "SESSION_FRIDAY_NO_TRADE"

        return None

    # =====================================
    # WEEKEND FLATTEN
    # =====================================
    def must_flatten_positions(self):
        now = self.now()
        weekday = now.weekday()

        if weekday != 4:
            return False

        flatten_time = now.replace(
            hour=self.portfolio_config.FRIDAY_FORCE_CLOSE_HOUR,
            minute=self.portfolio_config.FRIDAY_FORCE_CLOSE_MINUTE,
            second=0,
            microsecond=0
        )

        return now >= flatten_time

    # =====================================
    # STATUS
    # =====================================
    def status(self):
        return {
            "trading_allowed": self.trading_allowed(),
            "reason": self.reason(),
            "must_flatten": self.must_flatten_positions(),
            "broker_time": self.now()
        }
