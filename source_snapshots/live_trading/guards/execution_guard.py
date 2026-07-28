# core/execution_guard.py

from datetime import timedelta


class ExecutionGuard:
    def __init__(self, config):
        self.portfolio_config = config

        # global order throttle
        self.last_order_time = None

        # active pending execution
        self.pending_execution = False
        self.pending_since = None

    # =====================================
    # CAN SEND NEW ORDER?
    # =====================================
    def can_send_order(self, broker_now):
        """
        Returns True if execution engine is allowed
        to send a fresh order.
        """

        # block while waiting broker confirmation
        if self.pending_execution:
            return False

        # cooldown between orders
        if self.last_order_time is not None:
            delta = broker_now - self.last_order_time

            if delta.total_seconds() < self.portfolio_config.EXECUTION_COOLDOWN_SEC:
                return False

        return True

    # =====================================
    # MARK ORDER SENT
    # =====================================
    def mark_order_sent(self, broker_now):
        self.last_order_time = broker_now
        self.pending_execution = True
        self.pending_since = broker_now

    # =====================================
    # MARK FILL SUCCESS
    # =====================================
    def mark_fill_success(self):
        self.pending_execution = False
        self.pending_since = None

    # =====================================
    # MARK FAILURE / REJECT
    # =====================================
    def mark_order_failed(self):
        self.pending_execution = False
        self.pending_since = None

    # =====================================
    # STALE PENDING DETECTION
    # =====================================
    def pending_timed_out(self, broker_now):
        """
        Protection if broker never confirms order.
        """

        if not self.pending_execution:
            return False

        delta = broker_now - self.pending_since

        if delta.total_seconds() >= self.portfolio_config.PENDING_TIMEOUT_SEC:
            return True

        return False

    # =====================================
    # FORCE RESET
    # =====================================
    def reset(self):
        self.pending_execution = False
        self.pending_since = None