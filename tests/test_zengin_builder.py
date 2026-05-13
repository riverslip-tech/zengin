"""全銀レコードビルダーのテスト"""

from zengin_converter.models import ConsignorConfig, InvoiceData
from zengin_converter.zengin_builder import (
    build_header,
    build_data,
    build_trailer,
    build_end,
    RECORD_LENGTH,
)


def test_header_length():
    config = ConsignorConfig(
        consignor_code="1234567890",
        consignor_name="カ)テスト",
        bank_code="0001",
        bank_name="ミズホ",
        branch_code="001",
        branch_name="ホンテン",
        account_type="1",
        account_number="1234567",
        transfer_date="0501",
    )
    record = build_header(config)
    assert len(record) == RECORD_LENGTH
    assert record[0:1] == b"1"  # データ区分


def test_data_length():
    invoice = InvoiceData(
        payee_name="テスト株式会社",
        bank_name="みずほ銀行",
        bank_code="0001",
        branch_name="本店",
        branch_code="001",
        account_type="普通",
        account_number="1234567",
        amount=100000,
    )
    record = build_data(
        invoice=invoice,
        dest_bank_code="0001",
        dest_bank_name="ミズホ",
        dest_branch_code="001",
        dest_branch_name="ホンテン",
        payee_name_kana="テスト(カ",
    )
    assert len(record) == RECORD_LENGTH
    assert record[0:1] == b"2"  # データ区分


def test_trailer_length():
    record = build_trailer(total_count=3, total_amount=300000)
    assert len(record) == RECORD_LENGTH
    assert record[0:1] == b"8"


def test_end_length():
    record = build_end()
    assert len(record) == RECORD_LENGTH
    assert record[0:1] == b"9"


def test_header_field_values():
    config = ConsignorConfig(
        consignor_code="1234567890",
        consignor_name="カ)テスト",
        bank_code="0001",
        bank_name="ミズホ",
        branch_code="001",
        branch_name="ホンテン",
        account_type="1",
        account_number="1234567",
        transfer_date="0501",
    )
    record = build_header(config)
    # 種別コード = 21
    assert record[1:3] == b"21"
    # コード区分 = 0
    assert record[3:4] == b"0"
    # 委託者コード
    assert record[4:14] == b"1234567890"
    # 振込日
    assert record[54:58] == b"0501"


def test_trailer_amounts():
    record = build_trailer(total_count=5, total_amount=1234567)
    # 合計件数 (6桁)
    assert record[1:7] == b"000005"
    # 合計金額 (12桁)
    assert record[7:19] == b"000001234567"
