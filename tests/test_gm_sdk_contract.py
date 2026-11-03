from __future__ import annotations

import inspect
import unittest

from gm.api import fut_get_continuous_contracts, schedule, subscribe


class GmSdkContractTest(unittest.TestCase):
    def test_subscribe_signature_matches_adapter_usage(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(subscribe).parameters),
            (
                "symbols",
                "frequency",
                "count",
                "wait_group",
                "wait_group_timeout",
                "unsubscribe_previous",
                "fields",
                "format",
            ),
        )

    def test_continuous_contract_signature_matches_adapter_usage(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(fut_get_continuous_contracts).parameters),
            ("csymbol", "start_date", "end_date"),
        )

    def test_schedule_signature_matches_adapter_usage(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(schedule).parameters),
            ("schedule_func", "date_rule", "time_rule"),
        )


if __name__ == "__main__":
    unittest.main()
