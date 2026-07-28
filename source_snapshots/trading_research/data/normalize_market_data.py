"""Conservative multi-source normalization for historical M1 OHLC data."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from data.schema import DatasetContract, normalize_and_validate, project_root


PRICE_COLUMNS = ("open", "high", "low", "close")
HISTDATA_COLUMNS = ("date", "time", "open", "high", "low", "close", "volume")
HISTDATA_TIMEZONE = "America/New_York"
REFERENCE_TIMEFRAME_MINUTES = {"M5": 5, "M30": 30}


class MarketDataNormalizationError(RuntimeError):
    """Raised when source data cannot be normalized without guessing."""


@dataclass(frozen=True)
class NormalizationConfig:
    max_missing_run_minutes: int = 30
    max_anchor_distance_minutes: int = 45
    max_anchor_basis_change_pips: float = 8.0
    minimum_spike_pips: float = 30.0
    spike_mad_multiplier: float = 15.0
    surrounding_agreement_pips: float = 10.0
    primary_excursion_ratio: float = 2.0
    reference_agreement_pips: float = 10.0
    reference_primary_disagreement_pips: float = 30.0
    minimum_reference_m5_blocks: int = 20
    minimum_reference_m5_blocks_per_day: int = 20
    adaptive_reference_agreement_quantile: float = 0.995
    adaptive_reference_primary_disagreement_quantile: float = 0.99
    adaptive_reference_primary_disagreement_ratio: float = 5.0
    reference_shift_max_aligned_median_pips: float = 2.0
    reference_shift_min_improvement_pips: float = 3.0


def pip_size_for_symbol(symbol: str) -> float:
    symbol = symbol.upper().replace("/", "").replace("-", "")
    if symbol.endswith("JPY") or symbol.startswith(("XAU", "XAG")):
        return 0.01
    return 0.0001


def discover_histdata_files(symbol: str, source_dir: str | Path = "data/raw/histdata/source") -> list[Path]:
    root = Path(source_dir)
    if not root.is_absolute():
        root = project_root() / root
    pattern = f"DAT_MT_{symbol.upper()}_M1_*.csv"
    return sorted(root.rglob(pattern)) if root.is_dir() else []


def _source_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{path.name}:{file_digest}\n".encode("ascii"))
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    try:
        value = path.relative_to(project_root())
    except ValueError:
        value = path
    return str(value).replace("\\", "/")


def load_histdata_m1(
    symbol: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    source_dir: str | Path = "data/raw/histdata/source",
) -> tuple[pd.DataFrame, dict]:
    """Load HistData MetaTrader M1 files and convert observed New York wall time to UTC."""
    paths = discover_histdata_files(symbol, source_dir)
    if not paths:
        raise MarketDataNormalizationError(f"No HistData M1 files found for {symbol}")

    frames = []
    for path in paths:
        frame = pd.read_csv(path, header=None, names=HISTDATA_COLUMNS)
        if frame.empty:
            continue
        try:
            local = pd.to_datetime(
                frame["date"].astype(str) + " " + frame["time"].astype(str),
                format="%Y.%m.%d %H:%M",
                errors="raise",
            )
        except (TypeError, ValueError) as exc:
            raise MarketDataNormalizationError(f"Invalid HistData timestamp in {path}") from exc
        try:
            frame["timestamp"] = local.dt.tz_localize(
                HISTDATA_TIMEZONE,
                ambiguous="raise",
                nonexistent="raise",
            ).dt.tz_convert("UTC")
        except (TypeError, ValueError) as exc:
            raise MarketDataNormalizationError(
                f"Ambiguous or nonexistent HistData New York timestamp in {path}"
            ) from exc
        frame = frame[["timestamp", *PRICE_COLUMNS, "volume"]]
        frames.append(frame)
    if not frames:
        raise MarketDataNormalizationError(f"HistData files are empty for {symbol}")

    result = pd.concat(frames, ignore_index=True)
    duplicate_mask = result["timestamp"].duplicated(keep=False)
    duplicate_rows = int(duplicate_mask.sum())
    duplicates_removed = 0
    if duplicate_rows:
        duplicates = result.loc[duplicate_mask]
        conflicting = duplicates.groupby("timestamp")[[*PRICE_COLUMNS, "volume"]].nunique().gt(1).any(axis=1)
        if conflicting.any():
            timestamp = conflicting[conflicting].index[0]
            raise MarketDataNormalizationError(f"Conflicting HistData duplicate at {timestamp.isoformat()}")
        before = len(result)
        result = result.drop_duplicates("timestamp", keep="first")
        duplicates_removed = before - len(result)

    result = result.sort_values("timestamp").reset_index(drop=True)
    result = result.loc[(result["timestamp"] >= start) & (result["timestamp"] < end)].copy()
    result["tick_volume"] = pd.to_numeric(result.pop("volume"), errors="raise").astype(float)
    contract = DatasetContract(symbol=symbol, timeframe="M1", source="HistData", venue="HistData")
    result = normalize_and_validate(result, contract)
    metadata = {
        "files": [_display_path(path) for path in paths],
        "file_count": len(paths),
        "fingerprint_sha256": _source_fingerprint(paths),
        "timezone_input": "America/New_York wall clock with historical US daylight-saving rules",
        "timezone_conversion": "IANA America/New_York to UTC; UTC-5 in standard time and UTC-4 in daylight time",
        "identical_duplicate_rows_removed": duplicates_removed,
        "first_timestamp": result["timestamp"].iloc[0].isoformat(),
        "last_timestamp": result["timestamp"].iloc[-1].isoformat(),
        "row_count": int(len(result)),
    }
    return result, metadata


def _max_ohlc_difference(left: pd.DataFrame, right: pd.DataFrame) -> pd.Series:
    differences = [left[column].sub(right[column]).abs() for column in PRICE_COLUMNS]
    return pd.concat(differences, axis=1).max(axis=1)


def _aggregate_complete_timeframe(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate only complete blocks for third-source voting."""
    minutes = REFERENCE_TIMEFRAME_MINUTES[timeframe]
    grouped = frame.reset_index(drop=True).copy()
    timestamp_column = f"{timeframe.lower()}_timestamp"
    grouped[timestamp_column] = grouped["timestamp"].dt.floor(f"{minutes}min")
    counts = grouped.groupby(timestamp_column)["timestamp"].size()
    complete = counts[counts.eq(minutes)].index
    result = grouped.groupby(timestamp_column, sort=True).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    })
    result = result.loc[result.index.intersection(complete)]
    result.index.name = "timestamp"
    return result


def _aggregate_complete_m5(frame: pd.DataFrame) -> pd.DataFrame:
    return _aggregate_complete_timeframe(frame, "M5")


def _detect_reference_time_shift_days(
    primary_bars: pd.DataFrame,
    reference_bars: pd.DataFrame,
    pip: float,
    config: NormalizationConfig,
) -> tuple[set[object], list[dict]]:
    """Detect whole UTC days where primary prices align only after a one-hour shift."""
    daily = {}
    for lag_minutes in (-60, 0, 60):
        shifted = primary_bars[["close"]].copy()
        shifted.index = shifted.index + pd.Timedelta(minutes=lag_minutes)
        pair = reference_bars[["close"]].join(shifted, how="inner", lsuffix="_reference", rsuffix="_primary")
        pair["difference_pips"] = pair["close_reference"].sub(pair["close_primary"]).abs() / pip
        pair["date"] = pair.index.date
        daily[lag_minutes] = pair.groupby("date")["difference_pips"].agg(["count", "median"])

    dates = daily[0].index.intersection(daily[-60].index).intersection(daily[60].index)
    shifted_dates: set[object] = set()
    diagnostics = []
    for date in dates:
        counts = {lag: int(daily[lag].loc[date, "count"]) for lag in (-60, 0, 60)}
        if min(counts.values()) < config.minimum_reference_m5_blocks_per_day:
            continue
        medians = {lag: float(daily[lag].loc[date, "median"]) for lag in (-60, 0, 60)}
        best_lag = min(medians, key=medians.get)
        best_value = medians[best_lag]
        if (
            best_lag != 0
            and best_value <= config.reference_shift_max_aligned_median_pips
            and medians[0] - best_value >= config.reference_shift_min_improvement_pips
        ):
            shifted_dates.add(date)
            diagnostics.append({
                "date": date.isoformat(),
                "best_primary_timestamp_shift_minutes": int(best_lag),
                "aligned_median_close_difference_pips": best_value,
                "unshifted_median_close_difference_pips": medians[0],
                "overlap_blocks": counts[best_lag],
            })
    return shifted_dates, diagnostics


def _audit_row(
    action: str,
    timestamp: pd.Timestamp,
    primary: pd.Series | None,
    secondary: pd.Series,
    normalized: pd.Series,
    scale: float,
    detail: str,
) -> dict:
    row = {
        "timestamp": timestamp,
        "action": action,
        "detail": detail,
        "secondary_scale": float(scale),
    }
    for column in PRICE_COLUMNS:
        row[f"primary_{column}"] = np.nan if primary is None else float(primary[column])
        row[f"secondary_{column}"] = float(secondary[column])
        row[f"normalized_{column}"] = float(normalized[column])
    return row


def normalize_m1_sources(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    symbol: str,
    *,
    config: NormalizationConfig | None = None,
    reference_m5: pd.DataFrame | None = None,
    reference_bars: tuple[str, pd.DataFrame] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Patch high-confidence defects using a secondary feed and optional MT5 majority vote."""
    if reference_m5 is not None and reference_bars is not None:
        raise MarketDataNormalizationError("Pass either reference_m5 or reference_bars, not both")
    if reference_m5 is not None:
        reference_bars = ("M5", reference_m5)

    config = config or NormalizationConfig()
    pip = pip_size_for_symbol(symbol)
    primary = primary.copy().set_index("timestamp", drop=False).sort_index()
    secondary = secondary.copy().set_index("timestamp", drop=False).sort_index()
    secondary = secondary.loc[(secondary.index >= primary.index.min()) & (secondary.index <= primary.index.max())]
    overlap_index = primary.index.intersection(secondary.index)
    if len(overlap_index) < 100:
        raise MarketDataNormalizationError("Fewer than 100 aligned M1 bars across primary and secondary feeds")

    overlap_primary = primary.loc[overlap_index]
    overlap_secondary = secondary.loc[overlap_index]
    raw_basis = np.log(overlap_primary["close"] / overlap_secondary["close"])
    smooth_basis = raw_basis.rolling(121, center=True, min_periods=20).median().fillna(raw_basis.median())
    scale = np.exp(smooth_basis)
    adjusted_secondary = overlap_secondary.copy()
    adjusted_secondary.loc[:, PRICE_COLUMNS] = adjusted_secondary.loc[:, PRICE_COLUMNS].mul(scale, axis=0)
    disagreement_pips = _max_ohlc_difference(overlap_primary, adjusted_secondary) / pip
    median_disagreement = float(disagreement_pips.median())
    mad_disagreement = float((disagreement_pips - median_disagreement).abs().median())
    spike_threshold = max(
        config.minimum_spike_pips,
        median_disagreement + config.spike_mad_multiplier * max(mad_disagreement, 1e-12),
    )

    audits: list[dict] = []
    normalized = primary.copy()

    # Fill only short secondary-only runs bracketed by nearby, stable cross-feed anchors.
    secondary_only = secondary.index.difference(primary.index)
    secondary_only = secondary_only[(secondary_only > primary.index.min()) & (secondary_only < primary.index.max())]
    missing_table = pd.DataFrame(index=secondary_only)
    if len(missing_table):
        missing_table["run"] = missing_table.index.to_series().diff().ne(pd.Timedelta(minutes=1)).cumsum().to_numpy()
        run_sizes = missing_table.groupby("run").size()
        missing_table["run_size"] = missing_table["run"].map(run_sizes)
        basis_lookup = raw_basis.reindex(primary.index.union(secondary.index))
        previous_basis = basis_lookup.ffill().reindex(secondary_only)
        next_basis = basis_lookup.bfill().reindex(secondary_only)
        matched_timestamps = pd.Series(basis_lookup.index.where(basis_lookup.notna()), index=basis_lookup.index)
        previous_timestamp = matched_timestamps.ffill().reindex(secondary_only)
        next_timestamp = matched_timestamps.bfill().reindex(secondary_only)
        previous_distance = (missing_table.index.to_series() - previous_timestamp).dt.total_seconds() / 60
        next_distance = (next_timestamp - missing_table.index.to_series()).dt.total_seconds() / 60
        typical_price = secondary.loc[secondary_only, "close"]
        basis_change_pips = (np.exp(previous_basis.sub(next_basis).abs()) - 1.0) * typical_price / pip
        eligible = (
            missing_table["run_size"].le(config.max_missing_run_minutes)
            & previous_distance.le(config.max_anchor_distance_minutes)
            & next_distance.le(config.max_anchor_distance_minutes)
            & basis_change_pips.le(config.max_anchor_basis_change_pips)
            & previous_basis.notna()
            & next_basis.notna()
        )
        eligible_index = missing_table.index[eligible]
        if len(eligible_index):
            total_distance = (next_timestamp.loc[eligible_index] - previous_timestamp.loc[eligible_index]).dt.total_seconds()
            elapsed = (eligible_index.to_series() - previous_timestamp.loc[eligible_index]).dt.total_seconds()
            weight = elapsed / total_distance
            interpolated_basis = previous_basis.loc[eligible_index] + weight * (
                next_basis.loc[eligible_index] - previous_basis.loc[eligible_index]
            )
            missing_scales = np.exp(interpolated_basis)
            additions = secondary.loc[eligible_index].copy()
            additions.loc[:, PRICE_COLUMNS] = additions.loc[:, PRICE_COLUMNS].mul(missing_scales, axis=0)
            additions["tick_volume"] = 0.0
            normalized = pd.concat([normalized, additions]).sort_index()
            for timestamp in eligible_index:
                audits.append(_audit_row(
                    "fill_missing_primary_bar",
                    timestamp,
                    None,
                    secondary.loc[timestamp],
                    additions.loc[timestamp],
                    missing_scales.loc[timestamp],
                    f"short run bracketed by stable anchors; run_minutes={int(missing_table.loc[timestamp, 'run_size'])}",
                ))
        rejected_missing = int((~eligible).sum())
    else:
        eligible_index = secondary_only
        rejected_missing = 0

    # Replace only isolated out-and-back primary excursions. Persistent disagreements remain primary.
    previous_disagreement = disagreement_pips.shift(1)
    next_disagreement = disagreement_pips.shift(-1)
    consecutive = overlap_index.to_series().diff().eq(pd.Timedelta(minutes=1)).to_numpy()
    next_consecutive = overlap_index.to_series().shift(-1).sub(overlap_index.to_series()).eq(pd.Timedelta(minutes=1)).to_numpy()
    surrounded = pd.Series(consecutive & next_consecutive, index=overlap_index)
    isolated = (
        disagreement_pips.gt(spike_threshold)
        & previous_disagreement.le(config.surrounding_agreement_pips)
        & next_disagreement.le(config.surrounding_agreement_pips)
        & surrounded
    )
    isolated_index = overlap_index[isolated]
    replaced_spikes = []
    for timestamp in isolated_index:
        position = overlap_index.get_loc(timestamp)
        previous_close = float(overlap_primary.iloc[position - 1]["close"])
        next_open = float(overlap_primary.iloc[position + 1]["open"])
        bridge = (previous_close + next_open) / 2.0
        primary_excursion = float(overlap_primary.loc[timestamp, PRICE_COLUMNS].sub(bridge).abs().max())
        secondary_excursion = float(adjusted_secondary.loc[timestamp, PRICE_COLUMNS].sub(bridge).abs().max())
        if primary_excursion < config.primary_excursion_ratio * max(secondary_excursion, pip):
            continue
        replacement = adjusted_secondary.loc[timestamp].copy()
        replacement["tick_volume"] = 0.0
        normalized.loc[timestamp, [*PRICE_COLUMNS, "tick_volume"]] = replacement[[*PRICE_COLUMNS, "tick_volume"]]
        replaced_spikes.append(timestamp)
        audits.append(_audit_row(
            "replace_isolated_primary_spike",
            timestamp,
            primary.loc[timestamp],
            secondary.loc[timestamp],
            replacement,
            scale.loc[timestamp],
            f"isolated out-and-back excursion above {spike_threshold:.3f} pips",
        ))

    reference_blocks_replaced: list[pd.Timestamp] = []
    reference_minutes_replaced: list[pd.Timestamp] = []
    reference_validation = None
    reference_timeframe = None
    if reference_bars is not None:
        reference_timeframe, reference_frame = reference_bars
        reference_timeframe = reference_timeframe.upper()
        if reference_timeframe not in REFERENCE_TIMEFRAME_MINUTES:
            raise MarketDataNormalizationError(
                f"Unsupported MT5 reference timeframe: {reference_timeframe}"
            )
        reference_minutes = REFERENCE_TIMEFRAME_MINUTES[reference_timeframe]
        reference = reference_frame.copy()
        reference["timestamp"] = pd.to_datetime(reference["timestamp"], utc=True, errors="raise")
        reference = reference.set_index("timestamp", drop=False).sort_index()
        if reference.index.has_duplicates:
            raise MarketDataNormalizationError(f"MT5 {reference_timeframe} reference contains duplicate timestamps")

        primary_reference = _aggregate_complete_timeframe(primary, reference_timeframe)
        # Vote on the independent raw feeds. The usual secondary basis adjustment is
        # derived from Dukascopy and can inherit a persistent primary timing defect.
        secondary_reference = _aggregate_complete_timeframe(overlap_secondary, reference_timeframe)
        common_blocks = primary_reference.index.intersection(secondary_reference.index).intersection(reference.index)
        if len(common_blocks) < config.minimum_reference_m5_blocks:
            raise MarketDataNormalizationError(
                f"Fewer than {config.minimum_reference_m5_blocks} complete "
                f"{reference_timeframe} bars across all three feeds"
            )
        aligned_primary = primary_reference.loc[common_blocks]
        aligned_secondary = secondary_reference.loc[common_blocks]
        aligned_reference = reference.loc[common_blocks]
        primary_reference_pips = _max_ohlc_difference(aligned_primary, aligned_reference) / pip
        secondary_reference_pips = _max_ohlc_difference(aligned_secondary, aligned_reference) / pip
        adaptive_reference_agreement_pips = max(
            config.reference_agreement_pips,
            float(secondary_reference_pips.quantile(config.adaptive_reference_agreement_quantile)),
        )
        adaptive_primary_disagreement_base_pips = max(
            config.reference_agreement_pips,
            float(secondary_reference_pips.quantile(config.adaptive_reference_primary_disagreement_quantile)),
        )
        adaptive_primary_disagreement_pips = max(
            config.reference_primary_disagreement_pips,
            spike_threshold,
            adaptive_primary_disagreement_base_pips * config.adaptive_reference_primary_disagreement_ratio,
        )
        large_disagreement_blocks = common_blocks[
            primary_reference_pips.gt(adaptive_primary_disagreement_pips)
            & secondary_reference_pips.le(adaptive_reference_agreement_pips)
        ]
        shifted_dates, shift_diagnostics = _detect_reference_time_shift_days(
            primary_reference,
            reference,
            pip,
            config,
        )
        shifted_day_mask = pd.Series(
            [timestamp.date() in shifted_dates for timestamp in common_blocks],
            index=common_blocks,
        )
        shifted_day_blocks = common_blocks[
            shifted_day_mask & secondary_reference_pips.le(adaptive_reference_agreement_pips)
        ]
        confirmed_blocks = large_disagreement_blocks.union(shifted_day_blocks).sort_values()
        reference_blocks_replaced.extend(confirmed_blocks)
        if len(confirmed_blocks):
            replacement_index = pd.DatetimeIndex([
                block_start + pd.Timedelta(minutes=minute_offset)
                for block_start in confirmed_blocks
                for minute_offset in range(reference_minutes)
            ])
            replacements = overlap_secondary.loc[replacement_index].copy()
            replacements["tick_volume"] = 0.0
            normalized.loc[replacement_index, [*PRICE_COLUMNS, "tick_volume"]] = replacements[
                [*PRICE_COLUMNS, "tick_volume"]
            ].to_numpy()
            reference_minutes_replaced.extend(replacement_index)
            reference_audit = pd.DataFrame({
                "timestamp": replacement_index,
                "action": "replace_primary_bar_confirmed_by_mt5",
                "detail": [
                    f"complete {reference_timeframe} block confirmed by HistData and MT5; "
                    f"block={timestamp.floor(f'{reference_minutes}min').isoformat()}"
                    for timestamp in replacement_index
                ],
                "secondary_scale": 1.0,
            })
            for column in PRICE_COLUMNS:
                reference_audit[f"primary_{column}"] = primary.loc[replacement_index, column].to_numpy()
                reference_audit[f"secondary_{column}"] = secondary.loc[replacement_index, column].to_numpy()
                reference_audit[f"normalized_{column}"] = replacements.loc[replacement_index, column].to_numpy()
            audits.extend(reference_audit.to_dict("records"))
        reference_validation = {
            "timeframe": reference_timeframe,
            "overlap_complete_blocks": int(len(common_blocks)),
            "primary_vs_reference_p95_pips": float(primary_reference_pips.quantile(0.95)),
            "secondary_vs_reference_p95_pips": float(secondary_reference_pips.quantile(0.95)),
            "secondary_vs_reference_p99_pips": float(secondary_reference_pips.quantile(0.99)),
            "secondary_vs_reference_p995_pips": float(secondary_reference_pips.quantile(0.995)),
            "effective_reference_agreement_pips": float(adaptive_reference_agreement_pips),
            "effective_primary_disagreement_base_pips": float(adaptive_primary_disagreement_base_pips),
            "effective_primary_disagreement_pips": float(adaptive_primary_disagreement_pips),
            "confirmed_blocks_replaced": int(len(reference_blocks_replaced)),
            "confirmed_minutes_replaced": int(len(reference_minutes_replaced)),
            "time_shift_days": shift_diagnostics,
            "time_shift_blocks_replaced": int(len(shifted_day_blocks)),
        }

    normalized = normalized.reset_index(drop=True)
    normalized = normalize_and_validate(
        normalized,
        DatasetContract(symbol=symbol, timeframe="M1", source="Dukascopy+HistData", venue="composite", data_kind="cleaned"),
    )
    audit_columns = ["timestamp", "action", "detail", "secondary_scale"] + [
        f"{prefix}_{column}" for prefix in ("primary", "secondary", "normalized") for column in PRICE_COLUMNS
    ]
    audit = pd.DataFrame(audits, columns=audit_columns).sort_values("timestamp").reset_index(drop=True)
    replaced_timestamps = set(replaced_spikes).union(reference_minutes_replaced)
    severe = disagreement_pips[disagreement_pips > spike_threshold].nlargest(20)
    used_reference = reference_bars is not None
    report = {
        "symbol": symbol.upper(),
        "method": (
            "Dukascopy primary; locally basis-adjusted HistData fallback; "
            f"MT5 {reference_timeframe} majority confirmation for persistent primary defects; never average OHLC"
            if used_reference else
            "Dukascopy primary; locally basis-adjusted HistData fallback; never average OHLC"
        ),
        "config": asdict(config),
        "pip_size": pip,
        "primary_rows": int(len(primary)),
        "secondary_rows_in_primary_range": int(len(secondary)),
        "overlap_rows": int(len(overlap_index)),
        "secondary_only_rows": int(len(secondary_only)),
        "missing_primary_bars_filled": int(len(eligible_index)),
        "missing_primary_bars_rejected": rejected_missing,
        "isolated_primary_spikes_replaced": int(len(replaced_spikes)),
        "reference_timeframe": reference_timeframe,
        "reference_confirmed_blocks_replaced": int(len(reference_blocks_replaced)),
        "reference_confirmed_m5_blocks_replaced": int(len(reference_blocks_replaced)),
        "reference_confirmed_primary_bars_replaced": int(len(reference_minutes_replaced)),
        "reference_validation": reference_validation,
        "normalized_rows": int(len(normalized)),
        "cross_feed_disagreement_pips": {
            "median": median_disagreement,
            "mad": mad_disagreement,
            "p95": float(disagreement_pips.quantile(0.95)),
            "p99": float(disagreement_pips.quantile(0.99)),
            "maximum": float(disagreement_pips.max()),
            "isolated_spike_threshold": float(spike_threshold),
        },
        "largest_unresolved_cross_feed_disagreements": [
            {"timestamp": timestamp.isoformat(), "max_ohlc_difference_pips": float(value)}
            for timestamp, value in severe.items() if timestamp not in replaced_timestamps
        ],
        "policy_notes": [
            "Persistent cross-feed disagreements are not auto-patched because two feeds cannot establish a majority.",
            "When MT5 M5 or M30 is supplied, complete blocks are patched only where HistData and MT5 agree and Dukascopy does not.",
            "HistData bars are scaled to nearby Dukascopy anchors before substitution.",
            "Rejected secondary-only rows are retained in the report but not invented in the normalized dataset.",
        ],
    }
    return normalized, audit, report
