from rehearsal.dashboard.app import _latest_segment


def test_latest_segment_keeps_only_current_replay_loop():
    rows = [{"ts": 10, "equity": "1"}, {"ts": 20, "equity": "2"}, {"ts": 30, "equity": "3"},
            {"ts": 10, "equity": "4"}, {"ts": 25, "equity": "5"}]          # clock rewound at the 4th point
    assert [r["equity"] for r in _latest_segment(rows)] == ["4", "5"]
    assert _latest_segment([]) == []
    assert [r["ts"] for r in _latest_segment([{"ts": 5, "equity": "1"}, {"ts": 5, "equity": "2"}])] == [5, 5]


def test_replay_klines_serve_requested_interval_and_hide_future_bars(engine):
    import json
    from pathlib import Path
    snap = json.loads(Path("fixtures/replay/demo/snapshots.json").read_text())
    engine.feed._replay_snapshots = snap
    engine.feed.mode = "replay"
    now = engine.clock.now_ms()
    bars = engine.feed.passthrough("spot_klines", symbol="BTCUSDT", interval="15m", limit=20)
    assert len(bars) <= 20 and all(int(b[0]) <= now for b in bars)
    hourly = engine.feed.passthrough("spot_klines", symbol="BTCUSDT", interval="1h", limit=5)
    assert len(hourly) <= 5
    assert engine.feed.passthrough("spot_klines", symbol="BTCUSDT", interval="15m", limit=20) == bars  # deterministic
