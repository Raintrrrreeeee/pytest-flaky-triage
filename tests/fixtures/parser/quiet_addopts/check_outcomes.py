"""One test per pytest outcome the parser must recognise."""
import pytest


@pytest.fixture
def broken_setup():
    raise RuntimeError("setup fails")


def test_passes():
    assert True


def test_fails():
    assert 1 == 2


def test_setup_error(broken_setup):
    pass


def test_skipped():
    pytest.skip("not relevant here")


@pytest.mark.skip(reason="marked skip")
def test_skip_marker():
    pass


@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="bug was fixed")
def test_xpass():
    assert True
