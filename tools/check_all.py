"""Run every repository-level check in one pass.

    python tools/check_all.py            # all checks
    python tools/check_all.py --list     # what would run
    python tools/check_all.py --only links,examples

Why this exists: the checks were five separate ad-hoc commands, so nothing ran
them together and nothing ran them automatically. `contracts/README.md` says the
family constraint must be enforced at load time, not kept in prose, but a
validator only enforces anything when something calls it. This is the single
entry point a pre-commit hook, a CI job or a person runs.

Exit 0 = every check passed. Exit 1 = at least one failed. Exit 2 = a check
could not run (missing dependency, missing file), distinct from "found a
problem", because a check that did not run must never read as green.

Generated files: the scaffold generators write in place, so drift is detected by
rendering into a temporary root and comparing. The generators are imported, not
executed as subprocesses, so their ROOT is redirected without touching the
working tree.
"""

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows consoles and pipes default to the system code page (GBK here), which
# turns any non-ASCII character into bytes that are not valid UTF-8. Redrawing
# stdout as UTF-8 keeps redirected output parseable.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")
CONTRACTS = os.path.join(ROOT, "contracts")


class Result:
    def __init__(self, name):
        self.name = name
        self.failures = []
        self.note = ""

    def fail(self, msg):
        self.failures.append(msg)


def _utf8(path):
    """Read as text with line endings normalised.

    The generators write `newline='\\n'`. On a checkout with core.autocrlf=true
    (this repo's setting) those files come back as CRLF, so comparing raw bytes
    would report every generated file as drift on any fresh Windows clone. The
    comparison is about content, not about what the VCS did to it.
    """
    with open(path, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_json(name):
    r = Result(name)
    paths = sorted(glob.glob(os.path.join(CONTRACTS, "**", "*.json"), recursive=True))
    for p in paths:
        try:
            json.loads(_utf8(p))
        except Exception as e:
            r.fail(f"{os.path.relpath(p, ROOT)}: {e}")
    r.note = f"{len(paths)} file(s)"
    return r


def check_links(name):
    r = Result(name)
    fence = re.compile(r"```.*?```", re.S)
    inline = re.compile(r"`[^`\n]*`")
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
    total = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        if rel.startswith(("docs/design-package/", ".git/")) or "node_modules/" in rel:
            continue
        text = _utf8(p)
        # A link inside a fenced block or inline code is an example, not a
        # link: plans/ holds shell snippets with `->` and `[x](y)` shapes.
        text = inline.sub("", fence.sub("", text))
        base = os.path.dirname(p)
        for m in link.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#")[0]
            if not target:
                continue
            total += 1
            if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                line = text[: m.start()].count("\n") + 1
                r.fail(f"{rel}:{line} -> {target}")
    r.note = f"{total} local link(s)"
    return r


def check_catalog(name):
    r = Result(name)
    cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))["services"]
    ids = [s["service_id"] for s in cat]

    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        r.fail(f"duplicate service_id: {dupes}")

    # docs/02 is the human-readable mirror of the catalog and must not drift.
    doc02 = _utf8(os.path.join(ROOT, "docs", "02-service-catalog.md"))
    for i in ids:
        if i not in doc02:
            r.fail(f"{i} in catalog but not in docs/02")

    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    known = set(ids)
    seen_types = {}
    with_consumers = 0
    for t in topics:
        for field in ("producer",):
            owner = t.get(field)
            if owner and owner not in known:
                r.fail(f"topic {t.get('topic')}: {field} {owner!r} is not a catalog service_id")
        consumers = t.get("consumers")
        if consumers:
            with_consumers += 1
            for c in consumers:
                if c not in known:
                    r.fail(f"topic {t.get('topic')}: consumer {c!r} is not a catalog service_id")
        et = t.get("event_type")
        if et:
            if et in seen_types:
                r.fail(f"event_type {et!r} registered twice")
            seen_types[et] = t.get("topic")

    # No topic declares `consumers`, so the loop above has never had an entry to
    # check: the "consumers agree with the catalog" half of this check is dead
    # code. Adding the key is an owner decision (policy.revision-published.v2's
    # POL-* `fix_shape` says so), so the honest thing is to say it out loud
    # rather than let the green line imply the consumer leg ran.
    r.note = f"{len(ids)} service(s), {len(topics)} topic(s)"
    if not with_consumers:
        r.note += "  [producer-only: no topic declares consumers]"
    return r


def check_families(name):
    """The five invariants this file states about itself, checked literally.

    `fallback` is deliberately allowed to be human_review (and often is), and
    `forbidden` deliberately names actions outside the chain; that is the whole
    point of forbidding one. Both were wrong in an earlier draft of this check:
    the assertions were written from memory of "chain is the complete set of
    legal moves", which is not what the contract says.
    """
    r = Result(name)
    fam = json.loads(_utf8(os.path.join(CONTRACTS, "quality", "action-families.v2.json")))
    closed = set(fam["closed_type_set"])
    enum = set(fam["action_enum"])
    fatal = fam["fatal_fallback"]

    # invariant 2: families keys and closed_type_set are the same set
    if set(fam["families"]) != closed:
        r.fail(f"families keys != closed_type_set: {sorted(set(fam['families']) ^ closed)}")

    for t, d in fam["families"].items():
        chain, fb = d["chain"], d["fallback"]

        # invariant 1: every named action exists in action_enum
        named = list(chain) + [fb] + [f["action"] for f in d.get("forbidden", [])]
        for a in named:
            if a not in enum:
                r.fail(f"{t}: {a!r} is named but not in action_enum")

        # invariant 3: fallback is human_review or a chain member
        if fb != fatal and fb not in chain:
            r.fail(f"{t}: fallback {fb!r} is neither {fatal!r} nor a chain member")

        # invariant 4: human_review never appears in a chain
        if fatal in chain:
            r.fail(f"{t}: {fatal!r} is in the chain; it is the shared terminal "
                   f"of every family, not a chain member")

        # not stated as an invariant, but a chain that repeats an action would
        # make "walk outward from start_point" ambiguous
        if len(set(chain)) != len(chain):
            r.fail(f"{t}: chain names an action twice")

    r.note = f"{len(closed)} family/families"
    return r


def _load_generator(path, root):
    """Import a scaffold generator with its ROOT pointed at `root`."""
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    return mod


def check_scaffold(name):
    """Render the generated files into a temp root and diff against the tree."""
    r = Result(name)
    specs = [
        ("generate_services.py", lambda m, s, t: {
            "service.yaml": m.render_yaml(s, t),
            "service.md": m.render_md(s),
        }),
        ("generate_agents.py", None),
        ("generate_connectors.py", None),
    ]
    tmp = tempfile.mkdtemp(prefix="scaffold-")
    # Generators read their inputs from ROOT, so the temp root needs a
    # contracts/ symlink-free copy of the two source files.
    os.makedirs(os.path.join(tmp, "contracts"), exist_ok=True)
    for f in ("service-catalog.v2.json", "event-topics.v2.json"):
        with open(os.path.join(CONTRACTS, f), "rb") as src:
            data = src.read()
        with open(os.path.join(tmp, "contracts", f), "wb") as dst:
            dst.write(data)

    for fname, _ in specs:
        path = os.path.join(ROOT, "tools", "scaffold", fname)
        try:
            mod = _load_generator(path, tmp)
            mod.main()
        except Exception as e:
            r.fail(f"{fname}: could not render: {type(e).__name__}: {e}")
            continue

    # Compare every rendered file against the working tree.
    compared = 0
    for p in sorted(glob.glob(os.path.join(tmp, "**", "*"), recursive=True)):
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, tmp)
        tree = os.path.join(ROOT, rel)
        compared += 1
        if not os.path.exists(tree):
            r.fail(f"{rel}: generated but missing from the tree")
            continue
        if _utf8(p) != _utf8(tree):
            r.fail(f"{rel}: differs from its catalog-derived form; rerun the generator")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    r.note = f"{compared} generated file(s)"
    return r


def check_examples(name):
    r = Result(name)
    path = os.path.join(ROOT, "tools", "validate", "validate_examples.py")
    spec = importlib.util.spec_from_file_location("validate_examples", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    saved = sys.argv
    sys.argv = ["validate_examples.py"]
    try:
        rc = mod.main()
    finally:
        sys.argv = saved
    if rc == 2:
        r.fail("cannot run (jsonschema missing?); this is not a pass")
    elif rc != 0:
        r.fail("at least one example failed its schema")
    r.note = "contracts/examples/"
    return r


def check_registry(name):
    """Run validate_registry.py if a registry data file exists yet.

    The registry is not authored (99 checks, no extractable values — see
    delivery/acceptance/check-registry-missing-fields-2026-11-05.md), so this
    reports "not present" rather than failing. It exists so that the moment the
    file lands, the load-time family constraint runs without anyone wiring it up.
    """
    r = Result(name)
    path = os.path.join(CONTRACTS, "quality", "check-registry.v2.json")
    if not os.path.exists(path):
        r.note = "no check-registry.v2.json yet; nothing to validate"
        return r
    spec = importlib.util.spec_from_file_location(
        "validate_registry", os.path.join(CONTRACTS, "quality", "validate_registry.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    saved = sys.argv
    sys.argv = ["validate_registry.py", path]
    try:
        rc = mod.main()
    finally:
        sys.argv = saved
    if rc == 2:
        r.fail("cannot run (jsonschema missing / bad path); this is not a pass")
    elif rc != 0:
        r.fail("registry rejected at load time")
    r.note = os.path.relpath(path, ROOT).replace(os.sep, "/")
    return r


# Two-segment TECH ids in S35. Renaming them is a plan change that also touches
# acceptance items E24-T4/T5, so it waits on the S35 owner. Listed here so the
# check is green for everything else instead of being a permanently failing
# suite nobody reads.
KNOWN_BAD_CHECK_IDS = {
    "TECH.BLACK_FRAME",
    "TECH.FREEZE_FRAME",
    "TECH.FLASH_FRAME",
}


def check_payload_refs(name):
    """Every data_schema_ref / schema_ref a topic names must be a real file.

    A topic that points at a schema which does not exist is worse than one with
    no payload contract at all: consumers see a typed contract and the validator
    cannot load it, so the failure lands at read time in someone else's service.
    """
    r = Result(name)
    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    refs = 0
    with_payload = 0
    for t in topics:
        for key in ("schema_ref", "data_schema_ref"):
            ref = t.get(key)
            if not ref:
                continue
            refs += 1
            if key == "data_schema_ref":
                with_payload += 1
            if not os.path.exists(os.path.join(CONTRACTS, ref)):
                r.fail(f"topic {t.get('topic')}: {key} -> {ref} does not exist")
    r.note = f"{refs} schema ref(s), {with_payload} topic(s) with a payload schema"
    return r


def check_check_ids(name):
    """check_id values in the four QC plans match the schema's naming pattern.

    Only ids that appear in a plan's table row count: prose mentions like
    "TECH.BLACK_FRAME 那条" are discussion, and a plan may quote a name it is
    proposing to change. The table is what a registry would be built from.

    This runs before the registry data file exists because a plan whose ids the
    schema would reject cannot be transcribed into a valid registry at all, and
    finding that out during transcription is later than necessary.
    """
    r = Result(name)
    pattern = re.compile(r"^(TECH|VIS|AUD|SUB)\.[A-Z0-9_]+(\.[A-Z0-9_]+)+$")
    total = 0
    seen_bad = set()
    for fname, prefix in (
        ("S35-technical-qc", "TECH"),
        ("S36-semantic-qc", "VIS"),
        ("S37-audio-alignment", "AUD"),
        ("S38-subtitles", "SUB"),
    ):
        path = os.path.join(ROOT, "plans", fname + ".md")
        if not os.path.exists(path):
            r.fail(f"plans/{fname}.md is missing")
            continue
        text = _utf8(path)
        ids = set(re.findall(r"\|\s*`(" + prefix + r"\.[A-Z0-9_]+(?:\.[A-Z0-9_]+)*)`\s*\|", text))
        total += len(ids)
        for i in sorted(ids):
            if pattern.match(i):
                continue
            if i in KNOWN_BAD_CHECK_IDS:
                seen_bad.add(i)
                continue
            r.fail(
                f"plans/{fname}.md: {i} has {i.count('.') + 1} segment(s); the schema "
                f"requires at least three ({prefix}.<GROUP>.<NAME>)"
            )

    # A suppression entry that no longer matches anything means the id was
    # renamed (fixed) or the plan changed; either way the entry is stale and
    # should be removed rather than left to hide a future rename back.
    for i in sorted(KNOWN_BAD_CHECK_IDS - seen_bad):
        r.fail(f"{i} is in KNOWN_BAD_CHECK_IDS but no longer appears un-conforming; "
               f"remove the entry")
    r.note = (f"{total} check_id(s), {len(seen_bad)} known non-conforming "
              f"(owner decision, see contracts/README.md gap 4)")
    return r


CHECKS = [
    ("json", check_json, "every contracts/**.json parses"),
    ("payloads", check_payload_refs, "every topic schema_ref/data_schema_ref names a real file"),
    ("checkids", check_check_ids, "check_id values in the QC plans match the schema pattern"),
    ("links", check_links, "every relative markdown link resolves"),
    ("catalog", check_catalog, "service/topic ids agree across catalog, docs/02, topics"),
    ("families", check_families, "action-family structural invariants"),
    ("scaffold", check_scaffold, "generated files match the catalog"),
    ("examples", check_examples, "contracts/examples/ validate against their schemas"),
    ("registry", check_registry, "check-registry.v2.json passes load-time validation, if present"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated check names")
    args = ap.parse_args()

    if args.list:
        for n, _, d in CHECKS:
            print(f"{n:10} {d}")
        return 0

    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    selected = [c for c in CHECKS if not wanted or c[0] in wanted]
    if wanted - {c[0] for c in CHECKS}:
        print(f"unknown check(s): {sorted(wanted - {c[0] for c in CHECKS})}")
        return 2

    env_problem = False
    failed = False
    for name, fn, desc in selected:
        try:
            r = fn(name)
        except Exception as e:
            print(f"ERROR {name:10} {type(e).__name__}: {e}")
            env_problem = True
            continue
        if r.failures:
            failed = True
            print(f"FAIL  {name:10} {len(r.failures)} problem(s)  [{r.note}]")
            for f in r.failures[:20]:
                print(f"        {f}")
            if len(r.failures) > 20:
                print(f"        ... and {len(r.failures) - 20} more")
        else:
            print(f"ok    {name:10} {r.note}")

    print()
    if env_problem:
        print("environment problem; at least one check did not run")
        return 2
    if failed:
        return 1
    print(f"{len(selected)} check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
