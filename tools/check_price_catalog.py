#!/usr/bin/env python3
"""Fail CI when an imported price catalog is older than the allowed vintage."""
import argparse

from plutus_agent.catalog import DEFAULT_MAX_AGE_DAYS, catalog_is_fresh, load_catalog

p = argparse.ArgumentParser()
p.add_argument("catalog")
p.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
args = p.parse_args()
catalog = load_catalog(args.catalog)
as_of = catalog.get("as_of", "")
if not catalog_is_fresh(as_of, args.max_age_days):
    raise SystemExit(f"price catalog is stale or invalid: as_of={as_of!r}, max_age_days={args.max_age_days}")
print(f"price catalog fresh: as_of={as_of}")
