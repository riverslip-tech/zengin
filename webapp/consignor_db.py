"""振込元（委託者）情報を複数管理するための SQLite ヘルパー。

UI 上で複数の振込元を登録・切り替えできるようにする。
1件は必ず is_default=1 となり、変換タブで初期選択される。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "consignors.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS consignors (
    consignor_id     TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    consignor_code   TEXT NOT NULL,
    consignor_name   TEXT NOT NULL,
    bank_code        TEXT NOT NULL,
    bank_name        TEXT NOT NULL,
    branch_code      TEXT NOT NULL,
    branch_name      TEXT NOT NULL,
    account_type     TEXT NOT NULL DEFAULT '1',
    account_number   TEXT NOT NULL,
    is_default       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_consignors_default ON consignors(is_default DESC);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) AS n FROM consignors")
    return cur.fetchone()["n"]


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM consignors ORDER BY is_default DESC, updated_at DESC, name"
    )
    return cur.fetchall()


def get(conn: sqlite3.Connection, consignor_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM consignors WHERE consignor_id = ?", (consignor_id,))
    return cur.fetchone()


def get_default(conn: sqlite3.Connection) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM consignors WHERE is_default = 1 ORDER BY updated_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        # is_default 行が無い場合は1件目を返す
        cur = conn.execute("SELECT * FROM consignors ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
    return row


def insert(conn: sqlite3.Connection, fields: dict) -> str:
    """新規登録。consignor_id を発行して返す。is_default=1 のときは他を 0 にする。"""
    consignor_id = uuid.uuid4().hex
    is_default = 1 if fields.get("is_default") else 0
    if is_default:
        conn.execute("UPDATE consignors SET is_default = 0")
    conn.execute(
        """INSERT INTO consignors
           (consignor_id, name, consignor_code, consignor_name,
            bank_code, bank_name, branch_code, branch_name,
            account_type, account_number, is_default)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            consignor_id,
            fields.get("name") or "",
            fields.get("consignor_code") or "",
            fields.get("consignor_name") or "",
            fields.get("bank_code") or "",
            fields.get("bank_name") or "",
            fields.get("branch_code") or "",
            fields.get("branch_name") or "",
            fields.get("account_type") or "1",
            fields.get("account_number") or "",
            is_default,
        ),
    )
    conn.commit()
    return consignor_id


def update(conn: sqlite3.Connection, consignor_id: str, fields: dict) -> None:
    is_default = 1 if fields.get("is_default") else 0
    if is_default:
        conn.execute("UPDATE consignors SET is_default = 0")
    conn.execute(
        """UPDATE consignors SET
              name = ?,
              consignor_code = ?,
              consignor_name = ?,
              bank_code = ?,
              bank_name = ?,
              branch_code = ?,
              branch_name = ?,
              account_type = ?,
              account_number = ?,
              is_default = ?,
              updated_at = CURRENT_TIMESTAMP
           WHERE consignor_id = ?""",
        (
            fields.get("name") or "",
            fields.get("consignor_code") or "",
            fields.get("consignor_name") or "",
            fields.get("bank_code") or "",
            fields.get("bank_name") or "",
            fields.get("branch_code") or "",
            fields.get("branch_name") or "",
            fields.get("account_type") or "1",
            fields.get("account_number") or "",
            is_default,
            consignor_id,
        ),
    )
    conn.commit()


def set_default(conn: sqlite3.Connection, consignor_id: str) -> None:
    conn.execute("UPDATE consignors SET is_default = 0")
    conn.execute(
        "UPDATE consignors SET is_default = 1, updated_at = CURRENT_TIMESTAMP WHERE consignor_id = ?",
        (consignor_id,),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, consignor_id: str) -> None:
    conn.execute("DELETE FROM consignors WHERE consignor_id = ?", (consignor_id,))
    # デフォルトが消えたら、残りの1件を新しいデフォルトに
    cur = conn.execute("SELECT consignor_id FROM consignors ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE consignors SET is_default = 1 WHERE consignor_id = ?",
            (row["consignor_id"],),
        )
    conn.commit()


def seed_from_yaml_config(conn: sqlite3.Connection, yaml_cfg: dict) -> bool:
    """テーブルが空のとき、config.yaml の consignor/source を初期データとして1件投入する。

    投入したら True、テーブルが既に空でなければ False を返す。
    """
    if count(conn) > 0:
        return False
    consignor = (yaml_cfg.get("consignor") or {}) if isinstance(yaml_cfg, dict) else {}
    source = (yaml_cfg.get("source") or {}) if isinstance(yaml_cfg, dict) else {}
    if not (consignor.get("code") and source.get("account_number")):
        return False
    insert(conn, {
        "name": consignor.get("name") or "既定の振込元",
        "consignor_code": consignor.get("code", ""),
        "consignor_name": consignor.get("name", ""),
        "bank_code": source.get("bank_code", ""),
        "bank_name": source.get("bank_name", ""),
        "branch_code": source.get("branch_code", ""),
        "branch_name": source.get("branch_name", ""),
        "account_type": source.get("account_type", "1"),
        "account_number": source.get("account_number", ""),
        "is_default": True,
    })
    return True


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
