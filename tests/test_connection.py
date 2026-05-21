# Copyright (c) YugabyteDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied.  See the License for the specific language governing permissions and limitations
# under the License.

from __future__ import annotations

import unittest
from unittest import mock

from decolocate_tables.connection import (
    YbServer,
    append_ssl_to_dsn,
    clear_odyssey_pooled_prepares,
    libpq_ssl_env,
    resolve_ssl_options,
    server_for_bucket,
)


class TestResolveSslOptions(unittest.TestCase):
    def test_cli_wins_over_env_for_same_key(self):
        out = resolve_ssl_options(
            {"sslmode": "require", "sslcert": None, "sslkey": None,
             "sslrootcert": None, "sslcrl": None},
            env={"PGSSLMODE": "prefer", "PGSSLCERT": "/env.crt",
                 "PGSSLKEY": "", "PGSSLROOTCERT": "", "PGSSLCRL": ""},
        )
        self.assertEqual(
            out,
            {"sslmode": "require", "sslcert": "/env.crt"},
        )

    def test_env_fallback_when_cli_unset(self):
        out = resolve_ssl_options(
            {"sslmode": None, "sslcert": None, "sslkey": None,
             "sslrootcert": "/ca.pem", "sslcrl": None},
            env={"PGSSLMODE": "verify-ca", "PGSSLCERT": "", "PGSSLKEY": "",
                 "PGSSLROOTCERT": "/ca.pem", "PGSSLCRL": ""},
        )
        self.assertEqual(out, {"sslmode": "verify-ca", "sslrootcert": "/ca.pem"})

    def test_omit_when_neither_set(self):
        out = resolve_ssl_options(
            {"sslmode": None, "sslcert": None, "sslkey": None,
             "sslrootcert": None, "sslcrl": None},
            env={},
        )
        self.assertEqual(out, {})


class TestAppendSslToDsn(unittest.TestCase):
    def test_appends_subset_of_keys(self):
        dsn = append_ssl_to_dsn(
            "host=h port=5433 dbname=d user=u",
            {"sslmode": "require", "sslrootcert": "/ca.pem"},
        )
        self.assertIn("sslmode=require", dsn)
        self.assertIn("sslrootcert=/ca.pem", dsn)

    def test_quotes_paths_with_spaces(self):
        dsn = append_ssl_to_dsn(
            "host=h port=5433 dbname=d user=u",
            {"sslrootcert": "/path/with spaces/ca.pem"},
        )
        self.assertIn('sslrootcert="/path/with spaces/ca.pem"', dsn)

    def test_unchanged_when_no_ssl(self):
        base = "host=h port=5433 dbname=d user=u"
        self.assertEqual(append_ssl_to_dsn(base, {}), base)


class TestLibpqSslEnv(unittest.TestCase):
    def test_sets_pgssl_vars(self):
        env = libpq_ssl_env(
            {"sslmode": "require", "sslrootcert": "/ca.pem"},
            {},
        )
        self.assertEqual(env["PGSSLMODE"], "require")
        self.assertEqual(env["PGSSLROOTCERT"], "/ca.pem")
        self.assertNotIn("PGSSLCERT", env)

    def test_empty_conninfo_unchanged(self):
        base = {"HOME": "/tmp"}
        env = libpq_ssl_env({}, base)
        self.assertEqual(env, base)


class TestClearOdysseyPooledPrepares(unittest.TestCase):
    @mock.patch("decolocate_tables.connection.connect_psycopg")
    def test_deallocate_all_on_each_attempt(self, mock_connect) -> None:
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.__enter__ = mock.MagicMock(return_value=conn)
        conn.__exit__ = mock.MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = mock.MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_connect.return_value = conn

        conninfo = {
            "host": "pool.example.com",
            "port": 5433,
            "dbname": "db",
            "user": "u",
        }
        clear_odyssey_pooled_prepares(conninfo, attempts=2)

        self.assertEqual(mock_connect.call_count, 2)
        self.assertEqual(cur.execute.call_count, 2)
        cur.execute.assert_called_with("DEALLOCATE ALL")
        self.assertEqual(conn.close.call_count, 2)


class TestServerForBucket(unittest.TestCase):
    def setUp(self) -> None:
        self.servers = [
            YbServer("10.0.0.1", 5433),
            YbServer("10.0.0.2", 5433),
            YbServer("10.0.0.3", 5433),
        ]

    def test_src_round_robin(self):
        self.assertEqual(server_for_bucket(self.servers, 0, role="src").host, "10.0.0.1")
        self.assertEqual(server_for_bucket(self.servers, 1, role="src").host, "10.0.0.2")
        self.assertEqual(server_for_bucket(self.servers, 3, role="src").host, "10.0.0.1")

    def test_dst_uses_next_node_when_multiple(self):
        self.assertEqual(server_for_bucket(self.servers, 0, role="dst").host, "10.0.0.2")
        self.assertEqual(server_for_bucket(self.servers, 2, role="dst").host, "10.0.0.1")

    def test_single_node_src_and_dst_same(self):
        one = [YbServer("127.0.0.1", 5433)]
        self.assertEqual(server_for_bucket(one, 0, role="src"), one[0])
        self.assertEqual(server_for_bucket(one, 0, role="dst"), one[0])


if __name__ == "__main__":
    unittest.main()
