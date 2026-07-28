"""Tests for the P0-T01 offline configuration validator.

These fixtures are deliberately self-contained: no test imports MT5, a
strategy package, or secret_config.py.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


LIVE_ROOT = Path(__file__).resolve().parent.parent
if str(LIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(LIVE_ROOT))

from shared.config_validator import ConfigurationValidationError, validate_configuration  # noqa: E402


class ConfigValidatorTests(unittest.TestCase):
    def make_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "strategies" / "alpha").mkdir(parents=True)
        (root / "accounts.py").write_text("ACCOUNTS = {'hub_demo': {}}\n", encoding="utf-8")
        (root / "portfolio_config.py").write_text(
            "MARKET_GUARD_BY_ASSET = {'FX': {}, 'METAL': {}, 'INDEX': {}}\n",
            encoding="utf-8",
        )
        (root / "strategies.yaml").write_text(
            textwrap.dedent(
                """\
                strategies:
                  alpha:
                    symbol: EURUSD
                    asset_class: FX
                    magic: 101
                    account: hub_demo
                    enabled: true
                """
            ),
            encoding="utf-8",
        )
        (root / "strategies" / "alpha" / "config.py").write_text(
            textwrap.dedent(
                """\
                STRATEGY_NAME = 'ALPHA'
                SYMBOL = 'EURUSD'
                ASSET_CLASS = 'FX'
                MAGIC = 101
                ACCOUNT = 'hub_demo'
                SIGNAL_TIMEFRAME = 'H1'
                RISK_PER_TRADE_USD = 10
                DAILY_SL_LIMIT_USD = None
                WEEKLY_SL_LIMIT_USD = 100
                """
            ),
            encoding="utf-8",
        )
        (root / "strategies" / "alpha" / "strategy.py").write_text(
            "class AlphaStrategy:\n    pass\n", encoding="utf-8"
        )
        (root / "secret_config.py").write_text(
            textwrap.dedent(
                """\
                CHAT_IDS = {'alpha': -100123}
                ACCOUNTS = {
                    'hub_demo': {
                        'login': 12345,
                        'password': 'valid-test-password',  # pragma: allowlist secret
                        'server': 'Demo-Server',
                        'mt5_path': 'C:/MT5/terminal64.exe',
                    }
                }
                """
            ),
            encoding="utf-8",
        )
        return directory, root

    def assert_invalid(self, root: Path, message: str) -> None:
        with self.assertRaisesRegex(ConfigurationValidationError, message):
            validate_configuration(root)

    def test_valid_configuration(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        result = validate_configuration(root)
        self.assertEqual(result.accounts, ("hub_demo",))
        self.assertEqual(result.strategies, ("alpha",))

    def test_invalid_margin_policy_is_rejected_when_configured(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        with (root / "portfolio_config.py").open("a", encoding="utf-8") as stream:
            stream.write("MAX_MARGIN_UTILIZATION = 1.0\n")
        self.assert_invalid(root, "invalid MAX_MARGIN_UTILIZATION")

    def test_missing_account(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "strategies.yaml").write_text(
            "strategies:\n  alpha:\n    symbol: EURUSD\n    asset_class: FX\n    magic: 101\n    account: unknown\n    enabled: true\n",
            encoding="utf-8",
        )
        self.assert_invalid(root, "unknown account")

    def test_missing_secret_credentials(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "secret_config.py").write_text("CHAT_IDS = {'alpha': -100123}\nACCOUNTS = {}\n", encoding="utf-8")
        self.assert_invalid(root, "missing credentials")

    def test_missing_chat_id(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "secret_config.py").write_text(
            "CHAT_IDS = {}\nACCOUNTS = {'hub_demo': {'login': 1, 'password': 'valid', 'server': 'demo', 'mt5_path': 'C:/MT5'}}\n",
            encoding="utf-8",
        )
        self.assert_invalid(root, "CHAT_IDS")

    def test_duplicate_magic(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "strategies.yaml").write_text(
            "strategies:\n  alpha:\n    symbol: EURUSD\n    asset_class: FX\n    magic: 101\n    account: hub_demo\n    enabled: true\n  beta:\n    symbol: GBPUSD\n    asset_class: FX\n    magic: 101\n    account: hub_demo\n    enabled: false\n",
            encoding="utf-8",
        )
        (root / "strategies" / "beta").mkdir()
        (root / "strategies" / "beta" / "config.py").write_text(
            "STRATEGY_NAME = 'BETA'\nSYMBOL = 'GBPUSD'\nMAGIC = 101\nACCOUNT = 'hub_demo'\nSIGNAL_TIMEFRAME = 'H1'\nRISK_PER_TRADE_USD = 1\nDAILY_SL_LIMIT_USD = None\nWEEKLY_SL_LIMIT_USD = None\n",
            encoding="utf-8",
        )
        (root / "strategies" / "beta" / "strategy.py").write_text("class BetaStrategy:\n    pass\n", encoding="utf-8")
        self.assert_invalid(root, "duplicate magic")

    def test_missing_strategy_package(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "strategies" / "alpha" / "strategy.py").unlink()
        self.assert_invalid(root, "missing strategy.py")

    def test_malformed_yaml(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "strategies.yaml").write_text("strategies: [not: valid\n", encoding="utf-8")
        self.assert_invalid(root, "invalid YAML")

    def test_disabled_strategy_does_not_require_credentials_or_chat(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "strategies.yaml").write_text(
            "strategies:\n  alpha:\n    symbol: EURUSD\n    asset_class: FX\n    magic: 101\n    account: hub_demo\n    enabled: false\n",
            encoding="utf-8",
        )
        (root / "secret_config.py").write_text("CHAT_IDS = {}\nACCOUNTS = {}\n", encoding="utf-8")
        result = validate_configuration(root)
        self.assertEqual(result.accounts, ())
        self.assertEqual(result.strategies, ())

    def test_reset_flag_must_be_boolean_and_needs_no_extra_fields(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "accounts.py").write_text(
            "ACCOUNTS = {'hub_demo': {'risk_rules': {'reset_state_on_startup': True}}}\n",
            encoding="utf-8",
        )
        self.assertEqual(validate_configuration(root).accounts, ("hub_demo",))
        (root / "accounts.py").write_text(
            "ACCOUNTS = {'hub_demo': {'risk_rules': {'reset_state_on_startup': 'yes'}}}\n",
            encoding="utf-8",
        )
        self.assert_invalid(root, "reset_state_on_startup must be boolean")

    def test_validator_uses_no_mt5_import_or_initialization(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        (root / "secret_config.py").write_text(
            "import MetaTrader5 as mt5\nmt5.initialize()\nCHAT_IDS = {'alpha': -100123}\nACCOUNTS = {'hub_demo': {'login': 1, 'password': 'valid', 'server': 'demo', 'mt5_path': 'C:/MT5'}}\n",
            encoding="utf-8",
        )
        validate_configuration(root)  # Static parsing must not run the two first lines.

    def test_cli_works_from_different_current_directory(self) -> None:
        directory, root = self.make_fixture()
        self.addCleanup(directory.cleanup)
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        completed = subprocess.run(
            [sys.executable, str(LIVE_ROOT / "shared" / "config_validator.py"), "--root", str(root)],
            cwd=elsewhere,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("configuration valid", completed.stdout)

    def test_real_registry_proxies_validate_with_temporary_safe_secret(self) -> None:
        """Static validation accepts the production proxy shape without MT5."""
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "secret_config.py"
            secret.write_text(
                "CHAT_IDS = {" + ", ".join(
                    f"{name!r}: -100{index}"
                    for index, name in enumerate((
                        "audcad_h4_reversion",
                        "xau_h4_continuation_breakout",
                        "eurgbp_h4_reversion_return_filter",
                    ), 1)
                ) + "}\n"
                "ACCOUNTS = {'hub_demo': {'login': 1, 'password': 'temporary-safe-test-value', "  # pragma: allowlist secret
                "'server': 'Demo', 'mt5_path': 'C:/MT5/terminal64.exe'}}\n",
                encoding="utf-8",
            )
            result = validate_configuration(LIVE_ROOT, secret_config_path=secret)
            self.assertEqual(len(result.strategies), 3)


if __name__ == "__main__":
    unittest.main()
