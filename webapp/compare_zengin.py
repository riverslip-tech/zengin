"""全銀フォーマットファイル 2 本のスキーマ整合チェック。

引数で渡した2ファイルをレコードごとに分解し、構造が一致しているか検査する。
"""

from __future__ import annotations

import sys
from pathlib import Path


RECORD_LEN = 120
TRANSFER_TYPES = {"11": "給与振込", "12": "賞与振込", "21": "総合振込"}
ACCOUNT_TYPES = {"1": "普通", "2": "当座", "4": "貯蓄", "9": "その他"}


def parse_header(rec: bytes) -> dict:
    return {
        "種別": (rec[1:3].decode("ascii"), TRANSFER_TYPES.get(rec[1:3].decode("ascii"), "?")),
        "コード区分": rec[3:4].decode("ascii"),
        "委託者コード": rec[4:14].decode("ascii"),
        "委託者名": rec[14:54].decode("cp932").rstrip(),
        "振込日": rec[54:58].decode("ascii"),
        "仕向銀行コード": rec[58:62].decode("ascii"),
        "仕向銀行名": rec[62:77].decode("cp932").rstrip(),
        "仕向支店コード": rec[77:80].decode("ascii"),
        "仕向支店名": rec[80:95].decode("cp932").rstrip(),
        "預金種目": (rec[95:96].decode("ascii"), ACCOUNT_TYPES.get(rec[95:96].decode("ascii"), "?")),
        "口座番号": rec[96:103].decode("ascii"),
        "ダミー長": 120 - 103,
    }


def parse_data(rec: bytes) -> dict:
    return {
        "被仕向銀行コード": rec[1:5].decode("ascii"),
        "被仕向銀行名": rec[5:20].decode("cp932").rstrip(),
        "被仕向支店コード": rec[20:23].decode("ascii"),
        "被仕向支店名": rec[23:38].decode("cp932").rstrip(),
        "手形交換所番号": rec[38:42].decode("cp932"),
        "預金種目": (rec[42:43].decode("ascii"), ACCOUNT_TYPES.get(rec[42:43].decode("ascii"), "?")),
        "口座番号": rec[43:50].decode("ascii"),
        "受取人名": rec[50:80].decode("cp932").rstrip(),
        "振込金額": int(rec[80:90].decode("ascii")),
        "新規コード": rec[90:91].decode("ascii"),
        "顧客コード1": rec[91:101].decode("cp932").rstrip(),
        "顧客コード2": rec[101:111].decode("cp932").rstrip(),
        "振込区分": rec[111:112].decode("cp932"),
        "識別表示": rec[112:113].decode("cp932"),
    }


def parse_trailer(rec: bytes) -> dict:
    return {
        "合計件数": int(rec[1:7].decode("ascii")),
        "合計金額": int(rec[7:19].decode("ascii")),
    }


def analyze(path: Path) -> dict:
    data = path.read_bytes()
    n_records = len(data) // RECORD_LEN
    remain = len(data) % RECORD_LEN

    records = [data[i * RECORD_LEN : (i + 1) * RECORD_LEN] for i in range(n_records)]
    record_types = [r[0:1].decode("ascii") for r in records]

    header = parse_header(records[0]) if records[0][0:1] == b"1" else None
    data_records = [parse_data(r) for r in records if r[0:1] == b"2"]
    trailer = next((parse_trailer(r) for r in records if r[0:1] == b"8"), None)
    end = next((r for r in records if r[0:1] == b"9"), None)

    return {
        "path": path,
        "byte_size": len(data),
        "record_count": n_records,
        "remainder": remain,
        "record_types": record_types,
        "type_summary": {t: record_types.count(t) for t in sorted(set(record_types))},
        "header": header,
        "data_records": data_records,
        "trailer": trailer,
        "has_end": end is not None,
    }


def print_report(label: str, a: dict) -> None:
    print(f"\n{'=' * 60}")
    print(f"{label}: {a['path'].name}")
    print(f"{'=' * 60}")
    print(f"  ファイルサイズ: {a['byte_size']:,} bytes")
    print(f"  レコード数: {a['record_count']} (余り {a['remainder']} bytes)")
    print(f"  レコード種別構成: {a['type_summary']}")

    if a["header"]:
        print(f"\n  [ヘッダレコード]")
        for k, v in a["header"].items():
            print(f"    {k}: {v}")

    if a["trailer"]:
        print(f"\n  [トレーラレコード]")
        for k, v in a["trailer"].items():
            print(f"    {k}: {v:,}")

    print(f"\n  エンドレコード: {'あり ✓' if a['has_end'] else 'なし ✗'}")

    print(f"\n  [データレコード {len(a['data_records'])}件 抜粋]")
    for i, d in enumerate(a["data_records"][:5]):
        print(f"    #{i + 1}: {d['被仕向銀行コード']} {d['被仕向銀行名']:<14s} / "
              f"{d['被仕向支店コード']} {d['被仕向支店名']:<14s} / "
              f"{d['預金種目'][1]} {d['口座番号']} / "
              f"{d['受取人名']:<20s} / {d['振込金額']:,}円")
    if len(a["data_records"]) > 5:
        print(f"    ... 他 {len(a['data_records']) - 5} 件")


def compare(a: dict, b: dict) -> None:
    print(f"\n{'=' * 60}")
    print("整合性チェック")
    print(f"{'=' * 60}")

    # サイズ
    if a["byte_size"] == b["byte_size"]:
        print(f"  サイズ      : 同じ ({a['byte_size']} bytes)")
    else:
        diff = b["byte_size"] - a["byte_size"]
        print(f"  サイズ      : 異なる (A={a['byte_size']} B={b['byte_size']} 差={diff:+d})")

    # 余り
    for label, side in [("A", a), ("B", b)]:
        if side["remainder"] != 0:
            print(f"  ⚠ {label} の末尾に {side['remainder']} bytes の余りあり → 120倍数でない")

    # レコード構成
    if a["type_summary"] == b["type_summary"]:
        print(f"  レコード構成: 同じ {a['type_summary']}")
    else:
        print(f"  レコード構成: 異なる A={a['type_summary']} B={b['type_summary']}")

    # フィールド比較（ヘッダ）
    print(f"\n  [ヘッダ差分]")
    if a["header"] and b["header"]:
        for k in a["header"]:
            va, vb = a["header"][k], b["header"][k]
            mark = "  " if va == vb else "≠ "
            print(f"    {mark}{k}: A={va!r}  B={vb!r}")

    # トレーラ比較
    print(f"\n  [トレーラ差分]")
    if a["trailer"] and b["trailer"]:
        for k in a["trailer"]:
            va, vb = a["trailer"][k], b["trailer"][k]
            mark = "  " if va == vb else "≠ "
            print(f"    {mark}{k}: A={va:,}  B={vb:,}")

    # 各データレコードのスキーマ位置検査（境界確認）
    print(f"\n  [データレコードのバイト位置検証（仕様準拠）]")
    print(f"    全銀仕様: '2' + 銀行C(4) + 銀行名(15) + 支店C(3) + 支店名(15) + 交換所(4)")
    print(f"              + 種目(1) + 口座(7) + 受取人(30) + 金額(10) + 新規(1) + ...")
    for label, side in [("A", a), ("B", b)]:
        bad = []
        for i, d in enumerate(side["data_records"]):
            # 主要フィールドが数字 (or 期待形式) になっているかチェック
            if not d["被仕向銀行コード"].isdigit():
                bad.append((i, "銀行コード", d["被仕向銀行コード"]))
            if not d["被仕向支店コード"].isdigit():
                bad.append((i, "支店コード", d["被仕向支店コード"]))
            if not d["口座番号"].isdigit():
                bad.append((i, "口座番号", d["口座番号"]))
            if d["預金種目"][0] not in ACCOUNT_TYPES:
                bad.append((i, "預金種目", d["預金種目"][0]))
        if bad:
            print(f"    ⚠ {label}: 不正フィールド {len(bad)}件: {bad[:5]}")
        else:
            print(f"    ✓ {label}: 全 {len(side['data_records'])} 件、全フィールドが正しい位置にある")

    # 同じ銀行コード×口座番号でマッチング
    print(f"\n  [両ファイルに共通する受取人]")
    a_keys = {(d["被仕向銀行コード"], d["口座番号"]): d for d in a["data_records"]}
    b_keys = {(d["被仕向銀行コード"], d["口座番号"]): d for d in b["data_records"]}
    common = set(a_keys) & set(b_keys)
    print(f"    共通 {len(common)} 件 / A固有 {len(set(a_keys) - set(b_keys))} 件 / B固有 {len(set(b_keys) - set(a_keys))} 件")

    if common:
        print(f"\n  [共通受取人の名義表記比較（最初の10件）]")
        for key in sorted(common)[:10]:
            da, db = a_keys[key], b_keys[key]
            name_match = "" if da["受取人名"] == db["受取人名"] else " ≠"
            bank_match = "" if da["被仕向銀行名"] == db["被仕向銀行名"] else " ≠銀行名"
            branch_match = "" if da["被仕向支店名"] == db["被仕向支店名"] else " ≠支店名"
            print(f"    {key[0]} {key[1]:>7s}: "
                  f"A=「{da['被仕向銀行名']:<14s}/{da['被仕向支店名']:<14s}/{da['受取人名']:<20s}」"
                  f"  B=「{db['被仕向銀行名']:<14s}/{db['被仕向支店名']:<14s}/{db['受取人名']:<20s}」"
                  f"{name_match}{bank_match}{branch_match}")


def main() -> None:
    if len(sys.argv) < 3:
        print("使い方: python compare_zengin.py <fileA> <fileB>")
        sys.exit(1)
    a = analyze(Path(sys.argv[1]))
    b = analyze(Path(sys.argv[2]))
    print_report("A", a)
    print_report("B", b)
    compare(a, b)


if __name__ == "__main__":
    main()
