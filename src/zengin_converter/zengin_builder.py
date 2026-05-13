"""全銀フォーマット レコードビルダー

各レコードは固定長120バイト（Shift_JIS / cp932）。
レコード種別:
  1: ヘッダレコード
  2: データレコード
  8: トレーラレコード
  9: エンドレコード
"""

from .kana_utils import pack_n, pack_c, to_halfwidth_kana
from .models import ConsignorConfig, InvoiceData, ACCOUNT_TYPE_MAP

RECORD_LENGTH = 120


TRANSFER_TYPES = {
    "総合振込": "21",
    "給与振込": "11",
    "賞与振込": "12",
}


def build_header(config: ConsignorConfig, transfer_type: str = "総合振込") -> bytes:
    """ヘッダレコード (区分コード=1) を構築する。

    フィールド構成 (120バイト):
      データ区分        N(1)  = "1"
      種別コード        N(2)  = "21" (総合振込)
      コード区分        N(1)  = "0"  (JIS)
      委託者コード      N(10)
      委託者名          C(40)
      振込日            N(4)  MMDD
      仕向銀行番号      N(4)
      仕向銀行名        C(15)
      仕向支店番号      N(3)
      仕向支店名        C(15)
      預金種目          N(1)
      口座番号          N(7)
      ダミー            C(17)
    """
    record = b""
    record += pack_n("1", 1)                            # データ区分
    type_code = TRANSFER_TYPES.get(transfer_type, "21")
    record += pack_n(type_code, 2)                         # 種別コード
    record += pack_n("0", 1)                            # コード区分 (JIS)
    record += pack_n(config.consignor_code, 10)         # 委託者コード
    record += pack_c(to_halfwidth_kana(config.consignor_name), 40)  # 委託者名
    record += pack_n(config.transfer_date, 4)           # 振込日
    record += pack_n(config.bank_code, 4)               # 仕向銀行番号
    record += pack_c(to_halfwidth_kana(config.bank_name), 15)      # 仕向銀行名
    record += pack_n(config.branch_code, 3)             # 仕向支店番号
    record += pack_c(to_halfwidth_kana(config.branch_name), 15)    # 仕向支店名
    record += pack_n(config.account_type, 1)            # 預金種目
    record += pack_n(config.account_number, 7)          # 口座番号
    record += pack_c("", 17)                            # ダミー

    assert len(record) == RECORD_LENGTH, f"Header: {len(record)} bytes (expected {RECORD_LENGTH})"
    return record


def build_data(
    invoice: InvoiceData,
    dest_bank_code: str,
    dest_bank_name: str,
    dest_branch_code: str,
    dest_branch_name: str,
    payee_name_kana: str,
) -> bytes:
    """データレコード (区分コード=2) を構築する。

    フィールド構成 (120バイト):
      データ区分          N(1)  = "2"
      被仕向金融機関番号  N(4)
      被仕向金融機関名    C(15) 左詰、スペース埋め
      被仕向支店番号      N(3)
      被仕向支店名        C(15) 左詰、スペース埋め
      手形交換所番号      N(4)  0（全てゼロ）
      預金種目            N(1)  1:普通、2:当座、4:貯蓄、9:その他
      口座番号            N(7)  右詰、ZERO埋め
      受取人名            C(30) カナ
      振込金額            N(10) 右詰、ZERO埋め
      新規コード          N(1)  0:その他、1:第1回振込分、2:変更分
      顧客コード1         N(10) 識別表示=スペース時のみ
      顧客コード2         N(10) 識別表示=スペース時のみ
      振込区分            N(1)  7:電信振込
      識別表示            C(1)  Y:EDI使用、スペース:EDI不使用
      ダミー              C(7)  スペース
    """
    account_type_code = ACCOUNT_TYPE_MAP.get(invoice.account_type, "1")

    record = b""
    record += pack_n("2", 1)                            # データ区分
    record += pack_n(dest_bank_code, 4)                 # 被仕向金融機関番号
    record += pack_c(dest_bank_name, 15)                # 被仕向金融機関名
    record += pack_n(dest_branch_code, 3)               # 被仕向支店番号
    record += pack_c(dest_branch_name, 15)              # 被仕向支店名
    record += pack_c("", 4)                             # 手形交換所番号（スペース）
    record += pack_n(account_type_code, 1)              # 預金種目
    record += pack_n(invoice.account_number, 7)         # 口座番号
    record += pack_c(payee_name_kana, 30)               # 受取人名
    record += pack_n(str(invoice.amount), 10)           # 振込金額
    record += pack_n("0", 1)                            # 新規コード（0:その他）
    record += pack_c("", 10)                            # 顧客コード1（スペース）
    record += pack_c("", 10)                            # 顧客コード2（スペース）
    record += pack_c("", 1)                             # 振込区分（スペース）
    record += pack_c("", 1)                             # 識別表示（スペース:EDI不使用）
    record += pack_c("", 7)                             # ダミー

    assert len(record) == RECORD_LENGTH, f"Data: {len(record)} bytes (expected {RECORD_LENGTH})"
    return record


def build_trailer(total_count: int, total_amount: int) -> bytes:
    """トレーラレコード (区分コード=8) を構築する。

    フィールド構成 (120バイト):
      データ区分    N(1)  = "8"
      合計件数      N(6)
      合計金額      N(12)
      ダミー        C(101)
    """
    record = b""
    record += pack_n("8", 1)                            # データ区分
    record += pack_n(str(total_count), 6)               # 合計件数
    record += pack_n(str(total_amount), 12)             # 合計金額
    record += pack_c("", 101)                           # ダミー

    assert len(record) == RECORD_LENGTH, f"Trailer: {len(record)} bytes (expected {RECORD_LENGTH})"
    return record


def build_end() -> bytes:
    """エンドレコード (区分コード=9) を構築する。

    フィールド構成 (120バイト):
      データ区分    N(1)  = "9"
      ダミー        C(119)
    """
    record = b""
    record += pack_n("9", 1)                            # データ区分
    record += pack_c("", 119)                           # ダミー

    assert len(record) == RECORD_LENGTH, f"End: {len(record)} bytes (expected {RECORD_LENGTH})"
    return record
