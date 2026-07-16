import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.parent._compose_call_failed = report.failed
