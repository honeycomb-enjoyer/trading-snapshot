"""Download Dukascopy BID candles and build canonical UTC OHLC datasets."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import pandas as pd

from data.manifest import build_manifest, sha256_file, write_manifest
from data.normalize_market_data import discover_histdata_files, load_histdata_m1, normalize_m1_sources
from data.schema import DatasetContract, TIMEFRAME_DELTAS, normalize_and_validate, parse_utc, project_root


BASE_URL = "https://jetta.dukascopy.com/v1"
BAR_BOUNDARY_TIMEZONE = "America/New_York"
BAR_BOUNDARY_HOUR = 17
SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")
class DukascopyDataError(RuntimeError):
    """Raised when remote or cached Dukascopy data fails validation."""


def _request_json(url: str, *, retries: int = 4, timeout: int = 30) -> dict:
    request = Request(url, headers={"User-Agent": "trading-research/1.0 historical-data-downloader"})
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise DukascopyDataError(f"Expected JSON object from {url}")
            return payload
        except HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise DukascopyDataError(f"Dukascopy returned HTTP {exc.code} for {url}") from exc
            last_error = exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt + 1 < retries:
            time.sleep(0.5 * (2 ** attempt))
    raise DukascopyDataError(f"Dukascopy request failed after {retries} attempts: {url}") from last_error


def _instrument_code(symbol: str) -> str:
    normalized = symbol.upper().replace("/", "").replace("-", "")
    if len(normalized) != 6 or not normalized.isalpha():
        raise ValueError("Dukascopy FX symbol must look like AUDCAD or AUD/CAD")
    return f"{normalized[:3]}-{normalized[3:]}"


def _validate_compressed_payload(payload: dict, *, source: str) -> None:
    fields = ("times", "opens", "highs", "lows", "closes", "volumes")
    arrays = []
    for field in fields:
        value = payload.get(field, [])
        if not isinstance(value, list):
            raise DukascopyDataError(f"{source}: {field} must be an array")
        arrays.append(value)
    lengths = {len(value) for value in arrays}
    if len(lengths) != 1:
        raise DukascopyDataError(f"{source}: inconsistent compressed OHLCV array lengths")
    if arrays[0] and any(key not in payload for key in ("timestamp", "open", "high", "low", "close")):
        raise DukascopyDataError(f"{source}: missing compressed OHLCV base values")


def decode_minute_payload(payload: dict, *, source: str = "Dukascopy payload") -> pd.DataFrame:
    """Decode the delta-compressed response used by Dukascopy's export widget."""
    _validate_compressed_payload(payload, source=source)
    times = payload.get("times", [])
    if not times:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "tick_volume"])

    multiplier = float(payload.get("multiplier", 1.0))
    shift = int(payload.get("shift", 1))
    if multiplier <= 0 or shift <= 0:
        raise DukascopyDataError(f"{source}: multiplier and shift must be positive")

    timestamp = int(payload["timestamp"])
    price_units = {
        field: int(round(float(payload[field]) / multiplier))
        for field in ("open", "high", "low", "close")
    }
    rows = []
    previous_timestamp = None
    deltas = {field: payload[f"{field}s"] for field in ("open", "high", "low", "close")}
    for position, time_delta in enumerate(times):
        timestamp += shift * int(time_delta)
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise DukascopyDataError(f"{source}: timestamps are not strictly increasing")
        if timestamp % 60_000:
            raise DukascopyDataError(f"{source}: M1 timestamp is not minute-aligned")
        previous_timestamp = timestamp
        prices = {}
        for field in ("open", "high", "low", "close"):
            price_units[field] += int(deltas[field][position])
            prices[field] = price_units[field] * multiplier
        volume = float(payload["volumes"][position])
        if volume < 0:
            raise DukascopyDataError(f"{source}: volume must be non-negative")
        rows.append((timestamp, prices["open"], prices["high"], prices["low"], prices["close"], int(volume * 1_000_000 + 0.5)))

    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "tick_volume"]).assign(
        timestamp=lambda frame: pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    )


def _cache_path(cache_root: Path, instrument_code: str, side: str, day: pd.Timestamp) -> Path:
    return cache_root / instrument_code / side / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}.json"


def _fetch_day(instrument_code: str, side: str, day: pd.Timestamp, cache_root: Path) -> tuple[pd.Timestamp, Path, str]:
    path = _cache_path(cache_root, instrument_code, side, day)
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            _validate_compressed_payload(payload, source=str(path))
        except (OSError, json.JSONDecodeError, DukascopyDataError):
            path.unlink(missing_ok=True)
        else:
            return day, path, hashlib.sha256(path.read_bytes()).hexdigest()

    url = f"{BASE_URL}/candles/minute/{instrument_code}/{side}/{day.year}/{day.month}/{day.day}"
    try:
        payload = _request_json(url)
    except DukascopyDataError:
        if day.dayofweek >= 5:
            payload = {field: [] for field in ("times", "opens", "highs", "lows", "closes", "volumes")}
        else:
            raise
    _validate_compressed_payload(payload, source=url)
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.part")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return day, path, hashlib.sha256(encoded).hexdigest()


def _download_source_m1(
    instrument_code: str, side: str, start: pd.Timestamp, end: pd.Timestamp,
    cache_root: Path, *, workers: int,
) -> tuple[pd.DataFrame, str, int]:
    days = list(pd.date_range(start.normalize(), end.normalize() - pd.Timedelta(nanoseconds=1), freq="D", tz="UTC"))
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_day, instrument_code, side, day, cache_root): day for day in days}
        for completed, future in enumerate(as_completed(futures), start=1):
            day, path, digest = future.result()
            results[day] = (path, digest)
            if completed % 100 == 0 or completed == len(futures):
                print(f"Dukascopy days cached: {completed}/{len(futures)}")

    frames = []
    cache_digest = hashlib.sha256()
    for day in days:
        path, digest = results[day]
        cache_digest.update(f"{day.date().isoformat()}:{digest}\n".encode("ascii"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        frame = decode_minute_payload(payload, source=str(path))
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise DukascopyDataError("Dukascopy returned no M1 candles for the requested range")
    source = pd.concat(frames, ignore_index=True)
    source = source.loc[(source["timestamp"] >= start) & (source["timestamp"] < end)].reset_index(drop=True)
    return source, cache_digest.hexdigest(), len(days)


def _new_york_session_start(timestamps: pd.Series) -> pd.Series:
    """Return the 17:00 America/New_York session start for every UTC timestamp."""
    local_naive = timestamps.dt.tz_convert(BAR_BOUNDARY_TIMEZONE).dt.tz_localize(None)
    session_date = local_naive.dt.normalize()
    before_close = local_naive.dt.hour < BAR_BOUNDARY_HOUR
    session_date = session_date - pd.to_timedelta(before_close.astype(int), unit="D")
    local_start = (session_date + pd.Timedelta(hours=BAR_BOUNDARY_HOUR)).dt.tz_localize(
        BAR_BOUNDARY_TIMEZONE, ambiguous="raise", nonexistent="raise",
    )
    return local_start.dt.tz_convert("UTC")


def _bar_starts(timestamps: pd.Series, timeframe: str) -> pd.Series:
    session_start = _new_york_session_start(timestamps)
    if timeframe == "D1":
        return session_start
    if timeframe == "W1":
        local_naive = session_start.dt.tz_convert(BAR_BOUNDARY_TIMEZONE).dt.tz_localize(None)
        days_since_sunday = (local_naive.dt.dayofweek + 1) % 7
        week_date = local_naive.dt.normalize() - pd.to_timedelta(days_since_sunday, unit="D")
        return (week_date + pd.Timedelta(hours=BAR_BOUNDARY_HOUR)).dt.tz_localize(
            BAR_BOUNDARY_TIMEZONE, ambiguous="raise", nonexistent="raise",
        ).dt.tz_convert("UTC")
    interval = pd.Timedelta(TIMEFRAME_DELTAS[timeframe])
    bucket_number = ((timestamps - session_start) // interval).astype("int64")
    return session_start + bucket_number * interval


def _bar_ends(starts: pd.Series, timeframe: str) -> pd.Series:
    if timeframe not in {"D1", "W1"}:
        return starts + pd.Timedelta(TIMEFRAME_DELTAS[timeframe])
    days = 1 if timeframe == "D1" else 7
    local_naive = starts.dt.tz_convert(BAR_BOUNDARY_TIMEZONE).dt.tz_localize(None)
    next_local = local_naive + pd.Timedelta(days=days)
    return next_local.dt.tz_localize(
        BAR_BOUNDARY_TIMEZONE, ambiguous="raise", nonexistent="raise",
    ).dt.tz_convert("UTC")


def build_timeframe(source_m1: pd.DataFrame, timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    timeframe = timeframe.upper()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if timeframe == "M1":
        return source_m1.copy()
    grouped = source_m1.copy()
    grouped["timestamp"] = _bar_starts(grouped["timestamp"], timeframe)
    result = grouped.groupby("timestamp", sort=True, as_index=False).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    bar_ends = _bar_ends(result["timestamp"], timeframe)
    return result.loc[(result["timestamp"] >= start) & (bar_ends <= end)].reset_index(drop=True)


def repair_tiny_ohlc_violations(frame: pd.DataFrame, *, max_adjustment: float) -> tuple[pd.DataFrame, dict]:
    """Widen high/low for sub-pip compression inconsistencies, or fail closed."""
    repaired = frame.copy()
    required_high = repaired[["open", "close", "low"]].max(axis=1)
    required_low = repaired[["open", "close", "high"]].min(axis=1)
    high_adjustment = (required_high - repaired["high"]).clip(lower=0)
    low_adjustment = (repaired["low"] - required_low).clip(lower=0)
    largest = max(float(high_adjustment.max()), float(low_adjustment.max()))
    if largest > max_adjustment + 1e-12:
        raise DukascopyDataError(
            f"OHLC violation {largest:.10f} exceeds permitted compression repair {max_adjustment:.10f}"
        )
    high_mask = high_adjustment > 0
    low_mask = low_adjustment > 0
    repaired.loc[high_mask, "high"] = required_high.loc[high_mask]
    repaired.loc[low_mask, "low"] = required_low.loc[low_mask]
    stats = {
        "high_rows": int(high_mask.sum()),
        "low_rows": int(low_mask.sum()),
        "max_adjustment": largest,
        "limit": float(max_adjustment),
        "method": "widen high/low to include open and close; reject adjustments above one instrument pip",
    }
    return repaired, stats


def interval_gap_statistics(frame: pd.DataFrame, timeframe: str) -> dict:
    interval = pd.Timedelta(TIMEFRAME_DELTAS[timeframe])
    gaps = frame["timestamp"].diff()
    unexpected = gaps[gaps > interval]
    return {
        "intervals_larger_than_expected": int(len(unexpected)),
        "first_larger_interval": None if unexpected.empty else frame.loc[unexpected.index[0], "timestamp"].isoformat(),
        "max_interval_seconds": 0 if unexpected.empty else float(unexpected.max().total_seconds()),
        "note": "Includes normal weekends/holidays as well as missing quote intervals; schema validation separately warns about non-closure gaps.",
    }


def _write_dataset(
    frame: pd.DataFrame, *, symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp,
    output_dir: Path, cache_fingerprint: str, cached_days: int, side: str, repair_stats: dict,
) -> Path:
    contract = DatasetContract(symbol=symbol, timeframe=timeframe, source="Dukascopy Jetta", venue="Dukascopy", data_kind="raw" if timeframe == "M1" else "derived")
    frame = normalize_and_validate(frame, contract)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{symbol}_{timeframe}_{start:%Y%m%d}_{end:%Y%m%d}_UTC.csv"
    frame.to_csv(output, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    manifest = build_manifest(output, frame, contract, extra_metadata={
        "api": "Dukascopy Jetta public historical candles",
        "api_base_url": BASE_URL,
        "offer_side": side,
        "source_timeframe": "M1",
        "aggregation": "direct M1" if timeframe == "M1" else f"OHLC anchored to {BAR_BOUNDARY_HOUR:02d}:00 {BAR_BOUNDARY_TIMEZONE}",
        "bar_boundary_timezone": BAR_BOUNDARY_TIMEZONE,
        "bar_boundary_local_time": f"{BAR_BOUNDARY_HOUR:02d}:00",
        "bar_boundary_dst_policy": "IANA historical DST; 22:00 UTC in New York standard time and 21:00 UTC in New York daylight time",
        "volume_semantics": "Dukascopy quote volume; not centralized FX traded volume",
        "cache_fingerprint_sha256": cache_fingerprint,
        "cached_calendar_days": cached_days,
        "source_ohlc_repairs": repair_stats,
        "interval_gap_statistics": interval_gap_statistics(frame, timeframe),
        "requested_start": start.isoformat(),
        "requested_end_exclusive": end.isoformat(),
    })
    write_manifest(manifest, output)
    print(f"Saved {timeframe}: {output} ({len(frame)} bars)")
    return output


def _write_normalized_dataset(
    frame: pd.DataFrame, *, symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp,
    output_dir: Path, primary_cache_fingerprint: str, histdata_metadata: dict,
    normalization_report: dict, audit_path: Path, quality_report_path: Path,
) -> Path:
    used_mt5_reference = normalization_report.get("reference_validation") is not None
    contract = DatasetContract(
        symbol=symbol,
        timeframe=timeframe,
        source="Dukascopy Jetta + HistData + MT5 validation" if used_mt5_reference else "Dukascopy Jetta + HistData",
        venue="composite",
        data_kind="cleaned" if timeframe == "M1" else "derived",
    )
    frame = normalize_and_validate(frame, contract)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{symbol}_{timeframe}_{start:%Y%m%d}_{end:%Y%m%d}_UTC.csv"
    frame.to_csv(output, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    manifest = build_manifest(output, frame, contract, extra_metadata={
        "primary_source": "Dukascopy Jetta BID M1",
        "primary_cache_fingerprint_sha256": primary_cache_fingerprint,
        "secondary_source": "HistData MetaTrader BID M1",
        "secondary_fingerprint_sha256": histdata_metadata["fingerprint_sha256"],
        "secondary_timezone_input": histdata_metadata["timezone_input"],
        "secondary_files": histdata_metadata["files"],
        "normalization_method": normalization_report["method"],
        "normalization_counts": {
            "missing_primary_bars_filled": normalization_report["missing_primary_bars_filled"],
            "missing_primary_bars_rejected": normalization_report["missing_primary_bars_rejected"],
            "isolated_primary_spikes_replaced": normalization_report["isolated_primary_spikes_replaced"],
            "reference_timeframe": normalization_report.get("reference_timeframe"),
            "reference_confirmed_blocks_replaced": normalization_report.get(
                "reference_confirmed_blocks_replaced", 0,
            ),
            "reference_confirmed_m5_blocks_replaced": normalization_report.get(
                "reference_confirmed_m5_blocks_replaced", 0,
            ),
            "reference_confirmed_primary_bars_replaced": normalization_report.get(
                "reference_confirmed_primary_bars_replaced", 0,
            ),
        },
        "mt5_reference": normalization_report.get("mt5_reference"),
        "source_timeframe": "M1",
        "aggregation": "normalized M1" if timeframe == "M1" else f"OHLC anchored to {BAR_BOUNDARY_HOUR:02d}:00 {BAR_BOUNDARY_TIMEZONE}",
        "bar_boundary_timezone": BAR_BOUNDARY_TIMEZONE,
        "bar_boundary_local_time": f"{BAR_BOUNDARY_HOUR:02d}:00",
        "bar_boundary_dst_policy": "IANA historical DST; 22:00 UTC in New York standard time and 21:00 UTC in New York daylight time",
        "patch_audit": str(audit_path.relative_to(project_root())).replace("\\", "/"),
        "quality_report": str(quality_report_path.relative_to(project_root())).replace("\\", "/"),
        "interval_gap_statistics": interval_gap_statistics(frame, timeframe),
        "requested_start": start.isoformat(),
        "requested_end_exclusive": end.isoformat(),
    })
    write_manifest(manifest, output)
    print(f"Saved normalized {timeframe}: {output} ({len(frame)} bars)")
    return output


def compare_with_mt5(dukascopy_m1: pd.DataFrame, mt5_path: str | Path, *, output_dir: Path) -> Path:
    mt5_path = Path(mt5_path)
    mt5 = pd.read_csv(mt5_path)
    mt5["timestamp"] = pd.to_datetime(mt5["timestamp"], utc=True)
    name_parts = mt5_path.name.split("_")
    if len(name_parts) < 2 or name_parts[1].upper() not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Cannot infer MT5 timeframe from {mt5_path.name}")
    timeframe = name_parts[1].upper()
    start, end = mt5["timestamp"].min(), mt5["timestamp"].max() + pd.Timedelta(TIMEFRAME_DELTAS[timeframe])
    dukascopy = build_timeframe(dukascopy_m1, timeframe, start, end)
    merged = mt5.merge(dukascopy, on="timestamp", suffixes=("_mt5", "_dukascopy"))
    if merged.empty:
        raise DukascopyDataError(f"No aligned timestamps for comparison with {mt5_path}")
    differences = {}
    maximum_delta = pd.Series(0.0, index=merged.index)
    for column in ("open", "high", "low", "close"):
        delta = (merged[f"{column}_mt5"] - merged[f"{column}_dukascopy"]).abs()
        maximum_delta = maximum_delta.where(maximum_delta >= delta, delta)
        differences[column] = {
            "median_abs": float(delta.median()),
            "p95_abs": float(delta.quantile(0.95)),
            "max_abs": float(delta.max()),
        }
    symbol = name_parts[0].upper()
    pip = 0.01 if symbol.endswith("JPY") else 0.0001
    worst = merged.assign(max_abs_difference=maximum_delta).nlargest(10, "max_abs_difference")
    worst_rows = [{
        "timestamp": row["timestamp"].isoformat(),
        "max_abs_difference": float(row["max_abs_difference"]),
        "mt5_close": float(row["close_mt5"]),
        "dukascopy_close": float(row["close_dukascopy"]),
    } for _, row in worst.iterrows()]
    close_lag_diagnostics = {}
    for lag_minutes in (-120, -60, 0, 60, 120):
        shifted = dukascopy[["timestamp", "close"]].copy()
        shifted["timestamp"] += pd.Timedelta(minutes=lag_minutes)
        lagged = mt5[["timestamp", "close"]].merge(shifted, on="timestamp", suffixes=("_mt5", "_dukascopy"))
        close_delta = (lagged["close_mt5"] - lagged["close_dukascopy"]).abs()
        close_lag_diagnostics[str(lag_minutes)] = {
            "overlap_bars": int(len(lagged)),
            "median_abs": None if lagged.empty else float(close_delta.median()),
            "p95_abs": None if lagged.empty else float(close_delta.quantile(0.95)),
        }
    report = {
        "dukascopy_basis": "BID",
        "mt5_dataset": str(mt5_path).replace("\\", "/"),
        "timeframe": timeframe,
        "overlap_bars": int(len(merged)),
        "mt5_bars_in_range": int(len(mt5)),
        "dukascopy_bars_in_range": int(len(dukascopy)),
        "first_overlap": merged["timestamp"].iloc[0].isoformat(),
        "last_overlap": merged["timestamp"].iloc[-1].isoformat(),
        "absolute_price_differences": differences,
        "max_ohlc_difference_threshold_counts": {
            "above_1_pip": int((maximum_delta > pip).sum()),
            "above_5_pips": int((maximum_delta > 5 * pip).sum()),
            "above_20_pips": int((maximum_delta > 20 * pip).sum()),
        },
        "worst_aligned_bars": worst_rows,
        "close_time_lag_diagnostics_minutes": close_lag_diagnostics,
        "note": "Different venues are expected to differ; this report checks alignment and scale, not exact equality.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{mt5_path.stem}_vs_Dukascopy.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved comparison: {output}")
    return output


def download_data(
    symbol: str, timeframes: list[str] | tuple[str, ...], start, end=None, *, side: str = "BID",
    output_dir: str | Path = "data/raw/dukascopy", cache_dir: str | Path = "data/raw/dukascopy/source",
    workers: int = 8, compare_mt5: str | Path | None = None,
) -> dict[str, Path]:
    symbol = symbol.upper().replace("/", "").replace("-", "")
    side = side.upper()
    if side not in {"BID", "ASK"}:
        raise ValueError("side must be BID or ASK")
    normalized_timeframes = tuple(dict.fromkeys(value.upper() for value in timeframes))
    if not normalized_timeframes or any(value not in SUPPORTED_TIMEFRAMES for value in normalized_timeframes):
        raise ValueError(f"timeframes must use: {', '.join(SUPPORTED_TIMEFRAMES)}")
    start_utc = parse_utc(start, field_name="start")
    end_utc = parse_utc(end or datetime.now(timezone.utc).date(), field_name="end")
    if start_utc >= end_utc:
        raise ValueError("start must be before end")
    if start_utc.second or start_utc.microsecond or end_utc.second or end_utc.microsecond:
        raise ValueError("start and end must be aligned to whole minutes")
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")

    instrument_code = _instrument_code(symbol)
    metadata = _request_json(f"{BASE_URL}/instruments/{instrument_code}")
    if metadata.get("code") != instrument_code:
        raise DukascopyDataError(f"Instrument metadata mismatch for {instrument_code}")
    history_start = next((item.get("from") for item in metadata.get("histories", []) if item.get("period") == "MINUTE"), None)
    if history_start is None or start_utc < pd.Timestamp(int(history_start), unit="ms", tz="UTC"):
        raise DukascopyDataError(f"Requested start precedes available M1 history for {instrument_code}")

    resolved_output = Path(output_dir) if Path(output_dir).is_absolute() else project_root() / output_dir
    resolved_cache = Path(cache_dir) if Path(cache_dir).is_absolute() else project_root() / cache_dir
    source_m1, cache_fingerprint, cached_days = _download_source_m1(
        instrument_code, side, start_utc, end_utc, resolved_cache, workers=workers,
    )
    source_m1, repair_stats = repair_tiny_ohlc_violations(
        source_m1, max_adjustment=float(metadata.get("pipValue", 0.0001)),
    )
    source_contract = DatasetContract(symbol=symbol, timeframe="M1", source="Dukascopy Jetta", venue="Dukascopy")
    source_m1 = normalize_and_validate(source_m1, source_contract)

    outputs = {}
    for timeframe in normalized_timeframes:
        frame = build_timeframe(source_m1, timeframe, start_utc, end_utc)
        outputs[timeframe] = _write_dataset(
            frame, symbol=symbol, timeframe=timeframe, start=start_utc, end=end_utc,
            output_dir=resolved_output, cache_fingerprint=cache_fingerprint,
            cached_days=cached_days, side=side, repair_stats=repair_stats,
        )

    histdata_files = discover_histdata_files(symbol)
    if histdata_files:
        histdata_m1, histdata_metadata = load_histdata_m1(symbol, start_utc, end_utc)
        reference_bars = None
        mt5_reference_metadata = None
        if compare_mt5 is not None:
            reference_path = Path(compare_mt5)
            if not reference_path.is_absolute():
                reference_path = project_root() / reference_path
            name_parts = reference_path.name.split("_")
            reference_timeframe = name_parts[1].upper() if len(name_parts) >= 2 else None
            if reference_timeframe in {"M5", "M30"}:
                reference_frame = pd.read_csv(reference_path)
                reference_frame["timestamp"] = pd.to_datetime(reference_frame["timestamp"], utc=True, errors="raise")
                reference_frame = reference_frame.loc[
                    (reference_frame["timestamp"] >= start_utc) & (reference_frame["timestamp"] < end_utc)
                ].reset_index(drop=True)
                reference_frame = normalize_and_validate(
                    reference_frame,
                    DatasetContract(
                        symbol=symbol,
                        timeframe=reference_timeframe,
                        source="MetaTrader5",
                        venue="broker reference",
                    ),
                )
                reference_bars = (reference_timeframe, reference_frame)
                mt5_reference_metadata = {
                    "path": str(reference_path.relative_to(project_root())).replace("\\", "/"),
                    "timeframe": reference_timeframe,
                    "content_sha256": sha256_file(reference_path),
                    "row_count": int(len(reference_frame)),
                    "role": "third-source majority confirmation; never a standalone replacement source",
                }
        normalized_m1, patch_audit, normalization_report = normalize_m1_sources(
            source_m1, histdata_m1, symbol, reference_bars=reference_bars,
        )
        normalization_report["mt5_reference"] = mt5_reference_metadata
        normalized_output = project_root() / "data" / "normalized"
        quality_dir = normalized_output / "quality"
        quality_dir.mkdir(parents=True, exist_ok=True)
        audit_path = quality_dir / f"{symbol}_{start_utc:%Y%m%d}_{end_utc:%Y%m%d}_patches.csv"
        quality_report_path = quality_dir / f"{symbol}_{start_utc:%Y%m%d}_{end_utc:%Y%m%d}_normalization.json"
        patch_audit.to_csv(audit_path, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
        quality_report_path.write_text(json.dumps({
            **normalization_report,
            "histdata": histdata_metadata,
            "patch_audit": str(audit_path.relative_to(project_root())).replace("\\", "/"),
        }, indent=2) + "\n", encoding="utf-8")
        for timeframe in normalized_timeframes:
            frame = build_timeframe(normalized_m1, timeframe, start_utc, end_utc)
            outputs[timeframe] = _write_normalized_dataset(
                frame,
                symbol=symbol,
                timeframe=timeframe,
                start=start_utc,
                end=end_utc,
                output_dir=normalized_output,
                primary_cache_fingerprint=cache_fingerprint,
                histdata_metadata=histdata_metadata,
                normalization_report=normalization_report,
                audit_path=audit_path,
                quality_report_path=quality_report_path,
            )
        outputs["normalization_report"] = quality_report_path
        outputs["patch_audit"] = audit_path
    else:
        print(f"HistData M1 not found for {symbol}; saved Dukascopy-only datasets")
    if compare_mt5 is not None:
        comparison_dir = resolved_output / "quality"
        outputs["comparison"] = compare_with_mt5(source_m1, compare_mt5, output_dir=comparison_dir)
    return outputs


def main(argv: list[str] | None = None) -> dict[str, Path]:
    parser = argparse.ArgumentParser(description="Download Dukascopy M1 candles and build UTC OHLC datasets")
    parser.add_argument("--symbol", required=True, help="FX symbol, for example AUDCAD")
    parser.add_argument("--timeframes", nargs="+", required=True, choices=SUPPORTED_TIMEFRAMES)
    parser.add_argument("--start", required=True, help="Inclusive UTC start, for example 2021-01-01")
    parser.add_argument("--end", help="Exclusive UTC end; defaults to today's 00:00 UTC")
    parser.add_argument("--side", default="BID", choices=("BID", "ASK"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--compare-mt5", help="Optional MT5 CSV used for cross-feed quality comparison")
    args = parser.parse_args(argv)
    return download_data(
        args.symbol, args.timeframes, args.start, args.end, side=args.side,
        workers=args.workers, compare_mt5=args.compare_mt5,
    )


if __name__ == "__main__":
    main()
