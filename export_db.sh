#!/bin/bash
# Bazani boshqa kompyuterga ko'chirish uchun bitta faylga saqlaydi.
# Faylni Windows'dagi loyiha papkasiga (start_new_pc.bat yoniga) tashlang —
# start_new_pc.bat uni keyingi ishga tushirishda o'zi tiklaydi.
#
#   ./export_db.sh                    -> ~/Downloads/bank_intelligence.dump
#   ./export_db.sh /path/to/file.dump
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
DATABASE_URL="${DATABASE_URL:-postgresql://localhost:5432/bank_intelligence}"
OUT="${1:-$HOME/Downloads/bank_intelligence.dump}"

PG_DUMP="$(command -v pg_dump || ls /opt/homebrew/opt/postgresql*/bin/pg_dump /usr/local/opt/postgresql*/bin/pg_dump 2>/dev/null | head -1)"
if [ -z "$PG_DUMP" ]; then
  echo "pg_dump topilmadi (PostgreSQL o'rnatilganini tekshiring)." >&2
  exit 1
fi

"$PG_DUMP" --format=custom --no-owner --no-privileges -d "$DATABASE_URL" -f "$OUT"
echo "Saqlandi: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Faylda xodimlarning shaxsiy ma'lumotlari bor — uni faqat ishonchli yo'l bilan ko'chiring."
