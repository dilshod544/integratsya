"""Общие помощники тестов истории/плана."""
from __future__ import annotations


def seed_snapshot(conn, atm_id, at, total, cassettes, agent="online"):
    """Снимок + кассеты. count=None — купюр столько, чтобы сумма кассет равнялась total."""
    sid = conn.execute(
        """INSERT INTO atm_snapshots (atm_id, polled_at, agent_status, service_status, app_status,
               cdm_total_uzs, dispenser_status)
           VALUES (%s,%s,%s,'InService','InService',%s,'ONLINE') RETURNING id""",
        (atm_id, at, agent, total),
    ).fetchone()["id"]
    for i, (status, count, nominal) in enumerate(cassettes, start=1):
        if count is None:
            count = int(total // nominal // len(cassettes))
        conn.execute(
            """INSERT INTO cassette_snapshots (atm_snapshot_id, atm_id, polled_at, cassette_index,
                   cassette_type, status, count, currency, nominal)
               VALUES (%s,%s,%s,%s,'BILLCASSETTE',%s,%s,'UZS',%s)""",
            (sid, atm_id, at, i, status, count, nominal),
        )
    return sid
