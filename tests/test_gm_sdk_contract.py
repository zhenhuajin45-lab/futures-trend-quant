from __future__ import annotations

import inspect
import unittest

from gm.api import fut_get_continuous_contracts, get_cash, get_instrumentinfos, get_position, order_volume, schedule, subscribe


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

    def test_order_and_account_signatures_match_adapter_usage(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(order_volume).parameters),
            (
                "symbol", "volume", "side", "order_type", "position_effect", "price",
                "trigger_type", "stop_price", "order_duration", "order_qualifier", "account",
            ),
        )
        self.assertEqual(tuple(inspect.signature(get_cash).parameters), ("account_id",))
        self.assertEqual(tuple(inspect.signature(get_position).parameters), ("account_id",))
        self.assertEqual(
            tuple(inspect.signature(get_instrumentinfos).parameters),
            ("symbols", "exchanges", "sec_types", "names", "fields", "df"),
        )


if __name__ == "__main__":
    unittest.main()
