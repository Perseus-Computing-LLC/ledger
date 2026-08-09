from datetime import datetime, timezone

from ledger_agent.catalog import catalog_is_fresh, import_catalog


_NOW = datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp()


def test_import_catalog_maps_provider_and_token_rates():
    catalog = import_catalog({
        "anthropic/claude-test": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_read_input_token_cost": 0.0000003,
            "cache_creation_input_token_cost": 0.00000375,
        }
    }, imported_at="2026-07-17")
    price = catalog["models"]["anthropic"]["anthropic/claude-test"]
    assert price == {"input": 3.0, "output": 15.0,
                     "cache_read": 0.3, "cache_write": 3.75}
    assert catalog["as_of"] == "2026-07-17"


def test_catalog_freshness_rejects_future_invalid_and_old_dates():
    assert catalog_is_fresh("2026-07-10", 45, now=_NOW)
    assert not catalog_is_fresh("2026-01-01", 45, now=_NOW)
    assert not catalog_is_fresh("not-a-date", 45, now=_NOW)
    assert not catalog_is_fresh("2026-08-01", 45, now=_NOW)
