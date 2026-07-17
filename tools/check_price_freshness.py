#!/usr/bin/env python3
"""CI guard for the checked-in fallback price table vintage."""
from plutus_agent.catalog import DEFAULT_MAX_AGE_DAYS, catalog_is_fresh
from plutus_agent.pricing import PRICE_TABLE_AS_OF

if not catalog_is_fresh(PRICE_TABLE_AS_OF, DEFAULT_MAX_AGE_DAYS):
    raise SystemExit(
        f"checked-in price table is stale: as_of={PRICE_TABLE_AS_OF}, "
        f"max_age_days={DEFAULT_MAX_AGE_DAYS}"
    )
print(f"checked-in price table fresh: as_of={PRICE_TABLE_AS_OF}")
