"""Parametrised IDs with characters that break naive splitting or CSV."""
import pytest


@pytest.mark.parametrize("value", ["a b", "c-d", "[x]", "has - dash", "café"])
def test_text(value):
    assert value != "c-d"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, id='comma,and"quote'),
        pytest.param(2, id="trailing space "),
        pytest.param(3, id="semi;colon"),
    ],
)
def test_custom_ids(value):
    assert value != 3


@pytest.mark.parametrize("a, b", [(1, 2), (3, 4)])
def test_two_args(a, b):
    assert a < b
