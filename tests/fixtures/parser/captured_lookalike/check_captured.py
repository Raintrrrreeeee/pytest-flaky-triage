"""A failing test prints text that looks like another test's result line.

pytest shows captured output under FAILURES, after the real result lines.
"""


def test_prints_fake_line():
    print("check_captured.py::test_innocent FAILED                        [ 50%]")
    print("check_captured.py::test_prints_fake_line PASSED                [100%]")
    assert False


def test_innocent():
    assert True
