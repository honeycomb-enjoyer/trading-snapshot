"""Load, validate, split, and identify research market-data datasets."""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping

import pandas as pd

from data.data_split_config import DATA_CONFIG, DATE_SPLITS, RATIO_SPLITS, SPLIT_MODE
from data.manifest import build_manifest, load_manifest, verify_manifest
from data.schema import DataContractError, DatasetContract, normalize_and_validate, parse_utc, resolve_project_path


class DataManager:
    def __init__(self, data_config: Mapping[str, object] | None = None, *, split_mode: str | None = None,
                 date_splits: Mapping[str, object] | None = None,
                 ratio_splits: Mapping[str, float] | None = None):
        self.data_config = deepcopy(dict(data_config or DATA_CONFIG))
        self.contract = DatasetContract.from_mapping(self.data_config)
        self.split_mode = split_mode or SPLIT_MODE
        self.date_splits = dict(date_splits or DATE_SPLITS)
        self.ratio_splits = dict(ratio_splits or RATIO_SPLITS)
        self.data_path = resolve_project_path(str(self.data_config["path"]))
        self.df: pd.DataFrame | None = None
        self.manifest: dict | None = None

    def load(self) -> pd.DataFrame:
        if not self.data_path.is_file():
            raise FileNotFoundError(f"Dataset not found: {self.data_path}")
        raw = pd.read_csv(self.data_path)
        self.df = normalize_and_validate(raw, self.contract)
        self.manifest = load_manifest(self.data_path)
        if self.manifest is not None:
            try:
                verify_manifest(self.data_path, self.manifest)
            except ValueError as exc:
                raise DataContractError(str(exc)) from exc
        else:
            # Keep one in-memory identity for this run; a legacy CSV still does
            # not become a fully reproducible source artifact without a sidecar.
            self.manifest = build_manifest(self.data_path, self.df, self.contract)
        self.df.attrs["dataset_reference"] = self._build_dataset_reference(self.df)
        return self.df

    def prepare(self) -> pd.DataFrame:
        """Load, validate splits, and add canonical calendar features."""
        self.load()
        self.validate()
        return self.add_features()

    def validate(self) -> None:
        if self.df is None:
            raise RuntimeError("Load data first")
        self._validate_splits()

    def _validate_splits(self) -> None:
        if self.split_mode == "manual":
            required = ("train", "holdout")
            intervals = []
            for name in required:
                try:
                    start = parse_utc(self.date_splits[f"{name}_start"], field_name=f"{name}_start")
                    end = parse_utc(self.date_splits[f"{name}_end"], field_name=f"{name}_end")
                except KeyError as exc:
                    raise DataContractError(f"DATE_SPLITS missing {exc.args[0]}") from exc
                if start >= end:
                    raise DataContractError(f"{name} split must have start before end")
                intervals.append((start, end, name))
            intervals.sort()
            for (_, previous_end, previous_name), (current_start, _, current_name) in zip(intervals, intervals[1:]):
                if current_start < previous_end:
                    raise DataContractError(f"Split overlap: {previous_name} and {current_name}")
        elif self.split_mode == "ratio":
            total = sum(self.ratio_splits.get(name, 0) for name in ("train", "holdout"))
            if abs(total - 1.0) > 1e-9 or any(self.ratio_splits.get(name, 0) <= 0 for name in ("train", "holdout")):
                raise DataContractError("Ratio splits must be positive and sum to 1.0")
        else:
            raise RuntimeError(f"Unsupported split mode: {self.split_mode}")

    def show_gaps(self) -> None:
        if self.df is None:
            raise RuntimeError("Load data first")
        print("Gap analysis: contract validation passed")

    def add_features(self) -> pd.DataFrame:
        if self.df is None:
            raise RuntimeError("Load data first")
        df = self.df.copy()
        df["year"] = df["timestamp"].dt.year
        df["month"] = df["timestamp"].dt.month
        df["day"] = df["timestamp"].dt.day
        df["hour"] = df["timestamp"].dt.hour
        df["weekday"] = df["timestamp"].dt.weekday
        self.df = df
        return df

    def _split_by_ratio(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        if self.df is None:
            raise RuntimeError("Load data first")
        self._validate_splits()
        total = len(self.df)
        train_end = int(total * self.ratio_splits["train"])
        return (
            self.df.iloc[:train_end].copy(),
            self.df.iloc[train_end:].copy(),
        )

    def _manual_split(self, name: str) -> pd.DataFrame:
        if self.df is None:
            raise RuntimeError("Load data first")
        self._validate_splits()
        start = parse_utc(self.date_splits[f"{name}_start"], field_name=f"{name}_start")
        end = parse_utc(self.date_splits[f"{name}_end"], field_name=f"{name}_end")
        return self.df[(self.df["timestamp"] >= start) & (self.df["timestamp"] < end)].copy()

    def get_train(self) -> pd.DataFrame:
        frame = self._manual_split("train") if self.split_mode == "manual" else self._split_by_ratio()[0]
        return self._attach_dataset_reference(frame, "train")

    def get_holdout(self) -> pd.DataFrame:
        frame = self._manual_split("holdout") if self.split_mode == "manual" else self._split_by_ratio()[1]
        return self._attach_dataset_reference(frame, "holdout")

    def select(self, split: str) -> pd.DataFrame:
        """Select a configured split or the complete normalized dataset."""
        if self.df is None:
            raise RuntimeError("Load data first")
        if split == "full":
            frame = self.df.copy()
            frame.attrs["dataset_reference"] = self._build_dataset_reference(frame, "full")
            return frame
        if split not in {"train", "holdout"}:
            raise ValueError(f"Unknown dataset mode: {split}")
        return getattr(self, f"get_{split}")()

    def _build_dataset_reference(self, frame: pd.DataFrame, split: str | None = None) -> dict:
        if self.df is None:
            raise RuntimeError("Load data first")
        manifest = self.manifest or build_manifest(self.data_path, self.df, self.contract)
        reference = {"manifest": manifest}
        if split is not None:
            reference["split"] = {
                "name": split,
                "interval": "[start, end)",
                "start": frame["timestamp"].iloc[0].isoformat() if not frame.empty else None,
                "end_exclusive": (
                    frame["timestamp"].iloc[-1] + pd.Timedelta(self.contract.interval)
                ).isoformat() if not frame.empty else None,
                "row_count": len(frame),
            }
        return reference

    def _attach_dataset_reference(self, frame: pd.DataFrame, split: str) -> pd.DataFrame:
        frame.attrs["dataset_reference"] = self._build_dataset_reference(frame, split)
        return frame

    def dataset_reference(self, split: str | None = None) -> dict:
        """Return the exact data identity that a backtest/report must record."""
        if self.df is None:
            raise RuntimeError("Load data first")
        if split is None:
            return deepcopy(self._build_dataset_reference(self.df))
        if split not in {"train", "holdout"}:
            raise ValueError(f"Unknown split: {split}")
        return deepcopy(getattr(self, f"get_{split}")().attrs["dataset_reference"])

    def summary(self) -> None:
        train, holdout = self.get_train(), self.get_holdout()
        print("========== DATASET SPLIT ==========")
        print(f"Mode:    {self.split_mode}")
        print(f"Train:   {len(train)} bars")
        print(f"Holdout: {len(holdout)} bars")
        print("Intervals use [start, end) in UTC")
        print("===================================")
