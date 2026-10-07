"""Unit tests for short-code generation (``app.services.encoding``)."""

from __future__ import annotations

import pytest
from app.config import Settings
from app.services.encoding import (
    BASE62_ALPHABET,
    base62_decode,
    base62_encode,
    generate_code,
    validate_custom_alias,
)


def _settings(**kw):
    return Settings.model_validate(
        {
            "code_strategy": kw.pop("code_strategy", "base62"),
            "code_length": kw.pop("code_length", 7),
        }
    )


class TestBase62:
    def test_encode_zero(self):
        assert base62_encode(0) == "0"

    def test_encode_single_digits(self):
        # 0..61 map to the alphabet verbatim.
        for i, ch in enumerate(BASE62_ALPHABET):
            assert base62_encode(i) == ch

    def test_encode_roundtrip(self):
        for value in (0, 1, 61, 62, 63, 12345, 2**32, 2**63, (1 << 64) - 1):
            assert base62_decode(base62_encode(value)) == value

    def test_encode_negative_raises(self):
        with pytest.raises(ValueError):
            base62_encode(-1)

    def test_decode_invalid_char_raises(self):
        with pytest.raises(ValueError):
            base62_decode("ab!")

    def test_decode_empty_raises(self):
        with pytest.raises(ValueError):
            base62_decode("")

    def test_monotonic(self):
        # Encoded form must stay unique across a range.
        codes = {base62_encode(i) for i in range(5000)}
        assert len(codes) == 5000


class TestGenerateCode:
    def test_base62_requires_sequence(self):
        with pytest.raises(ValueError):
            generate_code(_settings(code_strategy="base62"))

    def test_base62_uses_sequence(self):
        code = generate_code(_settings(code_strategy="base62", code_length=7), sequence_value=42)
        assert code == base62_encode(42)

    def test_murmur3_is_deterministic_length(self):
        code = generate_code(_settings(code_strategy="murmur3", code_length=7))
        assert len(code) == 7
        assert all(c in BASE62_ALPHABET for c in code)

    def test_murmur3_respects_length(self):
        code = generate_code(_settings(code_strategy="murmur3", code_length=10))
        assert len(code) == 10


class TestValidateAlias:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("", False),
            ("abc", True),
            ("a-b_c9", True),
            ("-lead", False),  # must start alnum
            ("a*b", False),  # bad char
            ("a" * 65, False),  # too long
            ("a" * 64, True),
            ("9", True),
        ],
    )
    def test_alias_rules(self, alias, expected):
        assert validate_custom_alias(alias) is expected
