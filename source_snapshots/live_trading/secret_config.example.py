# secret_config.example.py
#
# Template only. Copy to ignored secret_config.py and fill real local values.
# Do not commit credentials, tokens, account numbers, chat IDs, terminals, or
# runtime database files.

TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = "REPLACE_WITH_REAL_TELEGRAM_BOT_TOKEN"  # pragma: allowlist secret
MAIN_CHAT_ID = -1000000000000

# Keys must match enabled strategy IDs in strategies.yaml.
CHAT_IDS = {
    "audcad_h4_reversion": -1000000000001,
    "eurgbp_h4_reversion_return_filter": -1000000000002,
    "xau_h4_continuation_breakout": -1000000000003,
}

# Keys must match accounts.py:ACCOUNTS.
ACCOUNTS = {
    "hub_demo": {
        "login": 11111,
        "password": "REPLACE_WITH_REAL_PASSWORD",  # pragma: allowlist secret
        "server": "Demo-Server",
        "mt5_path": r"C:\Path\To\terminal64.exe",
    },
}
