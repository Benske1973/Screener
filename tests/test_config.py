from __future__ import annotations

import textwrap

import pytest

from local_high.config import ConfigError, load_config


def _write(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_load_minimal_config_uses_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "timeframe: 4hour\n"))
    assert cfg.timeframe == "4hour"
    assert cfg.lookbacks == (30, 90)
    assert cfg.interval_seconds == 14_400
    assert cfg.max_lookback == 90


def test_repo_config_yaml_is_valid():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.timeframe == "1hour"
    assert cfg.max_breakout_distance_pct == 5.0


def test_negative_max_breakout_distance_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="max_breakout_distance_pct"):
        load_config(_write(tmp_path, "max_breakout_distance_pct: -1\n"))


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(_write(tmp_path, "wobble: 3\n"))


def test_bad_timeframe_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not a KuCoin candle type"):
        load_config(_write(tmp_path, "timeframe: 7min\n"))


def test_candle_history_must_cover_lookback(tmp_path):
    with pytest.raises(ConfigError, match="candle_history"):
        load_config(_write(tmp_path, "lookbacks: [200]\ncandle_history: 50\n"))


def test_alert_on_validates_kinds(tmp_path):
    with pytest.raises(ConfigError, match="unknown event kinds"):
        load_config(_write(tmp_path, "alert_on: [BREAKOUT, MOON]\n"))


def test_score_weights_partial_override(tmp_path):
    cfg = load_config(_write(tmp_path, "score_weights:\n  rvol: 2.0\n"))
    assert cfg.score_weights.rvol == 2.0
    assert cfg.score_weights.trend == 0.5


def test_symbol_lists_are_upper_cased_tuples(tmp_path):
    cfg = load_config(_write(tmp_path, "symbol_allowlist: [btc-usdt]\n"))
    assert cfg.symbol_allowlist == ("BTC-USDT",)
