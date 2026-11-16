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


def test_btech_location_overrides_registry_coords_and_address():
    """Карта должна показывать место с BTech, даже если в Excel другой адрес."""
    rows = [
        {"terminal_id": "T1", "lat": 41.0, "lon": 69.0, "address": "Старый адрес из Excel"},
    ]
    live = {
        "T1": {
            "lat": 41.3, "lon": 69.5, "address": "Новый адрес BTech", "place": "ТЦ",
            "balance": 1, "btech_terminal_id": None, "serial": None,
        },
    }
    match_live_to_rows(rows, live, {})
    assert rows[0]["live"] is True
    assert rows[0]["lat"] == 41.3 and rows[0]["lon"] == 69.5
    assert rows[0]["location_source"] == "btech"
    assert rows[0]["registry_lat"] == 41.0 and rows[0]["registry_lon"] == 69.0
    assert rows[0]["address"] == "Новый адрес BTech"
    assert rows[0]["registry_address"] == "Старый адрес из Excel"
    assert rows[0]["place"] == "ТЦ"


def test_invalid_btech_gps_keeps_registry_coords():
    """BTech 0,0 не должен сдвигать точку в океан — остаётся реестр."""
    rows = [
        {"terminal_id": "T2", "lat": 41.31, "lon": 69.28, "address": "Excel адрес"},
    ]
    live = {
        "T2": {
            "lat": 0.0, "lon": 0.0, "address": "BTech адрес", "place": None,
            "balance": 1, "btech_terminal_id": None, "serial": None,
        },
    }
    match_live_to_rows(rows, live, {})
    assert rows[0]["live"] is True
    assert rows[0]["lat"] == 41.31 and rows[0]["lon"] == 69.28
    assert rows[0]["location_source"] == "registry"
    assert rows[0]["address"] == "BTech адрес"
    assert rows[0]["registry_address"] == "Excel адрес"


def test_valid_gps_rejects_junk():
    from api.core.atm_live import _valid_gps

    assert _valid_gps(0, 0) is None
    assert _valid_gps("nan", "nan") is None
    assert _valid_gps(41.3, 69.2) == (41.3, 69.2)
    assert _valid_gps("41.3", "69.2") == (41.3, 69.2)
    assert _valid_gps(50.0, 69.0) is None


def test_fill_live_marks_usd_cassettes():
    from api.core.atm_live import _fill_live

    rows = [{"terminal_id": "T1", "lat": 41.0, "lon": 69.0}]
    live = {
        "lat": 41.1, "lon": 69.1, "balance": 10, "btech_atm_id": 1,
        "address": None, "place": None,
    }
    cass = {
        "T1": [
            {"index": 1, "type": "BILLCASSETTE", "status": "OK", "count": 10, "currency": "UZS", "nominal": 100000, "balance": 1_000_000},
            {"index": 2, "type": "BILLCASSETTE", "status": "OK", "count": 50, "currency": "USD", "nominal": 100, "balance": 5000},
        ]
    }
    _fill_live(rows[0], "T1", "terminal_id", live, cass)
    assert rows[0]["has_usd"] is True
    assert rows[0]["usd_balance"] == 5000.0
    assert len(rows[0]["cassettes"]) == 2
