"""API validation, error handling, and end-to-end backtest flow (fixture-backed)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_strategies_lists_baseline(client: TestClient) -> None:
    r = client.get("/api/v1/strategies")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "ema_crossover" in names


def test_market_candles_from_fixture(client: TestClient) -> None:
    r = client.get("/api/v1/market/candles", params={"limit": 10})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 10
    assert rows[0]["ts"] < rows[-1]["ts"]
    assert r.headers["x-driftline-data-source"] == "synthetic_fixture"


def test_market_candles_limit_validation(client: TestClient) -> None:
    r = client.get("/api/v1/market/candles", params={"limit": 5000})
    assert r.status_code == 422


def test_market_indicators_warmup_and_values(client: TestClient) -> None:
    r = client.get("/api/v1/market/indicators", params={"limit": 100})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 100
    assert rows[-1]["slow_ema"] is not None
    # Early points within a short window may still be warming up.
    r2 = client.get("/api/v1/market/indicators", params={"limit": 60})
    assert r2.status_code == 200


def test_indicators_reject_bad_periods(client: TestClient) -> None:
    r = client.get("/api/v1/market/indicators", params={"fast_period": 50, "slow_period": 20})
    assert r.status_code == 422


def test_backtest_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/api/v1/backtests",
        json={"symbol": "BTC/USDT", "timeframe": "1h", "max_candles": 300},
    )
    assert created.status_code == 200, created.text
    summary = created.json()
    assert summary["num_candles"] == 300
    assert summary["data_source"] == "synthetic_fixture"
    assert "total_return_pct" in summary["metrics"]

    bid = summary["id"]
    got = client.get(f"/api/v1/backtests/{bid}")
    assert got.status_code == 200
    trades = client.get(f"/api/v1/backtests/{bid}/trades")
    assert trades.status_code == 200
    assert isinstance(trades.json(), list)
    equity = client.get(f"/api/v1/backtests/{bid}/equity")
    assert equity.status_code == 200
    assert len(equity.json()) == 300


def test_backtest_unknown_strategy_rejected(client: TestClient) -> None:
    r = client.post("/api/v1/backtests", json={"strategy": "nope"})
    assert r.status_code == 422


def test_backtest_missing_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/backtests/doesnotexist").status_code == 404


def test_backtest_param_validation(client: TestClient) -> None:
    r = client.post("/api/v1/backtests", json={"initial_capital": -5})
    assert r.status_code == 422
