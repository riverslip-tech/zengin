"""PDF読み取りモジュール

PyMuPDF でテキスト抽出を試み、テキストが少ない場合は
Tesseract OCR にフォールバックする。
"""

import base64
from pathlib import Path
from dataclasses import dataclass

import fitz  # PyMuPDF


@dataclass
class PdfContent:
    """PDF読み取り結果"""

    text: str
    base64_data: str  # Claude API に送るための base64 エンコード
    page_count: int
    extraction_method: str  # "text" or "ocr"


# テキスト抽出の最小文字数閾値 (これ以下ならOCRフォールバック)
MIN_TEXT_THRESHOLD = 50


def read_pdf(pdf_path: str | Path) -> PdfContent:
    """PDFファイルを読み取り、テキストとbase64データを返す。

    Args:
        pdf_path: PDFファイルパス
    Returns:
        PdfContent オブジェクト
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDFファイルが見つかりません: {pdf_path}")

    # base64エンコード (Claude API用)
    raw_bytes = pdf_path.read_bytes()
    base64_data = base64.standard_b64encode(raw_bytes).decode("ascii")

    # PyMuPDF でテキスト抽出
    doc = fitz.open(str(pdf_path))
    page_count = len(doc)

    text_parts = []
    for page in doc:
        text_parts.append(page.get_text())
    doc.close()

    full_text = "\n".join(text_parts).strip()

    # テキストが十分にあればそのまま返す
    non_space_chars = len(full_text.replace(" ", "").replace("\n", ""))
    if non_space_chars >= MIN_TEXT_THRESHOLD:
        return PdfContent(
            text=full_text,
            base64_data=base64_data,
            page_count=page_count,
            extraction_method="text",
        )

    # OCR フォールバック
    ocr_text = _ocr_pdf(pdf_path)
    return PdfContent(
        text=ocr_text if ocr_text else full_text,
        base64_data=base64_data,
        page_count=page_count,
        extraction_method="ocr" if ocr_text else "text",
    )


def _ocr_pdf(pdf_path: Path) -> str:
    """Tesseract OCR でPDFからテキストを抽出する。"""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        print("警告: pytesseract/Pillow が未インストールのため OCR をスキップします")
        return ""

    doc = fitz.open(str(pdf_path))
    text_parts = []

    for page in doc:
        # 300 DPI で画像化
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        try:
            text = pytesseract.image_to_string(img, lang="jpn")
            text_parts.append(text)
        except Exception as e:
            print(f"警告: OCR処理でエラーが発生しました (ページ {page.number + 1}): {e}")

    doc.close()
    return "\n".join(text_parts).strip()
