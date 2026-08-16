#!/usr/bin/env python3
"""Deterministic NyayaVerifyBench harness for Ledger issue #251.

Run from the repository root with::

    python benchmark/nyaya_verify_bench.py

The generator intentionally uses no LLM or network calls.  It creates exactly
1,800 structured scenarios: four languages, six injected hallucination types
at 50 cases each, and 150 clean controls per language.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Make direct ``python benchmark/...`` execution work from any cwd while still
# keeping the harness itself stdlib-only.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger_agent.tool_receipts import (  # noqa: E402
    HALLUCINATION_TYPES,
    ToolReceiptLedger,
    build_tool_receipt,
    verify_response,
)

LANGUAGES = ("en", "es", "fr", "hi")
BENCHMARK_KEY_ID = "bench-key"
BENCHMARK_KEY = b"nyaya-verify-bench-key"
BASE_TIMESTAMP_MS = 1_700_000_000_000

_SENDERS = ("Alice", "Bob", "Carol", "Dev")
_SUBJECTS = ("Deadline update", "Budget review", "Project launch", "Meeting notes")
_URL_ROOT = "https://example.test/nyaya"

_TEMPLATES = {
    "en": {
        "direct": "{sender} sent {count} emails about {subject}.",
        "inference": "{sender} seems worried about the {subject}.",
        "absence": "No emails were found for this search.",
        "source": "According to the fetched article at {url}, the report is available.",
        "comparison": "The message is comparable to the {subject} message.",
    },
    "es": {
        "direct": "{sender} envió {count} correos sobre {subject}.",
        "inference": "{sender} parece preocupado por {subject}.",
        "absence": "No se encontraron correos para esta búsqueda.",
        "source": "Según el artículo descargado en {url}, el informe está disponible.",
        "comparison": "El mensaje es comparable al mensaje sobre {subject}.",
    },
    "fr": {
        "direct": "{sender} a envoyé {count} e-mails au sujet de {subject}.",
        "inference": "{sender} semble inquiet au sujet de {subject}.",
        "absence": "Aucun e-mail n'a été trouvé pour cette recherche.",
        "source": "Selon l'article récupéré à {url}, le rapport est disponible.",
        "comparison": "Le message est comparable au message sur {subject}.",
    },
    "hi": {
        "direct": "{sender} ने {subject} के बारे में {count} ईमेल भेजे।",
        "inference": "ऐसा लगता है कि {sender} {subject} को लेकर चिंतित हैं।",
        "absence": "इस खोज के लिए कोई ईमेल नहीं मिला।",
        "source": "{url} पर प्राप्त लेख के अनुसार रिपोर्ट उपलब्ध है।",
        "comparison": "यह संदेश {subject} वाले संदेश के समान है।",
    },
}


def _email_receipt(
    *, language: str, case_index: int, sender: str, subject: str, count: int
) -> dict[str, Any]:
    output = json.dumps(
        {"sender": sender, "subject": subject, "count": count},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return build_tool_receipt(
        tool_name="email_search",
        input_params={"query": sender, "language": language},
        raw_output=output,
        result_count=count,
        facts={"sender": sender, "subject": subject, "count": count},
        duration_ms=8 + (case_index % 17),
        key_id=BENCHMARK_KEY_ID,
        key=BENCHMARK_KEY,
        timestamp_ms=BASE_TIMESTAMP_MS + case_index,
        id=f"{language}-{case_index:04d}-email",
    )


def _web_receipt(*, language: str, case_index: int, url: str) -> dict[str, Any]:
    output = json.dumps(
        {"body": "A deterministic benchmark article.", "url": url},
        sort_keys=True,
        separators=(",", ":"),
    )
    return build_tool_receipt(
        tool_name="web_fetch",
        input_params={"url": url, "language": language},
        raw_output=output,
        result_count=1,
        facts={"source_url": url, "fetched_urls": [url]},
        duration_ms=11 + (case_index % 13),
        key_id=BENCHMARK_KEY_ID,
        key=BENCHMARK_KEY,
        timestamp_ms=BASE_TIMESTAMP_MS + case_index,
        id=f"{language}-{case_index:04d}-web",
    )


def _claim_for_type(
    *,
    language: str,
    kind: str,
    email: dict[str, Any],
    web: dict[str, Any],
    sender: str,
    subject: str,
    count: int,
    clean_variant: int | None = None,
) -> dict[str, Any]:
    templates = _TEMPLATES[language]
    direct_text = templates["direct"].format(sender=sender, subject=subject, count=count)
    if kind == "fabricated_call":
        return {
            "text": direct_text,
            "pramana": "pratyaksha",
            "receipt_id": f"{email['id']}-missing",
            "expected_count": count,
            "expected_facts": {"sender": sender},
        }
    if kind == "count_mismatch":
        return {
            "text": templates["direct"].format(sender=sender, subject=subject, count=count + 1),
            "pramana": "pratyaksha",
            "receipt_id": email["id"],
            "expected_count": count + 1,
        }
    if kind == "fact_mismatch":
        wrong_sender = "Mallory" if sender != "Mallory" else "Eve"
        return {
            "text": templates["direct"].format(sender=wrong_sender, subject=subject, count=count),
            "pramana": "pratyaksha",
            "receipt_id": email["id"],
            "expected_count": count,
            "expected_facts": {"sender": wrong_sender},
        }
    if kind == "inference_as_fact":
        return {
            "text": templates["inference"].format(sender=sender, subject=subject),
            "pramana": "pratyaksha",
            "receipt_id": email["id"],
            "premise_facts": ["sender", "subject"],
        }
    if kind == "false_absence":
        return {
            "text": templates["absence"],
            "pramana": "abhava",
            "receipt_id": email["id"],
        }
    if kind == "source_fabrication":
        missing_url = f"{_URL_ROOT}/{language}/{web['id']}/never-fetched"
        return {
            "text": templates["source"].format(url=missing_url),
            "pramana": "shabda",
            "receipt_id": web["id"],
            "cited_source_url": missing_url,
        }

    # Clean controls rotate across all six epistemic paths that can be
    # grounded.  The fifth variant uses an empty email receipt for abhava.
    assert kind == "clean"
    variant = 0 if clean_variant is None else clean_variant
    if variant % 5 == 0:
        return {
            "text": direct_text,
            "pramana": "pratyaksha",
            "receipt_id": email["id"],
            "expected_count": count,
            "expected_facts": {"sender": sender, "subject": subject},
        }
    if variant % 5 == 1:
        return {
            "text": templates["inference"].format(sender=sender, subject=subject),
            "pramana": "anumana",
            "receipt_id": email["id"],
            "premise_facts": [{"sender": sender}, {"subject": subject}],
        }
    if variant % 5 == 2:
        return {
            "text": templates["comparison"].format(subject=subject),
            "pramana": "upamana",
            "receipt_id": email["id"],
            "premise_facts": ["sender", "subject"],
        }
    if variant % 5 == 3:
        return {
            "text": templates["source"].format(url=web["facts"]["source_url"]),
            "pramana": "shabda",
            "receipt_id": web["id"],
            "cited_source_url": web["facts"]["source_url"],
        }
    return {
        "text": templates["absence"],
        "pramana": "abhava",
        "receipt_id": email["id"],
    }


def generate_benchmark_scenarios(seed: int = 251) -> list[dict[str, Any]]:
    """Generate the exact, JSON-serializable 1,800-case benchmark corpus."""
    rng = random.Random(seed)
    scenarios: list[dict[str, Any]] = []
    case_index = 0

    for language in LANGUAGES:
        for kind in HALLUCINATION_TYPES:
            for _ in range(50):
                sender = rng.choice(_SENDERS)
                subject = rng.choice(_SUBJECTS)
                count = rng.randint(1, 5)
                email = _email_receipt(
                    language=language,
                    case_index=case_index,
                    sender=sender,
                    subject=subject,
                    count=count,
                )
                url = f"{_URL_ROOT}/{language}/{case_index:04d}"
                web = _web_receipt(language=language, case_index=case_index, url=url)
                claim = _claim_for_type(
                    language=language,
                    kind=kind,
                    email=email,
                    web=web,
                    sender=sender,
                    subject=subject,
                    count=count,
                )
                scenarios.append(
                    {
                        "scenario_id": f"{language}-{case_index:04d}",
                        "language": language,
                        "hallucination_type": kind,
                        "receipts": [email, web],
                        "claims": [claim],
                        "ground_truth": {"status": "flagged", "hallucination_type": kind},
                    }
                )
                case_index += 1

        for clean_index in range(150):
            sender = rng.choice(_SENDERS)
            subject = rng.choice(_SUBJECTS)
            count = rng.randint(1, 5)
            # Every fifth control is a true absence claim.  Its receipt is
            # empty while all other controls use the non-empty email receipt.
            email_count = 0 if clean_index % 5 == 4 else count
            email = _email_receipt(
                language=language,
                case_index=case_index,
                sender=sender,
                subject=subject,
                count=email_count,
            )
            url = f"{_URL_ROOT}/{language}/{case_index:04d}"
            web = _web_receipt(language=language, case_index=case_index, url=url)
            claim = _claim_for_type(
                language=language,
                kind="clean",
                email=email,
                web=web,
                sender=sender,
                subject=subject,
                count=email_count,
                clean_variant=clean_index,
            )
            scenarios.append(
                {
                    "scenario_id": f"{language}-{case_index:04d}",
                    "language": language,
                    "hallucination_type": "clean",
                    "receipts": [email, web],
                    "claims": [claim],
                    "ground_truth": {"status": "verified", "hallucination_type": None},
                }
            )
            case_index += 1

    assert len(scenarios) == 1800
    return scenarios


def run_benchmark(seed: int = 251) -> dict[str, Any]:
    """Run the deterministic verifier and return metrics for the CLI/tests."""
    scenarios = generate_benchmark_scenarios(seed=seed)
    detections: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    clean_flagged = 0
    clean_total = 0
    response_times_ns: list[int] = []
    claim_times_ns: list[int] = []

    for scenario in scenarios:
        ledger = ToolReceiptLedger(key_registry={BENCHMARK_KEY_ID: BENCHMARK_KEY})
        for receipt in scenario["receipts"]:
            ledger.register(receipt)
        started = time.perf_counter_ns()
        result = verify_response(scenario["claims"], ledger)
        elapsed = time.perf_counter_ns() - started
        response_times_ns.append(elapsed)
        claim_times_ns.extend([elapsed // max(1, len(scenario["claims"]))] * len(scenario["claims"]))

        kind = scenario["hallucination_type"]
        verdicts = result["claims"]
        if kind == "clean":
            clean_total += 1
            if any(verdict["status"] == "flagged" for verdict in verdicts):
                clean_flagged += 1
        else:
            totals[kind] += 1
            if any(verdict["status"] == "flagged" for verdict in verdicts):
                detections[kind] += 1

    per_type = {
        kind: {
            "detected": detections[kind],
            "total": totals[kind],
            "rate": detections[kind] / totals[kind] if totals[kind] else 0.0,
        }
        for kind in HALLUCINATION_TYPES
    }
    total_ns = sum(response_times_ns)
    fabricated_rate = per_type["fabricated_call"]["rate"]
    return {
        "seed": seed,
        "scenarios": len(scenarios),
        "per_type": per_type,
        "fabricated_tool_reference_detection_rate": fabricated_rate,
        "false_positive_rate": clean_flagged / clean_total if clean_total else 0.0,
        "clean_flagged": clean_flagged,
        "clean_total": clean_total,
        "total_verify_ms": total_ns / 1_000_000,
        "mean_response_ms": total_ns / max(1, len(response_times_ns)) / 1_000_000,
        "median_response_ms": statistics.median(response_times_ns) / 1_000_000,
        "median_claim_ms": statistics.median(claim_times_ns) / 1_000_000,
    }


def _pct(value: float) -> str:
    return f"{value * 100:6.2f}%"


def print_report(metrics: dict[str, Any]) -> None:
    print(f"NyayaVerifyBench seed={metrics['seed']} scenarios={metrics['scenarios']}")
    print("\nHallucination type                         Detected/total   Detection rate")
    print("-----------------------------------------  ---------------  --------------")
    for kind, values in metrics["per_type"].items():
        print(f"{kind:41s}  {values['detected']:7d}/{values['total']:<7d}  {_pct(values['rate'])}")
    print(
        "\nOVERALL fabricated-tool-reference detection rate: "
        f"{_pct(metrics['fabricated_tool_reference_detection_rate'])}"
    )
    print(
        "False-positive rate on clean claims: "
        f"{metrics['clean_flagged']}/{metrics['clean_total']} ({_pct(metrics['false_positive_rate'])})"
    )
    print(
        "Verification overhead: "
        f"{metrics['mean_response_ms']:.4f} ms/response average; "
        f"{metrics['median_response_ms']:.4f} ms/response median; "
        f"{metrics['median_claim_ms']:.4f} ms/claim median"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=251)
    args = parser.parse_args(argv)
    metrics = run_benchmark(seed=args.seed)
    print_report(metrics)
    if metrics["fabricated_tool_reference_detection_rate"] < 0.90:
        return 2
    if metrics["median_response_ms"] > 20.0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
