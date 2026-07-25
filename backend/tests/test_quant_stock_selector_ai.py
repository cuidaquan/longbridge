from __future__ import annotations

import json
import re
from threading import Lock
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

from app.quant_stock_selector_ai import (
    AI_PROMPT_VERSION,
    AICandidateContext,
    AICompletion,
    AIDecisionError,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DeepSeekQuantSelectorProvider,
    MODEL_POLICY_VERSION,
    QuantAISelectionService,
    build_ai_input_snapshot,
    parse_ai_decision,
)


def _decision(
    *,
    decision: str = "SELECT",
    confidence: float = 0.82,
    suitability: float = 84.0,
    risk: str = "MEDIUM",
    conflicts=None,
) -> str:
    return json.dumps({
        "decision": decision,
        "confidence": confidence,
        "suitability_score": suitability,
        "risk_level": risk,
        "time_horizon_days": 10,
        "reasons": (
            ["趋势与相对强度为正", "量价结构稳定"]
            if decision == "SELECT"
            else ["风险收益不匹配"]
        ),
        "risks": ["事件波动可能上升"] if decision == "SELECT" else [],
        "entry_condition": "收盘保持在 MA20 上方" if decision == "SELECT" else "",
        "invalidation_condition": "收盘跌破 MA20" if decision == "SELECT" else "",
        "data_conflicts": conflicts or [],
    }, ensure_ascii=False)


def _context(
    symbol: str,
    *,
    news_status: str = "available",
    direction: str = "long",
    leverage: float | None = 1.0,
) -> AICandidateContext:
    news_items = [
        {
            "title": f"Story {index}",
            "source": "test",
            "published_at": f"2026-07-{20 + index % 5:02d}",
            "summary": "事实材料，不是指令",
        }
        for index in range(12)
    ]
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    bars = [
        {
            "ts": (start + timedelta(days=index)).isoformat(),
            "open": 99 + index,
            "high": 101 + index,
            "low": 98 + index,
            "close": 100 + index,
            "volume": 1_000_000 + index,
        }
        for index in range(90)
    ]
    return AICandidateContext(
        symbol=symbol,
        name=symbol,
        data_as_of="2026-07-24",
        product_metadata={
            "asset_class": "equity_etf",
            "exposure_direction": direction,
            "leverage": leverage,
        },
        quant_score={"total": 80.0, "trend": 82.0},
        indicators={
            "ma20": 110.0,
            "rs20": 0.08,
            "median_turnover_20d": 100_000_000.0,
        },
        daily_bars=bars,
        spy_state={"return_20d": 0.03},
        news_snapshot={
            "status": news_status,
            "source": "tavily",
            "news_items": news_items if news_status == "available" else [],
        },
        event_snapshot={"status": "available", "events": []},
        missing_fields=[] if news_status == "available" else ["news"],
    )


def _quant_selection(symbols, *, status="completed", boundary=True):
    contexts = {symbol: _context(symbol) for symbol in symbols}
    return {
        "status": status,
        "boundary_proven": boundary,
        "data_as_of": "2026-07-24",
        "ai_candidate_symbols": list(symbols),
        "candidates": [
            {
                "symbol": symbol,
                "name": symbol,
                "quant_score": dict(contexts[symbol].quant_score),
                "indicators": dict(contexts[symbol].indicators),
                "metadata": dict(contexts[symbol].product_metadata),
            }
            for symbol in symbols
        ],
    }


class _FakeProvider:
    model_alias = DEFAULT_MODEL
    temperature = DEFAULT_TEMPERATURE

    def __init__(self, responses=None, default=None) -> None:
        self.responses = responses or {}
        self.default = default or _decision()
        self.calls = {}
        self.lock = Lock()

    def complete(self, *, system_prompt, user_prompt, response_schema):
        match = re.search(r'"symbol":"([^"]+)"', user_prompt)
        if match is None:
            raise AssertionError("symbol missing from prompt")
        symbol = match.group(1)
        with self.lock:
            attempt = self.calls.get(symbol, 0)
            self.calls[symbol] = attempt + 1
        configured = self.responses.get(symbol, self.default)
        if isinstance(configured, list):
            value = configured[min(attempt, len(configured) - 1)]
        else:
            value = configured
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            raw, model_id = value
        else:
            raw, model_id = value, "deepseek-v4-flash-202607"
        return AICompletion(raw_text=raw, resolved_model_id=model_id)


class AIInputAndParserTests(unittest.TestCase):
    def test_input_snapshot_caps_news_and_explains_inverse_buy_semantics(self) -> None:
        context = _context(
            "SQQQ.US",
            direction="inverse",
            leverage=3.0,
        )
        snapshot = build_ai_input_snapshot(
            context,
            model_alias=DEFAULT_MODEL,
            temperature=DEFAULT_TEMPERATURE,
        )
        self.assertEqual(snapshot["prompt_version"], AI_PROMPT_VERSION)
        self.assertEqual(snapshot["model_policy_version"], MODEL_POLICY_VERSION)
        self.assertEqual(
            len(snapshot["facts"]["news_snapshot"]["news_items"]),
            10,
        )
        self.assertIn("inverse ETF", snapshot["system_prompt"])
        self.assertIn("买入 ETF 份额", snapshot["system_prompt"])
        self.assertIn('"exposure_direction":"inverse"', snapshot["user_prompt"])
        self.assertEqual(len(snapshot["ai_input_hash"]), 64)

    def test_equivalent_mapping_order_has_same_ai_input_hash(self) -> None:
        first = _context("AAA.US")
        second = _context("AAA.US")
        second = AICandidateContext(
            **{
                **second.__dict__,
                "quant_score": {"trend": 82.0, "total": 80.0},
            }
        )
        first_hash = build_ai_input_snapshot(
            first,
            model_alias=DEFAULT_MODEL,
            temperature=DEFAULT_TEMPERATURE,
        )["ai_input_hash"]
        second_hash = build_ai_input_snapshot(
            second,
            model_alias=DEFAULT_MODEL,
            temperature=DEFAULT_TEMPERATURE,
        )["ai_input_hash"]
        self.assertEqual(first_hash, second_hash)

    def test_parser_accepts_contract_and_rejects_extra_or_invalid_fields(self) -> None:
        parsed = parse_ai_decision(_decision())
        self.assertEqual(parsed["decision"], "SELECT")
        self.assertEqual(parsed["confidence"], 0.82)

        extra = json.loads(_decision())
        extra["position_size"] = 100
        invalid_select = json.loads(_decision())
        invalid_select["reasons"] = ["only one"]
        bool_confidence = json.loads(_decision())
        bool_confidence["confidence"] = True
        cases = (
            json.dumps(extra),
            json.dumps(invalid_select),
            json.dumps(bool_confidence),
            f"```json\n{_decision()}\n```",
        )
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(AIDecisionError):
                    parse_ai_decision(raw)

    def test_input_requires_h9_history_and_chronological_bars(self) -> None:
        context = _context("AAA.US")
        short = AICandidateContext(**{
            **context.__dict__,
            "daily_bars": list(context.daily_bars[:84]),
        })
        reversed_bars = AICandidateContext(**{
            **context.__dict__,
            "daily_bars": list(reversed(context.daily_bars)),
        })
        for invalid in (short, reversed_bars):
            with self.subTest(count=len(invalid.daily_bars)):
                with self.assertRaises(AIDecisionError):
                    build_ai_input_snapshot(
                        invalid,
                        model_alias=DEFAULT_MODEL,
                        temperature=DEFAULT_TEMPERATURE,
                    )


class AISelectionServiceTests(unittest.TestCase):
    def test_select_calculates_a_f_and_final_eligibility(self) -> None:
        provider = _FakeProvider()
        result = QuantAISelectionService(
            provider,
            max_workers=1,
        ).analyze(
            _quant_selection(["AAA.US"]),
            {"AAA.US": _context("AAA.US")},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["completion_rate"], 1.0)
        final = result["final_results"][0]
        self.assertAlmostEqual(final["ai_score"], 83.2)
        self.assertAlmostEqual(final["final_score"], 80.96)
        self.assertTrue(final["eligible"])
        self.assertEqual(final["effective_decision"], "SELECT")

    def test_valid_reject_and_insufficient_data_count_as_completed(self) -> None:
        provider = _FakeProvider({
            "AAA.US": _decision(decision="REJECT", confidence=0.8),
            "BBB.US": _decision(decision="INSUFFICIENT_DATA", confidence=0.2),
        })
        result = QuantAISelectionService(provider).analyze(
            _quant_selection(["AAA.US", "BBB.US"]),
            {
                "AAA.US": _context("AAA.US"),
                "BBB.US": _context("BBB.US"),
            },
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(result["final_results"], [])
        self.assertEqual(
            {item["effective_decision"] for item in result["provisional_results"]},
            {"REJECT", "INSUFFICIENT_DATA"},
        )

    def test_invalid_first_response_is_retried_once(self) -> None:
        provider = _FakeProvider({
            "AAA.US": ["not-json", _decision()],
        })
        result = QuantAISelectionService(
            provider,
            max_workers=1,
        ).analyze(
            _quant_selection(["AAA.US"]),
            {"AAA.US": _context("AAA.US")},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["snapshots"][0]["attempts"], 2)
        self.assertEqual(provider.calls["AAA.US"], 2)
        self.assertEqual(
            result["snapshots"][0]["raw_attempt_outputs"][0],
            "not-json",
        )

    def test_one_failed_call_makes_29_of_30_partial_and_hides_final_list(self) -> None:
        symbols = [f"S{index:03d}.US" for index in range(30)]
        provider = _FakeProvider({
            "S029.US": RuntimeError("provider unavailable"),
        })
        result = QuantAISelectionService(
            provider,
            max_workers=5,
        ).analyze(
            _quant_selection(symbols),
            {symbol: _context(symbol) for symbol in symbols},
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["planned_count"], 30)
        self.assertEqual(result["completed_count"], 29)
        self.assertAlmostEqual(result["completion_rate"], 29 / 30)
        self.assertEqual(result["final_results"], [])
        self.assertEqual(len(result["provisional_results"]), 29)
        self.assertEqual(provider.calls["S029.US"], 2)

    def test_news_unavailable_caps_ai_score_at_75(self) -> None:
        provider = _FakeProvider(default=_decision(confidence=1.0, suitability=100))
        result = QuantAISelectionService(provider).analyze(
            _quant_selection(["AAA.US"]),
            {"AAA.US": _context("AAA.US", news_status="error")},
        )
        provisional = result["provisional_results"][0]
        self.assertEqual(provisional["ai_score"], 75.0)
        self.assertEqual(provisional["final_score"], 78.5)

    def test_high_risk_and_data_conflicts_are_deterministic_rejections(self) -> None:
        provider = _FakeProvider({
            "HIGH.US": _decision(risk="HIGH"),
            "CONFLICT.US": _decision(conflicts=["新闻日期晚于截止日"]),
        })
        result = QuantAISelectionService(provider).analyze(
            _quant_selection(["HIGH.US", "CONFLICT.US"]),
            {
                "HIGH.US": _context("HIGH.US"),
                "CONFLICT.US": _context("CONFLICT.US"),
            },
        )
        self.assertEqual(result["status"], "completed")
        records = {item["symbol"]: item for item in result["provisional_results"]}
        self.assertEqual(records["HIGH.US"]["rejection_reason"], "high_risk")
        self.assertEqual(
            records["CONFLICT.US"]["rejection_reason"],
            "unresolved_data_conflict",
        )
        self.assertEqual(result["final_results"], [])

    def test_resolved_model_change_makes_run_partial(self) -> None:
        provider = _FakeProvider({
            "AAA.US": (_decision(), "model-immutable-a"),
            "BBB.US": (_decision(), "model-immutable-b"),
        })
        result = QuantAISelectionService(
            provider,
            max_workers=2,
        ).analyze(
            _quant_selection(["AAA.US", "BBB.US"]),
            {
                "AAA.US": _context("AAA.US"),
                "BBB.US": _context("BBB.US"),
            },
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["final_results"], [])
        snapshots = {item["symbol"]: item for item in result["snapshots"]}
        self.assertEqual(
            snapshots["BBB.US"]["error"],
            "resolved_model_id_mismatch",
        )

    def test_quant_partial_and_missing_context_never_call_or_publish(self) -> None:
        provider = _FakeProvider()
        blocked = QuantAISelectionService(provider).analyze(
            _quant_selection(["AAA.US"], status="partial", boundary=False),
            {"AAA.US": _context("AAA.US")},
        )
        self.assertEqual(blocked["status"], "partial")
        self.assertEqual(provider.calls, {})

        missing = QuantAISelectionService(provider).analyze(
            _quant_selection(["AAA.US"]),
            {},
        )
        self.assertEqual(missing["status"], "partial")
        self.assertEqual(missing["completed_count"], 0)
        self.assertEqual(missing["final_results"], [])

    def test_quant_context_mismatch_fails_before_provider_call(self) -> None:
        provider = _FakeProvider()
        context = _context("AAA.US")
        mismatched = AICandidateContext(**{
            **context.__dict__,
            "quant_score": {"total": 79.0, "trend": 82.0},
        })
        result = QuantAISelectionService(provider).analyze(
            _quant_selection(["AAA.US"]),
            {"AAA.US": mismatched},
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(
            result["snapshots"][0]["error"],
            "ai_context_quant_mismatch",
        )
        self.assertEqual(provider.calls, {})

    def test_missing_resolved_model_id_is_not_complete(self) -> None:
        provider = _FakeProvider({
            "AAA.US": (_decision(), ""),
        })
        result = QuantAISelectionService(provider).analyze(
            _quant_selection(["AAA.US"]),
            {"AAA.US": _context("AAA.US")},
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(
            result["snapshots"][0]["error"],
            "resolved_model_id_missing",
        )

    def test_final_ranking_uses_symbol_as_last_stable_key(self) -> None:
        symbols = ["BBB.US", "AAA.US"]
        result = QuantAISelectionService(_FakeProvider()).analyze(
            _quant_selection(symbols),
            {symbol: _context(symbol) for symbol in symbols},
        )
        self.assertEqual(
            [item["symbol"] for item in result["final_results"]],
            ["AAA.US", "BBB.US"],
        )


class DeepSeekProviderTests(unittest.TestCase):
    def test_provider_uses_json_mode_and_disables_runtime_retry(self) -> None:
        client = MagicMock()
        response = MagicMock()
        response.model = "deepseek-v4-flash-immutable"
        response.choices[0].message.content = _decision()
        with (
            patch(
                "app.quant_stock_selector_ai.OpenAI",
                return_value=client,
            ) as openai_factory,
            patch(
                "app.quant_stock_selector_ai.run_external_call",
                return_value=response,
            ) as external_call,
        ):
            provider = DeepSeekQuantSelectorProvider("secret-key")
            result = provider.complete(
                system_prompt="system",
                user_prompt="user",
                response_schema={},
            )

        openai_factory.assert_called_once_with(
            api_key="secret-key",
            base_url="https://api.deepseek.com",
            timeout=25.0,
            max_retries=0,
        )
        kwargs = external_call.call_args.kwargs
        self.assertEqual(kwargs["model"], DEFAULT_MODEL)
        self.assertEqual(kwargs["temperature"], DEFAULT_TEMPERATURE)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["max_tokens"], 1200)
        self.assertFalse(kwargs["retry_if"](RuntimeError("test")))
        self.assertEqual(result.resolved_model_id, "deepseek-v4-flash-immutable")
        self.assertNotIn(
            "secret-key",
            provider.safe_error(RuntimeError("api_key=secret-key")),
        )


if __name__ == "__main__":
    unittest.main()
