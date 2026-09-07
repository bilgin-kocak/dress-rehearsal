from rehearsal.dashboard.app import _latest_segment


def test_latest_segment_keeps_only_current_replay_loop():
    rows = [{"ts": 10, "equity": "1"}, {"ts": 20, "equity": "2"}, {"ts": 30, "equity": "3"},
            {"ts": 10, "equity": "4"}, {"ts": 25, "equity": "5"}]          # clock rewound at the 4th point
    assert [r["equity"] for r in _latest_segment(rows)] == ["4", "5"]
    assert _latest_segment([]) == []
    assert [r["ts"] for r in _latest_segment([{"ts": 5, "equity": "1"}, {"ts": 5, "equity": "2"}])] == [5, 5]
