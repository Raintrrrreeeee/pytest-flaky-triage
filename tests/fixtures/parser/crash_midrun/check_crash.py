"""The interpreter dies part-way through the run (e.g. a segfault)."""
import os


def test_first():
    assert True


def test_kills_process():
    os._exit(9)


def test_third():
    assert True
