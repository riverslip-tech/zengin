"""カナ変換ユーティリティのテスト"""

from zengin_converter.kana_utils import to_halfwidth_kana, pack_n, pack_c


def test_fullwidth_to_halfwidth():
    assert to_halfwidth_kana("カブシキガイシャ") == "ｶﾌﾞｼｷｶﾞｲｼｬ"


def test_hiragana_to_halfwidth_kana():
    result = to_halfwidth_kana("かぶしきがいしゃ")
    assert result == "ｶﾌﾞｼｷｶﾞｲｼｬ"


def test_mixed_text():
    result = to_halfwidth_kana("カ）テスト")
    assert "ｶ" in result
    assert "ﾃｽﾄ" in result


def test_fullwidth_numbers():
    result = to_halfwidth_kana("１２３")
    assert result == "123"


def test_pack_n_zero_pad():
    assert pack_n("42", 6) == b"000042"


def test_pack_n_truncate():
    assert pack_n("123456789", 4) == b"1234"


def test_pack_c_space_pad():
    result = pack_c("AB", 5)
    assert result == b"AB   "
    assert len(result) == 5


def test_pack_c_halfwidth_kana():
    result = pack_c("ﾃｽﾄ", 10)
    assert len(result) == 10
    # 半角カナはShift_JISで1バイト
    assert result[:3] == "ﾃｽﾄ".encode("cp932")


def test_pack_c_truncate():
    long_text = "A" * 50
    result = pack_c(long_text, 10)
    assert len(result) == 10
