"""Research API: strategy detail, compare, split, walk-forward, experiments."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_strategy_detail_and_404(client: TestClient) -> None:
    r = client.get("/api/v1/strategies/rsi_mean_reversion")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_id"] == "rsi_mean_reversion"
    assert body["version"] == "1.0.0"
    assert body["warmup_bars"] == 15
    assert "rsi" in body["required_indicators"]
    assert {p["name"] for p in body["parameter_schema"]} == {
        "rsi_period",
        "oversold",
        "exit_threshold",
    }
    assert client.get("/api/v1/strategies/ghost").status_code == 404


def test_compare_endpoint(client: TestClient) -> None:
    r = client.post(
        "/api/v1/research/compare",
        json={
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "max_candles": 400,
            "strategies": [
                {"strategy_id": "ema_crossover", "params": {}},
                {"strategy_id": "rsi_mean_reversion", "params": {}},
                {"strategy_id": "donchian_breakout", "params": {}},
            ],
            "sort_by": "total_return_pct",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["entries"]) == 3
    assert body["data_source"] == "synthetic_fixture"
    assert body["fingerprint"]
    assert "experiment_id" in body
    returns = [e["metrics"]["total_return_pct"] for e in body["entries"]]
    assert returns == sorted(returns, reverse=True)
    # Experiment persisted and retrievable.
    exp = client.get(f"/api/v1/research/experiments/{body['experiment_id']}")
    assert exp.status_code == 200
    assert exp.json()["methodology"] == "compare"


def test_compare_validation(client: TestClient) -> None:
    unknown = client.post(
        "/api/v1/research/compare",
        json={"strategies": [{"strategy_id": "ghost", "params": {}}]},
    )
    assert unknown.status_code == 422
    bad_params = client.post(
        "/api/v1/research/compare",
        json={
            "strategies": [
                {"strategy_id": "ema_crossover", "params": {"fast_period": 60, "slow_period": 20}}
            ]
        },
    )
    assert bad_params.status_code == 422
    too_many = client.post(
        "/api/v1/research/compare",
        json={"strategies": [{"strategy_id": "ema_crossover", "params": {}}] * 6},
    )
    assert too_many.status_code == 422
    bad_sort = client.post(
        "/api/v1/research/compare",
        json={"strategies": [{"strategy_id": "ema_crossover", "params": {}}], "sort_by": "vibes"},
    )
    assert bad_sort.status_code == 422


def test_split_endpoint(client: TestClient) -> None:
    r = client.post(
        "/api/v1/research/split-test",
        json={"strategy_id": "donchian_breakout", "max_candles": 500, "train_ratio": 0.7},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["split_index"] == 350
    assert body["in_sample"]["num_bars"] == 350
    assert body["out_sample"]["num_bars"] == 150
    assert body["capital_reset_between_windows"] is True
    assert "experiment_id" in body


def test_split_validation(client: TestClient) -> None:
    assert client.post("/api/v1/research/split-test", json={"train_ratio": 0.95}).status_code == 422
    unknown = client.post("/api/v1/research/split-test", json={"strategy_id": "ghost"})
    assert unknown.status_code == 422


def test_walk_forward_endpoint(client: TestClient) -> None:
    r = client.post(
        "/api/v1/research/walk-forward",
        json={
            "strategy_id": "ema_crossover",
            "param_grid": {"fast_period": [10, 20], "slow_period": [40, 60]},
            "train_window": 500,
            "test_window": 100,
            "step": 100,
            "max_candles": 900,
            "min_trades": 0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["folds"]) == 4
    assert body["aggregate_metrics"] is not None
    assert body["aggregate_equity"]
    assert "experiment_id" in body


def test_walk_forward_validation(client: TestClient) -> None:
    overlap = client.post(
        "/api/v1/research/walk-forward",
        json={
            "param_grid": {"fast_period": [10, 20]},
            "train_window": 500,
            "test_window": 200,
            "step": 100,
            "max_candles": 900,
        },
    )
    assert overlap.status_code == 422
    too_big = client.post(
        "/api/v1/research/walk-forward",
        json={
            "param_grid": {f"p{i}": [1, 2] for i in range(4)} | {"q": list(range(8))},
            "train_window": 500,
            "test_window": 100,
            "step": 100,
            "max_candles": 900,
        },
    )
    # 5 keys exceeds max_length=4 -> 422 from schema validation.
    assert too_big.status_code == 422
    wide = client.post(
        "/api/v1/research/walk-forward",
        json={
            "param_grid": {"a": [1, 2], "b": list(range(8)), "c": list(range(8))},
            "train_window": 500,
            "test_window": 100,
            "step": 100,
            "max_candles": 900,
        },
    )
    # 2*8*8 = 128 combos > 64 -> 422 from search-space guard.
    assert wide.status_code == 422
    bad_objective = client.post(
        "/api/v1/research/walk-forward",
        json={
            "param_grid": {"fast_period": [10, 20]},
            "objective": "vibes",
            "train_window": 500,
            "test_window": 100,
            "step": 100,
            "max_candles": 900,
        },
    )
    assert bad_objective.status_code == 422


def test_experiments_listing_and_404(client: TestClient) -> None:
    r = client.post(
        "/api/v1/research/compare",
        json={"strategies": [{"strategy_id": "ema_crossover", "params": {}}], "max_candles": 200},
    )
    assert r.status_code == 200
    listed = client.get("/api/v1/research/experiments")
    assert listed.status_code == 200
    assert any(e["experiment_id"] == r.json()["experiment_id"] for e in listed.json())
    assert client.get("/api/v1/research/experiments/missing").status_code == 404


def test_legacy_backtest_still_supports_all_strategies(client: TestClient) -> None:
    for strategy_id in ("ema_crossover", "rsi_mean_reversion", "donchian_breakout"):
        r = client.post(
            "/api/v1/backtests",
            json={"strategy": strategy_id, "max_candles": 300},
        )
        assert r.status_code == 200, (strategy_id, r.text)
    # Legacy EMA top-level fields still accepted for ema_crossover.
    r = client.post(
        "/api/v1/backtests",
        json={
            "strategy": "ema_crossover",
            "fast_period": 10,
            "slow_period": 30,
            "max_candles": 300,
        },
    )
    assert r.status_code == 200
