"""CLIエントリーポイント

使用例:
  # 単一PDF
  zengin-converter invoice.pdf --output transfers.txt

  # 複数PDF
  zengin-converter invoice1.pdf invoice2.pdf --output transfers.txt

  # 設定ファイル指定
  zengin-converter invoice.pdf --config my_config.yaml

  # Claude API を使わない (正規表現フォールバック)
  zengin-converter invoice.pdf --no-claude
"""

import argparse
import sys
from pathlib import Path

import yaml

from .models import ConsignorConfig, InvoiceData
from .pdf_reader import read_pdf
from .extractor import extract_invoice
from .kana_utils import to_halfwidth_kana
from .zengin_writer import generate_zengin


def load_config(config_path: Path) -> dict:
    """YAML設定ファイルを読み込む。"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_consignor_config(args: argparse.Namespace, yaml_config: dict) -> ConsignorConfig:
    """CLI引数とYAML設定から委託者設定を構築する。CLI引数が優先。"""
    consignor = yaml_config.get("consignor", {})
    source = yaml_config.get("source", {})

    return ConsignorConfig(
        consignor_code=args.consignor_code or consignor.get("code", "0000000000"),
        consignor_name=args.consignor_name or consignor.get("name", ""),
        bank_code=args.source_bank_code or source.get("bank_code", "0000"),
        bank_name=args.source_bank_name or source.get("bank_name", ""),
        branch_code=args.source_branch_code or source.get("branch_code", "000"),
        branch_name=args.source_branch_name or source.get("branch_name", ""),
        account_type=args.source_account_type or source.get("account_type", "1"),
        account_number=args.source_account_number or source.get("account_number", "0000000"),
        transfer_date=args.transfer_date or yaml_config.get("transfer_date", "0101"),
    )


def main():
    parser = argparse.ArgumentParser(
        description="請求書PDFを全銀フォーマットに変換する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
例:
  zengin-converter invoice.pdf
  zengin-converter *.pdf --output transfers.txt --transfer-date 0501
  zengin-converter invoice.pdf --no-claude
""",
    )

    parser.add_argument(
        "pdf_files",
        nargs="+",
        help="請求書PDFファイル (複数指定可)",
    )
    parser.add_argument(
        "--transfer-type",
        default="総合振込",
        choices=["総合振込", "給与振込", "賞与振込"],
        help="振込種別 (デフォルト: 総合振込)",
    )
    parser.add_argument(
        "-o", "--output",
        default="output/transfers.txt",
        help="出力ファイルパス (デフォルト: output/transfers.txt)",
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="設定ファイルパス (YAML)",
    )
    parser.add_argument(
        "--transfer-date",
        default=None,
        help="振込日 (MMDD形式、例: 0501)",
    )

    # 委託者情報 (CLI上書き用)
    consignor_group = parser.add_argument_group("委託者情報 (設定ファイルを上書き)")
    consignor_group.add_argument("--consignor-code", default=None, help="委託者コード (10桁)")
    consignor_group.add_argument("--consignor-name", default=None, help="委託者名 (半角カナ)")
    consignor_group.add_argument("--source-bank-code", default=None, help="仕向銀行コード (4桁)")
    consignor_group.add_argument("--source-bank-name", default=None, help="仕向銀行名 (半角カナ)")
    consignor_group.add_argument("--source-branch-code", default=None, help="仕向支店コード (3桁)")
    consignor_group.add_argument("--source-branch-name", default=None, help="仕向支店名 (半角カナ)")
    consignor_group.add_argument("--source-account-type", default=None, help="預金種目 (1:普通, 2:当座)")
    consignor_group.add_argument("--source-account-number", default=None, help="口座番号 (7桁)")

    # Claude API 設定
    api_group = parser.add_argument_group("Claude API 設定")
    api_group.add_argument("--api-key", default=None, help="Anthropic API キー")
    api_group.add_argument(
        "--model",
        default=None,
        help="Claude モデル名 (デフォルト: claude-sonnet-4-6)",
    )
    api_group.add_argument(
        "--no-claude",
        action="store_true",
        help="Claude API を使用せず正規表現で抽出する",
    )

    args = parser.parse_args()

    # 設定ファイル読み込み
    yaml_config = {}
    config_path = Path(args.config) if args.config else None

    # config引数がなければデフォルトパスを探す
    if not config_path:
        default_paths = [
            Path("config.yaml"),
            Path(__file__).parent.parent.parent / "config.yaml",
        ]
        for p in default_paths:
            if p.exists():
                config_path = p
                break

    if config_path and config_path.exists():
        yaml_config = load_config(config_path)
        print(f"設定ファイル読み込み: {config_path}")

    # 委託者設定
    consignor_config = build_consignor_config(args, yaml_config)

    # Claude API 設定
    claude_config = yaml_config.get("claude", {})
    model = args.model or claude_config.get("model", "claude-sonnet-4-6")
    use_claude = not args.no_claude

    # PDFファイル処理
    invoices: list[InvoiceData] = []

    for pdf_file in args.pdf_files:
        pdf_path = Path(pdf_file)
        if not pdf_path.exists():
            print(f"エラー: ファイルが見つかりません: {pdf_path}", file=sys.stderr)
            continue

        print(f"\n処理中: {pdf_path}")

        try:
            # PDF読み取り
            pdf_content = read_pdf(pdf_path)
            print(f"  抽出方法: {pdf_content.extraction_method} ({pdf_content.page_count}ページ)")

            # データ抽出
            invoice = extract_invoice(
                pdf_content,
                model=model,
                api_key=args.api_key,
                use_claude=use_claude,
            )

            print(f"  振込先: {to_halfwidth_kana(invoice.payee_name)}")
            print(f"  銀行: {to_halfwidth_kana(invoice.bank_name) if invoice.bank_name else '(?)'} / 支店: {to_halfwidth_kana(invoice.branch_name) if invoice.branch_name else '(?)'}")
            print(f"  口座: {invoice.account_type} {invoice.account_number}")
            print(f"  金額: {invoice.amount:,}円")

            invoices.append(invoice)

        except Exception as e:
            print(f"  エラー: {e}", file=sys.stderr)
            continue

    if not invoices:
        print("\nエラー: 処理できた請求書がありません", file=sys.stderr)
        sys.exit(1)

    # 全銀ファイル生成
    print(f"\n--- 全銀ファイル生成 ---")
    try:
        output = generate_zengin(
            invoices, consignor_config, args.output,
            transfer_type=args.transfer_type,
        )
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
