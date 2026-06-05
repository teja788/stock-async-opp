"""Tiny config loader shared by every module.

Why a dedicated loader (instead of reading YAML inline everywhere):
- One place computes the project root, so Windows paths stay consistent.
- Configs are cached after first read, so we don't re-parse YAML per call.
- Every module says `from scanner.config import load_settings` and gets the
  same view of the user's editable config files.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Project root = the folder that contains the `config/` directory.
# __file__ = .../scanner/config.py  ->  parents[1] = project root.
ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=None)
def load_settings() -> dict[str, Any]:
    """settings.yaml — global knobs (lookback window, rate limits, paths)."""
    return _load_yaml("settings.yaml")


@lru_cache(maxsize=None)
def load_sources() -> dict[str, Any]:
    """sources.yaml — news RSS feeds + exchange endpoints."""
    return _load_yaml("sources.yaml")


@lru_cache(maxsize=None)
def load_investors() -> dict[str, Any]:
    """investors.yaml — marquee investor watchlist + matching rule."""
    return _load_yaml("investors.yaml")


@lru_cache(maxsize=None)
def load_noise_filters() -> dict[str, Any]:
    """noise_filters.yaml — routine filing types to drop / down-rank."""
    return _load_yaml("noise_filters.yaml")


def resolve_path(relative: str) -> Path:
    """Resolve a config-relative path (e.g. 'runtime/context_pack.md') to absolute."""
    return (ROOT / relative).resolve()
