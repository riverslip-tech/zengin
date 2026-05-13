"""請求書データ抽出モジュール

Claude API (tool_use) で請求書PDFから振込情報を抽出する。
API が使えない場合は正規表現によるフォールバック抽出を行う。
"""

import json
import os
import re
from typing import Optional

from .models import InvoiceData
from .pdf_reader import PdfContent


EXTRACTION_TOOL = {
    "name": "extract_invoice_data",
    "description": "請求書から『お振込先（お金を受け取る側）』の口座情報と振込金額を抽出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "payee_name": {
                "type": "string",
                "description": (
                    "お振込先の口座名義（例: モリワキ ジユンコ、カ)マネーフォワード）。"
                    "請求書の『お振込先』『振込先』セクションに書かれている、お金を受け取る側の名義。"
                    "請求書の宛先（『○○様』『株式会社○○ 様』など、お金を払う側）ではない。"
                    "PDFに口座名義の記載がそのままあれば、それを最優先でそのまま使う。"
                    "全角カナで返す（半角変換はシステムが行う）。"
                ),
            },
            "bank_name": {
                "type": ["string", "null"],
                "description": "振込先の銀行名（例: みずほ銀行、住信SBIネット銀行）",
            },
            "bank_code": {
                "type": ["string", "null"],
                "description": (
                    "振込先の銀行コード 4桁（例: 0001、0038）。"
                    "請求書に『銀行名(0038)』のように括弧内に書かれていることが多いので必ず探すこと。"
                ),
            },
            "branch_name": {
                "type": ["string", "null"],
                "description": "振込先の支店名（例: 本店営業部、バナナ支店）",
            },
            "branch_code": {
                "type": ["string", "null"],
                "description": (
                    "振込先の支店コード 3桁（例: 001、107）。"
                    "請求書に『支店名(107)』のように括弧内に書かれていることが多いので必ず探すこと。"
                ),
            },
            "account_type": {
                "type": "string",
                "description": "預金種目（普通/当座/貯蓄/その他）。記載がなければ普通",
                "enum": ["普通", "当座", "貯蓄", "その他"],
            },
            "account_number": {
                "type": "string",
                "description": "振込先の口座番号（数字のみ、最大7桁）",
            },
            "amount": {
                "type": "integer",
                "description": (
                    "振込金額 = 請求書の『ご請求金額』『合計』『お支払金額』の数値（整数、円）。"
                    "カンマ・¥・円記号は除去。"
                ),
            },
        },
        "required": ["payee_name", "account_type", "account_number", "amount"],
    },
}

SYSTEM_PROMPT = """\
あなたは日本語の請求書を解析し、**お振込先（お金を受け取る側）の口座情報** を抽出する専門家です。

# 請求書の構造を理解する（最重要）
日本の請求書には必ず「お金を払う人」と「お金を受け取る人」の2者が登場します:

- **宛先 / 請求先 = お金を払う人**（例: 「株式会社DefactoOne 様」「○○ 御中」）
  → これは **絶対に payee_name に入れない**
- **差出人 / 発行者 = お金を受け取る人**（例: 「森脇 潤子」「関 飛鳥」など、宛先の隣や右上に書かれた個人名・会社名）
  → こちらが payee_name の候補

# 抽出ルール（payee_name）
1. 請求書の「お振込先」「振込先」セクションに口座名義がそのまま書かれていれば、それを **最優先で** 使う
   - 例: 「口座番号 1234567 ヤマダ タロウ」→ payee_name = "ヤマダ タロウ" を全角カナで返す
   - 例: 「セキアスカ」「モリワキ ジュンコ」と書かれていれば、そのまま使う
2. 口座名義の記載がない場合のみ、差出人の名前から推測する
3. **「○○様」「○○御中」は宛先（支払者）なので、絶対に payee_name にしない**

# 抽出ルール（amount = 振込金額）
請求書には複数の数字が登場するが、振込金額は **請求書全体の最終的な請求総額** です:
- 「ご請求金額」「請求金額」「合計」「お支払金額」「請求総額」の数値を使う
- 通常はPDF内で **最も大きく強調された** 金額（例: 「¥123,434」「39,037円」）
- 以下のような小さな内訳・計算根拠の数値を amount にしてはいけない:
  - 「適格請求書非対応値引額  3,615円×20%」の「3,615」 ← 計算根拠なのでNG
  - 「10%対象 46,322」のような税区分ごとの内訳 ← 合計と一致する場合のみOK
  - 各品目の単価・小計 ← NG
- カンマ・円マーク・「円」「¥」は除去して整数で返す

# 抽出ルール（銀行コード / 支店コード）
日本の請求書では、銀行名・支店名の **直後の括弧内** に4桁・3桁のコードが書かれていることが極めて多い:
- 例: 「住信SBIネット銀行(0038)」 → bank_code = "0038"
- 例: 「バナナ支店(107)」 → branch_code = "107"
- 例: 「楽天銀行(0036) ソング支店(237)」 → bank_code = "0036", branch_code = "237"
**必ず括弧内の数字を確認して埋めること**。括弧がない場合のみ null。

# 抽出ルール（その他）
- 預金種目が明示されていない場合は「普通」
- 口座番号は数字のみ、最大7桁
- payee_name は全角カナで返す（半角変換はシステム側で行う）

# カナ略称ルール（口座名義がなく、会社名からカナ表記を推測する場合のみ）
- 株式会社 → カ)、有限会社 → ユ)、合同会社 → ド)、一般社団法人 → シヤ)

# 完全な抽出例
請求書の内容:
  株式会社DefactoOne 様              ← 宛先（支払者、無視）
  関 飛鳥                           ← 差出人（受取人候補）
  請求書番号 INV-001
  件名 運営業務4月分
  小計 35,489 / 消費税 3,548 / 請求金額 39,037円  ← amount = 39037
  適格請求書非対応値引額 3,615円×20%  ← この「3615」は無視
  振込先:
  楽天銀行(0036) ソング支店(237)
  (普通) 口座番号 3624457 セキアスカ  ← payee_name = "セキアスカ"

正解:
{
  "payee_name": "セキアスカ",     ← 「株式会社DefactoOne」にしない！
  "bank_name": "楽天銀行",
  "bank_code": "0036",            ← 括弧内の数字！
  "branch_name": "ソング支店",
  "branch_code": "237",           ← 括弧内の数字！
  "account_type": "普通",
  "account_number": "3624457",
  "amount": 39037                 ← 「3615」にしない！
}
"""


def build_system_prompt(payee_hints: Optional[list[dict]] = None) -> str:
    """SYSTEM_PROMPT に登録済み取引先のヒントを追記して返す。"""
    if not payee_hints:
        return SYSTEM_PROMPT

    lines = [
        "",
        "# 既知の取引先候補（このシステムに登録されている請求書発行者リスト）",
        "請求書の中に以下と一致または類似する名前があれば、",
        "**その表記（カナ含む）を優先して payee_name に使ってください**。",
        "ただし、これらの名前が請求書の『宛先（○○様、○○御中）』として現れた場合は",
        "それは支払者なので無視してください（請求書の発行者・差出人と一致した場合のみ採用）。",
        "",
    ]
    for h in payee_hints[:300]:  # 念のため上限
        name = (h.get("name") or "").strip()
        kana = (h.get("kana") or "").strip()
        if name and kana:
            lines.append(f"- {name}（{kana}）")
        elif name:
            lines.append(f"- {name}")
    return SYSTEM_PROMPT + "\n".join(lines)


def extract_with_claude(
    pdf_content: PdfContent,
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
    payee_hints: Optional[list[dict]] = None,
) -> InvoiceData:
    """Claude API を使用して請求書データを抽出する。

    PDFをbase64で直接送信し、Claude のビジョン機能で解析する。

    Args:
        pdf_content: PDF読み取り結果
        model: Claude モデル名
        api_key: Anthropic API キー (未指定時は環境変数 ANTHROPIC_API_KEY)
        payee_hints: 既知取引先リスト [{"name": "...", "kana": "..."}, ...]
    Returns:
        InvoiceData オブジェクト
    """
    import anthropic

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY が設定されていません。"
            "環境変数またはコマンドライン引数で指定してください。"
        )

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = build_system_prompt(payee_hints)

    # PDF を直接送信 (Claude のPDFサポート)
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "extract_invoice_data"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_content.base64_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": "この請求書から振込に必要な情報を抽出してください。",
                    },
                ],
            }
        ],
    )

    # tool_use レスポンスからデータを取得
    for block in message.content:
        if block.type == "tool_use" and block.name == "extract_invoice_data":
            return InvoiceData(**block.input)

    raise RuntimeError("Claude API から期待したレスポンスが得られませんでした")


def extract_with_regex(pdf_content: PdfContent) -> InvoiceData:
    """正規表現で請求書テキストから振込情報を抽出する (フォールバック)。

    Args:
        pdf_content: PDF読み取り結果
    Returns:
        InvoiceData オブジェクト
    Raises:
        ValueError: 必須フィールドが抽出できない場合
    """
    text = pdf_content.text

    # 金額
    amount = _extract_amount(text)
    if amount is None:
        raise ValueError("金額を抽出できませんでした")

    # 口座番号 (「普通口座 1563343」「口座番号：1234567」等)
    account_number = _extract_pattern(
        text,
        [
            r"(?:普通|当座|貯蓄)口座[\s\u3000]*(\d{1,7})",
            r"口座番号[:\s：\u3000]*(\d{1,7})",
            r"口座[:\s：\u3000]*(\d{1,7})",
            r"No\.[:\s]*(\d{1,7})",
        ],
    )
    if not account_number:
        raise ValueError("口座番号を抽出できませんでした")

    # 銀行名 (「みずほ銀行」「三菱UFJ銀行（銀行コード 0001）」等)
    bank_name = _extract_pattern(
        text,
        [
            r"([\w]+銀行)",
            r"([\w]+信用金庫)",
            r"([\w]+信金)",
        ],
    )

    # 支店名 (「恵比寿支店」「本店営業部」等)
    branch_name = _extract_pattern(
        text,
        [
            r"([\w]+支店)",
            r"([\w]+営業部)",
            r"([\w]+出張所)",
            r"支店名?[:\s：]*([\w]+)",
        ],
    )

    # 預金種目 (「普通口座」「当座」等)
    account_type = "普通"
    if re.search(r"当座", text):
        account_type = "当座"
    elif re.search(r"貯蓄", text):
        account_type = "貯蓄"

    # 口座名義 (「口座名義 カ）マネーフォワード」等) → 最優先
    payee_name = _extract_pattern(
        text,
        [
            r"口座名義[\s\u3000]*(.+)",
            r"名義[\s\u3000]*(.+)",
        ],
    )
    if payee_name:
        payee_name = payee_name.strip()
    else:
        # 口座名義がなければ会社名から推測
        payee_name = _extract_pattern(
            text,
            [
                r"((?:株式会社|有限会社|合同会社)[\s]*[\w]+)",
                r"([\w]+(?:株式会社|有限会社|合同会社))",
            ],
        )
        if payee_name:
            # 株式会社→カ)、有限会社→ユ)、合同会社→ド) を付与
            payee_name = _add_company_prefix(payee_name)
        else:
            first_line = text.strip().split("\n")[0].strip()
            payee_name = first_line[:30] if first_line else "不明"

    # 銀行コード (「銀行コード 0001」「銀行コード：0001」等)
    bank_code = _extract_pattern(
        text,
        [
            r"銀行コード[\s\u3000：:]*(\d{4})",
        ],
    )

    # 支店コード (「店番号 188」「支店コード：188」等)
    branch_code = _extract_pattern(
        text,
        [
            r"(?:支店コード|店番号|店番)[\s\u3000：:]*(\d{3})",
        ],
    )

    return InvoiceData(
        payee_name=payee_name,
        bank_name=bank_name,
        bank_code=bank_code,
        branch_name=branch_name,
        branch_code=branch_code,
        account_type=account_type,
        account_number=account_number,
        amount=amount,
    )


def _add_company_prefix(name: str) -> str:
    """会社名にカナ略称プレフィックスを付ける。"""
    prefixes = [
        ("株式会社", "カ)"),
        ("有限会社", "ユ)"),
        ("合同会社", "ド)"),
    ]
    for kanji, kana in prefixes:
        if name.startswith(kanji):
            return kana + name[len(kanji):].strip()
        if name.endswith(kanji):
            return kana + name[:-len(kanji)].strip()
    return name


def _extract_amount(text: str) -> Optional[int]:
    """金額を抽出する。"""
    patterns = [
        r"(?:合計|請求|振込|お支払)[金額計い]*[:\s：￥¥]*([0-9,，]+)",
        r"(?:金額|合計額|請求額)[:\s：￥¥]*([0-9,，]+)",
        r"[￥¥]\s*([0-9,，]+)",
        r"([0-9,，]+)\s*円",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            amount_str = match.group(1).replace(",", "").replace("，", "")
            try:
                return int(amount_str)
            except ValueError:
                continue
    return None


def _extract_pattern(text: str, patterns: list[str]) -> Optional[str]:
    """複数の正規表現パターンを順に試し、最初にマッチしたものを返す。"""
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


def extract_invoice(
    pdf_content: PdfContent,
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
    use_claude: bool = True,
    payee_hints: Optional[list[dict]] = None,
) -> InvoiceData:
    """請求書データを抽出する (Claude API優先、フォールバック付き)。

    Args:
        pdf_content: PDF読み取り結果
        model: Claude モデル名
        api_key: API キー
        use_claude: Claude API を使用するかどうか
        payee_hints: 既知取引先のヒント [{"name": "...", "kana": "..."}, ...]
    Returns:
        InvoiceData オブジェクト
    """
    if use_claude:
        try:
            return extract_with_claude(
                pdf_content, model=model, api_key=api_key, payee_hints=payee_hints
            )
        except Exception as e:
            print(f"警告: Claude API でのデータ抽出に失敗しました: {e}")
            print("正規表現によるフォールバック抽出を試みます...")

    return extract_with_regex(pdf_content)
