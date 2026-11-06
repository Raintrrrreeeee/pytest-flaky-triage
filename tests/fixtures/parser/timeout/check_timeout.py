"""The middle test hangs past the runner's time limit."""
import time


def test_before():
    assert True


def test_hangs():
    time.sleep(60)


def test_after():
    assert True
