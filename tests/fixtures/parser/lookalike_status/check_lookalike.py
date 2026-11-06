"""IDs that contain the same words pytest uses for outcomes."""
import pytest


@pytest.mark.parametrize("label", ["x PASSED y", "x FAILED y", "ERROR"])
def test_label(label):
    assert "FAILED" not in label
