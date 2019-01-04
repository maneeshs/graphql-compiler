# Copyright 2018-present Kensho Technologies, LLC.
import sys
import time

import pytest
import six

from .test_data_tools.data_tool import (
    generate_orient_integration_data, generate_orient_snapshot_data, generate_sql_integration_data,
    init_sql_integration_test_backends, tear_down_integration_test_backends
)
from .test_data_tools.graph import get_test_graph
from .test_data_tools.schema import load_schema


# Pytest fixtures depend on name redefinitions to work,
# so this check generates tons of false-positives here.
# pylint: disable=redefined-outer-name


@pytest.fixture(scope='session')
def init_snapshot_graph_client():
    """Return a client for an initialized db, with all test data imported."""
    return _init_graph_client(load_schema, generate_orient_snapshot_data)


@pytest.fixture(scope='session')
def init_integration_graph_client():
    """Return a client for an initialized db, with all test data imported."""
    return _init_graph_client(load_schema, generate_orient_integration_data)


def _init_graph_client(load_schema_func, generate_data_func):
    graph_name = 'animals'

    # Try to set up the database for the test up to 20 times before giving up.
    set_up_successfully = False
    for _ in range(20):
        try:
            graph_client = get_test_graph(graph_name, load_schema_func, generate_data_func)
            set_up_successfully = True
            break
        except Exception as e:  # pylint: disable=broad-except
            sys.stderr.write(u'Failed to set up test DB: {}'.format(e))
            time.sleep(1)

    if not set_up_successfully:
        raise AssertionError(u'Failed to set up database without raising an exception!')

    return graph_client


@pytest.fixture(scope='class')
def graph_client(request, init_snapshot_graph_client):
    """Get a client for an initialized db, with all test data imported."""
    request.cls.graph_client = init_snapshot_graph_client


@pytest.fixture(scope='class')
def integration_graph_client(request, init_integration_graph_client):
    """Get a client for an initialized db, with all test data imported."""
    request.cls.graph_client = init_integration_graph_client


@pytest.fixture(scope='class')
def sql_integration_data(request):
    """Generate integration data for SQL backends."""
    # initialize each SQL backend, and open a transaction on each one.
    sql_test_backends = init_sql_integration_test_backends()
    # write fixture test data within the transaction
    generate_sql_integration_data(sql_test_backends)
    # make sql backends accessible within the test class
    request.cls.sql_test_backends = sql_test_backends
    # yield the fixture to allow testing class to run
    yield
    # tear down the fixture after the testing class runs all tests
    # including rolling back transaction to ensure all fixture data removed.
    tear_down_integration_test_backends(sql_test_backends)


@pytest.fixture(scope='function')
def sql_integration_test(request):
    """Open nested transaction before every test function, and rollback transaction afterwards."""
    sql_test_backends = request.cls.sql_test_backends
    test_transactions = []
    for sql_test_backend in six.itervalues(sql_test_backends):
        transaction = sql_test_backend.connection.begin_nested()
        test_transactions.append(transaction)
    yield
    for transaction in test_transactions:
        transaction.rollback()
