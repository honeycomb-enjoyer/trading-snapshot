"""Account-scoped position operations with fail-closed flatten reporting."""

from dataclasses import dataclass, field

from core.broker import AlreadyClosedPosition, PositionQueryError


@dataclass
class FlatVerification:
    remaining_tickets: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def is_flat(self):
        return self.error is None and not self.remaining_tickets


@dataclass
class FlattenResult:
    reason: str
    attempted_tickets: list[int] = field(default_factory=list)
    closed_tickets: list[int] = field(default_factory=list)
    already_closed_tickets: list[int] = field(default_factory=list)
    failed_tickets: dict[int, str] = field(default_factory=dict)
    remaining_tickets: list[int] = field(default_factory=list)
    verification_error: str | None = None

    @property
    def is_flat(self):
        return self.verification_error is None and not self.remaining_tickets


class AccountPositionService:
    """The only account-level close path; it never filters by magic/symbol."""

    def __init__(self, broker):
        self.broker = broker

    def list_all_positions(self):
        return self.broker.list_all_positions()

    def close_position_by_ticket(self, ticket):
        """Close one ticket and preserve MT5 result/error context."""
        try:
            result = self.broker.close_position_by_ticket(ticket)
        except PositionQueryError as exc:
            return "failed", str(exc)
        except Exception as exc:
            return "failed", f"unexpected close error: {exc}"

        if isinstance(result, AlreadyClosedPosition):
            return "already_closed", None
        if result is None:
            return "failed", "MT5 order_send returned no result"

        retcode = getattr(result, "retcode", None)
        done = getattr(self.broker, "trade_retcode_done", None)
        if done is None:
            # The real broker intentionally exposes no MT5 constants here;
            # this keeps the service broker-testable.
            import MetaTrader5 as mt5
            done = mt5.TRADE_RETCODE_DONE

        if retcode == done:
            return "closed", None
        return "failed", self._result_context(result)

    def flatten_account(self, reason):
        """Close every currently open ticket, then confirm account flatness."""
        result = FlattenResult(reason=reason)
        try:
            positions = self.list_all_positions()
        except Exception as exc:
            result.verification_error = f"could not list positions: {exc}"
            return result

        for position in positions:
            ticket = int(position.ticket)
            result.attempted_tickets.append(ticket)
            status, error = self.close_position_by_ticket(ticket)
            if status == "closed":
                result.closed_tickets.append(ticket)
            elif status == "already_closed":
                result.already_closed_tickets.append(ticket)
            else:
                result.failed_tickets[ticket] = error

        verification = self.verify_flat()
        result.remaining_tickets = verification.remaining_tickets
        result.verification_error = verification.error
        return result

    def verify_flat(self):
        """Re-query MT5; unknown state is not reported as flat."""
        try:
            positions = self.list_all_positions()
        except Exception as exc:
            return FlatVerification(error=str(exc))
        return FlatVerification(
            remaining_tickets=[int(position.ticket) for position in positions]
        )

    @staticmethod
    def _result_context(result):
        fields = ("retcode", "comment", "request_id", "deal", "order")
        context = {
            name: getattr(result, name)
            for name in fields
            if getattr(result, name, None) is not None
        }
        return f"MT5 close failed: {context or result!r}"
