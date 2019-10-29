import asyncio
import dataclasses
import importlib
import io
import json
import logging
import re
import sys
import time
from unittest.mock import Mock

import asynctest
import pytest
import pytest_mock

import kopf
from kopf.config import configure
from kopf.engines.logging import ObjectPrefixingFormatter
from kopf.structs.credentials import Vault, VaultKey, ConnectionInfo
from kopf.structs.resources import Resource


def pytest_configure(config):
    config.addinivalue_line('markers', "e2e: end-to-end tests with real operators.")
    config.addinivalue_line('markers', "resource_clustered: (internal parameterizatiom mark).")


def pytest_addoption(parser):
    parser.addoption("--only-e2e", action="store_true", help="Execute end-to-end tests only.")
    parser.addoption("--with-e2e", action="store_true", help="Include end-to-end tests.")


# This logic is not applied if pytest is started explicitly on ./examples/.
# In that case, regular pytest behaviour applies -- this is intended.
def pytest_collection_modifyitems(config, items):

    # Make all tests in this directory and below asyncio-compatible by default.
    for item in items:
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker('asyncio')

    # Put all e2e tests to the end, as they are assumed to be slow.
    def _is_e2e(item):
        path = item.location[0]
        return path.startswith('tests/e2e/') or path.startswith('examples/')
    etc = [item for item in items if not _is_e2e(item)]
    e2e = [item for item in items if _is_e2e(item)]

    # Mark all e2e tests, no matter how they were detected. Just for filtering.
    mark_e2e = pytest.mark.e2e
    for item in e2e:
        item.add_marker(mark_e2e)

    # Minikube tests are heavy and require a cluster. Skip them by default,
    # so that the contributors can run pytest without initial tweaks.
    mark_skip = pytest.mark.skip(reason="E2E tests are not enabled. "
                                        "Use --with-e2e/--only-e2e to enable.")
    if not config.getoption('--with-e2e') and not config.getoption('--only-e2e'):
        for item in e2e:
            item.add_marker(mark_skip)

    # Minify the test-plan if only e2e are requested (all other should be skipped).
    if config.getoption('--only-e2e'):
        items[:] = e2e
    else:
        items[:] = etc + e2e


# Substitute the regular mock with the async-aware mock in the `mocker` fixture.
@pytest.fixture(scope='session', autouse=True)
def enforce_asyncio_mocker():
    pytest_mock.plugin._get_mock_module = lambda config: asynctest
    pytest_mock._get_mock_module = pytest_mock.plugin._get_mock_module


@pytest.fixture()
def resource():
    """ The resource used in the tests. Usually mocked, so it does not matter. """
    return Resource('zalando.org', 'v1', 'kopfexamples')


#
# Mocks for Kopf's internal but global variables.
#


@pytest.fixture(autouse=True)
def clear_default_registry():
    """
    Ensure that the tests have a fresh new global (not re-used) registry.
    """
    old_registry = kopf.get_default_registry()
    new_registry = type(old_registry)()  # i.e. OperatorRegistry/GlobalRegistry
    kopf.set_default_registry(new_registry)
    try:
        yield new_registry
    finally:
        kopf.set_default_registry(old_registry)


#
# Mocks for Kubernetes API clients (any of them). Reasons:
# 1. We do not test the clients, we test the layers on top of them,
#    so everything low-level should be mocked and assumed to be functional.
# 2. No external calls must be made under any circumstances.
#    The unit-tests must be fully isolated from the environment.
#

@pytest.fixture()
def req_mock(mocker, resource, request, fake_vault):

    # Pykube config is needed to create a pykube's API instance.
    # But we do not want and do not need to actually authenticate, so we mock.
    # Some fields are used by pykube's objects: we have to know them ("leaky abstractions").
    cfg_mock = mocker.patch('kopf.clients.auth.get_pykube_cfg').return_value
    cfg_mock.cluster = {'server': 'localhost'}
    cfg_mock.namespace = 'default'

    # Simulated list of cluster-defined CRDs: all of them at once. See: `resource` fixture(s).
    # Simulate the resource as cluster-scoped is there is a marker on the test.
    namespaced = not any(marker.name == 'resource_clustered' for marker in request.node.own_markers)
    res_mock = mocker.patch('pykube.http.HTTPClient.resource_list')
    res_mock.return_value = {'resources': [
        {'name': 'kopfexamples', 'kind': 'KopfExample', 'namespaced': namespaced},
    ]}

    # Prevent ANY outer requests, no matter what. These ones are usually asserted.
    req_mock = mocker.patch('requests.Session').return_value
    return req_mock


@pytest.fixture()
def stream(req_mock, fake_vault):
    """ A mock for the stream of events as if returned by K8s client. """
    def feed(*args):
        side_effect = []
        for arg in args:
            if isinstance(arg, (list, tuple)):
                arg = iter(json.dumps(event).encode('utf-8') for event in arg)
            side_effect.append(arg)
        req_mock.get.return_value.iter_lines.side_effect = side_effect
    return Mock(spec_set=['feed'], feed=feed)

#
# Mocks for login & checks. Used in specifialised login tests,
# and in all CLI tests (since login is implicit with CLI commands).
#

@pytest.fixture()
def hostname():
    """ A fake hostname to be used in all aiohttp/aresponses tests. """
    return 'fake-host'


@dataclasses.dataclass(frozen=True, eq=False, order=False)
class LoginMocks:
    pykube_in_cluster: Mock = None
    pykube_from_file: Mock = None
    client_in_cluster: Mock = None
    client_from_file: Mock = None


@pytest.fixture()
def login_mocks(mocker):

    # Pykube config is needed to create a pykube's API instance.
    # But we do not want and do not need to actually authenticate, so we mock.
    # Some fields are used by pykube's objects: we have to know them ("leaky abstractions").
    cfg_mock = mocker.patch('kopf.clients.auth.get_pykube_cfg').return_value
    cfg_mock.cluster = {'server': 'localhost'}
    cfg_mock.namespace = 'default'

    # Make all client libraries potentially optional, but do not skip the tests:
    # skipping the tests is the tests' decision, not this mocking fixture's one.
    kwargs = {}
    try:
        import pykube
    except ImportError:
        pass
    else:
        kwargs.update(
            pykube_in_cluster=mocker.patch.object(pykube.KubeConfig, 'from_service_account'),
            pykube_from_file=mocker.patch.object(pykube.KubeConfig, 'from_file'),
        )
    try:
        import kubernetes
    except ImportError:
        pass
    else:
        kwargs.update(
            client_in_cluster=mocker.patch.object(kubernetes.config, 'load_incluster_config'),
            client_from_file=mocker.patch.object(kubernetes.config, 'load_kube_config'),
        )
    return LoginMocks(**kwargs)


@pytest.fixture(autouse=True)
def clean_kubernetes_client():
    try:
        import kubernetes
    except ImportError:
        pass  # absent client is already "clean" (or not "dirty" at least).
    else:
        kubernetes.client.configuration.Configuration.set_default(None)


@pytest.fixture()
def fake_vault(event_loop, mocker, hostname):
    """
    Provide a freshly created and populated authentication vault for every test.

    Most of the tests expect some credentials to be at least provided
    (even if not used). So, we create and set the vault as if every coroutine
    is invoked from the central `operator` method (where it is set normally).

    Any blocking activities are mocked, so that the tests do not hang.
    """
    from kopf.clients import auth

    key = VaultKey('fixture')
    info = ConnectionInfo(server=f'https://{hostname}')
    vault = Vault({key: info}, loop=event_loop)
    token = auth.vault_var.set(vault)
    mocker.patch.object(vault.readiness, 'wait')
    mocker.patch.object(vault.emptiness, 'wait')
    try:
        yield vault
    finally:
        auth.vault_var.reset(token)

#
# Simulating that Kubernetes client libraries are not installed.
#

def _with_module_present(name: str):
    # If the module is required, it should either be installed,
    # or skip the test: we cannot simulate its presence (unlike its absence).
    yield pytest.importorskip(name)


def _with_module_absent(name: str):

    class ProhibitedImportFinder:
        def find_spec(self, fullname, path, target=None):
            if fullname.split('.')[0] == name:
                raise ImportError("Import is prohibited for tests.")

    try:
        mod_before = importlib.import_module(name)
    except ImportError:
        yield
        return  # nothing to patch & restore.

    # Remove any cached modules.
    preserved = {}
    for fullname, mod in list(sys.modules.items()):
        if fullname.split('.')[0] == name:
            preserved[fullname] = mod
            del sys.modules[fullname]

    # Inject the prohibition for loading this module. And restore when done.
    finder = ProhibitedImportFinder()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(preserved)

        # Verify if it works and that we didn't break the importing machinery.
        mod_after = importlib.import_module(name)
        assert mod_after is mod_before


@pytest.fixture(params=[True], ids=['with-client'])  # for hinting suffixes
def kubernetes():
    yield from _with_module_present('kubernetes')


@pytest.fixture(params=[False], ids=['no-client'])  # for hinting suffixes
def no_kubernetes():
    yield from _with_module_absent('kubernetes')


@pytest.fixture(params=[False, True], ids=['no-client', 'with-client'])
def any_kubernetes(request):
    if request.param:
        yield from _with_module_present('kubernetes')
    else:
        yield from _with_module_absent('kubernetes')


@pytest.fixture(params=[True], ids=['with-pykube'])  # for hinting suffixes
def pykube():
    yield from _with_module_present('pykube')


@pytest.fixture(params=[False], ids=['no-pykube'])  # for hinting suffixes
def no_pykube():
    yield from _with_module_absent('pykube')


@pytest.fixture(params=[False, True], ids=['no-pykube', 'with-pykube'])
def any_pykube(request):
    if request.param:
        yield from _with_module_present('pykube')
    else:
        yield from _with_module_absent('pykube')

#
# Helpers for the timing checks.
#

@pytest.fixture()
def timer():
    return Timer()


class Timer(object):
    """
    A helper context manager to measure the time of the code-blocks.
    Also, supports direct comparison with time-deltas and the numbers of seconds.

    Usage:

        with Timer() as timer:
            do_something()
            print(f"Executing for {timer.seconds}s already.")
            do_something_else()

        print(f"Executed in {timer.seconds}s.")
        assert timer < 5.0
    """

    def __init__(self):
        super().__init__()
        self._ts = None
        self._te = None

    @property
    def seconds(self):
        if self._ts is None:
            return None
        elif self._te is None:
            return time.perf_counter() - self._ts
        else:
            return self._te - self._ts

    def __repr__(self):
        status = 'new' if self._ts is None else 'running' if self._te is None else 'finished'
        return f'<Timer: {self.seconds}s ({status})>'

    def __enter__(self):
        self._ts = time.perf_counter()
        self._te = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._te = time.perf_counter()

    def __int__(self):
        return int(self.seconds)

    def __float__(self):
        return float(self.seconds)

#
# Helpers for the logging checks.
#


@pytest.fixture()
def logstream(caplog):
    """ Prefixing is done at the final output. We have to intercept it. """

    logger = logging.getLogger()
    handlers = list(logger.handlers)

    configure(verbose=True)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    formatter = ObjectPrefixingFormatter('prefix %(message)s')
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            yield stream
    finally:
        logger.removeHandler(handler)
        logger.handlers[:] = handlers  # undo `configure()`


@pytest.fixture()
def assert_logs(caplog):
    """
    A function to assert the logs are present (by pattern).

    The listed message patterns MUST be present, in the order specified.
    Some other log messages can also be present, but they are ignored.
    """
    def assert_logs_fn(patterns, prohibited=[], strict=False):
        __traceback_hide__ = True
        remaining_patterns = list(patterns)
        for message in caplog.messages:
            # The expected pattern is at position 0.
            # Looking-ahead: if one of the following patterns matches, while the
            # 0th does not, then the log message is missing, and we fail the test.
            for idx, pattern in enumerate(remaining_patterns):
                m = re.search(pattern, message)
                if m:
                    if idx == 0:
                        remaining_patterns[:1] = []
                        break  # out of `remaining_patterns` cycle
                    else:
                        skipped_patterns = remaining_patterns[:idx]
                        raise AssertionError(f"Few patterns were skipped: {skipped_patterns!r}")
                elif strict:
                    raise AssertionError(f"Unexpected log message: {message!r}")

            # Check that the prohibited patterns do not appear in any message.
            for pattern in prohibited:
                m = re.search(pattern, message)
                if m:
                    raise AssertionError(f"Prohibited log pattern found: {message!r} ~ {pattern!r}")

        # If all patterns have been matched in order, we are done.
        # if some are left, but the messages are over, then we fail.
        if remaining_patterns:
            raise AssertionError(f"Few patterns were missed: {remaining_patterns!r}")

    return assert_logs_fn


#
# Helpers for asyncio checks.
#
@pytest.fixture(autouse=True)
def _no_asyncio_pending_tasks():
    """
    Ensure there are no unattended asyncio tasks after the test.

    It looks  both in the test's main event-loop, and in all other event-loops,
    such as the background thread of `KopfRunner` (used in e2e tests).

    Current solution uses some internals of asyncio, since there is no public
    interface for that. The warnings are printed only at the end of pytest.

    An alternative way: set event-loop's exception handler, force garbage
    collection after every test, and check messages from `asyncio.Task.__del__`.
    This, however, requires intercepting all event-loop creation in the code.
    """
    # See `asyncio.all_tasks()` implementation for reference.
    before = {t for t in list(asyncio.tasks._all_tasks) if not t.done()}
    yield
    after = {t for t in list(asyncio.tasks._all_tasks) if not t.done()}
    remains = after - before
    if remains:
        pytest.fail(f"Unattended asyncio tasks detected: {remains!r}")
