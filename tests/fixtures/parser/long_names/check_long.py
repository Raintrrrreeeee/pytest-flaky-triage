import pytest


def test_this_name_is_far_longer_than_a_normal_terminal_line_so_pytest_cannot_pad_the_status_column_at_all():
    assert True


@pytest.mark.parametrize("value", ["v" * 200])
def test_long_parameter(value):
    assert len(value) == 200


@pytest.mark.parametrize("value", [pytest.param(1, id="i" * 150)])
def test_long_custom_id(value):
    assert value == 2
