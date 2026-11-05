#!/usr/bin/env python3
"""Validate a check registry against the action families.

This is the reference implementation of the load-time constraint that
check-registry.v2.schema.json can only describe in prose: JSON Schema cannot
read another file's data, so the family rule lives here.

    python contracts/quality/validate_registry.py <registry.json>
    python contracts/quality/validate_registry.py <registry.json> --families <families.json>

Exit 0 = registry may load. Exit 1 = reject it (never warn and continue).
Exit 2 = environment problem.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FAMILIES = os.path.join(HERE, "action-families.v2.json")
DEFAULT_SCHEMA = os.path.join(HERE, "check-registry.v2.schema.json")

LAYER_OF_PREFIX = {"TECH": "technical", "VIS": "semantic", "AUD": "alignment", "SUB": "subtitle"}
LAYER_OWNER = {"technical": "S35", "semantic": "S36", "alignment": "S37", "subtitle": "S38"}


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate(registry, families):
    errors = []
    closed = set(families["closed_type_set"])
    fam = families["families"]
    seen = set()

    for i, c in enumerate(registry.get("checks", [])):
        cid = c.get("check_id", f"<entry {i}>")
        where = f"{cid}"

        if cid in seen:
            errors.append(f"{where}: duplicate check_id")
        seen.add(cid)

        t = c.get("defect_type")
        if t not in closed:
            errors.append(f"{where}: defect_type {t!r} not in the closed set")
            continue

        chain = fam[t]["chain"]
        allowed_by_family = set(chain)
        fallback = fam[t]["fallback"]

        d = c.get("default_action")
        if d not in allowed_by_family:
            errors.append(
                f"{where}: default_action {d!r} is not in the {t} family chain "
                f"{chain} (fallback {fallback!r} is a fallback, not a start point)"
            )

        for a in c.get("allowed_actions", []):
            if a not in allowed_by_family:
                errors.append(
                    f"{where}: allowed_actions contains {a!r}, outside the {t} family "
                    f"{sorted(allowed_by_family)}. A registry entry may narrow the "
                    f"family, never widen it."
                )

        esc = c.get("escalation_rule")
        if esc is not None:
            to = esc.get("escalate_to")
            if to not in allowed_by_family:
                errors.append(
                    f"{where}: escalation_rule.escalate_to {to!r} is outside the {t} family"
                )

        if c.get("never_auto_repair") and not c.get("no_auto_reason"):
            errors.append(f"{where}: never_auto_repair=true requires no_auto_reason")

        prefix = cid.split(".")[0]
        expect_layer = LAYER_OF_PREFIX.get(prefix)
        if expect_layer is None:
            errors.append(f"{where}: unknown check_id prefix {prefix!r}")
        else:
            if c.get("layer") != expect_layer:
                errors.append(
                    f"{where}: layer {c.get('layer')!r} does not match prefix "
                    f"{prefix!r} (expected {expect_layer!r})"
                )
            owner = LAYER_OWNER[expect_layer]
            if c.get("owner_service") != owner:
                errors.append(
                    f"{where}: owner_service {c.get('owner_service')!r} does not match "
                    f"layer {expect_layer!r} (expected {owner!r})"
                )

        sd, sm = c.get("severity_default"), c.get("severity_max")
        order = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
        if sd in order and sm in order and order[sd] > order[sm]:
            errors.append(f"{where}: severity_default {sd!r} exceeds severity_max {sm!r}")

    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("registry")
    ap.add_argument("--families", default=DEFAULT_FAMILIES)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    args = ap.parse_args()

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        # Half a validator is worse than none: a registry that fails the shape
        # rules would load with exit 0 and the bug surfaces in S39's routing.
        print("jsonschema is not installed; cannot run shape checks. Refusing to "
              "report OK. Install it: python -m pip install jsonschema")
        return 2

    for p in (args.registry, args.families, args.schema):
        if not os.path.exists(p):
            print(f"file not found: {p}")
            return 2

    registry = load(args.registry)
    families = load(args.families)

    errors = []
    schema = load(args.schema)
    v = Draft202012Validator(schema)
    for e in sorted(v.iter_errors(registry), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(x) for x in e.absolute_path) or "<root>"
        errors.append(f"schema {loc}: {e.message}")

    errors += validate(registry, families)

    n = len(registry.get("checks", []))
    if errors:
        print(f"REJECTED: {len(errors)} problem(s) in {n} check(s)")
        for e in errors:
            print("  " + e)
        return 1

    print(f"OK: {n} check(s) loaded, all within their action families")
    return 0


if __name__ == "__main__":
    sys.exit(main())
