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

from decolocate_tables.models import (
    BACKUP_NAME_SUFFIX,
    POSTGRES_MAX_IDENTIFIER_BYTES,
    derive_backup_name,
    identifier_byte_length,
)


class TestDeriveBackupName(unittest.TestCase):
    def test_short_table_uses_canonical_suffix(self) -> None:
        self.assertEqual(derive_backup_name("orders"), f"orders{BACKUP_NAME_SUFFIX}")

    def test_exactly_63_bytes_canonical(self) -> None:
        table_name = "a" * 49
        name = derive_backup_name(table_name)
        self.assertEqual(name, f"{table_name}{BACKUP_NAME_SUFFIX}")
        self.assertEqual(identifier_byte_length(name), POSTGRES_MAX_IDENTIFIER_BYTES)

    def test_long_table_is_shortened(self) -> None:
        table_name = "a" * 50
        name = derive_backup_name(table_name)
        self.assertLessEqual(identifier_byte_length(name), POSTGRES_MAX_IDENTIFIER_BYTES)
        self.assertNotEqual(name, f"{table_name}{BACKUP_NAME_SUFFIX}")
        self.assertTrue(name.endswith(BACKUP_NAME_SUFFIX))
        self.assertIn("_", name)

    def test_deterministic(self) -> None:
        table_name = "very_long_enterprise_table_name_that_exceeds_limits"
        self.assertEqual(
            derive_backup_name(table_name),
            derive_backup_name(table_name),
        )

    def test_golden_shortened_name(self) -> None:
        table_name = "a" * 50
        self.assertEqual(
            derive_backup_name(table_name),
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_f6c45839_colocated_bak",
        )

    def test_distinct_long_tables(self) -> None:
        a = derive_backup_name("x" * 50 + "1")
        b = derive_backup_name("x" * 50 + "2")
        self.assertNotEqual(a, b)

    def test_utf8_table_name(self) -> None:
        table_name = "t\u00e9" * 30
        name = derive_backup_name(table_name)
        self.assertLessEqual(identifier_byte_length(name), POSTGRES_MAX_IDENTIFIER_BYTES)

    def test_mixed_case_preserved_in_canonical(self) -> None:
        self.assertEqual(
            derive_backup_name("FBNK_CURRENCY"),
            f"FBNK_CURRENCY{BACKUP_NAME_SUFFIX}",
        )


if __name__ == "__main__":
    unittest.main()
