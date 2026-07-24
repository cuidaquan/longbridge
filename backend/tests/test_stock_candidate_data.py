from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.exceptions import LongbridgeAPIError
from app.external_service_resilience import (
    ExternalServiceBusyError,
    ExternalServiceCircuitOpenError,
    ExternalServiceTimeoutError,
)
from app import stock_candidate_data


def _namespace(**values):
    return SimpleNamespace(**values)


class StockCandidateDataTests(unittest.TestCase):
    def test_short_capacity_failure_classification_uses_exception_metadata(
        self,
    ) -> None:
        from longbridge.openapi import ErrorKind, OpenApiException

        cases = (
            (
                ValueError("请先在基础配置中保存完整的 Longbridge 凭据"),
                "credentials_missing",
            ),
            (
                stock_candidate_data.LongbridgeDependencyMissing("missing"),
                "dependency_missing",
            ),
            (
                OpenApiException(
                    ErrorKind.OAuth,
                    401003,
                    "trace-auth",
                    "expired",
                ),
                "authentication_failed",
            ),
            (
                OpenApiException(
                    ErrorKind.Http,
                    429,
                    "trace-rate",
                    "too many requests",
                ),
                "rate_limited",
            ),
            (
                ExternalServiceTimeoutError("trade timeout"),
                "timeout",
            ),
            (
                httpx.ConnectError("connection refused"),
                "network_error",
            ),
            (
                ExternalServiceBusyError("trade busy"),
                "service_busy",
            ),
            (
                ExternalServiceCircuitOpenError("trade circuit open"),
                "circuit_open",
            ),
            (
                OpenApiException(
                    ErrorKind.OpenApi,
                    403001,
                    "trace-reject",
                    "request rejected",
                ),
                "upstream_rejected",
            ),
            (RuntimeError("permission or risk control"), "unknown_error"),
        )

        for error, expected in cases:
            with self.subTest(expected=expected):
                diagnostic = (
                    stock_candidate_data.classify_short_capacity_failure(
                        error
                    )
                )
                self.assertEqual(
                    diagnostic["failure_category"],
                    expected,
                )

        secret_error = RuntimeError(
            "token=secret https://private.example/path"
        )
        diagnostic = stock_candidate_data.classify_short_capacity_failure(
            secret_error
        )
        self.assertNotIn("secret", diagnostic["error"])
        self.assertNotIn("private.example", diagnostic["error"])

    def test_short_capacity_failure_classification_follows_wrapped_cause(
        self,
    ) -> None:
        try:
            raise ExternalServiceTimeoutError("inner timeout")
        except ExternalServiceTimeoutError as cause:
            wrapped = LongbridgeAPIError("outer failure")
            wrapped.__cause__ = cause

        diagnostic = stock_candidate_data.classify_short_capacity_failure(
            wrapped
        )
        self.assertEqual(diagnostic["failure_category"], "timeout")

    def test_market_trading_day_includes_full_and_half_sessions(self) -> None:
        quote_context = MagicMock()
        quote_context.trading_days.side_effect = [
            _namespace(
                trading_days=[date(2026, 7, 24)],
                half_trading_days=[],
            ),
            _namespace(
                trading_days=[],
                half_trading_days=[date(2026, 7, 25)],
            ),
            _namespace(
                trading_days=[],
                half_trading_days=[],
            ),
            None,
        ]

        @contextmanager
        def fake_context(kind):
            self.assertEqual(kind, "quote")
            yield quote_context

        with patch.object(stock_candidate_data, "_context", fake_context):
            full_day = stock_candidate_data.is_market_trading_day(
                "us",
                date(2026, 7, 24),
            )
            half_day = stock_candidate_data.is_market_trading_day(
                "HK",
                date(2026, 7, 25),
            )
            closed_day = stock_candidate_data.is_market_trading_day(
                "US",
                date(2026, 7, 26),
            )
            with self.assertRaisesRegex(
                LongbridgeAPIError,
                "交易日历返回无效",
            ):
                stock_candidate_data.is_market_trading_day(
                    "US",
                    date(2026, 7, 27),
                )

        self.assertTrue(full_day)
        self.assertTrue(half_day)
        self.assertFalse(closed_day)
        self.assertEqual(quote_context.trading_days.call_count, 4)
        with self.assertRaisesRegex(ValueError, "market"):
            stock_candidate_data.is_market_trading_day(
                "JP",
                date(2026, 7, 24),
            )

    def test_tradeability_normalizes_status_and_calculates_depth(self) -> None:
        quote_context = MagicMock()
        quote_context.quote.return_value = [
            _namespace(
                symbol="AAA.US",
                trade_status=_namespace(name="Normal"),
                last_done="100",
                volume=1_000,
                turnover="100000",
                timestamp=datetime(2026, 7, 24, 10, 30),
            ),
            _namespace(
                symbol="HALT.US",
                trade_status=_namespace(name="Halted"),
                last_done="50",
                volume=0,
                turnover="0",
                timestamp=datetime(2026, 7, 24, 10, 30),
            ),
        ]
        quote_context.depth.side_effect = [
            _namespace(
                bids=[
                    _namespace(price="98", volume=500),
                    _namespace(price="99", volume=100),
                ],
                asks=[
                    _namespace(price="102", volume=500),
                    _namespace(price="101", volume=80),
                ],
            ),
            RuntimeError("depth unavailable"),
        ]

        @contextmanager
        def fake_context(kind):
            self.assertEqual(kind, "quote")
            yield quote_context

        with patch.object(stock_candidate_data, "_context", fake_context):
            result = stock_candidate_data.get_security_tradeability(
                ["aaa.us", "HALT.US"],
                include_depth=True,
            )

        self.assertEqual(result["AAA.US"]["trade_status"], "normal")
        self.assertTrue(result["AAA.US"]["is_tradable"])
        self.assertEqual(result["AAA.US"]["best_bid"], 99)
        self.assertEqual(result["AAA.US"]["best_ask"], 101)
        self.assertEqual(result["AAA.US"]["spread_bps"], 200)
        self.assertEqual(result["AAA.US"]["top_of_book_notional"], 8080)
        self.assertEqual(
            result["AAA.US"]["impact_cost_status"],
            "requires_order_size",
        )
        self.assertEqual(result["HALT.US"]["trade_status"], "halted")
        self.assertFalse(result["HALT.US"]["is_tradable"])
        self.assertIsNone(result["HALT.US"]["spread_bps"])
        self.assertIn("depth unavailable", result["HALT.US"]["depth_error"])
        quote_context.quote.assert_called_once_with(["AAA.US", "HALT.US"])

    def test_margin_requirements_do_not_claim_borrow_availability(self) -> None:
        trade_context = MagicMock()
        trade_context.margin_ratio.return_value = _namespace(
            im_factor="0.5",
            mm_factor="0.3",
            fm_factor="0.2",
        )

        @contextmanager
        def fake_context(kind):
            self.assertEqual(kind, "trade")
            yield trade_context

        with patch.object(stock_candidate_data, "_context", fake_context):
            result = stock_candidate_data.get_margin_requirements(["AAA.US"])

        item = result["AAA.US"]
        self.assertEqual(item["initial_margin_ratio"], 0.5)
        self.assertEqual(item["maintenance_margin_ratio"], 0.3)
        self.assertEqual(item["forced_close_margin_ratio"], 0.2)
        self.assertEqual(item["borrow_availability"], "unknown")
        self.assertIsNone(item["borrow_fee_rate"])
        self.assertIn("不代表实时券源", item["note"])

    def test_short_selling_capacity_uses_account_sell_estimate(self) -> None:
        trade_context = MagicMock()
        trade_context.estimate_max_purchase_quantity.side_effect = [
            _namespace(cash_max_qty="10", margin_max_qty="250.5"),
            _namespace(cash_max_qty="0", margin_max_qty="0"),
            RuntimeError("permission or risk control rejected"),
        ]

        @contextmanager
        def fake_context(kind):
            self.assertEqual(kind, "trade")
            yield trade_context

        with patch.object(stock_candidate_data, "_context", fake_context):
            result = stock_candidate_data.get_short_selling_capacity([
                "AAA.US",
                "ZERO.US",
                "ERROR.US",
                "700.HK",
            ])

        available = result["AAA.US"]
        self.assertEqual(available["status"], "available")
        self.assertEqual(available["cash_max_qty"], 10)
        self.assertEqual(available["margin_max_qty"], 250.5)
        self.assertEqual(available["short_selling_max_qty"], 250.5)
        self.assertEqual(available["availability"], "available")
        self.assertIsNone(available["borrow_fee_rate"])
        self.assertEqual(available["recall_risk"], "unknown")

        unavailable = result["ZERO.US"]
        self.assertEqual(unavailable["status"], "available")
        self.assertEqual(unavailable["short_selling_max_qty"], 0)
        self.assertEqual(unavailable["availability"], "unavailable")

        failed = result["ERROR.US"]
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["availability"], "unknown")
        self.assertEqual(failed["failure_category"], "unknown_error")
        self.assertIn("permission or risk control", failed["error"])

        unsupported = result["700.HK"]
        self.assertEqual(unsupported["status"], "unsupported")
        self.assertEqual(unsupported["availability"], "unknown")
        self.assertEqual(
            trade_context.estimate_max_purchase_quantity.call_count,
            3,
        )
        from longbridge.openapi import OrderSide, OrderType

        for call in (
            trade_context.estimate_max_purchase_quantity.call_args_list
        ):
            self.assertEqual(call.args[1], OrderType.LO)
            self.assertEqual(call.args[2], OrderSide.Sell)

    def test_short_selling_capacity_rejects_invalid_sdk_quantities(
        self,
    ) -> None:
        trade_context = MagicMock()
        trade_context.estimate_max_purchase_quantity.return_value = (
            _namespace(cash_max_qty="-1", margin_max_qty="not-a-number")
        )

        @contextmanager
        def fake_context(_kind):
            yield trade_context

        with patch.object(stock_candidate_data, "_context", fake_context):
            result = stock_candidate_data.get_short_selling_capacity([
                "AAA.US",
            ])

        self.assertEqual(result["AAA.US"]["status"], "no_data")
        self.assertEqual(
            result["AAA.US"]["failure_category"],
            "response_no_data",
        )
        self.assertIsNone(result["AAA.US"]["cash_max_qty"])
        self.assertIsNone(result["AAA.US"]["short_selling_max_qty"])
        self.assertEqual(result["AAA.US"]["availability"], "unknown")

    def test_fundamentals_are_directional_and_calendar_is_paginated(self) -> None:
        current_date = date(2026, 7, 24)
        fundamental_context = MagicMock()
        fundamental_context.operating.return_value = _namespace(list=[
            _namespace(
                latest=True,
                financial=_namespace(indicators=[
                    _namespace(
                        field_name="operating_revenue",
                        yoy="12.5%",
                    ),
                    _namespace(
                        field_name="net_profit_attributable_to_parent",
                        yoy="0.2",
                    ),
                    _namespace(
                        field_name="net_cash_flow_from_operating_activities",
                        yoy="-5%",
                    ),
                ]),
            ),
        ])
        fundamental_context.institution_rating.return_value = _namespace(
            latest=_namespace(
                evaluate=_namespace(
                    total=10,
                    buy=6,
                    over=2,
                    sell=1,
                    under=1,
                ),
            ),
            summary=_namespace(target="125.5"),
        )
        fundamental_context.forecast_eps.return_value = _namespace(items=[
            _namespace(
                forecast_end_date=datetime(2026, 12, 31),
                institution_total=5,
                institution_up=4,
                institution_down=1,
            ),
        ])
        fundamental_context.corp_action.return_value = _namespace(items=[
            _namespace(
                date="20260727",
                act_type="dividend",
                act_desc="除息",
            ),
        ])

        calendar_context = MagicMock()
        def calendar_response(_category, start, _end, _market):
            if start == "2026-07-24":
                return _namespace(
                    list=[],
                    next_date="2026-07-27",
                )
            return _namespace(
                list=[
                    _namespace(
                        date="2026-07-29",
                        infos=[
                            _namespace(
                                symbol="AAA.US",
                                date="2026.07.29",
                                content="季度财报",
                                financial_market_time="盘后",
                                star=3,
                            ),
                        ],
                    ),
                ],
                next_date="",
            )

        calendar_context.finance_calendar.side_effect = calendar_response

        @contextmanager
        def fake_context(kind):
            if kind == "fundamental":
                yield fundamental_context
            elif kind == "calendar":
                yield calendar_context
            else:
                self.fail(f"unexpected context: {kind}")

        with patch.object(stock_candidate_data, "_context", fake_context):
            long_result = stock_candidate_data.get_fundamental_profiles(
                ["AAA.US"],
                "US",
                "LONG",
                event_window_days=30,
                include_corporate_actions=True,
                today=current_date,
            )
            short_result = stock_candidate_data.get_fundamental_profiles(
                ["AAA.US"],
                "US",
                "SHORT",
                event_window_days=30,
                include_corporate_actions=False,
                today=current_date,
            )

        long_item = long_result["AAA.US"]
        self.assertEqual(long_item["status"], "available")
        self.assertEqual(long_item["revenue_yoy"], 0.125)
        self.assertEqual(long_item["net_profit_yoy"], 0.2)
        self.assertEqual(long_item["operating_cash_flow_yoy"], -0.05)
        self.assertAlmostEqual(long_item["analyst_alignment"], 0.6)
        self.assertAlmostEqual(long_item["eps_revision_alignment"], 0.6)
        self.assertEqual(long_item["target_price"], 125.5)
        self.assertEqual(long_item["days_to_financial_event"], 5)
        self.assertEqual(long_item["financial_event"]["market_time"], "盘后")
        self.assertEqual(long_item["days_to_corporate_action"], 3)
        self.assertAlmostEqual(
            short_result["AAA.US"]["analyst_alignment"],
            -0.6,
        )
        self.assertAlmostEqual(
            short_result["AAA.US"]["eps_revision_alignment"],
            -0.6,
        )

    def test_partial_fundamental_failures_are_explicit(self) -> None:
        fundamental_context = MagicMock()
        fundamental_context.operating.side_effect = RuntimeError("operating")
        fundamental_context.institution_rating.return_value = _namespace(
            latest=_namespace(
                evaluate=_namespace(
                    total=1,
                    buy=1,
                    over=0,
                    sell=0,
                    under=0,
                ),
            ),
            summary=_namespace(target="100"),
        )
        fundamental_context.forecast_eps.side_effect = RuntimeError("forecast")
        calendar_context = MagicMock()
        calendar_context.finance_calendar.return_value = _namespace(
            list=[],
            next_date="",
        )

        @contextmanager
        def fake_context(kind):
            yield (
                fundamental_context
                if kind == "fundamental"
                else calendar_context
            )

        with patch.object(stock_candidate_data, "_context", fake_context):
            result = stock_candidate_data.get_fundamental_profiles(
                ["AAA.US"],
                "US",
                "LONG",
                today=date(2026, 7, 24),
            )

        item = result["AAA.US"]
        self.assertEqual(item["status"], "partial")
        self.assertEqual(item["analyst_alignment"], 1)
        self.assertIsNone(item["revenue_yoy"])
        self.assertTrue(any("operating" in error for error in item["errors"]))
        self.assertTrue(any("forecast" in error for error in item["errors"]))

    def test_invalid_direction_and_context_failure_are_not_silenced(self) -> None:
        with self.assertRaisesRegex(ValueError, "LONG 或 SHORT"):
            stock_candidate_data.get_fundamental_profiles(
                ["AAA.US"],
                "US",
                "SIDEWAYS",
            )

        with patch.object(
            stock_candidate_data,
            "_context",
            side_effect=LongbridgeAPIError("connection failed"),
        ):
            with self.assertRaisesRegex(LongbridgeAPIError, "connection failed"):
                stock_candidate_data.get_security_tradeability(["AAA.US"])

    def test_ratio_and_date_parsers_preserve_missing_values(self) -> None:
        self.assertEqual(stock_candidate_data._ratio("12.5%"), 0.125)
        self.assertEqual(stock_candidate_data._ratio("0.125"), 0.125)
        self.assertIsNone(stock_candidate_data._ratio(""))
        self.assertEqual(
            stock_candidate_data._parse_date("20260724"),
            date(2026, 7, 24),
        )
        self.assertEqual(
            stock_candidate_data._parse_date("2026.07.24"),
            date(2026, 7, 24),
        )
        self.assertEqual(
            stock_candidate_data._parse_date("2026-07-24T09:30:00"),
            date(2026, 7, 24),
        )
        self.assertIsNone(stock_candidate_data._parse_date("unknown"))
        self.assertEqual(
            stock_candidate_data._best_depth(
                [
                    _namespace(price="99", volume=100),
                    _namespace(price="99", volume=50),
                    _namespace(price="98", volume=500),
                ],
                highest=True,
            ),
            (99, 150),
        )

    def test_installed_sdk_exposes_required_candidate_data_methods(self) -> None:
        from longbridge.openapi import (
            CalendarContext,
            FundamentalContext,
            QuoteContext,
            TradeStatus,
            TradeContext,
        )

        self.assertTrue(hasattr(QuoteContext, "quote"))
        self.assertTrue(hasattr(QuoteContext, "depth"))
        self.assertTrue(hasattr(QuoteContext, "trading_days"))
        self.assertTrue(hasattr(TradeContext, "margin_ratio"))
        self.assertTrue(
            hasattr(TradeContext, "estimate_max_purchase_quantity")
        )
        self.assertTrue(hasattr(FundamentalContext, "operating"))
        self.assertTrue(hasattr(FundamentalContext, "institution_rating"))
        self.assertTrue(hasattr(FundamentalContext, "forecast_eps"))
        self.assertTrue(hasattr(FundamentalContext, "corp_action"))
        self.assertTrue(hasattr(CalendarContext, "finance_calendar"))
        self.assertEqual(
            stock_candidate_data._enum_name(TradeStatus.Normal),
            "normal",
        )


if __name__ == "__main__":
    unittest.main()
