#!/usr/bin/env python3
"""Принудительный сброс пароля admin в ТЕКУЩЕЙ базе (та, что в .env / DATABASE_URL).

На банк-сервере:
  cd /path/to/deploy-bank
  PYTHONPATH=api python3 scripts/reset_admin.py 'YangiParol123'
  # затем перезапустите сервер и войдите: admin / YangiParol123

Не зависит от ADMIN_PASSWORD_SYNC. Пароль в git не пишется.
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
    from core import auth
    from core.config import ADMIN_USERNAME, DATABASE_URL
    from core.db import init_db
    from core.security import validate_password_strength

    init_db()

    username = (ADMIN_USERNAME or "admin").strip() or "admin"
    if len(sys.argv) >= 2:
        password = sys.argv[1]
    else:
        password = auth.generate_password()

    err = validate_password_strength(password)
    if err:
        print("ERROR:", err)
        print("Пример: PYTHONPATH=api python3 scripts/reset_admin.py 'AdminBank99'")
        return 1

    users = {u["username"]: u for u in auth.list_users()}
    print("DB:", DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL)
    print("users:", ", ".join(users) or "(пусто)")

    if username in users:
        auth.update_user(users[username]["id"], new_password=password, is_active=True)
        # update_user sets must_change=0 when new_password set
    else:
        auth.create_user(
            username,
            password=password,
            is_admin=True,
            must_change=False,
        )

    auth.write_admin_credentials(password)
    ok = auth.authenticate(username, password)
    print("login:", username)
    print("password:", password)
    print("verify:", "OK" if ok else "FAIL")
    if not ok:
        return 2
    print("Готово. Войдите в /dashboard/login.html этим логином/паролем.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
