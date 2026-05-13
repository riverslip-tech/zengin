"""取引先マスタ用 SQLite ヘルパー。

スキーマは MoneyForward の「取引先・取引先口座・支払先」CSV を素直に取り込める形にしている。
口座ユニークキー（account_unique_key）を主キーとして upsert する。
"""

from __future__ import annotations

import csv
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable


DB_PATH = Path(__file__).resolve().parent / "payees.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS payees (
    account_unique_key TEXT PRIMARY KEY,
    payee_unique_key   TEXT,
    payee_name         TEXT,
    payee_name_kana    TEXT,
    payee_code         TEXT,
    bank_name          TEXT,
    bank_code          TEXT,
    branch_name        TEXT,
    branch_code        TEXT,
    account_type       TEXT,
    account_number     TEXT,
    holder_name        TEXT,
    holder_kana        TEXT,
    note               TEXT,
    updated_at         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payee_name ON payees(payee_name);
CREATE INDEX IF NOT EXISTS idx_holder_kana ON payees(holder_kana);
CREATE INDEX IF NOT EXISTS idx_account_number ON payees(account_number);
"""


# CSV列名 → DB列名 のマッピング（MFエクスポート形式）
CSV_COLUMN_MAP = {
    "口座ユニークキー": "account_unique_key",
    "取引先ユニークキー": "payee_unique_key",
    "取引先名": "payee_name",
    "取引先名カナ": "payee_name_kana",
    "取引先コード": "payee_code",
    "銀行": "bank_name",
    "銀行コード": "bank_code",
    "銀行支店": "branch_name",
    "支店コード": "branch_code",
    "口座種別": "account_type",
    "口座番号": "account_number",
    "名義人": "holder_name",
    "名義人カナ": "holder_kana",
}

DB_FIELDS = list(CSV_COLUMN_MAP.values()) + ["note"]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_payee(conn: sqlite3.Connection, row: dict) -> None:
    """1件 upsert する。account_unique_key が無ければ uuid を発行。"""
    if not row.get("account_unique_key"):
        row = {**row, "account_unique_key": f"local-{uuid.uuid4().hex[:12]}"}
    fields = DB_FIELDS
    placeholders = ", ".join("?" for _ in fields)
    columns = ", ".join(fields)
    updates = ", ".join(f"{f}=excluded.{f}" for f in fields if f != "account_unique_key")
    values = [row.get(f, "") or "" for f in fields]
    conn.execute(
        f"""
        INSERT INTO payees ({columns}, updated_at)
        VALUES ({placeholders}, CURRENT_TIMESTAMP)
        ON CONFLICT(account_unique_key) DO UPDATE SET
            {updates},
            updated_at = CURRENT_TIMESTAMP
        """,
        values,
    )


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM payees ORDER BY payee_code, payee_name, bank_name, branch_name"
    )
    return cur.fetchall()


def search(conn: sqlite3.Connection, q: str) -> list[sqlite3.Row]:
    """フリーワード検索（取引先名・カナ・名義人・銀行・支店・口座番号）。"""
    like = f"%{q.strip()}%"
    cur = conn.execute(
        """
        SELECT * FROM payees
        WHERE payee_name LIKE ? OR payee_name_kana LIKE ?
           OR holder_name LIKE ? OR holder_kana LIKE ?
           OR bank_name LIKE ? OR branch_name LIKE ?
           OR account_number LIKE ? OR payee_code LIKE ?
        ORDER BY payee_code, payee_name
        """,
        (like,) * 8,
    )
    return cur.fetchall()


def count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) AS n FROM payees")
    return cur.fetchone()["n"]


def delete(conn: sqlite3.Connection, account_unique_key: str) -> None:
    conn.execute("DELETE FROM payees WHERE account_unique_key = ?", (account_unique_key,))


def delete_all(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM payees")
    return cur.rowcount


def import_mf_csv(conn: sqlite3.Connection, csv_bytes: bytes, replace: bool = False) -> tuple[int, int]:
    """MoneyForward の取引先CSVを取り込み、(insert+update件数, スキップ件数) を返す。

    replace=True なら取り込み前に全削除。
    """
    if replace:
        delete_all(conn)

    # UTF-8 BOM対応
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())

    upserted = 0
    skipped = 0
    for raw_row in reader:
        # 必須: 銀行/口座番号 が無ければスキップ（口座未登録の取引先行）
        if not raw_row.get("銀行") or not raw_row.get("口座番号"):
            skipped += 1
            continue
        mapped: dict = {}
        for csv_col, db_col in CSV_COLUMN_MAP.items():
            mapped[db_col] = (raw_row.get(csv_col) or "").strip()
        upsert_payee(conn, mapped)
        upserted += 1

    conn.commit()
    return upserted, skipped


def insert_manual(conn: sqlite3.Connection, fields: dict) -> str:
    """手動追加用。account_unique_key を発行して INSERT、生成キーを返す。"""
    key = f"local-{uuid.uuid4().hex[:12]}"
    payload = {**fields, "account_unique_key": key}
    upsert_payee(conn, payload)
    conn.commit()
    return key


def update(conn: sqlite3.Connection, account_unique_key: str, fields: dict) -> None:
    payload = {**fields, "account_unique_key": account_unique_key}
    upsert_payee(conn, payload)
    conn.commit()


# ── マッチング ──────────────────────────────────────

def _normalize_kana(s: str | None) -> str:
    if not s:
        return ""
    import jaconv

    # 半角カナ→全角カナ、英数全角→半角、空白除去
    s = jaconv.h2z(s, kana=True, ascii=False, digit=False)
    s = jaconv.z2h(s, kana=False, ascii=True, digit=True)
    return s.replace(" ", "").replace("　", "").strip()


def find_match(
    conn: sqlite3.Connection,
    *,
    account_number: str | None = None,
    holder_name: str | None = None,
    payee_name: str | None = None,
) -> sqlite3.Row | None:
    """抽出結果からマスタに最も近い1件を返す。

    優先順位:
    1. 口座番号一致（最も信頼度高い）
    2. 名義人カナ部分一致
    3. 取引先名部分一致
    """
    rows = list_all(conn)
    if not rows:
        return None

    # 1. 口座番号
    if account_number:
        acc = account_number.strip()
        for r in rows:
            if (r["account_number"] or "").strip() == acc:
                return r

    # 2. 名義人カナ
    if holder_name:
        target = _normalize_kana(holder_name)
        if target:
            for r in rows:
                hk = _normalize_kana(r["holder_kana"])
                if hk and (hk == target or target in hk or hk in target):
                    return r
            # 漢字名義でも比較
            for r in rows:
                hn = (r["holder_name"] or "").replace(" ", "").replace("　", "")
                if hn and (hn == holder_name.replace(" ", "").replace("　", "")):
                    return r

    # 3. 取引先名
    if payee_name:
        target = payee_name.replace(" ", "").replace("　", "")
        if target:
            for r in rows:
                pn = (r["payee_name"] or "").replace(" ", "").replace("　", "")
                if pn and (pn == target or target in pn or pn in target):
                    return r

    return None


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
