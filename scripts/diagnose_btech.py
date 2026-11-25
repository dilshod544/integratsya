#!/usr/bin/env python3
"""Диагностика доступа к BTech (для банк-сервера).

  cd /path/to/deploy-bank
  PYTHONPATH=api:. python3 scripts/diagnose_btech.py

Печатает только статусы, секреты не показывает.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)


def main() -> int:
    import os
    from datetime import datetime, timezone

    print("=== BTech diagnose ===")
    print("time_utc:", datetime.now(timezone.utc).isoformat())
    print("API_BASE_URL:", os.getenv("API_BASE_URL") or "(default)")
    print("AUTH_USERNAME:", "SET" if os.getenv("AUTH_USERNAME") else "MISSING")
    print("AUTH_PASSWORD:", "SET" if os.getenv("AUTH_PASSWORD") else "MISSING")
    otp = (os.getenv("AUTH_OTP_SECRET") or "").replace(" ", "").strip()
    print("AUTH_OTP_SECRET:", "SET" if otp else "MISSING", f"len={len(otp)}" if otp else "")
    print("ATM_COLLECTOR:", os.getenv("ATM_COLLECTOR", "1"))

    if otp:
        try:
            import pyotp
            from atm_monitor.auth import _trusted_unix_time

            import time as _time

            local = _time.time()
            net = _trusted_unix_time()
            print("clock_local:", int(local), "clock_network:", int(net), "skew_sec:", int(net - local))
            print("TOTP_network:", pyotp.TOTP(otp).at(int(net)))
        except Exception as e:
            print("TOTP_FAIL:", type(e).__name__, str(e)[:120])

    # 1) DNS / HTTPS
    try:
        import requests
        from atm_monitor.config import settings

        base = settings.API_BASE_URL.rstrip("/")
        r = requests.get(base + "/", timeout=15)
        print("HTTP_base:", r.status_code)
    except Exception as e:
        print("HTTP_base_FAIL:", type(e).__name__, str(e)[:200])
        print("HINT: банк-сервер не достучался до monitoring.btech.uz (сеть/прокси/DNS/firewall)")
        return 2

    # 2) Login + OTP
    try:
        from atm_monitor.auth import TokenManager

        tm = TokenManager()
        tok = tm.get_token()
        print("LOGIN_OK: token_len=", len(tok or ""))
    except Exception as e:
        msg = str(e)
        print("LOGIN_FAIL:", type(e).__name__, msg[:250])
        low = msg.lower()
        if "otp" in low or "code" in low:
            print("HINT: OTP secret неверный / часы сервера сбиты")
        elif "many" in low or "rate" in low or "cooldown" in low:
            print("HINT: BTech rate-limit — подождите 15+ минут")
        elif "password" in low or "401" in low or "400" in low:
            print("HINT: AUTH_USERNAME / AUTH_PASSWORD неверны")
        else:
            print("HINT: проверьте .env и доступ к /api/login")
        return 3

    # 3) Fetch ATM list page
    try:
        from atm_monitor.api_client import fetch_all_atms

        # only first page via internal call to avoid long run — use _get_items
        from atm_monitor import api_client

        url = settings.API_BASE_URL.rstrip("/") + settings.ATM_LIST_PATH
        batch = api_client._get_items(
            url, {**settings.QUERY_PARAMS, "offset": 0, "limit": 5}
        )
        print("ATM_LIST_OK: sample=", len(batch))
    except Exception as e:
        print("ATM_LIST_FAIL:", type(e).__name__, str(e)[:250])
        print("HINT: логин прошёл, но список ATM не отдаётся (права/путь API)")
        return 4

    # 4) One encashment call if we have an id
    try:
        atm_id = None
        if batch and isinstance(batch[0], dict):
            atm_id = batch[0].get("id") or batch[0].get("atmId")
        if atm_id:
            from atm_monitor.api_client import fetch_encashments

            rows = fetch_encashments(int(atm_id))
            print("ENCASH_OK: atm_id=", atm_id, "cycles=", len(rows))
        else:
            print("ENCASH_SKIP: no atm id in sample")
    except Exception as e:
        print("ENCASH_FAIL:", type(e).__name__, str(e)[:250])
        print("HINT: список ATM есть, инкассации по ATM падают — путь/права encashment")
        return 5

    print("ALL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
