"""Perseus Ledger — verifiable provenance for autonomous systems.

Perseus Ledger records usage and agent activity as append-only, hash-chained
evidence. Each record can carry an actor, workspace boundary, model/provider,
task, resource allocation, and external references so an organization can prove
what happened and independently verify the event history.

The stable ``ledger_agent`` package, ``ledger`` CLI, database paths, and ``/v1``
API are the canonical interfaces. Stripe billing is an
optional settlement adapter; it is not the product boundary.
"""

__version__ = "1.1.2"
# The frozen `/v1` HTTP API contract version (SemVer), tracked in `openapi.yaml`.
# Intentionally INDEPENDENT of `__version__`: the package ships fixes/features on
# its own cadence, but the wire contract only bumps on an actual `/v1` change
# (additive = minor, breaking = major). `test_version_single_source.py` pins
# `openapi.yaml` to this value so the two can never silently drift.
__api_version__ = "1.0.0"
__product__ = "Perseus Ledger"
__tagline__ = "Verifiable provenance for autonomous systems."
__company__ = "Perseus Computing LLC"
__homepage__ = "https://perseus.observer/ledger/"
__default_port__ = 8420

__all__ = [
    "__version__",
    "__api_version__",
    "__product__",
    "__tagline__",
    "__default_port__",
    "Meter",
]


def __getattr__(name):
    # Lazy export so `from ledger_agent import Meter` works without importing
    # the world at package-load time.
    if name == "Meter":
        from .client import Meter
        return Meter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
