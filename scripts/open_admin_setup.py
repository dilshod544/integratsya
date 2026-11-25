#!/usr/bin/env python3
"""Открыть одноразовую регистрацию admin на странице входа.

На банк-сервере (если забыли пароль):
  cd /path/to/deploy-bank
  PYTHONPATH=api python3 scripts/open_admin_setup.py
  # перезапуск сервера не обязателен — достаточно обновить login.html
  # откройте /dashboard/login.html → форма «Создать и войти»
  # после успеха форма закроется сама
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
    from core.auth import _mark_setup_pending, setup_needed
    from core.db import init_db

    init_db()
    _mark_setup_pending()
    print("OK: одноразовая регистрация открыта.")
    print("Откройте: /dashboard/login.html")
    print("Задайте логин/пароль admin один раз.")
    print("setup_needed =", setup_needed())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
