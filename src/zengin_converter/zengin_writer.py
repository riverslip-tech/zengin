"""全銀フォーマットファイル出力モジュール

パイプライン全体を統合し、.zen ファイルを生成する。
同一振込先（銀行+支店+口座番号+預金種目）の請求書は金額を合算する。
"""

from pathlib import Path
from dataclasses import dataclass, field

from .models import InvoiceData, ConsignorConfig, ACCOUNT_TYPE_MAP
from .kana_utils import to_halfwidth_kana
from .bank_resolver import (
    resolve_bank_code,
    resolve_branch_code,
    get_bank_name_kana,
    get_branch_name_kana,
)
from .zengin_builder import build_header, build_data, build_trailer, build_end



@dataclass
class ResolvedTransfer:
    """解決済み振込データ"""

    bank_code: str
    bank_name: str       # 半角カナ
    branch_code: str
    branch_name: str     # 半角カナ
    account_type: str    # 元の預金種目文字列 (普通/当座等)
    account_number: str
    payee_name_kana: str # 半角カナ
    amount: int
    source_invoices: list[int] = field(default_factory=list)  # 元の請求書番号

    @property
    def group_key(self) -> tuple:
        """同一振込先を判定するキー"""
        return (self.bank_code, self.branch_code,
                ACCOUNT_TYPE_MAP.get(self.account_type, "1"),
                self.account_number)


def process_invoice(invoice: InvoiceData) -> ResolvedTransfer:
    """請求書データを全銀レコード用に加工する。"""
    # 銀行コード解決
    bank_code = invoice.bank_code
    if not bank_code and invoice.bank_name:
        bank_code = resolve_bank_code(invoice.bank_name)
    if not bank_code:
        raise ValueError(
            f"銀行コードを解決できません: bank_name={invoice.bank_name}, bank_code={invoice.bank_code}"
        )

    # 支店コード解決
    branch_code = invoice.branch_code
    if not branch_code and invoice.branch_name:
        branch_code = resolve_branch_code(bank_code, invoice.branch_name)
    if not branch_code:
        raise ValueError(
            f"支店コードを解決できません: branch_name={invoice.branch_name}, branch_code={invoice.branch_code}"
        )

    # 銀行名・支店名の半角カナ取得
    bank_name_kana = get_bank_name_kana(bank_code) or ""
    if not bank_name_kana and invoice.bank_name:
        bank_name_kana = to_halfwidth_kana(invoice.bank_name)

    branch_name_kana = get_branch_name_kana(bank_code, branch_code) or ""
    if not branch_name_kana and invoice.branch_name:
        branch_name_kana = to_halfwidth_kana(invoice.branch_name)

    # 受取人名の半角カナ変換
    payee_name_kana = to_halfwidth_kana(invoice.payee_name)

    return ResolvedTransfer(
        bank_code=bank_code,
        bank_name=bank_name_kana,
        branch_code=branch_code,
        branch_name=branch_name_kana,
        account_type=invoice.account_type,
        account_number=invoice.account_number,
        payee_name_kana=payee_name_kana,
        amount=invoice.amount,
    )


def merge_transfers(transfers: list[ResolvedTransfer]) -> list[ResolvedTransfer]:
    """同一振込先の振込データを合算する。"""
    merged: dict[tuple, ResolvedTransfer] = {}

    for t in transfers:
        key = t.group_key
        if key in merged:
            merged[key].amount += t.amount
            merged[key].source_invoices.extend(t.source_invoices)
        else:
            merged[key] = t

    return list(merged.values())


def _unique_path(path: Path) -> Path:
    """既存ファイルと重複しない連番付きパスを返す。"""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    n = 1
    while True:
        new_path = parent / f"{stem}_{n}{suffix}"
        if not new_path.exists():
            return new_path
        n += 1


def generate_zengin(
    invoices: list[InvoiceData],
    config: ConsignorConfig,
    output_path: str | Path,
    transfer_type: str = "総合振込",
) -> Path:
    """請求書データリストから全銀フォーマットファイルを生成する。

    同一振込先の請求書は金額を合算して1レコードにまとめる。
    """
    output_path = _unique_path(Path(output_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 各請求書を解決
    resolved: list[ResolvedTransfer] = []
    for i, invoice in enumerate(invoices, 1):
        try:
            t = process_invoice(invoice)
            t.source_invoices = [i]
            resolved.append(t)
        except ValueError as e:
            print(f"警告: 請求書 {i} をスキップしました: {e}")

    if not resolved:
        raise ValueError("有効な振込データがありません")

    # 同一振込先を合算
    merged = merge_transfers(resolved)

    if len(merged) < len(resolved):
        print(f"  同一振込先を合算: {len(resolved)}件 -> {len(merged)}件")

    # レコード構築
    records: list[bytes] = []
    records.append(build_header(config, transfer_type=transfer_type))

    total_count = 0
    total_amount = 0

    for t in merged:
        # build_data に渡すための InvoiceData を作成 (合算済み金額)
        merged_invoice = InvoiceData(
            payee_name=t.payee_name_kana,
            account_type=t.account_type,
            account_number=t.account_number,
            amount=t.amount,
        )
        record = build_data(
            invoice=merged_invoice,
            dest_bank_code=t.bank_code,
            dest_bank_name=t.bank_name,
            dest_branch_code=t.branch_code,
            dest_branch_name=t.branch_name,
            payee_name_kana=t.payee_name_kana,
        )
        records.append(record)
        total_count += 1
        total_amount += t.amount

    # トレーラ・エンド
    records.append(build_trailer(total_count, total_amount))
    records.append(build_end())

    # ファイル出力 (レコード連結、区切りなし)
    with open(output_path, "wb") as f:
        for record in records:
            f.write(record)

    print(f"全銀ファイルを出力しました: {output_path}")
    print(f"  振込件数: {total_count}")
    print(f"  合計金額: {total_amount:,}円")

    return output_path
