"""Validate contracts/examples/* against the schemas they illustrate.

Run from the repository root:

    python tools/validate/validate_examples.py
    python tools/validate/validate_examples.py --list

Why this exists: the schemas reference each other by their `.invalid` $id, and
jsonschema resolves an unknown $id by fetching it over the network. The design
package forbids that ("解析使用本地注册表；不允许联网拉取未知schema"), and the
fetch fails anyway because the host does not exist — so a naive validation run
crashes on the first cross-file reference instead of reporting example problems.
This builds the local registry the package assumes exists. Only
execution-context.v2.schema.json is referenced from other files today, but the
registry loads all nine so the next cross-file reference needs no change here.

Exit 0 = every mapped example validated. Exit 1 = at least one failed.
Exit 2 = environment problem (jsonschema missing, or an example with no mapping).
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTRACTS = os.path.join(ROOT, "contracts")
EXAMPLES = os.path.join(CONTRACTS, "examples")

# Example file stem -> schema file name. None means "structure only, no schema",
# which is a deliberate state and not a failure.
SCHEMA_OF = {
    "admission": "admission-record.v2.schema.json",
    "admission-unknown": "admission-record.v2.schema.json",
    "artifact-ingested": "event-envelope.v2.schema.json",
    "automation-policy": "automation-policy.v2.schema.json",
    "connector-observation-received": "event-envelope.v2.schema.json",
    "context-pack": "context-pack.v2.schema.json",
    "delivery-package-published": "event-envelope.v2.schema.json",
    "enterprise-change-plan": "enterprise-change-plan.v2.schema.json",
    "episode-manifest": "episode-manifest.v2.schema.json",
    "execution-context": "execution-context.v2.schema.json",
    "finance-ledger-entry-posted": "event-envelope.v2.schema.json",
    "generation-command": "generation-command.v2.schema.json",
    "generation-job-state-changed": "event-envelope.v2.schema.json",
    "memory-claim": "memory-claim.v2.schema.json",
    "memory-committed-event": "event-envelope.v2.schema.json",
    "orchestration-run-state-changed": "event-envelope.v2.schema.json",
    "post-master-rendered": "event-envelope.v2.schema.json",
    "post-subtitle-ready": "event-envelope.v2.schema.json",
    "post-timeline-revision-created": "event-envelope.v2.schema.json",
    "production-image-ready": "event-envelope.v2.schema.json",
    "production-lipsync-ready": "event-envelope.v2.schema.json",
    "production-music-ready": "event-envelope.v2.schema.json",
    "production-sfx-ready": "event-envelope.v2.schema.json",
    "production-speech-ready": "event-envelope.v2.schema.json",
    "production-video-ready": "event-envelope.v2.schema.json",
    "quality-alignment-completed": "event-envelope.v2.schema.json",
    "quality-repair-planned": "event-envelope.v2.schema.json",
    "quality-semantic-completed": "event-envelope.v2.schema.json",
    "quality-technical-completed": "event-envelope.v2.schema.json",
    "review-decision-recorded": "event-envelope.v2.schema.json",
    "scheduler-permit-changed": "event-envelope.v2.schema.json",
    "capacity-plan": None,
}


def local_registry():
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    reg = Registry()
    for p in glob.glob(os.path.join(CONTRACTS, "*.schema.json")):
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        reg = reg.with_resource(
            s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)
        )
    return reg


def payload_schemas():
    """event_type -> schema file name, for topics that register a payload schema.

    `schema_ref` names the envelope, so it types the whole event but leaves
    `data` an unconstrained object. A topic that wants its payload typed adds
    `data_schema_ref` in contracts/event-topics.v2.json. Topics without one are
    envelope-only, which is the normal state and not a gap.
    """
    with open(os.path.join(CONTRACTS, "event-topics.v2.json"), encoding="utf-8") as f:
        topics = json.load(f)["topics"]
    return {
        t["event_type"]: t["data_schema_ref"]
        for t in topics
        if t.get("data_schema_ref")
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list mappings and exit")
    args = ap.parse_args()

    if args.list:
        for stem in sorted(SCHEMA_OF):
            print(f"{stem:28} -> {SCHEMA_OF[stem] or '(no schema)'}")
        return 0

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("jsonschema is not installed. Install it: python -m pip install jsonschema")
        return 2

    reg = local_registry()
    payload = payload_schemas()
    paths = sorted(glob.glob(os.path.join(EXAMPLES, "*.json")))
    if not paths:
        print(f"no examples found under {EXAMPLES}")
        return 2

    unmapped, failures, ok, skipped = [], [], 0, 0
    for p in paths:
        stem = os.path.basename(p).split(".")[0]
        if stem not in SCHEMA_OF:
            unmapped.append(os.path.basename(p))
            continue
        schema_name = SCHEMA_OF[stem]
        if schema_name is None:
            skipped += 1
            continue
        with open(os.path.join(CONTRACTS, schema_name), encoding="utf-8") as f:
            schema = json.load(f)
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)

        # An envelope schema types everything except `data`. When the example's
        # topic registers a payload schema, validate `data` against it too —
        # otherwise the payload is exactly as unconstrained as having no schema.
        checked = [(schema, doc, "")]
        payload_name = payload.get(doc.get("type"))
        if payload_name:
            with open(os.path.join(CONTRACTS, payload_name), encoding="utf-8") as f:
                checked.append((json.load(f), doc["data"], "data/"))

        found = []
        for target, sub, prefix in checked:
            for e in Draft202012Validator(target, registry=reg).iter_errors(sub):
                found.append((prefix + "/".join(str(x) for x in e.absolute_path), e))
        found.sort(key=lambda pair: pair[0])

        if found:
            loc, first = found[0]
            failures.append((os.path.basename(p), len(found), loc or "<root>", first.message))
        else:
            ok += 1

    for name, n, loc, msg in failures:
        print(f"FAIL {name}: {n} error(s), first at {loc}: {msg}")
    for name in unmapped:
        print(f"UNMAPPED {name}: add it to SCHEMA_OF (or map it to None)")

    print(f"\n{ok} example(s) valid, {len(failures)} failing, {skipped} without a schema")
    if failures or unmapped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
