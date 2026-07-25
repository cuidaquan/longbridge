"""Point-in-time outcome evaluation for quantitative selection runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import random
import re
from statistics import mean, median
from typing import Any, Callable, Mapping, Protocol, Sequence

from .db import get_connection
from .quant_stock_selector_hashing import canonical_json, canonical_sha256
from .quant_stock_selector_metadata import normalize_symbol
from .quant_stock_selector_service import QuantSelectionRunRepository


EVALUATION_VERSION = "quant-selector-effect-v1"
OUTCOME_SCHEMA_VERSION = "quant-selector-outcome-bundle-v1"
BOOTSTRAP_METHOD = "circular-moving-block-bootstrap-v1"
HORIZONS = (5, 10, 20)
TOP_K = 10
SLOT_WEIGHT = 0.1
COST_PER_SIDE = 0.001
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
BOOTSTRAP_SEED = 20260725
MINIMUM_COMPLETED_DATES = 60
MINIMUM_PAIRED_DATES = 40
MINIMUM_AI_COMPLETION_RATE = 0.95
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ELIGIBLE_ASSET_CLASSES = {"common_stock", "equity_etf"}
_TERMINAL_KINDS = {
    "delisting_cash_settlement",
    "last_tradable_total_return_quote",
}


class QuantOutcomeError(ValueError):
    pass


@dataclass(frozen=True)
class QuantOutcomeSnapshot:
    payload: Mapping[str, Any]
    payload_hash: str


class QuantOutcomeProvider(Protocol):
    def capture(self) -> QuantOutcomeSnapshot:
        ...


def _iso_date(value: Any, *, field: str) -> str:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError as exc:
        raise QuantOutcomeError(f"{field} must be an ISO date") from exc


def _iso_timestamp(value: Any, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuantOutcomeError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QuantOutcomeError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise QuantOutcomeError(f"{field} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise QuantOutcomeError(f"{field} must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise QuantOutcomeError(f"{field} must be a positive finite number")
    return result


class JsonQuantOutcomeProvider:
    """Load verified total-return prices and official NYSE sessions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def capture(self) -> QuantOutcomeSnapshot:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise QuantOutcomeError(f"quant outcome bundle not found: {self.path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise QuantOutcomeError(
                f"quant outcome bundle is invalid: {type(exc).__name__}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise QuantOutcomeError("outcome bundle must be an object")
        if raw.get("schema_version") != OUTCOME_SCHEMA_VERSION:
            raise QuantOutcomeError("unsupported outcome bundle schema_version")
        source = str(raw.get("source") or "").strip()
        source_version = str(raw.get("source_version") or "").strip()
        if not source or not source_version:
            raise QuantOutcomeError("outcome source and source_version are required")

        raw_calendar = raw.get("nyse_calendar")
        if not isinstance(raw_calendar, list):
            raise QuantOutcomeError("nyse_calendar must be an array")
        calendar = sorted({
            _iso_date(value, field="nyse_calendar") for value in raw_calendar
        })
        if len(calendar) != len(raw_calendar):
            raise QuantOutcomeError("nyse_calendar must contain unique dates")
        calendar_set = set(calendar)
        captured_at = _iso_timestamp(raw.get("captured_at"), field="captured_at")
        if any(value > captured_at[:10] for value in calendar):
            raise QuantOutcomeError("nyse_calendar cannot contain dates after captured_at")

        raw_symbols = raw.get("symbols")
        if not isinstance(raw_symbols, Mapping):
            raise QuantOutcomeError("symbols must be an object")
        symbols: dict[str, Any] = {}
        for raw_symbol, raw_detail in raw_symbols.items():
            symbol = normalize_symbol(raw_symbol)
            if symbol in symbols:
                raise QuantOutcomeError(f"duplicate normalized symbol: {symbol}")
            if not isinstance(raw_detail, Mapping):
                raise QuantOutcomeError(f"symbols.{symbol} must be an object")
            if raw_detail.get("return_basis") != "validated_total_return":
                raise QuantOutcomeError(
                    f"symbols.{symbol}.return_basis must be validated_total_return"
                )
            raw_bars = raw_detail.get("bars")
            if not isinstance(raw_bars, list):
                raise QuantOutcomeError(f"symbols.{symbol}.bars must be an array")
            bars = []
            seen_dates = set()
            for index, raw_bar in enumerate(raw_bars):
                if not isinstance(raw_bar, Mapping):
                    raise QuantOutcomeError(f"symbols.{symbol}.bars[{index}] must be an object")
                trade_date = _iso_date(
                    raw_bar.get("trade_date"),
                    field=f"symbols.{symbol}.bars[{index}].trade_date",
                )
                if trade_date not in calendar_set:
                    raise QuantOutcomeError(f"{symbol} bar date is absent from NYSE calendar")
                if trade_date in seen_dates:
                    raise QuantOutcomeError(f"duplicate {symbol} bar date: {trade_date}")
                if raw_bar.get("official_session") is not True:
                    raise QuantOutcomeError(f"{symbol} bar must be an official regular session")
                seen_dates.add(trade_date)
                bars.append({
                    "trade_date": trade_date,
                    "open_total_return": _positive_number(
                        raw_bar.get("open_total_return"),
                        field=f"{symbol}.{trade_date}.open_total_return",
                    ),
                    "close_total_return": _positive_number(
                        raw_bar.get("close_total_return"),
                        field=f"{symbol}.{trade_date}.close_total_return",
                    ),
                    "official_session": True,
                })
            if raw_detail.get("corporate_actions_status") != "complete":
                raise QuantOutcomeError(
                    f"symbols.{symbol}.corporate_actions_status must be complete"
                )
            terminal_status = str(raw_detail.get("terminal_status") or "")
            if terminal_status not in {"active_as_of_capture", "terminated"}:
                raise QuantOutcomeError(f"symbols.{symbol}.terminal_status is invalid")
            detail: dict[str, Any] = {
                "return_basis": "validated_total_return",
                "corporate_actions_status": "complete",
                "terminal_status": terminal_status,
                "bars": sorted(bars, key=lambda item: item["trade_date"]),
            }
            terminal = raw_detail.get("terminal_outcome")
            if terminal_status == "terminated" and terminal is None:
                raise QuantOutcomeError(f"{symbol} terminated without terminal outcome")
            if terminal_status == "active_as_of_capture" and terminal is not None:
                raise QuantOutcomeError(f"{symbol} active security has terminal outcome")
            if terminal is not None:
                if not isinstance(terminal, Mapping):
                    raise QuantOutcomeError(f"symbols.{symbol}.terminal_outcome must be an object")
                kind = str(terminal.get("kind") or "")
                if kind not in _TERMINAL_KINDS or terminal.get("authoritative") is not True:
                    raise QuantOutcomeError(f"{symbol} terminal outcome is not authoritative")
                terminal_date = _iso_date(
                    terminal.get("trade_date"),
                    field=f"{symbol}.terminal_outcome.trade_date",
                )
                if terminal_date not in calendar_set:
                    raise QuantOutcomeError(f"{symbol} terminal date is absent from NYSE calendar")
                if any(item["trade_date"] > terminal_date for item in bars):
                    raise QuantOutcomeError(f"{symbol} has bars after terminal date")
                detail["terminal_outcome"] = {
                    "trade_date": terminal_date,
                    "kind": kind,
                    "total_return_value": _positive_number(
                        terminal.get("total_return_value"),
                        field=f"{symbol}.terminal_outcome.total_return_value",
                    ),
                    "authoritative": True,
                }
            symbols[symbol] = detail

        normalized = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "source": source,
            "source_version": source_version,
            "captured_at": captured_at,
            "nyse_calendar": calendar,
            "symbols": {symbol: symbols[symbol] for symbol in sorted(symbols)},
        }
        return QuantOutcomeSnapshot(
            payload=normalized,
            payload_hash=canonical_sha256(normalized),
        )


class QuantSelectionEvaluationService:
    """Evaluate deployed AI-selected portfolios against quant and SPY peers."""

    def __init__(
        self,
        outcome_provider: QuantOutcomeProvider | None,
        *,
        connection_factory: Callable[..., Any] = get_connection,
        integrity_checker: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.outcome_provider = outcome_provider
        self.connection_factory = connection_factory
        self.integrity_checker = integrity_checker or QuantSelectionRunRepository(
            connection_factory=connection_factory
        ).verify_integrity
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, *, persist: bool = True) -> dict[str, Any]:
        if self.outcome_provider is None:
            raise QuantOutcomeError("QUANT_SELECTOR_OUTCOME_BUNDLE_PATH is not configured")
        outcome = self.outcome_provider.capture()
        terminal_runs = self._load_terminal_runs()
        anchor = next(
            (item for item in terminal_runs if item["status"] == "completed"),
            None,
        )
        cohort = self._cohort(anchor)
        cohort_runs = [
            item for item in terminal_runs if self._matches_cohort(item, cohort)
        ] if cohort else []
        completed = [
            item for item in cohort_runs
            if item["status"] == "completed"
            and item["resolved_model_id"] == cohort["resolved_model_id"]
            and item["data_as_of"] is not None
        ]
        representatives: dict[str, dict[str, Any]] = {}
        for item in completed:
            representatives.setdefault(item["data_as_of"], item)

        planned = sum(item["ai_planned_count"] for item in cohort_runs)
        ai_completed = sum(item["ai_completed_count"] for item in cohort_runs)
        ai_completion_rate = ai_completed / planned if planned else None
        exclusions: Counter[str] = Counter()
        observations: dict[int, list[dict[str, Any]]] = defaultdict(list)
        input_valid = 0
        product_evidence_total = 0
        product_evidence_valid = 0
        portfolio_dates: list[dict[str, Any]] = []
        representative_runs = []

        calendar = list(outcome.payload["nyse_calendar"])
        for run_date in sorted(representatives):
            run = representatives[run_date]
            hashes_valid = (
                bool(_HASH_PATTERN.fullmatch(str(run.get("quant_input_hash") or "")))
                and bool(_HASH_PATTERN.fullmatch(str(run.get("run_input_hash") or "")))
                and self.integrity_checker(run["run_id"])
            )
            representative_runs.append({
                "run_date": run_date,
                "run_id": run["run_id"],
                "input_integrity_valid": hashes_valid,
            })
            if hashes_valid:
                input_valid += 1
            else:
                exclusions["input_hash_or_snapshot_integrity_failed"] += 1
                continue
            final_symbols, quant_symbols, evidence = self._load_portfolios(run["run_id"])
            product_evidence_total += evidence["total"]
            product_evidence_valid += evidence["valid"]
            portfolio_dates.append({
                "run_date": run_date,
                "final_symbols": final_symbols,
                "quant_symbols": quant_symbols,
            })
            future_dates = [value for value in calendar if value > run_date]
            if len(future_dates) < max(HORIZONS):
                exclusions["insufficient_future_nyse_sessions"] += 1
                continue
            entry_date = future_dates[0]
            for horizon in HORIZONS:
                exit_date = future_dates[horizon - 1]
                final_return = self._portfolio_return(
                    final_symbols, entry_date, exit_date, outcome.payload
                )
                quant_return = self._portfolio_return(
                    quant_symbols, entry_date, exit_date, outcome.payload
                )
                spy_return = self._portfolio_return(
                    ["SPY.US"], entry_date, exit_date, outcome.payload,
                    slot_weight=1.0,
                )
                incomplete = [
                    value for value in (final_return, quant_return, spy_return)
                    if value["return"] is None
                ]
                if incomplete:
                    for value in incomplete:
                        exclusions[f"incomplete_{value['reason']}"] += 1
                    continue
                observations[horizon].append({
                    "observation_date": run_date,
                    "run_id": run["run_id"],
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "final_return": final_return["return"],
                    "quant_return": quant_return["return"],
                    "spy_return": spy_return["return"],
                    "final_minus_quant": final_return["return"] - quant_return["return"],
                    "final_minus_spy": final_return["return"] - spy_return["return"],
                })

        completed_dates = len(representatives)
        input_hash_coverage = input_valid / completed_dates if completed_dates else None
        product_coverage = (
            product_evidence_valid / product_evidence_total
            if product_evidence_total else 1.0
        )
        paired_counts = {str(horizon): len(observations[horizon]) for horizon in HORIZONS}
        gate_reasons = []
        if cohort is None:
            gate_reasons.append("no_completed_run_cohort")
        if completed_dates < MINIMUM_COMPLETED_DATES:
            gate_reasons.append("insufficient_completed_run_dates")
        if ai_completion_rate is None or ai_completion_rate < MINIMUM_AI_COMPLETION_RATE:
            gate_reasons.append("insufficient_ai_completion_rate")
        if input_hash_coverage != 1.0:
            gate_reasons.append("incomplete_input_hash_coverage")
        if product_coverage != 1.0:
            gate_reasons.append("incomplete_product_scope_coverage")
        for horizon in HORIZONS:
            if paired_counts[str(horizon)] < MINIMUM_PAIRED_DATES:
                gate_reasons.append(f"insufficient_paired_run_dates_{horizon}d")
        ready = not gate_reasons
        metrics = (
            {
                "portfolio_turnover": {
                    "final_top_10_average": self._average_turnover(
                        portfolio_dates, "final_symbols"
                    ),
                    "quant_top_10_average": self._average_turnover(
                        portfolio_dates, "quant_symbols"
                    ),
                },
                "horizons": {
                    str(horizon): self._summarize(observations[horizon])
                    for horizon in HORIZONS
                },
            }
            if ready else None
        )
        report = {
            "evaluation_version": EVALUATION_VERSION,
            "created_at": self._utc_timestamp(self.clock()),
            "ready": ready,
            "cohort": cohort,
            "parameters": {
                "horizons": list(HORIZONS),
                "top_k": TOP_K,
                "slot_weight": SLOT_WEIGHT,
                "buy_cost_bps": int(COST_PER_SIDE * 10000),
                "sell_cost_bps": int(COST_PER_SIDE * 10000),
                "minimum_completed_run_dates": MINIMUM_COMPLETED_DATES,
                "minimum_paired_run_dates": MINIMUM_PAIRED_DATES,
                "minimum_ai_completion_rate": MINIMUM_AI_COMPLETION_RATE,
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "bootstrap_confidence_level": BOOTSTRAP_CONFIDENCE_LEVEL,
                "bootstrap_seed": BOOTSTRAP_SEED,
                "bootstrap_block_size": "max(2, ceil(distinct_dates^(1/3)))",
            },
            "gate": {"ready": ready, "reasons": gate_reasons},
            "coverage": {
                "terminal_runs_in_cohort": len(cohort_runs),
                "completed_runs_in_cohort": len(completed),
                "distinct_completed_run_dates": completed_dates,
                "duplicate_completed_runs": len(completed) - completed_dates,
                "representative_runs": representative_runs,
                "ai_planned_calls": planned,
                "ai_completed_calls": ai_completed,
                "ai_completion_rate": ai_completion_rate,
                "input_hash_coverage": input_hash_coverage,
                "product_scope_evidence_total": product_evidence_total,
                "product_scope_evidence_valid": product_evidence_valid,
                "product_scope_coverage": product_coverage,
                "paired_run_dates_by_horizon": paired_counts,
                "exclusions": dict(sorted(exclusions.items())),
                "outcome_source": outcome.payload["source"],
                "outcome_source_version": outcome.payload["source_version"],
                "outcome_snapshot_hash": outcome.payload_hash,
                "latest_label_date": max(
                    (
                        item["exit_date"]
                        for horizon in HORIZONS
                        for item in observations[horizon]
                    ),
                    default=None,
                ),
            },
            "metrics": metrics,
            "methodology": {
                "portfolios": "final Top 10, same-run quant Q Top 10, and 100% SPY",
                "slots": "ten fixed 10% slots; unfilled slots remain zero-return cash",
                "prices": (
                    "next NYSE session official open to horizon official close "
                    "using validated total-return values"
                ),
                "terminal_events": (
                    "authoritative delisting settlement or last tradable "
                    "total-return quote; otherwise incomplete"
                ),
                "costs": "10 bps at entry plus 10 bps at exit for each filled slot and SPY",
                "pairing": (
                    "one latest completed run per data_as_of date; security rows "
                    "are not independent samples"
                ),
                "inference": (
                    "date-clustered circular moving-block bootstrap on "
                    "final-minus-quant returns"
                ),
            },
        }
        if persist:
            report["id"] = self._save_report(report, outcome)
        return report

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = min(100, max(1, int(limit)))
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT id, created_at, evaluation_version, parameters,
                       result, ready, data_as_of, outcome_snapshot_hash
                FROM quant_selection_evaluations
                ORDER BY created_at DESC, id DESC LIMIT {safe_limit}
                """
            ).fetchall()
        return [{
            "id": row[0],
            "created_at": self._utc_timestamp(row[1]),
            "evaluation_version": row[2],
            "parameters": json.loads(row[3]),
            "result": json.loads(row[4]),
            "ready": bool(row[5]),
            "data_as_of": row[6].isoformat() if row[6] else None,
            "outcome_snapshot_hash": row[7],
        } for row in rows]

    def _load_terminal_runs(self) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            cursor = connection.execute(
                """
                SELECT run_id, status, data_as_of, completed_at,
                       universe_version, filter_version, score_version,
                       prompt_version, model_policy_version, model_alias,
                       resolved_model_id, ai_planned_count,
                       ai_completed_count, quant_input_hash, run_input_hash
                FROM quant_selection_runs
                WHERE status IN ('completed', 'partial', 'failed')
                ORDER BY completed_at DESC NULLS LAST, run_id DESC
                """
            )
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
        values = []
        for row in rows:
            item = dict(zip(columns, row))
            item["data_as_of"] = item["data_as_of"].isoformat() if item["data_as_of"] else None
            item["ai_planned_count"] = int(item["ai_planned_count"] or 0)
            item["ai_completed_count"] = int(item["ai_completed_count"] or 0)
            values.append(item)
        return values

    @staticmethod
    def _cohort(anchor: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if anchor is None:
            return None
        keys = (
            "universe_version", "filter_version", "score_version",
            "prompt_version", "model_policy_version", "model_alias",
            "resolved_model_id",
        )
        return {key: anchor.get(key) for key in keys}

    @staticmethod
    def _matches_cohort(run: Mapping[str, Any], cohort: Mapping[str, Any] | None) -> bool:
        if cohort is None:
            return False
        policy_matches = all(run.get(key) == cohort.get(key) for key in (
            "universe_version", "filter_version", "score_version",
            "prompt_version", "model_policy_version", "model_alias",
        ))
        return policy_matches and run.get("resolved_model_id") in {
            None,
            cohort.get("resolved_model_id"),
        }

    def _load_portfolios(self, run_id: str) -> tuple[list[str], list[str], dict[str, int]]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT symbol, selection_status, quant_score, quant_rank,
                       final_rank, final_selected, payload
                FROM quant_selection_candidates WHERE run_id = ?
                """,
                [run_id],
            ).fetchall()
        candidates = []
        for row in rows:
            payload = json.loads(row[6])
            candidates.append({
                "symbol": normalize_symbol(row[0]),
                "selection_status": row[1],
                "quant_score": row[2],
                "quant_rank": row[3],
                "final_rank": row[4],
                "final_selected": bool(row[5]),
                "payload": payload,
            })
        final = sorted(
            (item for item in candidates if item["final_selected"] and item["final_rank"]),
            key=lambda item: (item["final_rank"], item["symbol"]),
        )[:TOP_K]
        quant = sorted(
            (
                item for item in candidates
                if item["selection_status"] == "quant_eligible"
                and item["quant_score"] is not None
                and float(item["quant_score"]) >= 65.0
                and item["quant_rank"]
                and self._all_hard_filters_pass(item["payload"])
            ),
            key=lambda item: (item["quant_rank"], item["symbol"]),
        )[:TOP_K]
        evidence_items = {
            item["symbol"]: item for item in [*final, *quant]
        }
        evidence_valid = sum(
            self._has_product_evidence(item["payload"])
            for item in evidence_items.values()
        )
        return (
            [item["symbol"] for item in final],
            [item["symbol"] for item in quant],
            {"total": len(evidence_items), "valid": evidence_valid},
        )

    @staticmethod
    def _all_hard_filters_pass(payload: Mapping[str, Any]) -> bool:
        filters = payload.get("hard_filters")
        return isinstance(filters, Mapping) and all(
            isinstance(filters.get(f"H{index}"), Mapping)
            and filters[f"H{index}"].get("status") == "pass"
            for index in range(1, 12)
        )

    @staticmethod
    def _has_product_evidence(payload: Mapping[str, Any]) -> bool:
        metadata = payload.get("metadata")
        return (
            isinstance(metadata, Mapping)
            and metadata.get("asset_class") in _ELIGIBLE_ASSET_CLASSES
            and bool(str(metadata.get("source") or "").strip())
            and bool(str(metadata.get("source_version") or "").strip())
        )

    @classmethod
    def _portfolio_return(
        cls,
        symbols: Sequence[str],
        entry_date: str,
        exit_date: str,
        outcome: Mapping[str, Any],
        *,
        slot_weight: float = SLOT_WEIGHT,
    ) -> dict[str, Any]:
        total = 0.0
        for symbol in symbols:
            security = outcome["symbols"].get(symbol)
            if not isinstance(security, Mapping):
                return {"return": None, "reason": "symbol_outcome_missing"}
            bars = {item["trade_date"]: item for item in security["bars"]}
            entry = bars.get(entry_date)
            if entry is None:
                return {"return": None, "reason": "official_entry_open_missing"}
            terminal = security.get("terminal_outcome")
            exit_value = None
            if isinstance(terminal, Mapping) and terminal["trade_date"] < entry_date:
                return {"return": None, "reason": "terminal_before_entry"}
            if (
                isinstance(terminal, Mapping)
                and entry_date <= terminal["trade_date"] <= exit_date
            ):
                exit_value = terminal["total_return_value"]
            else:
                exit_bar = bars.get(exit_date)
                if exit_bar is None:
                    return {"return": None, "reason": "official_exit_close_missing"}
                exit_value = exit_bar["close_total_return"]
            gross = exit_value / entry["open_total_return"] - 1.0
            total += slot_weight * (gross - 2 * COST_PER_SIDE)
        return {"return": total, "reason": None}

    def _summarize(self, observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        final_values = [float(item["final_return"]) for item in observations]
        quant_values = [float(item["quant_return"]) for item in observations]
        spy_values = [float(item["spy_return"]) for item in observations]
        final_quant = [float(item["final_minus_quant"]) for item in observations]
        final_spy = [float(item["final_minus_spy"]) for item in observations]
        inference = self._block_bootstrap(observations)
        estimate = inference["estimate"]
        lower = inference["lower"]
        positive = (
            inference["ready"] and estimate is not None and estimate > 0
            and lower is not None and lower >= 0
        )
        return {
            "paired_run_dates": len(observations),
            "final_top_10": self._return_summary(final_values),
            "quant_top_10": self._return_summary(quant_values),
            "spy": self._return_summary(spy_values),
            "final_minus_spy": self._return_summary(final_spy),
            "final_minus_quant": self._return_summary(final_quant),
            "final_minus_quant_inference": inference,
            "ai_increment_conclusion": (
                "positive_increment_in_current_sample"
                if positive else "no_positive_increment_conclusion"
            ),
        }

    @classmethod
    def _block_bootstrap(cls, observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        clusters: dict[str, list[float]] = defaultdict(list)
        for item in observations:
            clusters[str(item["observation_date"])].append(float(item["final_minus_quant"]))
        dates = sorted(clusters)
        count = len(dates)
        block_size = max(2, math.ceil(count ** (1 / 3)))
        result = {
            "ready": False,
            "reason": None,
            "method": BOOTSTRAP_METHOD,
            "cluster_unit": "run_date",
            "estimate": mean(
                value for values in clusters.values() for value in values
            ) if clusters else None,
            "confidence_level": BOOTSTRAP_CONFIDENCE_LEVEL,
            "lower": None,
            "upper": None,
            "standard_error": None,
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "effective_block_size": block_size,
            "distinct_dates": count,
            "seed": BOOTSTRAP_SEED,
        }
        if count < 4:
            result["reason"] = "insufficient_distinct_dates"
            return result
        if block_size >= count:
            result["reason"] = "block_size_not_less_than_date_count"
            return result
        generator = random.Random(BOOTSTRAP_SEED)
        samples = []
        ordered = [clusters[value] for value in dates]
        for _ in range(BOOTSTRAP_SAMPLES):
            sampled_clusters = []
            while len(sampled_clusters) < count:
                start = generator.randrange(count)
                take = min(block_size, count - len(sampled_clusters))
                sampled_clusters.extend(
                    ordered[(start + offset) % count] for offset in range(take)
                )
            samples.append(mean(value for cluster in sampled_clusters for value in cluster))
        average = mean(samples)
        result.update({
            "ready": True,
            "lower": cls._percentile(samples, (1 - BOOTSTRAP_CONFIDENCE_LEVEL) / 2),
            "upper": cls._percentile(samples, 1 - (1 - BOOTSTRAP_CONFIDENCE_LEVEL) / 2),
            "standard_error": math.sqrt(
                sum((value - average) ** 2 for value in samples) / (len(samples) - 1)
            ),
        })
        return result

    @classmethod
    def _return_summary(cls, values: Sequence[float]) -> dict[str, Any]:
        return {
            "sample_count": len(values),
            "average": mean(values) if values else None,
            "median": median(values) if values else None,
            "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
            "maximum_drawdown": cls._maximum_drawdown(values),
        }

    @staticmethod
    def _maximum_drawdown(values: Sequence[float]) -> float | None:
        if not values:
            return None
        equity = 1.0
        peak = 1.0
        drawdown = 0.0
        for value in values:
            equity *= 1 + value
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1)
        return drawdown

    @staticmethod
    def _average_turnover(portfolios: Sequence[Mapping[str, Any]], key: str) -> float | None:
        if len(portfolios) < 2:
            return None
        values = []
        for previous, current in zip(portfolios, portfolios[1:]):
            previous_weights = Counter({symbol: SLOT_WEIGHT for symbol in previous[key]})
            current_weights = Counter({symbol: SLOT_WEIGHT for symbol in current[key]})
            previous_weights["CASH"] = 1 - SLOT_WEIGHT * len(previous[key])
            current_weights["CASH"] = 1 - SLOT_WEIGHT * len(current[key])
            assets = set(previous_weights) | set(current_weights)
            values.append(0.5 * sum(
                abs(previous_weights[asset] - current_weights[asset]) for asset in assets
            ))
        return mean(values)

    @staticmethod
    def _percentile(values: Sequence[float], probability: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    def _save_report(
        self,
        report: Mapping[str, Any],
        outcome: QuantOutcomeSnapshot,
    ) -> int:
        result = {key: value for key, value in report.items() if key not in {"id", "parameters"}}
        data_as_of = report["coverage"].get("latest_label_date")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO quant_selection_evaluations (
                    evaluation_version, parameters, result, ready,
                    data_as_of, outcome_snapshot_hash, outcome_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                [
                    EVALUATION_VERSION,
                    canonical_json(report["parameters"]),
                    canonical_json(result),
                    bool(report["ready"]),
                    data_as_of,
                    outcome.payload_hash,
                    canonical_json(outcome.payload),
                ],
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _utc_timestamp(value: Any) -> str:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
