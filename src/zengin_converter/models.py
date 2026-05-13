"""データモデル定義"""

from pydantic import BaseModel, Field
from typing import Optional


class InvoiceData(BaseModel):
    """請求書から抽出されたデータ"""

    payee_name: str = Field(description="振込先名")
    bank_name: Optional[str] = Field(default=None, description="銀行名")
    bank_code: Optional[str] = Field(default=None, description="銀行コード (4桁)")
    branch_name: Optional[str] = Field(default=None, description="支店名")
    branch_code: Optional[str] = Field(default=None, description="支店コード (3桁)")
    account_type: str = Field(default="普通", description="預金種目 (普通/当座/貯蓄/その他)")
    account_number: str = Field(description="口座番号 (最大7桁)")
    amount: int = Field(description="振込金額 (円)")
    customer_code: Optional[str] = Field(default=None, description="顧客コード")


# 預金種目マッピング
ACCOUNT_TYPE_MAP = {
    "普通": "1",
    "普通預金": "1",
    "当座": "2",
    "当座預金": "2",
    "貯蓄": "4",
    "貯蓄預金": "4",
    "その他": "9",
}


class ConsignorConfig(BaseModel):
    """委託者（振込元）設定"""

    consignor_code: str = Field(description="委託者コード (10桁)")
    consignor_name: str = Field(description="委託者名 (半角カナ)")
    bank_code: str = Field(description="仕向銀行コード (4桁)")
    bank_name: str = Field(description="仕向銀行名 (半角カナ)")
    branch_code: str = Field(description="仕向支店コード (3桁)")
    branch_name: str = Field(description="仕向支店名 (半角カナ)")
    account_type: str = Field(default="1", description="預金種目")
    account_number: str = Field(description="口座番号 (7桁)")
    transfer_date: str = Field(description="振込日 (MMDD)")
