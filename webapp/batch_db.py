"""振込バッチ永続化用 SQLite ヘルパー。

バッチ = 振込日ごとのまとまり。各バッチに複数の PDF アイテムが紐付く。
PDF 自体は BLOB として保存し、再表示できるようにする。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "batches.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    transfer_date   TEXT,
    transfer_type   TEXT DEFAULT '総合振込',
    consignor_id    TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batch_items (
    item_id            TEXT PRIMARY KEY,
    batch_id           TEXT NOT NULL,
    order_index        INTEGER DEFAULT 0,
    filename           TEXT,
    pdf_blob           BLOB,
    page_count         INTEGER DEFAULT 1,
    payee_name         TEXT,
    bank_name          TEXT,
    bank_code          TEXT,
    branch_name        TEXT,
    branch_code        TEXT,
    account_type       TEXT DEFAULT '普通',
    account_number     TEXT,
    amount             INTEGER DEFAULT 0,
    matched_payee_key  TEXT,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id, order_index);
CREATE INDEX IF NOT EXISTS idx_batches_updated ON batches(updated_at DESC);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # 既存DBに consignor_id カラムが無ければ追加（マイグレーション）
    try:
        conn.execute("ALTER TABLE batches ADD COLUMN consignor_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 既に存在
    return conn


def _touch_batch(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE batches SET updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
        (batch_id,),
    )


def auto_batch_name(transfer_date: str | None) -> str:
    """振込日 (MMDD) からバッチ名を生成する。"""
    today = datetime.now()
    year = today.year
    if transfer_date and len(transfer_date) == 4 and transfer_date.isdigit():
        mm = int(transfer_date[:2])
        dd = int(transfer_date[2:])
        try:
            d = datetime(year=year, month=mm, day=dd)
            return f"{d.year}年{d.month:02d}月{d.day:02d}日振込分"
        except ValueError:
            pass
    return f"{today.year}年{today.month:02d}月{today.day:02d}日振込分（未指定）"


def create_batch(
    conn: sqlite3.Connection,
    transfer_date: str | None,
    transfer_type: str = "総合振込",
    name: str | None = None,
    consignor_id: str | None = None,
) -> str:
    """新しいバッチを作成し、batch_id を返す。同名があれば自動で連番を付ける。"""
    base = name or auto_batch_name(transfer_date)
    final_name = base
    n = 2
    while True:
        cur = conn.execute("SELECT 1 FROM batches WHERE name = ?", (final_name,))
        if cur.fetchone() is None:
            break
        final_name = f"{base} ({n})"
        n += 1

    batch_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO batches (batch_id, name, transfer_date, transfer_type, consignor_id)
           VALUES (?, ?, ?, ?, ?)""",
        (batch_id, final_name, transfer_date or "", transfer_type, consignor_id),
    )
    conn.commit()
    return batch_id


def rename_batch(conn: sqlite3.Connection, batch_id: str, new_name: str) -> None:
    if not new_name.strip():
        return
    conn.execute(
        "UPDATE batches SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
        (new_name.strip(), batch_id),
    )
    conn.commit()


def update_batch_meta(
    conn: sqlite3.Connection,
    batch_id: str,
    transfer_date: str | None = None,
    transfer_type: str | None = None,
    consignor_id: str | None = None,
) -> None:
    sets, vals = [], []
    if transfer_date is not None:
        sets.append("transfer_date = ?")
        vals.append(transfer_date)
    if transfer_type is not None:
        sets.append("transfer_type = ?")
        vals.append(transfer_type)
    if consignor_id is not None:
        sets.append("consignor_id = ?")
        vals.append(consignor_id)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(batch_id)
    conn.execute(f"UPDATE batches SET {', '.join(sets)} WHERE batch_id = ?", vals)
    conn.commit()


def delete_batch(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute("DELETE FROM batches WHERE batch_id = ?", (batch_id,))
    conn.commit()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """バッチ一覧（明細件数・合計金額付き、更新日時降順）。"""
    cur = conn.execute(
        """
        SELECT
            b.batch_id, b.name, b.transfer_date, b.transfer_type, b.consignor_id,
            b.created_at, b.updated_at,
            COUNT(i.item_id) AS item_count,
            COALESCE(SUM(i.amount), 0) AS total_amount
        FROM batches b
        LEFT JOIN batch_items i ON i.batch_id = b.batch_id
        GROUP BY b.batch_id
        ORDER BY b.updated_at DESC
        """
    )
    return cur.fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
    return cur.fetchone()


def latest_batch_id(conn: sqlite3.Connection) -> str | None:
    cur = conn.execute("SELECT batch_id FROM batches ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    return row["batch_id"] if row else None


def list_items(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        """SELECT * FROM batch_items WHERE batch_id = ? ORDER BY order_index, item_id""",
        (batch_id,),
    )
    return cur.fetchall()


def upsert_item(conn: sqlite3.Connection, batch_id: str, item: dict) -> None:
    """1件 upsert。新規なら item_id を発行。"""
    item_id = item.get("item_id") or uuid.uuid4().hex
    fields = [
        "item_id", "batch_id", "order_index", "filename", "pdf_blob", "page_count",
        "payee_name", "bank_name", "bank_code", "branch_name", "branch_code",
        "account_type", "account_number", "amount", "matched_payee_key",
    ]
    values = (
        item_id,
        batch_id,
        int(item.get("order_index") or 0),
        item.get("filename") or "",
        item.get("pdf_blob"),  # bytes or None
        int(item.get("page_count") or 1),
        item.get("payee_name") or "",
        item.get("bank_name") or "",
        item.get("bank_code") or "",
        item.get("branch_name") or "",
        item.get("branch_code") or "",
        item.get("account_type") or "普通",
        item.get("account_number") or "",
        int(item.get("amount") or 0),
        item.get("matched_payee_key"),
    )
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{f}=excluded.{f}" for f in fields if f != "item_id")
    conn.execute(
        f"""
        INSERT INTO batch_items ({", ".join(fields)})
        VALUES ({placeholders})
        ON CONFLICT(item_id) DO UPDATE SET {updates}
        """,
        values,
    )
    _touch_batch(conn, batch_id)


def replace_items(conn: sqlite3.Connection, batch_id: str, items: list[dict]) -> None:
    """バッチの items を一度全削除して入れ直す（並び順含む完全反映）。"""
    conn.execute("DELETE FROM batch_items WHERE batch_id = ?", (batch_id,))
    for i, item in enumerate(items):
        item_with_order = {**item, "order_index": i, "batch_id": batch_id}
        upsert_item(conn, batch_id, item_with_order)
    _touch_batch(conn, batch_id)
    conn.commit()


def delete_item(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute("DELETE FROM batch_items WHERE item_id = ?", (item_id,))
    conn.commit()
