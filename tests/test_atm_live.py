from api.core.atm_live import match_live_to_rows
from atm_monitor.ingest import _cassette_nominal


def test_exact_tid_match_beats_nearby_neighbor():
    rows = [
        {"terminal_id": "A1", "lat": 41.0, "lon": 69.0, "balance": None},
        {"terminal_id": "A2", "lat": 41.0002, "lon": 69.0, "balance": None},
    ]
    live = {
        "A1": {"lat": 41.0, "lon": 69.0, "balance": 10, "btech_terminal_id": None, "serial": None},
        "B9": {"lat": 41.0001, "lon": 69.0, "balance": 99, "btech_terminal_id": None, "serial": None},
    }
    match_live_to_rows(rows, live, {})
    assert rows[0]["live_tid"] == "A1"
    assert rows[0]["balance"] == 10
    assert rows[1]["live_tid"] == "B9"
    assert rows[1]["live_match"] == "location"


def test_location_match_is_one_to_one():
    rows = [
        {"terminal_id": "X1", "lat": 41.0, "lon": 69.0},
        {"terminal_id": "X2", "lat": 41.00005, "lon": 69.0},
    ]
    live = {
        "L1": {"lat": 41.0, "lon": 69.0, "balance": 50_000_000, "btech_terminal_id": None, "serial": None},
    }
    match_live_to_rows(rows, live, {})
    matched = [r for r in rows if r.get("live")]
    assert len(matched) == 1
    assert matched[0]["live_tid"] == "L1"
    assert sum(1 for r in rows if not r.get("live")) == 1


def test_btech_terminal_id_and_serial_keys():
    rows = [
        {"terminal_id": "34884", "atm_number": "99", "serial": "SN-1", "lat": 0, "lon": 0},
        {"terminal_id": "other", "atm_number": None, "serial": "SN-2", "lat": 0, "lon": 0},
    ]
    live = {
        "TID-A": {"lat": 1, "lon": 1, "balance": 1, "btech_terminal_id": "34884", "serial": None},
        "TID-B": {"lat": 2, "lon": 2, "balance": 2, "btech_terminal_id": None, "serial": "SN-2"},
    }
    match_live_to_rows(rows, live, {})
    assert rows[0]["live_tid"] == "TID-A"
    assert rows[1]["live_tid"] == "TID-B"


def test_cassette_nominal_accepts_value_list_and_values():
    assert _cassette_nominal({"values": 100000}) == 100000
    assert _cassette_nominal({"value": 50000}) == 50000
    assert _cassette_nominal({"nominal": 200000}) == 200000
    assert _cassette_nominal({"values": [10000]}) == 10000
    assert _cassette_nominal({"values": None, "value": 2000}) == 2000
