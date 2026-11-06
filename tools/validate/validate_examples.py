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
execution-context.v2.schema.json is referenced from other files today (three
`$ref`s, all to it). The glob is not recursive, so the registry holds the 64
top-level schemas and not the 7 in contracts/domain-v1/ and contracts/quality/:
a `$ref` to one of those raises `Unresolvable` rather than reaching the network,
so the offline guarantee holds, but the reference does not work. Nothing
validates against those 7 today — no SCHEMA_OF entry, no `data_schema_ref`, and
no `$ref` names them.

Exit 0 = every mapped example validated. Exit 1 = at least one failed.
Exit 2 = environment problem (jsonschema missing, or an example with no mapping).
"""

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTRACTS = os.path.join(ROOT, "contracts")
EXAMPLES = os.path.join(CONTRACTS, "examples")

# Example file stem -> schema file name. None means "structure only, no schema",
# which is a deliberate state and not a failure.
SCHEMA_OF = {
    "access-grant-changed": "event-envelope.v2.schema.json",
    "audit-export-ready": "event-envelope.v2.schema.json",
    "project-episode-created": "event-envelope.v2.schema.json",
    "integration-command-accepted": "event-envelope.v2.schema.json",
    "projection-cursor-advanced": "event-envelope.v2.schema.json",
    "adaptation-training-completed": "event-envelope.v2.schema.json",
    "admission": "admission-record.v2.schema.json",
    "admission-unknown": "admission-record.v2.schema.json",
    "agent-task-completed": "event-envelope.v2.schema.json",
    "artifact-ingested": "event-envelope.v2.schema.json",
    "asset-voice-authorization-changed": "event-envelope.v2.schema.json",
    "asset-binding-set-created": "event-envelope.v2.schema.json",
    "automation-policy": "automation-policy.v2.schema.json",
    "change-plan-applied": "event-envelope.v2.schema.json",
    "connector-observation-received": "event-envelope.v2.schema.json",
    "context-pack": "context-pack.v2.schema.json",
    "context-pack-created": "event-envelope.v2.schema.json",
    "continuity-check-completed": "event-envelope.v2.schema.json",
    "delivery-package-published": "event-envelope.v2.schema.json",
    "delivery-package-created": "event-envelope.v2.schema.json",
    "delivery-package-sealed": "event-envelope.v2.schema.json",
    "delivery-publish-failed": "event-envelope.v2.schema.json",
    "analytics-metric-snapshot-created": "event-envelope.v2.schema.json",
    "analytics-evaluation-completed": "event-envelope.v2.schema.json",
    "analytics-cost-projection-updated": "event-envelope.v2.schema.json",
    "enterprise-change-plan": "enterprise-change-plan.v2.schema.json",
    "episode-manifest": "episode-manifest.v2.schema.json",
    "execution-context": "execution-context.v2.schema.json",
    "finance-ledger-entry-posted": "event-envelope.v2.schema.json",
    "generation-command": "generation-command.v2.schema.json",
    "generation-job-state-changed": "event-envelope.v2.schema.json",
    "lineage-impact-computed": "event-envelope.v2.schema.json",
    "memory-claim": "memory-claim.v2.schema.json",
    "memory-committed-event": "event-envelope.v2.schema.json",
    "model-capability-changed": "event-envelope.v2.schema.json",
    "model-route-selected": "event-envelope.v2.schema.json",
    "orchestration-run-state-changed": "event-envelope.v2.schema.json",
    "post-master-rendered": "event-envelope.v2.schema.json",
    "post-mix-completed": "event-envelope.v2.schema.json",
    "post-render-failed": "event-envelope.v2.schema.json",
    "post-subtitle-ready": "event-envelope.v2.schema.json",
    "post-timeline-revision-created": "event-envelope.v2.schema.json",
    "production-image-ready": "event-envelope.v2.schema.json",
    "production-lipsync-ready": "event-envelope.v2.schema.json",
    "production-music-ready": "event-envelope.v2.schema.json",
    "production-sfx-ready": "event-envelope.v2.schema.json",
    "production-speech-ready": "event-envelope.v2.schema.json",
    "production-video-ready": "event-envelope.v2.schema.json",
    "prompt-compiled": "event-envelope.v2.schema.json",
    "quality-alignment-completed": "event-envelope.v2.schema.json",
    "quality-repair-planned": "event-envelope.v2.schema.json",
    "quality-semantic-completed": "event-envelope.v2.schema.json",
    "quality-technical-completed": "event-envelope.v2.schema.json",
    "review-decision-recorded": "event-envelope.v2.schema.json",
    "scheduler-permit-changed": "event-envelope.v2.schema.json",
    "snapshot-manifest-committed": "event-envelope.v2.schema.json",
    "storyboard-shot-revision-created": "event-envelope.v2.schema.json",
    "narrative-script-revision-created": "event-envelope.v2.schema.json",
    "asset-scene-revision-accepted": "event-envelope.v2.schema.json",
    "asset-prop-revision-accepted": "event-envelope.v2.schema.json",
    "asset-character-revision-accepted": "event-envelope.v2.schema.json",
    "asset-costume-revision-accepted": "event-envelope.v2.schema.json",
    "breakdown-revision-created": "event-envelope.v2.schema.json",
    "policy-revision-published": "event-envelope.v2.schema.json",
    "capacity-plan": None,
}


class _PlainError:
    """A jsonschema-error-shaped stand-in for the checks that are not schema checks."""

    def __init__(self, message):
        self.message = message


def _strings(node, path="data"):
    """Yield (path, value) for every string leaf under `node`."""
    if isinstance(node, dict):
        for k, v in node.items():
            for pair in _strings(v, f"{path}.{k}"):
                yield pair
    elif isinstance(node, list):
        for i, v in enumerate(node):
            for pair in _strings(v, f"{path}[{i}]"):
                yield pair
    elif isinstance(node, str):
        yield path, node


# `aggregate_id` must be reachable from `data`, because a consumer that has to
# re-read the aggregate source (the normal case) needs the id it just got told
# about. An event whose aggregate is a *scope* or a *composite key* rather than
# the payload's primary key cannot satisfy that, and each such case below is a
# documented decision, not an oversight. The check is either/or on purpose:
# an unlisted mismatch fails, and a listed event that starts matching fails too
# (the list stops being true the moment the underlying doc is fixed).
#
# event_type -> (why aggregate_id is not in data, where that is written down)
AGGREGATE_EXEMPT = {
    "connector.observation.received": (
        "aggregate_id 是平台侧 provider job id（`pj_`），payload 里的 dispatch_id 是 S27 "
        "自己的幂等锚点、provider_job_ref 是供应商侧任务 id，三者是三个不同的 id",
        "PCR-6 / PCR-7（contracts/connector.observation-received.v2.schema.json），"
        "S27 §9.2 行 683 写 aggregate_id = provider_job_id（可空）",
    ),
    "context.pack.created": (
        "aggregate_id 是装配作用域 `series:branch:episode`，payload 的 context_pack_id "
        "是 pack 自己的 id，不是同一个实体",
        "CCP-4（contracts/context.pack-created.v2.schema.json），"
        "S18 §6.3 行 469–470",
    ),
    "continuity.check.completed": (
        "aggregate_id 是 `series:branch:episode` 作用域，payload 的 report_id 才是这条事件"
        "真正指向的实体",
        "contracts/README.md 行 206 与 CCP-4，S17 §5.2 行 343–344",
    ),
    "generation.job.state-changed": (
        "aggregate_id = provider_job_id，按 S26 明文选择，不是 payload 里的 attempt_id",
        "S26 §12 行 1016；本 schema 的 description 复述了这条选择",
    ),
    "memory.canon.committed": (
        "aggregate_id 是 `series:branch`，也就是 commit_sequence 的单调作用域"
        "（DDL 行 98「每 series+branch 单调递增」）",
        "MSC-7（contracts/memory.canon-committed.v2.schema.json）",
    ),
    "model.capability.changed": (
        "aggregate_id 是四段复合键 `{provider}/{channel}/{model_id}/{model_snapshot}`，"
        "payload 只带内容寻址的 snapshot_id",
        "MCG-2（contracts/model.capability-changed.v2.schema.json），S24 §3.5 行 596",
    ),
}


def aggregate_identity_problems(doc):
    """Check that the envelope's aggregate_id is reachable from `data`."""
    agg = doc.get("aggregate_id")
    if not isinstance(agg, str) or not agg:
        return []
    data = doc.get("data")
    if not isinstance(data, dict):
        return []
    hits = [p for p, v in _strings(data) if v == agg]
    etype = doc.get("type")
    exempt = AGGREGATE_EXEMPT.get(etype)
    if hits:
        if exempt:
            return [(
                "<aggregate_id>",
                f"STALE exemption for {etype}: aggregate_id is now reachable at "
                f"{hits[0]}; delete the AGGREGATE_EXEMPT entry",
            )]
        return []
    if exempt:
        return []
    return [(
        "<aggregate_id>",
        f"aggregate_id {agg!r} appears nowhere in data, and {etype} is not in "
        f"AGGREGATE_EXEMPT. Either the example drifted or the exemption is missing",
    )]


# The envelope types `type` and `source` as bare non-empty strings, so a typo in
# either passes both schema layers — `catalog` only checks the topics file, never
# the instances. Both `source` spellings the tree actually uses are accepted: a
# bare service_id, and `/cells/<cell>/services/<S..>` (recorded as drift by
# access.grant-changed.v2.schema.json's `envelope_drift`, not silently normalised).
SOURCE_BARE = re.compile(r"^(S\d{2})$")
SOURCE_CELL_URI = re.compile(r"^/cells/([^/]+)/services/(S\d{2})$")


def _registries():
    with open(os.path.join(CONTRACTS, "event-topics.v2.json"), encoding="utf-8") as f:
        topics = {t["event_type"]: t for t in json.load(f)["topics"]}
    with open(os.path.join(CONTRACTS, "service-catalog.v2.json"), encoding="utf-8") as f:
        catalog = {s["service_id"] for s in json.load(f)["services"]}
    return topics, catalog


def envelope_identity_problems(doc, topics, catalog):
    """Check that a typed example names a real topic, from that topic's producer."""
    etype = doc.get("type")
    if not isinstance(etype, str) or not etype:
        return []                       # payload-only document, not an envelope
    out = []
    topic = topics.get(etype)
    if topic is None:
        out.append((
            "<type>",
            f"type {etype!r} is not an event_type in contracts/event-topics.v2.json; "
            f"the envelope only requires a non-empty string, so nothing else catches this",
        ))
        return out                      # producer check would be noise without a topic

    src = doc.get("source")
    if not isinstance(src, str) or not src:
        return out + [("<source>", "source is missing or empty")]
    m = SOURCE_BARE.match(src)
    service = m.group(1) if m else None
    cell = None
    if service is None:
        m = SOURCE_CELL_URI.match(src)
        if m:
            cell, service = m.group(1), m.group(2)
    if service is None:
        return out + [(
            "<source>",
            f"source {src!r} is neither a catalog service_id nor "
            f"/cells/<cell>/services/<S..>",
        )]
    if service not in catalog:
        out.append(("<source>", f"source names {service}, which is not in service-catalog.v2.json"))
    elif topic.get("producer") != service:
        out.append((
            "<source>",
            f"source names {service} but topic {etype!r} is produced by "
            f"{topic.get('producer')!r}; one of the two is wrong",
        ))
    if cell is not None and doc.get("home_cell") != cell:
        out.append((
            "<source>",
            f"source says cell {cell!r} but home_cell is {doc.get('home_cell')!r}",
        ))
    return out


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
    topics, catalog = _registries()
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
        for loc, msg in aggregate_identity_problems(doc):
            found.append((loc, _PlainError(msg)))
        for loc, msg in envelope_identity_problems(doc, topics, catalog):
            found.append((loc, _PlainError(msg)))
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
