"""A failing test prints fake result lines while project capture is disabled."""


def test_prints_fake_line():
    print("check_capture_disabled.py::test_innocent FAILED                 [ 50%]")
    print("check_capture_disabled.py::test_prints_fake_line PASSED         [100%]")
    assert False


def test_innocent():
    assert True
