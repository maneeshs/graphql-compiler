# Copyright 2018-present Kensho Technologies, LLC.
from unittest import TestCase

import pytest
import six

from ..test_helpers import get_schema
from .integration_backend_config import MATCH_BACKENDS, SQL_BACKENDS
from .integration_test_helpers import (
    compile_and_run_match_query, compile_and_run_sql_query, sort_db_results
)


# The following test class uses several fixtures adding members that pylint
# does not recognize
# pylint: disable=no-member


# Store the typical fixtures required for an integration tests.
# Individual tests can supply the full @pytest.mark.usefixtures to override if necessary.
integration_fixture_decorator = pytest.mark.usefixtures(
    'integration_graph_client',
    'sql_integration_data',
    'sql_integration_test',
)


class IntegrationTests(TestCase):

    @classmethod
    def setUpClass(cls):
        """Initialize the test schema once for all tests, and disable max diff limits."""
        cls.maxDiff = None
        cls.schema = get_schema()

    def assertResultsEqual(self, expected_results, results):
        """Assert that two lists of DB results are equal, independent of order."""
        self.assertListEqual(sort_db_results(expected_results), sort_db_results(results))

    def assertAllResultsEqual(self, graphql_query, parameters, expected_results):
        """Assert that all DB backends return the expected results, independent of order."""
        backend_results = self.compile_and_run_query(graphql_query, parameters)
        for results in six.itervalues(backend_results):
            self.assertResultsEqual(expected_results, results)

    @classmethod
    def compile_and_run_query(cls, graphql_query, parameters):
        """Compiles and runs the graphql query with the supplied parameters against all backends.

        Args:
            graphql_query: str, GraphQL query string to run against every backend.
            parameters: Dict[str, Any], input parameters to the query.

        Returns:
            Dict[str, Dict], dictionary mapping the TestBackend to the results fetched from that
                             backend.
        """
        backend_to_results = {}
        for backend_name in SQL_BACKENDS:
            sql_test_backend = cls.sql_test_backends[backend_name]
            results = compile_and_run_sql_query(
                cls.schema, graphql_query, parameters, sql_test_backend)
            backend_to_results[backend_name] = results
        for backend_name in MATCH_BACKENDS:
            results = compile_and_run_match_query(
                cls.schema, graphql_query, parameters, cls.graph_client)
            backend_to_results[backend_name] = results
        return backend_to_results

    @integration_fixture_decorator
    def test_backends(self):
        graphql_query = '''
        {
            Animal {
                name @output(out_name: "animal_name")
            }
        }
        '''
        expected_results = [
            {'animal_name': 'Animal 1'},
            {'animal_name': 'Animal 2'},
            {'animal_name': 'Animal 3'},
            {'animal_name': 'Animal 4'},
        ]
        self.assertAllResultsEqual(graphql_query, {}, expected_results)

# pylint: enable=no-member
