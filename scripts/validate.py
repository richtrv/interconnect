#!/usr/bin/env python3
"""Dependency-free publication checks for the static site and aggregate data."""

import csv
import json
import math
import re
import sys
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "premium.json"
SCHEMA = ROOT / "data" / "premium.schema.json"
SITE_BUDGET = 350 * 1024


class ValidationError(AssertionError):
    pass


def fail(message):
    raise ValidationError(message)


def matches_type(value, expected):
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    fail(f"schema uses unsupported type {expected!r}")


def schema_matches(value, schema):
    try:
        validate_schema(value, schema)
        return True
    except ValidationError:
        return False


def validate_schema(value, schema, path="$"):
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(matches_type(value, item) for item in types):
            fail(f"{path}: expected type {expected!r}")

    if "const" in schema and value != schema["const"]:
        fail(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        fail(f"{path}: expected one of {schema['enum']!r}")
    if "minimum" in schema and value is not None and value < schema["minimum"]:
        fail(f"{path}: value is below minimum {schema['minimum']}")

    if schema.get("format") == "date":
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{path}: invalid ISO date") from exc
    if schema.get("format") == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValidationError(f"{path}: invalid ISO date-time") from exc

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            fail(f"{path}: missing required fields {missing}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                fail(f"{path}: unexpected fields {extra}")
        for key, child in properties.items():
            if key in value:
                validate_schema(value[key], child, f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")

    for condition in schema.get("allOf", []):
        if "if" in condition and schema_matches(value, condition["if"]):
            validate_schema(value, condition.get("then", {}), path)


def validate_premium_data():
    doc = json.loads(DATA.read_text())
    schema = json.loads(SCHEMA.read_text())
    validate_schema(doc, schema)

    days = doc["days"]
    dates = [day["date"] for day in days]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        fail("premium days must be unique and sorted")

    minimum = doc["meta"]["min_count_per_side"]
    for day in days:
        for field in ("standalone_median", "clustered_median"):
            if day[field] is not None and day[field] <= 0:
                fail(f"{day['date']}: {field} must be positive when present")
        if day["valid"]:
            if day["standalone_n"] < minimum or day["clustered_n"] < minimum:
                fail(f"{day['date']}: valid day is below the minimum count")
            if day["standalone_median"] is None or day["clustered_median"] is None:
                fail(f"{day['date']}: valid day is missing a median")
            expected = round(
                100
                * (day["clustered_median"] - day["standalone_median"])
                / day["standalone_median"],
                2,
            )
            if day["spread_pct"] != expected:
                fail(f"{day['date']}: spread does not match stored medians")
        else:
            if day["spread_pct"] is not None or not day.get("reason"):
                fail(f"{day['date']}: invalid day needs a null spread and reason")

        provenance = day.get("provenance")
        if provenance:
            chain = [
                provenance["raw_offer_n"],
                provenance["schema_valid_offer_n"],
                provenance["usable_offer_n"],
            ]
            if chain != sorted(chain, reverse=True):
                fail(f"{day['date']}: provenance filter counts are not monotonic")
            if day["standalone_n"] > provenance["standalone_machine_n_pretrim"]:
                fail(f"{day['date']}: standalone retained count exceeds pre-trim count")
            if day["clustered_n"] > provenance["clustered_machine_n_pretrim"]:
                fail(f"{day['date']}: clustered retained count exceeds pre-trim count")


class StructureParser(HTMLParser):
    def __init__(self, filename):
        super().__init__()
        self.filename = filename
        self.ids = set()
        self.sections = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        identifier = attrs.get("id")
        if identifier:
            if identifier in self.ids:
                fail(f"{self.filename}: duplicate id {identifier!r}")
            self.ids.add(identifier)
        if tag == "section":
            self.sections.append(False)
        elif tag in {"h2", "h3", "h4", "h5", "h6"} and self.sections:
            self.sections[-1] = True

    def handle_endtag(self, tag):
        if tag == "section":
            if not self.sections:
                fail(f"{self.filename}: unmatched closing section")
            if not self.sections.pop():
                fail(f"{self.filename}: section has no semantic heading")


def validate_html_structure():
    for filename in ("index.html", "monitor.html"):
        parser = StructureParser(filename)
        parser.feed((ROOT / filename).read_text())
        if parser.sections:
            fail(f"{filename}: unclosed section")


def validate_methodology():
    with (ROOT / "methodology.csv").open(newline="") as handle:
        rows = list(csv.reader(handle))
    if rows[0] != ["section", "item", "value", "unit", "source", "note"]:
        fail("methodology.csv: unexpected header")
    seen = set()
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != 6 or not all(cell.strip() for cell in row):
            fail(f"methodology.csv:{line}: expected six populated columns")
        key = tuple(row[:2])
        if key in seen:
            fail(f"methodology.csv:{line}: duplicate section/item {key}")
        seen.add(key)


def validate_copy_and_budget():
    copy_files = ["index.html", "monitor.html", "app.js", "methodology.csv"]
    combined = "\n".join((ROOT / filename).read_text() for filename in copy_files)
    for forbidden in ("GB" + "/S", "SIZE " + "EST", "plotted at " + "1T"):
        if forbidden in combined:
            fail(f"copy rule: forbidden text remains: {forbidden!r}")
    if "\u2013" in combined or "\u2014" in combined:
        fail("copy rule: en/em dash found")

    html = (ROOT / "index.html").read_text() + (ROOT / "monitor.html").read_text()
    first_person = len(re.findall(r"\bI\b", html))
    if first_person > 2:
        fail(f"copy rule: first-person singular count is {first_person}; maximum is 2")

    site_files = [
        "index.html",
        "monitor.html",
        "site.css",
        "app.js",
        "data/premium.json",
    ]
    total = sum((ROOT / filename).stat().st_size for filename in site_files)
    if total > SITE_BUDGET:
        fail(f"static site payload {total} bytes exceeds {SITE_BUDGET}-byte budget")


def validate_arithmetic():
    gradient_bytes_per_second_per_parameter = 2 * 2 / 5
    crossover_billion = 50e9 / gradient_bytes_per_second_per_parameter / 1e9
    if crossover_billion != 62.5:
        fail("published 62.5B crossover arithmetic changed")
    sharded = 405e9 * 2 * 2 / (8 * 16 * 5.9)
    if not 2.1e9 < sharded < 2.2e9:
        fail("Llama sharded comparison no longer rounds to 2.1 GB/s")


def main():
    validate_premium_data()
    validate_html_structure()
    validate_methodology()
    validate_copy_and_budget()
    validate_arithmetic()
    print("validation passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, json.JSONDecodeError, csv.Error, ValidationError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
