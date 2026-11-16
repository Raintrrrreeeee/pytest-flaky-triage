import os


def setup_module():
    os._exit(9)


def test_would_pass():
    assert True

