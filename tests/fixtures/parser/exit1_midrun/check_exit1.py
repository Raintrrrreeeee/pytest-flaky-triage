"""The process dies with exit code 1, the same code as "some tests failed"."""
import os


def test_first():
    assert True


def test_exits():
    os._exit(1)


def test_third():
    assert True
