"""Keep the two independently deployed services' `app` packages isolated."""

from contextlib import contextmanager
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
AGENT_TESTS = {"test_daily_ops_parser.py", "test_daily_scheduler.py"}
SERVICE_MODULES = {"api": {}, "agent": {}}


def service_for(path):
    path = Path(str(path))
    return "agent" if "agent" in path.parts or path.name in AGENT_TESTS else "api"


def app_modules():
    return {name: module for name, module in sys.modules.items() if name == "app" or name.startswith("app.")}


@contextmanager
def service_imports(service):
    previous_modules = app_modules()
    previous_path = sys.path[:]
    for name in previous_modules:
        del sys.modules[name]
    sys.modules.update(SERVICE_MODULES[service])
    sys.path.insert(0, str(ROOT / "services" / service))
    try:
        yield
    finally:
        current_modules = app_modules()
        SERVICE_MODULES[service].update(current_modules)
        for name in current_modules:
            del sys.modules[name]
        sys.modules.update(previous_modules)
        sys.path[:] = previous_path


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if isinstance(collector, pytest.Module):
        with service_imports(service_for(collector.path)):
            yield
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    with service_imports(service_for(item.path)):
        yield
