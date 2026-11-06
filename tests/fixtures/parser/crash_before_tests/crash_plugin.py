"""Collection works, but the process dies before the first test runs."""
import os


def pytest_collection_finish(session):
    if not session.config.option.collectonly:
        os._exit(9)
