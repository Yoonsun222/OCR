from raglab.track_c.tokenizer import MAX_CHARACTERS, MAX_TOKENS, analyze


def test_analyze_mixed_text_is_stable():
    assert analyze("Ｐｕｍｐ A 압력상승 部品C") == (
        "pump", "a", "압력상", "력상승", "部品c",
    )


def test_controls_and_punctuation_are_separators():
    assert analyze("A\x00B、カタカナ\u202eC") == ("a", "b", "カタカ", "タカナ", "c")


def test_limits_original_characters_before_normalization():
    assert analyze((" " * (MAX_CHARACTERS - 1)) + "Ａdrop") == ("a",)


def test_caps_trigrams_without_changing_their_order():
    text = "가나다" * (MAX_TOKENS + 1)
    result = analyze(text)
    assert len(result) == MAX_TOKENS
    assert result[:4] == ("가나다", "나다가", "다가나", "가나다")
