#!/usr/bin/env python3
"""Daily pull of Vast.ai H100 SXM asks for the interconnect premium proxy.

Filter spec v2 (versioned before the first valid observation):
  Universe:    Vast.ai public search API, rentable asks, H100 SXM 80GB,
               verification status "verified" only.
  Clustered:   whole-machine offers, num_gpus >= 8, gpu_frac >= 0.99,
               NVLink bandwidth reported above zero.
  Standalone:  single-GPU offers, num_gpus = 1.
  Price:       ask per GPU-hour (dph_total / num_gpus).
  Observation: collapse qualifying offers to one within-machine median
               per side using Vast.ai machine_id.
  Trim:        within each side, drop machine observations outside 0.5x
               to 2.0x of the side's raw median, then take the median.
  Validity:    a day is valid only if both sides retain at least
               MIN_COUNT machines after the trim. Invalid days are stored
               with their counts and a reason, never silently dropped.
  Storage:     daily aggregates and filter counts only. Individual
               listings and machine identifiers are never republished.
"""

import json
import math
import statistics
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

API = "https://console.vast.ai/api/v0/bundles"
DATA = Path(__file__).resolve().parent.parent / "data" / "premium.json"
METHODOLOGY_VERSION = "v2"
SCHEMA_VERSION = 2
QUERY_LIMIT = 1000
MIN_COUNT = 5
TRIM_LO, TRIM_HI = 0.5, 2.0
GPU_NAME = "H100 SXM"
GPU_RAM_MIN, GPU_RAM_MAX = 75_000, 90_000


class CollectionError(RuntimeError):
    """The API response cannot safely be treated as a market observation."""


def validate_response(payload):
    if not isinstance(payload, dict):
        raise CollectionError("Vast.ai response must be a JSON object")
    if "offers" not in payload:
        raise CollectionError("Vast.ai response is missing the offers field")
    offers = payload["offers"]
    if not isinstance(offers, list):
        raise CollectionError("Vast.ai offers field must be a list")
    if not offers:
        raise CollectionError("Vast.ai returned no offers; refusing to record an outage as a market day")
    if len(offers) >= QUERY_LIMIT:
        raise CollectionError(
            f"Vast.ai returned {len(offers)} offers at the {QUERY_LIMIT}-offer limit; "
            "refusing a potentially truncated observation"
        )
    return offers


def fetch_offers():
    q = {
        "gpu_name": {"eq": GPU_NAME},
        "rentable": {"eq": True},
        "type": "ask",
        "limit": QUERY_LIMIT,
    }
    url = API + "?q=" + urllib.parse.quote(json.dumps(q))
    req = urllib.request.Request(url, headers={"User-Agent": "interconnect-premium-monitor"})
    with urllib.request.urlopen(req, timeout=60) as r:
        try:
            payload = json.loads(r.read())
        except json.JSONDecodeError as exc:
            raise CollectionError("Vast.ai response was not valid JSON") from exc
    return validate_response(payload)


def finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def schema_valid_offer(offer):
    if not isinstance(offer, dict):
        return False
    num_gpus = offer.get("num_gpus")
    machine_id = offer.get("machine_id")
    return (
        finite_number(num_gpus)
        and num_gpus >= 1
        and int(num_gpus) == num_gpus
        and finite_number(offer.get("dph_total"))
        and offer["dph_total"] > 0
        and finite_number(offer.get("gpu_ram"))
        and machine_id is not None
        and not isinstance(machine_id, bool)
        and str(machine_id).strip() != ""
    )


def full_gpu(offer):
    fraction = offer.get("gpu_frac")
    return finite_number(fraction) and 0.99 <= fraction <= 1.0


def per_gpu(offer):
    return offer["dph_total"] / offer["num_gpus"]


def trimmed_median(prices):
    if not prices:
        return None, 0
    raw = statistics.median(prices)
    kept = [p for p in prices if TRIM_LO * raw <= p <= TRIM_HI * raw]
    if not kept:
        return None, 0
    return statistics.median(kept), len(kept)


def machine_prices(offers):
    by_machine = defaultdict(list)
    for offer in offers:
        by_machine[str(offer["machine_id"])].append(per_gpu(offer))
    return [statistics.median(prices) for prices in by_machine.values()]


def build_day(offers, today):
    if not isinstance(offers, list):
        raise TypeError("offers must be a list")
    shaped = [offer for offer in offers if schema_valid_offer(offer)]
    usable = [
        offer for offer in shaped
        if offer.get("verification") == "verified"
        and GPU_RAM_MIN <= offer["gpu_ram"] <= GPU_RAM_MAX
    ]
    standalone_offers = [
        offer for offer in usable
        if offer["num_gpus"] == 1
    ]
    clustered_offers = [
        offer for offer in usable
        if offer["num_gpus"] >= 8
        and full_gpu(offer)
        and finite_number(offer.get("bw_nvlink"))
        and offer["bw_nvlink"] > 0
    ]
    standalone = machine_prices(standalone_offers)
    clustered = machine_prices(clustered_offers)
    s_med, s_n = trimmed_median(standalone)
    c_med, c_n = trimmed_median(clustered)
    valid = s_n >= MIN_COUNT and c_n >= MIN_COUNT
    day = {
        "date": today,
        "methodology_version": METHODOLOGY_VERSION,
        "standalone_median": round(s_med, 4) if s_med is not None else None,
        "clustered_median": round(c_med, 4) if c_med is not None else None,
        "standalone_n": s_n,
        "clustered_n": c_n,
        "spread_pct": round(100 * (c_med - s_med) / s_med, 2) if valid else None,
        "valid": valid,
        "provenance": {
            "raw_offer_n": len(offers),
            "schema_valid_offer_n": len(shaped),
            "usable_offer_n": len(usable),
            "standalone_offer_n": len(standalone_offers),
            "clustered_offer_n": len(clustered_offers),
            "standalone_machine_n_pretrim": len(standalone),
            "clustered_machine_n_pretrim": len(clustered),
            "query_limit": QUERY_LIMIT,
        },
    }
    if not valid:
        thin = []
        if s_n < MIN_COUNT:
            thin.append(f"standalone_n {s_n}")
        if c_n < MIN_COUNT:
            thin.append(f"clustered_n {c_n}")
        day["reason"] = "below minimum count: " + ", ".join(thin)
    return day


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    offers = fetch_offers()
    day = build_day(offers, today)

    if DATA.exists():
        doc = json.loads(DATA.read_text())
    else:
        doc = {
            "meta": {
                "schema_version": SCHEMA_VERSION,
                "series": "interconnect premium, Vast.ai H100 SXM 80GB",
                "current_methodology_version": METHODOLOGY_VERSION,
                "construction": (
                    "median verified total ask per GPU-hour, whole 8+ GPU NVLink nodes "
                    "versus single-GPU asks, one observation per unique machine"
                ),
                "trim": "machine observations outside 0.5x to 2.0x of side median dropped",
                "min_count_per_side": MIN_COUNT,
                "collection_began": today,
            },
            "days": [],
        }

    if not isinstance(doc, dict) or not isinstance(doc.get("meta"), dict):
        raise CollectionError("data/premium.json must contain a meta object")
    if not isinstance(doc.get("days"), list):
        raise CollectionError("data/premium.json must contain a days list")

    doc["meta"].update({
        "schema_version": SCHEMA_VERSION,
        "current_methodology_version": METHODOLOGY_VERSION,
        "construction": (
            "median verified total ask per GPU-hour, whole 8+ GPU NVLink nodes "
            "versus single-GPU asks, one observation per unique machine"
        ),
        "trim": "machine observations outside 0.5x to 2.0x of side median dropped",
        "min_count_per_side": MIN_COUNT,
    })
    doc["days"] = [d for d in doc["days"] if d["date"] != today] + [day]
    doc["days"].sort(key=lambda d: d["date"])
    doc["meta"]["last_pull_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    DATA.parent.mkdir(parents=True, exist_ok=True)
    pending = DATA.with_suffix(".json.tmp")
    pending.write_text(json.dumps(doc, indent=1) + "\n")
    pending.replace(DATA)
    print(json.dumps(day))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CollectionError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"collection failed: {exc}", file=sys.stderr)
        sys.exit(1)
