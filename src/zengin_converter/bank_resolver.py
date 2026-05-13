"""銀行コード・支店コード解決モジュール

zengin-code ライブラリを使用して銀行名/支店名からコードを逆引きする。
"""

from typing import Optional
from functools import lru_cache

import jaconv


@lru_cache(maxsize=1)
def _load_bank_data() -> dict:
    """zengin-code データを読み込み、逆引き辞書を構築する。"""
    try:
        from zengin_code import Bank
    except ImportError:
        raise ImportError(
            "zengin-code パッケージが必要です: pip install zengin-code"
        )

    banks = Bank.all  # OrderedDict of Bank objects

    # 銀行名 → コード の逆引き辞書
    bank_by_name: dict[str, str] = {}
    bank_by_kana: dict[str, str] = {}
    # (銀行コード, 支店名) → 支店コード
    branch_by_name: dict[tuple[str, str], str] = {}
    branch_by_kana: dict[tuple[str, str], str] = {}

    for code, bank in banks.items():
        name = bank.name or ""
        kana = bank.kana or ""

        bank_by_name[name] = code
        short_name = _strip_suffix(name)
        if short_name != name:
            bank_by_name[short_name] = code
        if kana:
            bank_by_kana[kana] = code

        for br_code, branch in bank.branches.items():
            br_name = branch.name or ""
            br_kana = branch.kana or ""
            branch_by_name[(code, br_name)] = br_code
            short_br = _strip_suffix(br_name)
            if short_br != br_name:
                branch_by_name[(code, short_br)] = br_code
            if br_kana:
                branch_by_kana[(code, br_kana)] = br_code

    return {
        "banks": banks,
        "bank_by_name": bank_by_name,
        "bank_by_kana": bank_by_kana,
        "branch_by_name": branch_by_name,
        "branch_by_kana": branch_by_kana,
    }


def _strip_suffix(name: str) -> str:
    """銀行名・支店名から一般的な接尾辞を除去する。"""
    suffixes = ["銀行", "信用金庫", "信金", "信用組合", "労働金庫", "支店", "出張所", "営業部"]
    result = name
    for suffix in suffixes:
        if result.endswith(suffix):
            result = result[: -len(suffix)]
    return result


def _normalize(text: str) -> str:
    """検索用にテキストを正規化する。"""
    text = jaconv.z2h(text, kana=False, ascii=True, digit=True)
    text = jaconv.h2z(text, kana=True, ascii=False, digit=False)
    text = text.strip()
    return text


def resolve_bank_code(bank_name: str) -> Optional[str]:
    """銀行名から銀行コード (4桁) を解決する。"""
    data = _load_bank_data()
    name = _normalize(bank_name)

    if name in data["bank_by_name"]:
        return data["bank_by_name"][name]

    short = _strip_suffix(name)
    if short in data["bank_by_name"]:
        return data["bank_by_name"][short]

    kana = jaconv.z2h(jaconv.hira2kata(name), kana=True)
    if kana in data["bank_by_kana"]:
        return data["bank_by_kana"][kana]

    for registered_name, code in data["bank_by_name"].items():
        if short in registered_name or registered_name in name:
            return code

    return None


def resolve_branch_code(bank_code: str, branch_name: str) -> Optional[str]:
    """支店名から支店コード (3桁) を解決する。"""
    data = _load_bank_data()
    name = _normalize(branch_name)

    if (bank_code, name) in data["branch_by_name"]:
        return data["branch_by_name"][(bank_code, name)]

    short = _strip_suffix(name)
    if (bank_code, short) in data["branch_by_name"]:
        return data["branch_by_name"][(bank_code, short)]

    kana = jaconv.z2h(jaconv.hira2kata(name), kana=True)
    if (bank_code, kana) in data["branch_by_kana"]:
        return data["branch_by_kana"][(bank_code, kana)]

    for (bc, registered_name), br_code in data["branch_by_name"].items():
        if bc == bank_code and (short in registered_name or registered_name in name):
            return br_code

    return None


def get_bank_name_kana(bank_code: str) -> Optional[str]:
    """銀行コードから半角カナ銀行名を取得する（全銀フォーマット用に正規化済み）。"""
    from .kana_utils import to_halfwidth_kana

    data = _load_bank_data()
    bank = data["banks"].get(bank_code)
    if bank and bank.kana:
        # to_halfwidth_kana を通して全銀許可文字のみに正規化（ダッシュ字種統一含む）
        return to_halfwidth_kana(bank.kana)
    return None


def get_branch_name_kana(bank_code: str, branch_code: str) -> Optional[str]:
    """銀行コード+支店コードから半角カナ支店名を取得する（全銀フォーマット用に正規化済み）。"""
    from .kana_utils import to_halfwidth_kana

    data = _load_bank_data()
    bank = data["banks"].get(bank_code)
    if bank:
        branch = bank.branches.get(branch_code)
        if branch and branch.kana:
            return to_halfwidth_kana(branch.kana)
    return None
