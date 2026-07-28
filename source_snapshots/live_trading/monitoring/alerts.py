# monitoring/alerts.py

import requests
import time


class Alerts:
    def __init__(
        self,
        enabled=True,
        telegram_enabled=False,
        telegram_token=None,
        telegram_chat_id=None,          # MAIN CHAT
        strategy_chat_id=None,          # STRATEGY CHAT
        telegram_trade_alerts_only=True,
        routing=None,
    ):
        self.enabled = enabled

        self.telegram_enabled = telegram_enabled
        self.telegram_token = telegram_token

        self.telegram_chat_id = telegram_chat_id
        self.strategy_chat_id = strategy_chat_id

        self.telegram_trade_alerts_only = telegram_trade_alerts_only

        # ===============================
        # CENTRAL ROUTING CONFIG
        # ===============================
        default_routing = {
            "heartbeat": {
                "main": False,
                "strategy": True
            },
            "position_opened": {
                "main": False,
                "strategy": True
            },
            "position_closed": {
                "main": False,
                "strategy": True
            },
            "management": {
                "main": False,
                "strategy": True
            },
            "warning": {
                "main": False,
                "strategy": True
            },
            "critical": {
                "main": True,
                "strategy": True
            },
            "terminal": {
                "main": False,
                "strategy": False
            }
        }
        self.routing = {
            channel: dict(destinations)
            for channel, destinations in (routing or default_routing).items()
        }

        # ===============================
        # COOLDOWNS
        # ===============================
        self.cooldowns = {}
        self.default_cooldown_sec = 300

    # =====================================
    # COOLDOWN CHECK
    # =====================================
    def _can_send_with_cooldown(self, key, cooldown_sec=None):
        if cooldown_sec is None:
            cooldown_sec = self.default_cooldown_sec

        now = time.time()
        last = self.cooldowns.get(key)

        if last is None:
            self.cooldowns[key] = now
            return True

        if now - last >= cooldown_sec:
            self.cooldowns[key] = now
            return True

        return False

    # =====================================
    # RAW TELEGRAM SEND
    # =====================================
    def _send_telegram(self, message, chat_id):
        if not self.telegram_enabled:
            return

        if not self.telegram_token:
            return

        if not chat_id:
            return

        try:
            url = (
                f"https://api.telegram.org/"
                f"bot{self.telegram_token}/sendMessage"
            )

            payload = {
                "chat_id": chat_id,
                "text": message
            }

            requests.post(
                url,
                json=payload,
                timeout=5
            )

        except Exception as e:
            print(f"Telegram send failed: {e}")

    # =====================================
    # CHAT SENDERS
    # =====================================
    def _send_main_chat(self, message):
        self._send_telegram(
            message,
            self.telegram_chat_id
        )

    def _send_strategy_chat(self, message):
        self._send_telegram(
            message,
            self.strategy_chat_id
        )

    # =====================================
    # CENTRAL ROUTER
    # =====================================
    def _route_message(self, channel, message):
        route = self.routing.get(channel, {})

        if route.get("main"):
            self._send_main_chat(message)

        if route.get("strategy"):
            self._send_strategy_chat(message)

    # =====================================
    # PUBLIC MANUAL SEND
    # =====================================
    def send_main_message(self, message):
        self._send_main_chat(message)

    def send_strategy_message(self, message, silent=True):
        self._send_strategy_chat(message)

    # =====================================
    # INTERNAL SEND
    # =====================================
    def _send(self, message):
        if not self.enabled:
            return

        print("")
        print(message)
        print("")

    # =====================================
    # TERMINAL MIRROR
    # =====================================
    def send_terminal(self, message):
        print(message)
        self._route_message("terminal", message)

    def send_terminal_throttled(
        self,
        key,
        message,
        cooldown_sec=None
    ):
        if not self._can_send_with_cooldown(
            key,
            cooldown_sec
        ):
            return

        self.send_terminal(message)

    # =====================================
    # GENERIC INFO
    # =====================================
    def send_info(self, message):
        msg = f"INFO\n{message}"
        self._send(msg)

    # =====================================
    # WARNING
    # =====================================
    def send_warning(self, message):
        msg = f"WARNING\n{message}"
        self._send(msg)
        self._route_message("warning", msg)

    def send_throttled_warning(
        self,
        key,
        message,
        cooldown_sec=None
    ):
        if not self._can_send_with_cooldown(
            key,
            cooldown_sec
        ):
            return

        self.send_warning(message)

    # =====================================
    # CRITICAL
    # =====================================
    def send_critical(self, message):
        self._send(message)
        self._route_message("critical", message)

    def send_throttled_critical(
        self,
        key,
        message,
        cooldown_sec=None
    ):
        if not self._can_send_with_cooldown(key, cooldown_sec):
            return
        self.send_critical(message)

    # =====================================
    # PRE-SIGNAL ALERT (DISABLED)
    # =====================================
    def alert_pre_signal(
        self,
        strategy_name,
        side,
        trigger,
        distance_points,
        expected_entry,
        stop_distance,
        tp_distance,
        risk_usd
    ):
        pass

    # =====================================
    # POSITION OPEN ALERT
    # =====================================
    def alert_position_opened(
        self,
        strategy_name,
        side,
        entry,
        sl,
        tp,
        risk_usd,
        volume
    ):
        tp_text = "None" if tp in (None, 0) else str(round(tp, 5))
        msg = (
            f"✅ POSITION OPENED\n\n"
            f"Bot: {strategy_name}\n"
            f"Side: {side.upper()}\n\n"
            f"Entry: {round(entry, 5)}\n"
            f"SL: {round(sl, 5)}\n"
            f"TP: {tp_text}\n\n"
            f"Risk: ${round(risk_usd, 2)}\n"
            f"Lot: {volume}"
        )

        self._send(msg)
        self._route_message("position_opened", msg)

    # =====================================
    # POSITION CLOSE ALERT
    # =====================================
    def alert_position_closed(
        self,
        strategy_name,
        pnl_usd,
        r_multiple,
        reason
    ):
        if pnl_usd >= 0:
            pnl_text = f"+${round(pnl_usd, 2)}"
        else:
            pnl_text = f"-${abs(round(pnl_usd, 2))}"

        if r_multiple >= 0:
            r_text = f"+{round(r_multiple, 2)}"
        else:
            r_text = f"{round(r_multiple, 2)}"

        msg = (
            f"📊 POSITION CLOSED\n\n"
            f"Bot: {strategy_name}\n\n"
            f"PnL: {pnl_text}\n"
            f"R: {r_text}\n"
            f"Reason: {reason}"
        )

        self._send(msg)
        self._route_message("position_closed", msg)

    # =====================================
    # MANAGEMENT ALERT
    # =====================================
    def alert_management(self, message):
        self._send(message)
        self._route_message("management", message)

    def alert_break_even(self, strategy_name, side, ticket, entry, new_sl):
        msg = (
            f"🛡️ BREAK-EVEN ACTIVATED\n\n"
            f"Bot: {strategy_name}\n"
            f"Side: {side.upper()}\n"
            f"Ticket: {ticket}\n\n"
            f"Entry: {round(entry, 5)}\n"
            f"New SL: {round(new_sl, 5)}"
        )
        self.alert_management(msg)

    # =====================================
    # HEARTBEAT ALERT
    # =====================================
    def alert_heartbeat(self, message):
        self._route_message("heartbeat", message)

    # =====================================
    # SYSTEM ISSUE / KILL SWITCH
    # =====================================
    def alert_system_issue(
        self,
        strategy_name,
        reason
    ):
        msg = (
            f"🛑 SYSTEM ISSUE\n\n"
            f"Bot: {strategy_name}\n\n"
            f"{reason}\n"
            f"Trading halted"
        )

        self.send_critical(msg)
