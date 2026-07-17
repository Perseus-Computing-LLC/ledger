#!/usr/bin/env python3
"""Refresh a Plutus override catalog from LiteLLM.

Usage: python tools/update_price_catalog.py [output.json]
"""
from pathlib import Path
import sys

from plutus_agent.catalog import fetch_catalog, write_catalog

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pricing/catalog.json")
catalog = fetch_catalog()
write_catalog(catalog, out)
print(f"wrote {len(catalog['models'])} providers to {out} (as_of={catalog['as_of']})")
