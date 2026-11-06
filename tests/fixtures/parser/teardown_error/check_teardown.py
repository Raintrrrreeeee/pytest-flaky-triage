"""A teardown error is reported as a second line after the call result."""
import pytest


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown fails")


def test_pass_then_teardown_error(broken_teardown):
    assert True


def test_fail_then_teardown_error(broken_teardown):
    assert False


def test_unaffected():
    assert True
