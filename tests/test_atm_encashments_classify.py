from api.core.atm_encashments import classify

UZS = lambda t: {"UZS": {"total": t, "values": {}}}


def test_replace_and_load_empty():
    assert classify(UZS(50e6), UZS(300e6), True) == "replace"
    assert classify(UZS(0), UZS(300e6), True) == "load_empty"


def test_unload_and_unknown_before():
    assert classify(UZS(50e6), UZS(0), True) == "unload"
    assert classify({}, UZS(300e6), False) == "load"


def test_usd_unknown_is_not_called_empty():
    # «было» посчитано только по UZS (= 0), а в банкомате были доллары — пустым не называем
    assert classify(UZS(0), {**UZS(300e6), "USD": {"total": 20000, "values": {}}}, True, {"USD"}) == "load"
    assert classify(UZS(10e6), UZS(300e6), True, {"USD"}) == "replace"
