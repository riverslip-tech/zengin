"""半角カナ変換・パディングユーティリティ

全銀フォーマットで使用可能な文字:
- 半角カタカナ (ｦ-ﾟ)
- 半角英大文字 (A-Z)
- 半角数字 (0-9)
- 半角スペース
- 記号: ( ) / - . ,
"""

import jaconv
import re

# 全銀フォーマットで許可される文字パターン
ZENGIN_ALLOWED = re.compile(
    r"[ｦ-ﾟA-Z0-9 \(\)\-\./,\\]"
)


def to_halfwidth_kana(text: str) -> str:
    """日本語テキストを全銀フォーマット用の半角カナに変換する。

    1. ひらがな → カタカナ
    2. 全角カタカナ → 半角カタカナ
    3. 全角英数 → 半角英数
    4. 小文字 → 大文字
    5. 許可されない文字を除去
    """
    # cp932でエンコードできない文字を事前に除去/変換
    text = text.replace("\u00a5", "")   # ¥ (U+00A5)
    text = text.replace("\uffe5", "")   # ￥ (U+FFE5)
    text = text.replace("\u301c", "-")  # 〜 (WAVE DASH)
    text = text.replace("\uff5e", "-")  # ～ (FULLWIDTH TILDE)
    text = text.replace("・", ".")      # 中黒 → ピリオド
    # ひらがな → カタカナ
    text = jaconv.hira2kata(text)
    # 全角 → 半角 (カナ、ASCII、数字)
    text = jaconv.z2h(text, kana=True, ascii=True, digit=True)
    # 小文字 → 大文字
    text = text.upper()
    # 全角スペース → 半角スペース
    text = text.replace("\u3000", " ")
    # 全角括弧 → 半角括弧
    text = text.replace("（", "(").replace("）", ")")
    # 長音符・ダッシュ系すべてを半角ハイフンに統一（cp932で2バイトになる字種を避ける）
    # ー(U+30FC JIS長音), －(U+FF0D 全角ハイフン), ‐(U+2010), ‒(U+2012), –(U+2013),
    # —(U+2014), ―(U+2015), −(U+2212 マイナス記号), ｰ(U+FF70 半角長音はそのまま残す)
    for dash in ("ー", "－", "‐", "‒", "–", "—", "―", "−"):
        text = text.replace(dash, "-")
    # 小さいカナ → 大きいカナ (全銀フォーマットでは小文字カナ不可)
    SMALL_TO_LARGE = str.maketrans(
        "ｧｨｩｪｫｯｬｭｮ",
        "ｱｲｳｴｵﾂﾔﾕﾖ",
    )
    text = text.translate(SMALL_TO_LARGE)
    # 許可文字のみフィルタ
    result = ""
    for ch in text:
        if ZENGIN_ALLOWED.match(ch):
            result += ch
    return result


def pack_n(value: str, length: int) -> bytes:
    """数値フィールド: 右寄せ・ゼロ埋め・ASCII。

    Args:
        value: 数値文字列
        length: バイト長
    Returns:
        固定長のASCIIバイト列
    """
    value = value.strip()
    if not value:
        value = "0"
    return value.rjust(length, "0")[:length].encode("ascii")


def pack_c(value: str, length: int) -> bytes:
    """文字フィールド: 左寄せ・スペース埋め・Shift_JIS(cp932)。

    半角カナは1バイト、全角文字は2バイトでエンコードされる。
    指定バイト長を超える場合は切り詰め、不足する場合はスペースで埋める。

    Args:
        value: 文字列 (半角カナ推奨)
        length: バイト長
    Returns:
        固定長のShift_JISバイト列
    """
    # cp932エンコード不可の文字を除去してからエンコード
    value = _sanitize_for_cp932(value)
    encoded = value.encode("cp932", errors="ignore")
    if len(encoded) > length:
        # バイト単位で切り詰め (マルチバイト文字の途中で切らない)
        encoded = _truncate_cp932(value, length)
    elif len(encoded) < length:
        encoded = encoded + b" " * (length - len(encoded))
    return encoded


def _sanitize_for_cp932(text: str) -> str:
    """cp932でエンコードできない文字を除去する。"""
    result = []
    for ch in text:
        try:
            ch.encode("cp932")
            result.append(ch)
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass  # エンコード不可文字をスキップ
    return "".join(result)


def _truncate_cp932(text: str, max_bytes: int) -> bytes:
    """Shift_JIS(cp932)でエンコードし、max_bytesに収まるよう安全に切り詰める。"""
    result = b""
    for ch in text:
        ch_bytes = ch.encode("cp932", errors="replace")
        if len(result) + len(ch_bytes) > max_bytes:
            break
        result += ch_bytes
    # 不足分をスペースで埋める
    result += b" " * (max_bytes - len(result))
    return result
