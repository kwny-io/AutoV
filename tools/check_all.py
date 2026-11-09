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
import contextlib
import glob
import hashlib
import importlib.util
import io
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
    """One check's outcome.

    `skipped` is for a check that is wired up and correct but had nothing to act
    on: `registry` with no data file, for instance. It is not a failure (there is
    nothing wrong with the repo) and it is not a pass either — nothing was
    verified. The distinction has to survive into the summary line, because
    "{N} check(s) passed" that counts a no-op check as passed is exactly the
    "a check that did not run must never read as green" failure this module's
    docstring warns about.
    """

    def __init__(self, name):
        self.name = name
        self.failures = []
        self.note = ""
        self.skipped = False

    def fail(self, msg):
        self.failures.append(msg)

    def skip(self, why):
        self.skipped = True
        self.note = why


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
    """Every relative markdown link resolves.

    Scope has two invisible edges, and both are stated in the note because a
    count that quietly covers less than it appears to is the failure this
    module's docstring warns about:

    - `docs/design-package/` is skipped on purpose. It is the archived original
      design package, read-only by AGENTS.md, so a broken link in it is not
      something this repo may repair. Counted below so the skip is visible.
    - `glob("**/*.md")` does not descend into dot-directories. That happens to
      exclude `.claude/worktrees/` -- other sessions' checkouts, which are not
      this checkout's content -- but it is an accident of the glob, not a
      decision. So the loop below walks the tree separately and requires every
      md the glob missed to be under `.claude/worktrees/`; anything else hidden
      in a dot-directory fails instead of going unread.
    """
    r = Result(name)
    fence = re.compile(r"(?ms)^ {0,3}(`{3,})[^`\n]*$.*?(?:^ {0,3}\1`*[ \t]*$|\Z)|^ {0,3}(~{3,})[^\n]*$.*?(?:^ {0,3}\2~*[ \t]*$|\Z)")
    inline = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\1).)+?\1(?!`)", re.S)
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
    total = 0
    skipped_files = 0
    skipped_links = 0
    skipped_broken = []
    for p in sorted(glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        if rel.startswith(("docs/design-package/", ".git/")) or "node_modules/" in rel:
            # Read-only archive: counted, not checked. A broken link here is
            # reported in the note rather than failed, so it stays visible
            # without turning the suite red for something nobody may edit.
            if rel.startswith("docs/design-package/"):
                skipped_files += 1
                text = inline.sub("", fence.sub("", _utf8(p)))
                base = os.path.dirname(p)
                for m in link.finditer(text):
                    t = m.group(1)
                    if t.startswith(("http://", "https://", "mailto:", "#")):
                        continue
                    t = t.split("#")[0]
                    if not t:
                        continue
                    skipped_links += 1
                    if not os.path.exists(os.path.normpath(os.path.join(base, t))):
                        skipped_broken.append(rel)
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
    # What the glob could not see, counted rather than assumed. A dot-directory
    # is skipped by `glob("**/*.md")`; that is only acceptable for
    # `.claude/worktrees/` (other sessions' checkouts). Anywhere else, an md
    # would sit unchecked and read as covered.
    scanned = {os.path.relpath(p, ROOT).replace(os.sep, "/")
               for p in glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)}
    walked = set()
    for dirpath, _, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        if rel_dir == ".git" or rel_dir.startswith(".git/"):
            continue
        for fn in filenames:
            if fn.endswith(".md"):
                walked.add(os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/"))
    hidden = walked - scanned
    stray = sorted(h for h in hidden if not h.startswith(".claude/worktrees/"))
    for h in stray:
        r.fail(f"{h}: hidden from the glob by a dot-directory, so its links "
               f"are unchecked (only .claude/worktrees/ may hide)")

    r.note = (f"{total} local link(s)"
              f"; docs/design-package/ ({skipped_files} md, {skipped_links} link(s)) "
              f"is read-only archive, not checked"
              + (f" -- {len(skipped_broken)} broken there" if skipped_broken else "")
              + f"; {len(walked)} md walked, {len(hidden)} hidden by dot-dirs "
                f"({len(hidden) - len(stray)} under .claude/worktrees/, {len(stray)} elsewhere)")
    return r


def check_catalog(name):
    r = Result(name)
    cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))["services"]
    ids = [s["service_id"] for s in cat]

    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        r.fail(f"duplicate service_id: {dupes}")

    # docs/02 is the human-readable mirror of the catalog and must not drift.
    # The old test was `if i not in doc02` over the whole file text, which an id
    # passes if it appears anywhere at all -- in a sentence, in an example, in a
    # stray mention -- so a row could be deleted or its name/domain/owner/team
    # edited and the check stayed green. Parse the table and compare the columns
    # it mirrors.
    doc02 = _utf8(os.path.join(ROOT, "docs", "02-service-catalog.md"))
    row = re.compile(
        r"^\|\s*(S\d{2})\s*\|\s*\[([^\]]*)\]\(([^)]*)\)\s*\|\s*([^|]*?)\s*\|"
        r"\s*`([^`]*)`\s*\|\s*([^|]*?)\s*\|\s*$", re.M)
    mirrored = {}
    for m in row.finditer(doc02):
        mirrored[m.group(1)] = dict(name=m.group(2), link=m.group(3),
                                    domain=m.group(4), schema=m.group(5),
                                    team=m.group(6))
    for s in cat:
        sid = s["service_id"]
        if sid not in mirrored:
            r.fail(f"{sid} in catalog but has no row in docs/02")
            continue
        got = mirrored[sid]
        for field, want, have in (
            ("name", s["name"], got["name"]),
            ("link", f"../services/{s['slug']}/service.md", got["link"]),
            ("domain", s["domain"], got["domain"]),
            ("schema_owner", s["schema_owner"], got["schema"]),
            ("runtime_owner_team", s["runtime_owner_team"], got["team"]),
        ):
            if want != have:
                r.fail(f"docs/02 {sid} {field}: catalog says {want!r}, "
                       f"docs/02 says {have!r}")
    for sid in sorted(set(mirrored) - set(ids)):
        r.fail(f"docs/02 has a row for {sid}, which is not a catalog service_id")

    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    known = set(ids)
    seen_types = {}
    with_consumers = 0
    for t in topics:
        for field in ("producer",):
            owner = t.get(field)
            # A topic with no producer at all used to pass silently: the `if
            # owner and ...` guard skipped both the ownership test and any
            # complaint about the missing field. Every topic has one today, so
            # this never fired, but a topic added without one would have gone
            # green. `event_type` has the same shape and the same guard.
            if not owner:
                r.fail(f"topic {t.get('topic')}: no {field}")
            elif owner not in known:
                r.fail(f"topic {t.get('topic')}: {field} {owner!r} is not a catalog service_id")
        consumers = t.get("consumers")
        if consumers:
            with_consumers += 1
            for c in consumers:
                if c not in known:
                    r.fail(f"topic {t.get('topic')}: consumer {c!r} is not a catalog service_id")
        et = t.get("event_type")
        if not et:
            r.fail(f"topic {t.get('topic')}: no event_type")
        elif et in seen_types:
            r.fail(f"event_type {et!r} registered twice")
        else:
            seen_types[et] = t.get("topic")

    # `service-catalog.v2.json` says which event types each service publishes,
    # and `event-topics.v2.json` says which service produces each topic. Those
    # are the same fact recorded twice, and nothing compared them: a service
    # could gain a row in the topic table without its catalog entry following,
    # or the catalog could claim an event type that no topic carries, and both
    # files would each still parse.
    #
    # They are not the same shape, which is the trap: the catalog lists
    # *event_type*s (`access.grant.changed`), the topic table lists *topics*
    # (`drama.tenant-access.v2`) and carries the event_type alongside. Comparing
    # the two by set membership straight across reports all 48 services as
    # disagreeing -- I did exactly that first. Resolve to event_type on both
    # sides.
    types_by_producer = {}
    for t in topics:
        et = t.get("event_type")
        if et:
            types_by_producer.setdefault(t.get("producer"), set()).add(et)
    published_total = 0
    for s in cat:
        claimed = set(s.get("published_events") or [])
        published_total += len(claimed)
        actual = types_by_producer.get(s["service_id"], set())
        for et in sorted(claimed - actual):
            r.fail(f"{s['service_id']} catalog lists {et!r} in published_events, "
                   f"but no topic it produces has that event_type")
        for et in sorted(actual - claimed):
            r.fail(f"{s['service_id']} produces {et!r}, but its catalog row does "
                   f"not list it in published_events")

    # `depends_on` had no check at all: 125 edges whose only requirement was
    # that the file parses. A typo'd or retired service id there is a dependency
    # on nothing, and the scaffold below happily copies it into 48 service.yaml
    # files. Reachability is deliberately NOT tested -- S26 <-> S27 is a real
    # cycle that plans/S26 §8 says is broken by events (dispatch grant), not by
    # removing an edge, so a cycle here is not automatically a defect.
    edges = 0
    for s in cat:
        sid = s["service_id"]
        deps = s.get("depends_on") or []
        dupes = sorted({d for d in deps if deps.count(d) > 1})
        if dupes:
            r.fail(f"{sid} lists the same depends_on target twice: {dupes}")
        for d in deps:
            edges += 1
            if not isinstance(d, str):
                r.fail(f"{sid} depends_on has a non-string entry {d!r}")
            elif d == sid:
                r.fail(f"{sid} depends on itself")
            elif d not in known:
                r.fail(f"{sid} depends_on {d!r}, which is not a catalog service_id")

    # No topic declares `consumers`, so the loop above has never had an entry to
    # check: the "consumers agree with the catalog" half of this check is dead
    # code. Adding the key is an owner decision (policy.revision-published.v2's
    # POL-* `fix_shape` says so), so the honest thing is to say it out loud
    # rather than let the green line imply the consumer leg ran.
    r.note = (f"{len(ids)} service(s), {len(topics)} topic(s), "
              f"{published_total} published_event(s), {edges} depends_on edge(s)")
    if not with_consumers:
        r.note += "  [producer-only: no topic declares consumers]"
    return r


def check_families(name):
    """Four of the five invariants this file states about itself.

    `fallback` is deliberately allowed to be human_review (and often is), and
    `forbidden` deliberately names actions outside the chain; that is the whole
    point of forbidding one. Both were wrong in an earlier draft of this check:
    the assertions were written from memory of "chain is the complete set of
    legal moves", which is not what the contract says.

    The fifth invariant is about *registry entries*, not about this file, so it
    cannot be evaluated until check-registry.v2.json exists: it belongs to
    validate_registry.py, which check_registry() runs. Saying "the five" here
    made a green line claim coverage it does not have.
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

    stated = len(fam.get("invariants") or [])
    r.note = (f"{len(closed)} family/families  [4 of the {stated} stated invariants; "
              f"the one about registry entries is check_registry's]")
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

    # Compare every rendered file against the working tree. The two inputs staged
    # above live under the temp root too, so they walk past here; counting them
    # made the note read 120 for 118 generated files, and the README quoted that
    # number back as its coverage claim.
    compared = 0
    for p in sorted(glob.glob(os.path.join(tmp, "**", "*"), recursive=True)):
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, tmp)
        if rel.split(os.sep)[0] == "contracts":
            continue
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
    verbose = io.StringIO()
    try:
        with contextlib.redirect_stdout(verbose):
            rc = mod.main()
    finally:
        sys.argv = saved
    if rc == 2:
        r.fail("cannot run (jsonschema missing?); this is not a pass")
    elif rc != 0:
        r.fail("at least one example failed its schema")
        for line in verbose.getvalue().splitlines():
            if line.startswith(("FAIL", "UNMAPPED")):
                r.fail(line)

    # validate_examples.py's local_registry() globs contracts/*.schema.json, which
    # is not recursive, so a schema in a subdirectory is never loaded and never
    # gets a chance to reject anything. Nothing $refs those files either, so an
    # unsatisfiable pattern inside one is invisible: no example is checked against
    # it and no other schema pulls it in. Making the glob recursive is a semantic
    # change (files that have never been enforced would start enforcing), so the
    # honest thing is to count them out loud until an owner decides.
    top_level = {os.path.basename(p)
                 for p in glob.glob(os.path.join(CONTRACTS, "*.schema.json"))}
    unloaded = [
        os.path.relpath(p, CONTRACTS)
        for p in glob.glob(os.path.join(CONTRACTS, "**", "*.schema.json"), recursive=True)
        if os.path.basename(p) not in top_level
    ]
    r.note = "contracts/examples/"
    if unloaded:
        by_dir = {}
        for rel in unloaded:
            d = os.path.dirname(rel).replace(os.sep, "/") or "."
            by_dir[d] = by_dir.get(d, 0) + 1
        summary = ", ".join(f"{n} in contracts/{d}/" for d, n in sorted(by_dir.items()))
        r.note += (f"  [{len(unloaded)} schema file(s) never loaded by the validator, "
                   f"counting contracts/** not contracts/examples/: {summary}]")

    # contracts/README.md quotes a count of examples in its nine-check summary,
    # and that count went 57 -> 63 -> 66 while the directory kept growing to 72,
    # because nothing tied the sentence to the files. The validator's own tally is
    # the source of truth; check that it closes over the directory so the number
    # can be copied from a green line instead of remembered.
    json_files = glob.glob(os.path.join(CONTRACTS, "examples", "*.json"))
    on_disk = len(json_files)
    # "N file(s) on disk" used to mean N JSON files while the directory held two
    # more, both YAML. The number read as a directory total and was not one --
    # `safety` covers runtime-safety.yaml, nothing covers service-interactions.yaml,
    # and neither was mentioned anywhere on this line. Report the exclusion.
    every = [p for p in glob.glob(os.path.join(CONTRACTS, "examples", "*"))
             if os.path.isfile(p)]
    skipped = sorted(os.path.basename(p) for p in every if not p.endswith(".json"))
    stems = {os.path.basename(p).split(".")[0] for p in json_files}
    mapped = {s for s in stems if s in mod.SCHEMA_OF}
    tally = re.search(r"(\d+) example\(s\) valid, (\d+) failing, (\d+) without a schema",
                      verbose.getvalue())
    if tally:
        r.note += (f"  {tally.group(1)} valid, {tally.group(3)} without a schema; "
                   f"{on_disk} json file(s) of {len(every)} on disk, "
                   f"{len(stems)} stem(s), {len(mapped)} mapped")
    else:
        r.note += (f"  {on_disk} json file(s) of {len(every)} on disk, "
                   f"{len(stems)} stem(s), {len(mapped)} mapped")
    if skipped:
        r.note += f"  [not JSON-schema examples, so not validated here: {', '.join(skipped)}]"
    if len(mapped) < len(stems):
        r.fail(f"{sorted(stems - mapped)} is on disk but missing from SCHEMA_OF; "
               f"the validator prints UNMAPPED for it and the README's count "
               f"cannot close")
    return r


def check_maturity(name):
    """`maturity` must describe the tree it is written into.

    README.md tells a reader that this repo is at the skeleton stage and that
    every service's `maturity` is `design_only`. Both halves are statements
    about the working tree, and nothing compared them against it: the value is
    copied straight from the catalog by the generator, so it says what the
    catalog author intended, not what is on disk. A service that gains code
    without the catalog being bumped would still read `design_only`, and a
    catalog bumped ahead of the code would read as more finished than it is.

    The two directions are not equally bad, so they are not treated equally.
    Code without a bumped maturity hides progress (the repo looks less done
    than it is, so nobody runs its tests). A bumped maturity with an empty
    `src/` claims progress that isn't there (AGENTS.md's "不能把 schema 校验
    通过当作分布式正确性证明" is the same failure one level down).
    """
    r = Result(name)
    cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))["services"]
    states = {}
    for s in cat:
        states[s["maturity"]] = states.get(s["maturity"], 0) + 1

    with_code, without = [], []
    for s in cat:
        slug = s.get("slug") or s.get("service_id")
        src = os.path.join(ROOT, "services", slug, "src")
        files = []
        if os.path.isdir(src):
            for p in glob.glob(os.path.join(src, "**", "*"), recursive=True):
                if not os.path.isfile(p):
                    continue
                if os.path.basename(p) in (".gitkeep", ".keep"):
                    continue
                files.append(p)
        (with_code if files else without).append(s["service_id"])
        if files and s["maturity"] == "design_only":
            r.fail(f"services/{slug}/src/ holds {len(files)} file(s) but the catalog "
                   f"still says maturity=design_only; bump the catalog")
        if not files and s["maturity"] != "design_only":
            r.fail(f"services/{slug}/src/ is empty but the catalog says "
                   f"maturity={s['maturity']!r}; the catalog is ahead of the tree")

    # The README's sentence is only true while every service is design_only.
    readme = _utf8(os.path.join(ROOT, "README.md"))
    design_only = states.get("design_only", 0)
    if design_only == len(cat):
        if "所有服务的 `maturity` 均为 `design_only`" not in readme:
            r.fail("every service is design_only but README.md no longer says so")
    elif "所有服务的 `maturity` 均为 `design_only`" in readme:
        r.fail(f"README.md still says every service is design_only, but "
               f"{len(cat) - design_only} of {len(cat)} are not")

    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(states.items()))
    r.note = f"{len(cat)} service(s): {breakdown}; {len(with_code)} with code in src/"
    return r


def check_safety_switches(name):
    """`contracts/examples/runtime-safety.yaml` really does say "all off".

    AGENTS.md opens with "所有生产开关默认关闭" and lists what must never happen:
    bypass S26 for model calls, bypass S23/S22 for payment, pre-fill a provider
    as `active`. This file is the machine-readable form of that paragraph --
    `plans/S43:3` cites its `external_publish_enabled: false` for "外部分发一律
    默认关闭", and `plans/S27:24` cites its `accepted_manifest_authority: S08` as
    the authority that S27 does *not* hold. Nothing read the file. Every check in
    this module globs `.json`; `examples` globs `*.json` too, so both YAML files
    in that directory were skipped by a detail of the glob.

    A flag flipped to `true` here is not a broken link -- it is the repo claiming
    a production switch is on. That failure is silent everywhere else: the file
    parses, no schema covers it, and the plans keep citing it as if it still said
    false. So every gate named in the YAML block is asserted to be `False` by
    name, not merely counted -- a count of "4 false values" stays 4 when one true
    and one false are swapped around a rename.

    Only the closed set below is asserted. If a new gate appears in the file it
    is not silently ignored: it shows up in the note as unrestrained. Refusing to
    fail on an unknown key keeps this a check on the switches AGENTS.md names,
    not a schema that would reject a legitimate addition by an owner.
    """
    r = Result(name)
    path = os.path.join(CONTRACTS, "examples", "runtime-safety.yaml")
    if not os.path.exists(path):
        r.fail("contracts/examples/runtime-safety.yaml is missing")
        return r
    try:
        import yaml
    except ImportError:
        r.fail("PyYAML is not installed, so the safety switches cannot be read; "
               "this is not a pass")
        return r
    doc = yaml.safe_load(_utf8(path))

    # Each is False in AGENTS.md's terms: off by default, not enabled by a flag.
    GATES = ("real_provider_calls_enabled", "business_services_deployed",
             "external_publish_enabled", "real_credential_values_present")
    gates_ok = 0
    for g in GATES:
        if g not in doc:
            r.fail(f"{g} is not in runtime-safety.yaml; the file no longer states it")
        elif doc[g] is not False:
            r.fail(f"{g} is {doc[g]!r}, not false -- AGENTS.md requires every "
                   f"production switch closed by default")
        else:
            gates_ok += 1

    # AGENTS.md: "预填供应商状态为 active" is forbidden. Every provider entry in
    # this file must say enabled: false; a provider that is simply absent from
    # `providers` is not enabled, so this only inspects what is declared.
    providers = doc.get("providers") or {}
    on = [k for k, v in providers.items() if (v or {}).get("enabled") is not False]
    for k in on:
        r.fail(f"provider {k!r} is not explicitly enabled: false -- providers are "
               f"always inactive until an operator verifies them")
    no_key = [k for k, v in providers.items() if "enabled" not in (v or {})]
    for k in no_key:
        r.fail(f"provider {k!r} has no `enabled` key, so its state cannot be read")
    prov_off = len(providers) - len({*on, *no_key})

    # A provider entry put in the wrong block is invisible to the loop above, so
    # it is caught by shape instead of by name: any mapping nested under
    # enterprise_defaults that carries an `enabled` key is a provider that
    # escaped `providers`. Probe 1b found this the hard way -- appending
    # `smuggled_provider: {enabled: true}` puts it here and nothing noticed.
    defaults_early = doc.get("enterprise_defaults") or {}
    misplaced = sorted(k for k, v in defaults_early.items()
                       if isinstance(v, dict) and "enabled" in v)
    for k in misplaced:
        r.fail(f"enterprise_defaults.{k} is a mapping with an `enabled` key; "
               f"provider entries belong under `providers`, where they are checked")

    # The enterprise defaults name which service owns each authority. Those are
    # service ids, so a rename in the catalog that misses this file would leave a
    # dangling authority nobody owns. Checked against the catalog rather than a
    # hardcoded list.
    cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))["services"]
    ids = {s["service_id"] for s in cat}
    defaults = defaults_early
    auth = sorted(k for k in defaults if k.endswith("_authority"))
    dangling = [k for k in auth if defaults[k] not in ids]
    for k in dangling:
        r.fail(f"enterprise_defaults.{k} = {defaults[k]!r} is not a catalog service_id")

    # `shared_database_writes` is the file's copy of AGENTS.md's first hard rule
    # (no cross-service DB writes). It is a gate too, just nested one level down.
    db_ok = defaults.get("shared_database_writes") is False
    if not db_ok:
        r.fail(f"enterprise_defaults.shared_database_writes is "
               f"{defaults.get('shared_database_writes')!r}, not false -- AGENTS.md "
               f"forbids cross-service database writes")

    obvious = set(GATES) | {"shared_database_writes"}
    unrestrained = sorted(k for k in defaults if isinstance(defaults[k], bool)
                          and k not in obvious)
    # Every clause is passing/total. A bare "4 gate(s) false" would stay 4 while
    # one gate turned true and another was renamed into its place -- and the
    # rename is the case the first draft missed entirely, r.fail-ing correctly
    # while all three clauses of its note stayed green. Fifth time in this module.
    r.note = (f"{gates_ok}/{len(GATES)} gate(s) false, "
              f"{db_ok:d}/1 shared_database_writes false, "
              f"{prov_off}/{len(providers)} provider(s) explicitly disabled, "
              f"{len(auth) - len(dangling)}/{len(auth)} authorities name a catalog "
              f"service, {0 if misplaced else 1}/1 no provider entry outside "
              f"`providers`"
              + (f"; {len(unrestrained)} boolean default(s) not asserted: "
                 + ", ".join(unrestrained) if unrestrained else ""))
    return r


def check_interactions(name):
    """`contracts/examples/service-interactions.yaml` names real services, topics
    and authorities, in the order the paid-admission saga requires.

    This is the repo's only machine-readable statement of "who calls whom". It
    was read by nothing: every check here globs `.json` (`examples` globs
    `*.json`), so both YAML files in that directory were invisible. `safety`
    took `runtime-safety.yaml`; this covers the other one.

    Four things in it can go wrong without any other check noticing:

    - a service id typed as S99 or renamed in the catalog;
    - an event name that no topic declares -- the step then describes a message
      nothing ever publishes;
    - an event step whose `from` is not that topic's `producer`, which draws an
      arrow from a service that cannot send it;
    - an `action` step addressed to a service that is not the authority step
      `runtime-safety.yaml` names. AGENTS.md forbids bypassing S23/S22 for
      payment and forbids making any service a second authority; a grant issued
      before the budget is reserved, or a release sent to whoever is nearest, is
      exactly that, and it reads like a plausible diagram.

    The last one is checked by name rather than by a rule, because there is no
    action registry to derive ownership from: `docs/04` describes the saga in
    prose and `plans/S22:516-527` tabulates it. The map below is those two steps
    that have a single declared owner in `runtime-safety.yaml`; the remaining
    action names have none declared and are counted, not guessed at.
    """
    r = Result(name)
    path = os.path.join(CONTRACTS, "examples", "service-interactions.yaml")
    if not os.path.exists(path):
        r.fail("contracts/examples/service-interactions.yaml is missing")
        return r
    try:
        import yaml
    except ImportError:
        r.fail("PyYAML is not installed, so the interactions cannot be read; "
               "this is not a pass")
        return r
    try:
        doc = yaml.safe_load(_utf8(path))
    except Exception as exc:
        r.fail(f"service-interactions.yaml does not parse: {exc}")
        return r

    cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))["services"]
    ids = {s["service_id"] for s in cat}
    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    producer = {t["event_type"]: t.get("producer") for t in topics
                if t.get("event_type")}
    safety = yaml.safe_load(_utf8(
        os.path.join(CONTRACTS, "examples", "runtime-safety.yaml")))
    defaults = safety.get("enterprise_defaults") or {}

    interactions = doc.get("interactions") or []
    if not interactions:
        r.fail("service-interactions.yaml has no interactions")
        return r

    steps = [(it.get("id"), n, st)
             for it in interactions
             for n, st in enumerate(it.get("steps") or [], 1)]
    if not steps:
        r.fail("service-interactions.yaml declares interactions but no steps")
        return r

    refs_ok = 0
    refs_bad = 0
    for iid, n, st in steps:
        where = f"{iid} step {n}"
        for side in ("from", "to"):
            svc = st.get(side)
            if svc is None:
                r.fail(f"{where} has no `{side}` service")
                refs_bad += 1
            elif svc not in ids:
                r.fail(f"{where} `{side}: {svc}` is not a catalog service_id")
                refs_bad += 1
            else:
                refs_ok += 1

    # Exactly one of action/event per step: a step with both says two things, a
    # step with neither is a line drawing. Neither is a schema failure here.
    shape_bad = 0
    for iid, n, st in steps:
        kinds = [k for k in ("action", "event") if k in st]
        if len(kinds) != 1:
            r.fail(f"{iid} step {n} has {kinds or 'neither `action` nor `event`'}; "
                   f"each step carries exactly one")
            shape_bad += 1
    shape_ok = len(steps) - shape_bad

    evt_ok = evt_bad = prod_ok = prod_bad = 0
    for iid, n, st in steps:
        if "event" not in st:
            continue
        et = st["event"]
        if et not in producer:
            r.fail(f"{iid} step {n} emits {et!r}, which no topic in "
                   f"event-topics.v2.json declares")
            evt_bad += 1
            continue
        evt_ok += 1
        if st.get("from") != producer[et]:
            r.fail(f"{iid} step {n} draws {et!r} from {st.get('from')}, but that "
                   f"topic's producer is {producer[et]}")
            prod_bad += 1
        else:
            prod_ok += 1
    # The producer clause is only meaningful for names that exist, so its
    # denominator is evt_ok, not the total. With a typo'd name both clauses would
    # otherwise drop together and the line would read as two separate defects.
    producer_total = evt_ok or 1

    # Each action here has exactly one owning authority declared in
    # runtime-safety.yaml; a step addressed elsewhere has bypassed it.
    OWNER = {
        "request_acceptance_CAS": "accepted_manifest_authority",
        "reserve_budget": "financial_authority",
        "confirm_authorization": "financial_authority",
        "accrue_and_settle": "financial_authority",
        "acquire_permit": "quota_authority",
        "confirm_dispatch_permit": "quota_authority",
        "release_confirmed_terminal_permit": "quota_authority",
    }
    own_ok = own_bad = 0
    for iid, n, st in steps:
        want_key = OWNER.get(st.get("action"))
        if want_key is None:
            continue
        want = defaults.get(want_key)
        if want is None:
            r.fail(f"runtime-safety.yaml declares no {want_key}, so "
                   f"{iid} step {n} ({st['action']}) has no authority to check against")
            own_bad += 1
        elif st.get("to") != want:
            r.fail(f"{iid} step {n} sends {st['action']} to {st.get('to')}, but "
                   f"{want_key} is {want} -- AGENTS.md forbids bypassing the authority")
            own_bad += 1
        else:
            own_ok += 1

    # plans/S22:516-527 orders the seven paid-admission steps. A grant issued
    # before the budget reservation, or a settlement before the grant, is the
    # "绕过 S23/S22 直接付费" AGENTS.md forbids -- and a reordered YAML still
    # reads as a reasonable diagram.
    #
    # `reserve_budget < acquire_permit` is the one pair the first draft omitted:
    # the two are adjacent, so swapping them broke none of the other clauses and
    # the check stayed green. plans/S22:521-522 numbers them steps 1 and 2, and
    # its failure table only makes sense in that order -- "步 2 失败（S23 已预留）"
    # says the budget is already held when the slot cannot be taken. Reverse
    # them and taking the slot can fail with nothing reserved, which is not a
    # compensation path the plan describes.
    ORDER = [
        ("reserve_budget", "acquire_permit"),
        ("reserve_budget", "confirm_authorization"),
        ("acquire_permit", "confirm_dispatch_permit"),
        ("confirm_authorization", "issue_fenced_dispatch_grant"),
        ("confirm_dispatch_permit", "issue_fenced_dispatch_grant"),
        ("issue_fenced_dispatch_grant", "accrue_and_settle"),
        ("issue_fenced_dispatch_grant", "release_confirmed_terminal_permit"),
    ]
    pos = {}
    for it in interactions:
        if it.get("id") != "paid_admission":
            continue
        for n, st in enumerate(it.get("steps") or []):
            if "action" in st:
                pos.setdefault(st["action"], n)
    order_ok = order_bad = 0
    for before, after in ORDER:
        a, b = pos.get(before), pos.get(after)
        if a is None or b is None:
            r.fail(f"paid_admission no longer contains both {before!r} and "
                   f"{after!r}, so plans/S22:516-527's order cannot be confirmed")
            order_bad += 1
        elif a > b:
            r.fail(f"paid_admission runs {before!r} (step {a + 1}) after "
                   f"{after!r} (step {b + 1}); plans/S22:516-527 puts it first")
            order_bad += 1
        else:
            order_ok += 1

    act_total = sum(1 for _, _, st in steps if "action" in st)
    evt_total = sum(1 for _, _, st in steps if "event" in st)
    # Every clause is passing/total -- see the note in `safety`.
    r.note = (f"{len(interactions)} interaction(s), {len(steps)} step(s); "
              f"{refs_ok}/{refs_ok + refs_bad} endpoint(s) name a catalog service, "
              f"{shape_ok}/{len(steps)} step(s) carry exactly one action-or-event, "
              f"{evt_ok}/{evt_total} event name(s) declared by a topic, "
              f"{prod_ok}/{producer_total} event(s) emitted by that topic's producer, "
              f"{own_ok}/{own_ok + own_bad} action(s) addressed to the declared "
              f"authority, {order_ok}/{order_ok + order_bad} paid-admission "
              f"ordering constraint(s) hold"
              + (f"; {act_total - own_ok - own_bad} of {act_total} action(s) have "
                 f"no declared owner and are not checked here"
                 if act_total > own_ok + own_bad else ""))
    return r


# The numbers contracts/README.md quotes in its check summary, pinned to the
# numbers the checks produce. Each entry is (label as README says it, regex
# over the README summary paragraph, the check whose Result supplies the truth,
# regex over that check's note).
#
# The note regexes are the coupling risk: they are matched against strings
# formatted for a human reading `ok examples ...`, so rewording a check's note
# breaks this check even though nothing is wrong. It fails loudly and says
# which label it could not read, rather than silently skipping it, because a
# count that quietly stops being compared is the exact failure this check exists
# to prevent. If a note changes shape, update the regex here.
README_CLAIMS = [
    ("JSON 解析", r"`json`（(\d+) 个 JSON 解析）", "json", r"(\d+) file"),
    ("schema_ref", r"`payloads`（(\d+) 条 `schema_ref`", "payloads", r"(\d+) schema ref"),
    ("payload topic", r"其中 (\d+) 个 topic 带 payload schema", "payloads", r"(\d+) topic\(s\) with a payload"),
    ("openapi file ref", r"(\d+) 处指向真实文件", "openapi", r"(\d+) naming a file"),
    ("topic field", r"55 个 topic 必须各带 (\d+) 个字段", "topics", r"(\d+) required field"),
    ("secret class", r"(\d+) 个 secret class 的", "redaction", r"(\d+) secret class"),
    ("MCP tool", r"(\d+) 个工具的 `readOnlyHint`", "mcp", r"(\d+) distinct tool\(s\)"),
    ("check_id", r"(\d+) 个\s*\n?`check_id` 是否符合", "checkids", r"(\d+) check_id"),
    ("links", r"`links`（(\d+) 条相对链接", "links", r"(\d+) local link"),
    ("stated invariant", r"自己声明的\s*\n?(\d+) 条 invariant", "families", r"(\d+) stated invariant"),
    ("scaffold", r"`scaffold`（(\d+) 个生成物", "scaffold", r"(\d+) generated file"),
    ("example", r"`examples`（\*\*(\d+)\*\* 个示例", "examples", r"(\d+) valid"),
    ("unmapped example", r"另 (\d+) 个刻意不映射", "examples", r"(\d+) without a schema"),
    ("archive match", r"逐个比字节数并算哈希，\*\*(\d+)\*\* 个对得上",
     "archive", r"(\d+)/\d+ match MANIFEST\.md"),
    ("safety provider", r"\*\*(\d+)\*\* 个 provider 都显式",
     "safety", r"(\d+)/\d+ provider\(s\) explicitly disabled"),
    ("interaction step", r"3 段交互共 (\d+) 步",
     "interactions", r"(\d+) step\(s\)"),
    ("interaction endpoint", r"(\d+) 个端点必须都是真实",
     "interactions", r"(\d+)/(\d+) endpoint\(s\)"),
    ("plantopics plan file", r"`plans/\*\.md` 是设计散文，(\d+) 份方案里",
     "plantopics", r"(\d+) plan file\(s\)"),
    ("plantopics topic mention", r"`drama\.\*\.vN` 是注册表专属命名空间——(\d+) 处提及今天",
     "plantopics", r"(\d+) topic mention\(s\)"),
    ("plantopics unjudged tokens", r"带点反引号 token 共 (\d+) 处",
     "plantopics", r"(\d+) undeclared dotted token\(s\) are not judged"),
    ("plantopics filename tokens", r"其中 (\d+) 是文件名",
     "plantopics", r"(\d+) file name\(s\)"),
    ("plantopics versioned-doc tokens", r"(\d+) 是带版本的文档名",
     "plantopics", r"(\d+) versioned document name\(s\)"),
    ("plantopics event-or-action tokens", r"剩下 (\d+) 处「事件或动作形状」",
     "plantopics", r"(\d+) event-or-action shaped"),
    # contracts/README.md also restates what docsreadme covers. Pinning it here
    # is the same rule as the enumerated list: the number inside a check's own
    # description is still a hand-typed number.
    ("docsreadme count", r"`docs/README\.md` 的 (\d+) 条断言",
     "docsreadme", r"(\d+)/\d+ assertion\(s\) in docs/README\.md"),
    # The count in `citerefs`'s own description is hand-typed like every other,
    # so it is pinned the same way. It is small today (6) and that is the point:
    # it is the denominator the "all named" claim is a fraction of, and a
    # denominator that shrinks silently would make the fraction look better.
    ("citerefs citation", r"今天共 \*\*(\d+)\*\* 处引用", "citerefs", r"(\d+) check_all\.py citation"),
    ("planrefs ref", r"`planrefs`（[^）]*?今天共 \*\*(\d+)\*\* 处引用",
     "planrefs", r"^(\d+) ref\(s\)"),
    ("planrefs judged", r"`planrefs`（[^）]*?其中 \*\*(\d+)\*\* 处受检",
     "planrefs", r"\((\d+) judged\)"),
    # `470 个段号对，470/470 命中`: the two 470s are different quantities that
    # happen to be equal (pairs, and resolved pairs). These two rows pin the
    # hits (before the slash) and the denominator (after it). Neither pins the
    # leading `470 个段号对` -- that number is written into the pattern itself,
    # so editing only it in the README would go unnoticed. Left that way
    # deliberately: a third row matching the bare prefix would also match any
    # other `470 个段号对` sentence anyone adds later.
    ("planrefs pair", r"470 个段号对，(\d+)/\d+ 命中", "planrefs", r"(\d+)/\d+ resolve"),
    ("planrefs pair hit", r"470 个段号对，\d+/(\d+) 命中", "planrefs", r"(\d+)/\d+ resolve"),
    # The two "其中 N 处受检" sentences differ only by their lead-in, so each
    # claim is anchored on text unique to its own paragraph. Matching the bare
    # phrase let planrefs' 467 be read as docrefs' value.
    ("docrefs ref", r"`docrefs`（[^）]*?共 \*\*(\d+)\*\* 处引用", "docrefs", r"^(\d+) ref\(s\)"),
    ("docrefs judged", r"`docrefs`（[^）]*?其中 \*\*(\d+)\*\* 处受检", "docrefs", r"\((\d+) judged\)"),
    ("docrefs pair", r"(\d+) 个段号对：(\d+) 个命中标题", "docrefs",
     r"(\d+) section pair\(s\).*?(\d+) resolve to a heading"),
]


_CN_DIGITS = "零一二三四五六七八九"


def _cn_numeral(text):
    """Parse the small Chinese numerals this file's prose uses (一 .. 九十九)."""
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (_CN_DIGITS.index(tens) if tens else 1) * 10 + (
            _CN_DIGITS.index(ones) if ones else 0)
    return _CN_DIGITS.index(text)


def check_readme_counts(name):
    """The README's check summary must quote numbers the checks still produce.

    contracts/README.md opens its check section with a paragraph that states, per
    check, how many things that check looked at. Nothing tied those numbers to
    the checks: they were typed by hand and would drift every time a schema,
    example or link was added, the way the `examples` cell drifted 53 -> 63 -> 66
    while the directory reached 72. The rest of that file quotes counts too, but
    this paragraph is the one place they are all in one sentence, so this is
    where a reader forms an impression of coverage.

    Only the counts that are stable enough to pin are pinned. Deliberately not
    pinned: the "4 of 5" numerator (that 4 is a semantic decision — which
    invariant belongs to check_registry — not a count, and the 5 next to it is
    pinned as `stated invariant`), the `checkids` non-conforming count (a
    whitelist that is meant to shrink), and the passed/skipped split (it flips
    the moment a data file lands, and the sentence describing it says so).

    The paragraph's leading "N 项检查：" is pinned too. It went stale the moment a
    check was added — the first draft of this check said "ten" in its own anchor
    and broke itself — so the numeral is compared against len(CHECKS) instead of
    being typed here a second time.
    """
    r = Result(name)
    readme = _utf8(os.path.join(CONTRACTS, "README.md"))
    # Restrict to the summary paragraph, so a number in some later section that
    # happens to match the same pattern cannot satisfy or break this.
    head = readme.find("\n## 一个入口跑完所有仓库级检查")
    if head < 0:
        r.fail("contracts/README.md no longer has the '一个入口跑完所有仓库级检查' section")
        return r
    section = readme[head:readme.find("\n## ", head + 1)]
    opener = re.search(r"\n([一二三四五六七八九十]+)项检查：", section)
    if not opener:
        r.fail("contracts/README.md's check section no longer opens with "
               "'N 项检查：'; this check cannot find the summary paragraph")
        return r
    stated = _cn_numeral(opener.group(1))
    if stated != len(CHECKS):
        r.fail(f"README says {opener.group(1)}项检查, but {len(CHECKS)} checks are "
               f"registered; --list is the authority")
    # The same total is restated further down, as "十九项里 `registry` 那一格…".
    # That copy was typed by hand and went stale the round a check was added --
    # the opener above was pinned, this one was not. Same class as the count
    # list: two copies of one number, only one of them nailed down.
    tail_total = re.search(r"\n([一二三四五六七八九十]+)项里 `registry` 那一格", section)
    if not tail_total:
        r.fail("contracts/README.md no longer states 'N 项里 `registry` 那一格' "
               "about the skipped check")
    elif _cn_numeral(tail_total.group(1)) != len(CHECKS):
        r.fail(f"README's later sentence says {tail_total.group(1)}项里, but "
               f"{len(CHECKS)} checks are registered")
    start = head + opener.end()
    para = readme[start:readme.find("\n\n", start)]

    # A third copy of the same count, in the paragraph that explains this check:
    # "把本段里 N 个手敲的数字逐条和对应检查自己算出的数比". Changing that N alone
    # left this check green -- the summary paragraph and the enumerated list were
    # both pinned, but this sentence restates the number a third time and was not.
    # Same failure as the enumerated list, one copy further out.
    hand = re.search(r"把本段里 (\d+) 个手敲的数字", section)
    if not hand:
        r.fail("contracts/README.md's readme paragraph no longer says "
               "'把本段里 N 个手敲的数字'")
    else:
        stated_hand = int(hand.group(1))
        if stated_hand != len(README_CLAIMS):
            r.fail(f"README says {stated_hand} 个手敲的数字, but "
                   f"{len(README_CLAIMS)} claims are registered")

    results = {n: fn(n) for n, fn, _ in CHECKS if n in {c[2] for c in README_CLAIMS}}
    for label, readme_pat, check_name, note_pat in README_CLAIMS:
        claimed = re.search(readme_pat, para)
        if not claimed:
            r.fail(f"README summary no longer states the {label} count in the "
                   f"form this check reads; update README_CLAIMS or the prose")
            continue
        note = results[check_name].note
        actual = re.search(note_pat, note)
        if not actual:
            r.fail(f"cannot read the {label} count out of {check_name}'s note "
                   f"({note!r}); this check's extraction is stale")
            continue
        if int(claimed.group(1)) != int(actual.group(1)):
            r.fail(f"README says {claimed.group(1)} for {label}, {check_name} "
                   f"says {actual.group(1)}")

    # Below the summary paragraph the same numbers appear a second time, as a
    # hand-transcribed list ("那 N 个数是 ..."). That copy was unchecked: changing
    # `links` 1330 to 1331 there alone left this check reporting ok, because the
    # summary above still said 1330 and nothing compared the two. Pin the list to
    # the same notes, and pin its stated length to len(README_CLAIMS), so the copy
    # cannot drift away from the original.
    lst = re.search(r"那 (\d+) 个数是(.*?)。", readme, re.S)
    listed_note = ""
    if not lst:
        r.fail("contracts/README.md no longer enumerates the pinned counts "
               "('那 N 个数是 ...')")
    else:
        stated_n = int(lst.group(1))
        if stated_n != len(README_CLAIMS):
            r.fail(f"README's count list says {stated_n} numbers, but "
                   f"{len(README_CLAIMS)} claims are registered")
        entries = [x.strip() for x in re.split(r"[、\n]", lst.group(2)) if x.strip()]
        entries = [x for x in entries if re.search(r"\d", x)]
        if len(entries) != len(README_CLAIMS):
            r.fail(f"README's count list states {len(entries)} entries after "
                   f"splitting, but {len(README_CLAIMS)} claims are registered")
        # Each entry is "<label> <value>"; match the label to its claim by the
        # order both are written in, which is the order README_CLAIMS is declared.
        for (label, _, check_name, note_pat), entry in zip(README_CLAIMS, entries):
            mval = re.search(r"(\d+)\s*$", entry)
            if not mval:
                r.fail(f"README's count list entry {entry!r} does not end in a "
                       f"number")
                continue
            actual = re.search(note_pat, results[check_name].note)
            if not actual:
                continue  # already reported above
            if int(mval.group(1)) != int(actual.group(1)):
                r.fail(f"README's count list says {mval.group(1)} for {label}, "
                       f"{check_name} says {actual.group(1)}")
        listed_note = f"; count list {len(entries)} value(s)"

    # The '## 布局' block, near the top of the same file, states four counts in
    # the same hand-typed way. They were not covered by README_CLAIMS (that
    # table is anchored to the check summary), and one of them was wrong: the
    # block said "48 个 topic" while eleven other places in this file said 55
    # and the file holds 55. Reading the numbers from the sources rather than
    # from a check's note, because no check reports these four.
    lay = re.search(r"\n## 布局\n\n```text\n(.*?)```", readme, re.S)
    counts_note = ""
    if not lay:
        r.fail("contracts/README.md no longer has the '## 布局' block")
    else:
        block = lay.group(1)
        cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))
        services = cat["services"] if isinstance(cat, dict) else cat
        topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
        mcp = json.loads(_utf8(os.path.join(CONTRACTS, "mcp-tools.v2.design.json")))
        api = _utf8(os.path.join(CONTRACTS, "openapi.v2.yaml"))
        truth = [
            ("services", r"(\d+) 服务元数据", len(services)),
            ("topics", r"(\d+) 个 topic，", len(topics)),
            ("event_type", r"注册 (\d+) 个 event_type", len(topics)),
            ("mcp tools", r"(\d+) 个 MCP 工具", len(mcp["tools"])),
            ("openapi operations", r"OpenAPI 3\.1，(\d+) 个核心操作",
             len(re.findall(r"^\s{4}(?:get|post|put|patch|delete|head|options):", api, re.M))),
        ]
        for label, pat, actual in truth:
            m = re.search(pat, block)
            if not m:
                r.fail(f"README's layout block no longer states the {label} count "
                       f"in the form this check reads")
            elif int(m.group(1)) != actual:
                r.fail(f"README layout block says {m.group(1)} for {label}, "
                       f"the source file has {actual}")
        counts_note = f"layout block {len(truth)} count(s)"

    r.note = (f"{len(README_CLAIMS)} count(s) in contracts/README.md's summary"
              + listed_note
              + (f"; {counts_note}" if counts_note else ""))
    return r


def check_docs_readme(name):
    """docs/README.md states seven counts about its own directory. None were pinned.

    That file opens with "17 篇" and closes with a paragraph about the archive
    that gives `DESIGN.md` a line count and "48 份" twice. All seven are typed by
    hand and every one of them is checkable against a file on disk, but nothing
    read them: `readme` covers `contracts/README.md` only, and `archive` compares
    checksums without ever opening this file.

    Found on the first run: the archive paragraph said 850 lines for DESIGN.md.
    The file is 849 -- and so is the pristine copy under `_inbox/autov-design/`,
    so 850 was never true, not even before the import. The other six were right
    when measured (`17/17`, `13/13`, `48/48`, `48/48`, `849`), which is the
    reason this is worth a check rather than a one-off fix: six correct numbers
    next to one wrong one read as a proofread paragraph.

    Deliberately not judged: whether docs 01-17 are the *right* documents, and
    whether the `内容` column describes them correctly. Only the numbers are
    compared, because only the numbers can be computed.
    """
    r = Result(name)
    p = os.path.join(ROOT, "docs", "README.md")
    if not os.path.exists(p):
        r.fail("docs/README.md is missing")
        return r
    text = _utf8(p)
    pkg = os.path.join(ROOT, "docs", "design-package")
    # Every assertion below is counted in `total` the moment it is declared, and
    # only counted in `agreed` when it actually ran and matched. A conditionally
    # incremented counter would report a smaller number when an anchor vanished,
    # which reads as "fewer things to check" rather than "one of them did not
    # run" -- the 0/0 failure this module has made before.
    total = agreed = 0

    def assert_count(stated, actual, what):
        nonlocal total, agreed
        total += 1
        if stated == actual:
            agreed += 1
        else:
            r.fail(f"docs/README.md says {stated} for {what}, the file(s) say {actual}")
        return stated == actual

    # 1. "17 篇" -- how many numbered design docs there are.
    m = re.search(r"^(\d+) 篇，从设计包迁入", text, re.M)
    docs = sorted(glob.glob(os.path.join(ROOT, "docs", "[0-9][0-9]-*.md")))
    if not m:
        total += 1
        r.fail("docs/README.md no longer opens with 'N 篇，从设计包迁入'")
    else:
        assert_count(int(m.group(1)), len(docs), "the '17 篇' opener")

    # 2. The "全部文档" table must list exactly those docs -- not more, not fewer.
    total += 1
    table = set(re.findall(r"^\|.*?\((\d\d)-[\w-]+\.md\)", text, re.M))
    by_num = set(os.path.basename(d)[:2] for d in docs)
    if table == by_num:
        agreed += 1
    else:
        for n in sorted(by_num - table):
            r.fail(f"docs/README.md's 全部文档 table has no row for doc {n}")
        for n in sorted(table - by_num):
            r.fail(f"docs/README.md's 全部文档 table lists doc {n}, which is not "
                   f"on disk")

    # 3. "48 服务" in each doc-02 row -- the catalog is the authority.
    cat = json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))
    n_svc = len(cat["services"])
    rows = [ln for ln in text.splitlines()
            if "02-service-catalog.md" in ln and ln.lstrip().startswith("|")]
    if not rows:
        total += 1
        r.fail("docs/README.md has no row for 02-service-catalog.md")
    for row in rows:
        mm = re.search(r"(\d+) 服务", row)
        if not mm:
            total += 1
            r.fail(f"docs/README.md's doc-02 row no longer states 'N 服务': "
                   f"{row.strip()!r}")
            continue
        assert_count(int(mm.group(1)), n_svc,
                     f"the doc-02 row {row.strip()[:40]!r}")

    # 4. "13 个 Agent 角色" -- against the agent dirs AND the role list in docs/08.
    m = re.search(r"(\d+) 个 Agent 角色", text)
    agents = sorted(glob.glob(os.path.join(ROOT, "agents", "*", "agent.yaml")))
    stated_agents = None
    if not m:
        total += 1
        r.fail("docs/README.md no longer states 'N 个 Agent 角色' for doc 08")
    else:
        stated_agents = int(m.group(1))
        assert_count(stated_agents, len(agents), "'13 个 Agent 角色' vs agents/")
    d8 = os.path.join(ROOT, "docs", "08-agents-prompts.md")
    if not os.path.exists(d8):
        total += 1
        r.fail("docs/08-agents-prompts.md is missing")
    else:
        mm = re.search(r"(producer|writer)[^\n]*?(conversation-editor)", _utf8(d8))
        if not mm:
            total += 1
            r.fail("docs/08-agents-prompts.md no longer has its role list "
                   "('producer、writer、…、conversation-editor')")
        else:
            roles = [x.strip() for x in mm.group(0).split("、") if x.strip()]
            if stated_agents is None:
                total += 1
                r.fail("docs/README.md states no Agent count to compare against "
                       "docs/08's role list")
            else:
                assert_count(stated_agents, len(roles),
                             "'13 个 Agent 角色' vs docs/08's role list")

    # 5. The archive paragraph: one line count and two "48 份".
    def count_in_archive(pat, target, label, counter):
        nonlocal total, agreed
        mm = re.search(pat, text)
        if not mm:
            total += 1
            r.fail(f"docs/README.md's archive paragraph no longer gives "
                   f"{target} a count as {label}")
            return
        if not os.path.exists(target):
            total += 1
            r.fail(f"{target} is missing")
            return
        assert_count(int(mm.group(1)), counter(), f"{os.path.basename(target)} ({label})")

    count_in_archive(r"`DESIGN\.md`（(\d+) 行合并版）",
                     os.path.join(pkg, "DESIGN.md"), "'N 行合并版'",
                     lambda: len(_utf8(os.path.join(pkg, "DESIGN.md")).splitlines()))
    count_in_archive(r"`SERVICE-SPECIFICATIONS\.md`（(\d+) 份拼合）",
                     os.path.join(pkg, "SERVICE-SPECIFICATIONS.md"), "'N 份拼合'",
                     lambda: len(re.findall(
                         r"^#\s+S\d{2}\s+·",
                         _utf8(os.path.join(pkg, "SERVICE-SPECIFICATIONS.md")), re.M)))
    specs = sorted(glob.glob(os.path.join(pkg, "services", "*.md")))
    mm = re.search(r"`services/` 下 (\d+) 份原始服务规格", text)
    if not mm:
        total += 1
        r.fail("docs/README.md's archive paragraph no longer gives the raw spec "
               "count as 'services/ 下 N 份原始服务规格'")
    else:
        assert_count(int(mm.group(1)), len(specs), "the raw service specs")

    r.note = (f"{agreed}/{total} assertion(s) in docs/README.md agree with the "
              f"files on disk (7 numeric statements: {len(docs)} doc(s), {n_svc} "
              f"service(s) x2 rows, {len(agents)} agent(s), {len(specs)} raw "
              f"spec(s); plus 2 structural assertions)")
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
        r.skip("no check-registry.v2.json yet; nothing to validate")
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

# Ids cited by a QC plan that its owner's registry does not define. Each is a
# plan edit waiting on that plan's owner, not a defect this tool may fix: two are
# the same missing `SUB.LAYOUT.CPS_EXCEEDED` (S38 §4 step 3 and S35's validator
# note), one is `SUB.CUE.VERSION` in S35 -- S38 has no `CUE` group at all.
# Logged in delivery/acceptance/check-registry-missing-fields-2026-11-05.md
# §5.2. Suppressing them keeps the suite readable for everything else; the
# staleness test below forces the entry out once the plan is corrected.
KNOWN_DANGLING_REFS = {
    "SUB.CUE.VERSION",
    "SUB.LAYOUT.CPS_EXCEEDED",
    "SUB.TIMING.CPS",
}

# `docs/NN §X` refs that name no heading and are left for the owner, by the
# same rule as KNOWN_DANGLING_REFS above: recorded, not silently allowed, and
# the entry is itself a failure once the ref disappears or starts resolving.
# `docs/10 §1` is here because docs/10 has no numbered heading at all -- one
# `# 10` title and seven flat paragraphs -- so §1 can only mean "the first
# paragraph", a form that appears nowhere else in the corpus and that neither
# AGENTS.md nor any doc settles. Naming it as accepted would be this check
# choosing a side in a decision that is the owner's.
KNOWN_UNRESOLVED_DOCREFS = {
    "plans/S31-speech-production.md:5 cites docs/10 §1",
}

# The plans that own a check_id prefix, and the prefix each owns. A cited id is
# resolved against its prefix's registry no matter which plan cites it -- S35
# legitimately cites SUB.* ids when describing S38's behaviour.
QC_PLANS = (
    ("S35-technical-qc", "TECH"),
    ("S36-semantic-qc", "VIS"),
    ("S37-audio-alignment", "AUD"),
    ("S38-subtitles", "SUB"),
)


def check_payload_refs(name):
    """Every data_schema_ref / schema_ref a topic names must be a real file.

    A topic that points at a schema which does not exist is worse than one with
    no payload contract at all: consumers see a typed contract and the validator
    cannot load it, so the failure lands at read time in someone else's service.

    This walks topics forward only. The other direction -- a `contracts/*.schema.json`
    that no topic names -- is not a failure: 8 of the 64 are request/response bodies,
    reached from `contracts/openapi.v2.yaml` (11 `$ref`, 5 of them a whole body) and
    from `contracts/**/*.json` (6 `$ref`: 3 from sibling schemas, 3 from
    `mcp-tools.v2.design.json`). Every one of the 8 has at least one inbound
    `$ref`, so "named by no topic" is not the same as "unused". The count goes in the
    note because nothing else reports it, and the layout block that describes this
    directory used to call all 64 "single-topic payload schema".
    """
    r = Result(name)
    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    refs = 0
    with_payload = 0
    named = set()
    for t in topics:
        for key in ("schema_ref", "data_schema_ref"):
            ref = t.get(key)
            if not ref:
                continue
            refs += 1
            named.add(ref)
            if key == "data_schema_ref":
                with_payload += 1
            if not os.path.exists(os.path.join(CONTRACTS, ref)):
                r.fail(f"topic {t.get('topic')}: {key} -> {ref} does not exist")
    on_disk = {os.path.basename(p)
               for p in glob.glob(os.path.join(CONTRACTS, "*.schema.json"))}
    orphaned = sorted(on_disk - named)
    r.note = (f"{refs} schema ref(s), {with_payload} topic(s) with a payload schema; "
              f"{len(on_disk)} top-level schema file(s), "
              f"{len(orphaned)} of them named by no topic")
    return r


def check_topic_fields(name):
    """Every topic in event-topics.v2.json carries the ten fields its readers use.

    This file has no schema -- `contracts/*.schema.json` covers payloads, not the
    topic registry itself -- so nothing has ever validated its shape. The readers
    use `t.get(...)`, which means a missing key becomes `None` or `""` instead of
    an error: `render_yaml` would emit a `service.yaml` row whose `topic` is the
    empty string and every check would stay green.

    Missing fields fail. A field whose value stops matching the rest is not a
    failure: five of these are all the same value across all 55 topics today
    (`schema_ref` / `partition_key` / `delivery` / `replay_side_effects` /
    `pii_payload`), and `contracts/README.md` §16.31 uses that fact to argue they
    need not be copied into `service.yaml`. Deciding to make one differ is a
    design call, not a bug -- but it invalidates that argument, so the distinct
    count goes in the note and `readme` couples the prose to it.
    """
    r = Result(name)
    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    required = ("event_type", "topic", "producer", "schema_ref", "data_schema_ref",
                "partition_key", "delivery", "normal_retention_days",
                "replay_side_effects", "pii_payload")
    for t in topics:
        missing = [k for k in required if k not in t]
        if missing:
            r.fail(f"topic {t.get('event_type')!r} is missing {missing}")

    # A missing key is already a failure above; counting its absence as a second
    # "value" would make this note say 2 distinct where the real story is one
    # field gone, so only present values are compared.
    constant_claimed = ("schema_ref", "partition_key", "delivery",
                        "replay_side_effects", "pii_payload")
    worst = max((len({json.dumps(t[k]) for t in topics if k in t}) for k in constant_claimed),
                default=0)
    r.note = (f"{len(topics)} topic(s), {len(required)} required field(s); "
              f"{len(constant_claimed)} field(s) README calls constant, "
              f"max {worst} distinct value(s)")
    return r


def check_plan_topics(name):
    """`plans/*.md` names only topics `event-topics.v2.json` declares, and pairs
    each one with an event that topic actually carries.

    Both mistakes below were found by hand in `plans/S47-realtime-projection.md`,
    and nothing read plan prose for either:

    - An undeclared topic name. `drama.*.vN` is the registry's namespace, and no
      plan invents one -- every one of the 58 mentions names a declared topic, so
      an unknown name here is a typo rather than a proposal. S47 had two:
      `drama.connector-observations.v2` and `drama.review.v2`. Their `event_type`
      and 来源 columns were right, so nothing else noticed.
    - A topic paired with an event that belongs to a different topic. S47 had
      `drama.model-registry.v2` beside `model.route.selected`; that event lives on
      `drama.model-routing.v2`, with the same producer (S25) both ways -- which is
      exactly why the 来源 column agreed while the topic did not.

    Not checked, deliberately: event names the registry does not declare. As
    backtick tokens they are indistinguishable from permission actions
    (`asset.voice.authorize`, `platform.tenant.provision`), and plans legitimately
    propose new events and say so in the same line (`S04` 「新增，待登记」, `S06`,
    `S44`). The note therefore reports how large that unjudged surface is, split
    by shape, rather than pretending to cover it: most of it is not event names at
    all (file names such as `service.yaml`, versioned document names such as
    `shot-spec.v1`), and nothing in this repo declares permission action names, so
    the remainder cannot be separated either way. Four pairing cells reach this
    branch, and each is already reported as a gap by the plan that wrote it: the
    three in S47 are its own §11 (C47-12/13/14), and `S29:800`'s
    `image.job.state-changed` -- `S29` declares only `production.image.ready` --
    is its §12 C2, which says outright that three more events are needed.

    Scope is `plans/` alone. `delivery/` holds the corrections reports, which have
    to be able to quote a wrong name verbatim to say what was wrong.
    """
    r = Result(name)
    topics = json.loads(_utf8(os.path.join(CONTRACTS, "event-topics.v2.json")))["topics"]
    declared = {t["event_type"] for t in topics}
    events_of = {}
    for t in topics:
        events_of.setdefault(t["topic"], set()).add(t["event_type"])

    topic_re = re.compile(r"\bdrama\.[a-z0-9-]+\.v\d+\b")
    # `{1,4}` not `{2,4}`: two-part event names are real (`artifact.ingested`,
    # `prompt.compiled`), and requiring three parts silently read their cells as
    # empty -- S47's `artifact.ingested` looked unjudgeable for exactly that.
    _EVENT_IN_CELL = re.compile(r"`([a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,4})`")
    files = sorted(glob.glob(os.path.join(ROOT, "plans", "*.md")))
    mentions = 0
    undeclared = 0
    pair_lines = 0
    mispaired = 0
    unjudgeable = []
    # Size of the surface this check deliberately does not judge, split so the
    # note cannot read as if the whole token space were covered.
    _FILE_EXT = {"json", "yaml", "yml", "md", "sql", "proto", "sh", "py",
                 "toml", "txt", "xml", "lock", "ini"}
    shadow = {"filename": 0, "versioned-doc": 0, "event-or-action": 0}
    for p in files:
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        for lineno, line in enumerate(_utf8(p).splitlines(), 1):
            named = [m.group(0) for m in topic_re.finditer(line)]
            mentions += len(named)
            for t in named:
                if t not in events_of:
                    undeclared += 1
                    r.fail(f"{rel}:{lineno} names topic {t!r}, which "
                           f"event-topics.v2.json does not declare")
            for m in _EVENT_IN_CELL.finditer(line):
                tok = m.group(1)
                if tok in declared or tok in events_of:
                    continue
                tail = tok.rsplit(".", 1)[-1]
                if tail in _FILE_EXT:
                    shadow["filename"] += 1
                elif re.fullmatch(r"v\d+|\d+", tail):
                    shadow["versioned-doc"] += 1
                else:
                    shadow["event-or-action"] += 1
            known = [t for t in named if t in events_of]
            if not known:
                continue
            # A pairing claim is structural, not textual: one table cell holding
            # only the topic, the next cell holding only event names. Prose that
            # mentions a topic and other services' events in the same sentence
            # claims no pairing -- S43 C8 and S44 C8 are gap-table rows doing
            # exactly that, and line-scoped matching read them as pairs.
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            bare = [c.strip("`") for c in cells]
            # Plans put the topic on either side of the event (S47 topic-then-
            # event, S29 event-then-topic), so both neighbours are candidates.
            for i, cell in enumerate(bare):
                if cell not in events_of:
                    continue
                paired = None
                for j in (i - 1, i + 1):
                    if 0 <= j < len(cells) and j != i:
                        picked = [m.group(1) for m in _EVENT_IN_CELL.finditer(cells[j])]
                        claim = [e for e in picked if e in declared]
                        if claim:
                            paired = (picked, claim)
                            break
                if paired is None:
                    # Only worth reporting if a neighbour actually holds an
                    # event-shaped token; otherwise this cell is prose.
                    shaped = []
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(cells):
                            shaped += [m.group(1) for m in
                                       _EVENT_IN_CELL.finditer(cells[j])
                                       if "." in m.group(1)]
                    if shaped:
                        unjudgeable.append(f"{rel}:{lineno} {cell} -> {shaped}")
                    continue
                picked, claim = paired
                pair_lines += 1
                stray = [e for e in claim if e not in events_of[cell]]
                if stray:
                    mispaired += 1
                    r.fail(f"{rel}:{lineno} pairs {cell!r} with {stray}; the "
                           f"registry puts that event on "
                           f"{sorted(t for t, ev in events_of.items() if set(stray) & ev)}")
    r.note = (f"{len(files)} plan file(s), {mentions} topic mention(s); "
              f"{mentions - undeclared}/{mentions} name a declared topic; "
              f"{pair_lines - mispaired}/{pair_lines} topic->event pairing(s) "
              f"agree with the registry")
    if unjudgeable:
        r.note += (f"; {len(unjudgeable)} pairing cell(s) name no declared event "
                   f"and could not be judged: {', '.join(unjudgeable)}")
    shadowed = sum(shadow.values())
    r.note += (f"; {shadowed} undeclared dotted token(s) are not judged: "
               f"{shadow['filename']} file name(s), "
               f"{shadow['versioned-doc']} versioned document name(s), "
               f"{shadow['event-or-action']} event-or-action shaped")
    return r


def check_redaction_policy(name):
    """`redaction-policy.v2.json` is internally coherent and its section refs resolve.

    This is the one top-level contract no other check touched: `json` only proved
    it parses. It is also the security-critical one -- six secret classes and the
    rules for neutralising them -- and it is hand-authored (`generated_from` is
    null), so nothing regenerates it into correctness.

    Three things are enforced. A `must_match` pattern must be a regex Python can
    compile: the file calls them "reference patterns, not the definition", so an
    implementation need not use them, but a published pattern that cannot compile
    is a bug in the document either way. Each class's `id` must equal its map key,
    since `replacement_action` addresses classes by id. And every
    `plans/<file>.md §N.N` in `applies_to[].rule_ref` must name a heading that
    exists -- those plans are edited by other sessions and a renumbered section
    would otherwise leave this table pointing at prose that no longer says it.

    Binding a class id from `replacement_action` back to `secret_classes` is not
    checked: that block groups by class_a / class_b, and the ids appear in prose
    text rather than as keys, so a match there proves little.
    """
    r = Result(name)
    path = os.path.join(CONTRACTS, "redaction-policy.v2.json")
    if not os.path.exists(path):
        r.note = "redaction-policy.v2.json not present"
        return r
    pol = json.loads(_utf8(path))
    classes = pol.get("secret_classes", {}).get("classes", {})
    bad_regex = []
    for key, cls in classes.items():
        if cls.get("id") != key:
            r.fail(f"secret class key {key!r} declares id {cls.get('id')!r}")
        pat = cls.get("must_match")
        if not pat:
            r.fail(f"secret class {key!r} has no must_match")
            bad_regex.append(key)
            continue
        try:
            re.compile(pat)
        except re.error as e:
            r.fail(f"secret class {key!r} must_match does not compile: {e}")
            bad_regex.append(key)

    checked = unresolved = 0
    for entry in pol.get("applies_to", []):
        ref, cur = entry.get("rule_ref", ""), None
        # A ref reads "plans/S45-audit.md §2.2 (…), §2.5 (…), §3.3 (…)". Carry the
        # last path forward, or only the first section of each entry gets checked.
        for m in re.finditer(r"(plans/[\w\-]+\.md)|§([\d.]+)", ref):
            if m.group(1):
                cur = m.group(1)
                continue
            if not cur:
                continue
            checked += 1
            sec = m.group(2)
            tgt = os.path.join(ROOT, cur)
            if not os.path.exists(tgt):
                r.fail(f"{cur} §{sec}: file does not exist")
                unresolved += 1
            elif not re.search(rf"^#+\s*{re.escape(sec)}[\.\s]", _utf8(tgt), re.M):
                r.fail(f"{cur} §{sec}: no heading with that number")
                unresolved += 1

    # "compiled" and "resolved" are claims about the run, so a failing run must not
    # print them as totals -- same shape as the x-service-id note. Count what
    # actually passed and name the shortfall.
    r.note = (f"{len(classes)} secret class(es), "
              f"{len(classes) - len(bad_regex)}/{len(classes)} regex(es) compiled"
              + (f" ({len(bad_regex)} did not)" if bad_regex else "")
              + f"; {len(pol.get('applies_to', []))} applies_to entr(ies), "
              f"{checked - unresolved} of {checked} plan section ref(s) resolved")
    return r


def check_mcp_tools(name):
    """`mcp-tools.v2.design.json` is internally coherent and its schema refs resolve.

    Nothing read this file except one number: `catalog` takes `len(mcp["tools"])`
    to keep the layout block honest. Everything that makes these 12 tools safe to
    expose was unread -- `required_scope` (what the caller must hold), `inputSchema`
    (what arguments are accepted, including 3 cross-file `$ref`s), and the four
    MCP safety annotations. A duplicate tool name, a `required` key with no
    matching property, or a `$ref` to a renamed schema would all have stayed green.

    Two invariants are enforced because both are decidable without choosing a
    side. `readOnlyHint` and `required_scope` must agree on read vs write: a tool
    annotated read-only but scoped to `execute` grants write authority under a
    read-only label, and the reverse hides a write behind `read`. And every
    `$ref` must name a file this repo has -- the design note says the server
    "解析schema refs", so a dangling one is a bug in the document regardless of
    the decision on how scopes map to `permission_key` (S19 C7, still open).

    The six scope values are reported, not enforced: `publish` is outside the
    five-value closed set `docs/13` declares, and that is an authorization-granularity
    decision for the owner, not something a check should settle.
    """
    r = Result(name)
    path = os.path.join(CONTRACTS, "mcp-tools.v2.design.json")
    if not os.path.exists(path):
        r.fail("contracts/mcp-tools.v2.design.json is missing")
        return r
    tools = json.loads(_utf8(path))["tools"]

    names = [t.get("name") for t in tools]
    seen = set()
    for n in names:
        if n in seen:
            r.fail(f"duplicate tool name {n!r}")
        seen.add(n)

    scope_vs_ro = 0
    bad_required = 0
    refs, dangling = 0, []
    for t in tools:
        n = t.get("name")
        scope = t.get("required_scope")
        ro = (t.get("annotations") or {}).get("readOnlyHint")
        if isinstance(ro, bool):
            if ro and scope != "read":
                r.fail(f"{n!r} is readOnlyHint=true but required_scope={scope!r}")
                scope_vs_ro += 1
            elif not ro and scope == "read":
                r.fail(f"{n!r} is readOnlyHint=false but required_scope='read'")
                scope_vs_ro += 1

        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        orphan = [k for k in (schema.get("required") or []) if k not in props]
        for k in orphan:
            r.fail(f"{n!r}: required key {k!r} is not in properties")
        if orphan:
            bad_required += 1
        for m in re.finditer(r'"\$ref"\s*:\s*"([^"]+)"', json.dumps(schema)):
            refs += 1
            target = m.group(1)
            if not os.path.exists(os.path.join(CONTRACTS, target)):
                r.fail(f"{n!r}: inputSchema $ref -> {target} does not exist")
                dangling.append(target)

    scopes = sorted({t.get("required_scope") for t in tools if t.get("required_scope")})
    # Every claim below is about the run, so each is expressed as passing/total.
    # A bare "12 tool(s)" would stay true-looking when two tools share a name, and
    # a note that omits the required-key check entirely would let probe #2 fail
    # with three green clauses and no mention of the thing that broke.
    distinct = len(seen)
    r.note = (f"{distinct} distinct tool(s)"
              + (f" ({len(tools) - distinct} duplicate)" if distinct < len(tools) else "")
              + f", {len(tools) - scope_vs_ro}/{len(tools)} readOnlyHint agrees with "
                f"required_scope, {len(tools) - bad_required}/{len(tools)} required keys "
                f"are in properties; {len(scopes)} scope value(s); "
                f"{refs - len(dangling)}/{refs} inputSchema $ref(s) resolve")
    return r


def check_openapi_refs(name):
    """Every $ref in contracts/openapi.v2.yaml that names a file resolves.

    Nothing else parsed this file. `readme` counts its `get:`/`post:` lines to
    keep the layout block honest and gap 5 reports that none of its paths use
    `:verb`, but both work on the raw text. The 11 external `$ref` values were
    never looked at, so renaming any one of the 8 target schemas would have left
    every check green while the API document pointed at nothing.

    YAML is parsed with a line scan rather than a YAML library: this repo has no
    PyYAML dependency and the only thing needed is `$ref: <value>` occurrences.
    A `$ref` whose value starts with `#` is a local pointer, and it must name a
    component this file defines under `components:`; 32 of the 43 point into 3
    components (Error / Operation / Run).

    The same line scan also checks `x-service-id`, which nothing else read: 21
    occurrences name 13 services, and a retyped id (`S2` for `S02`, or a service
    that was renumbered out of the catalog) would have stayed green. It is not an
    error for a catalog service to have no operation here -- this document is
    explicit that it is "核心API设计，非完整48服务实现" -- so only the direction
    that can be wrong is enforced.
    """
    r = Result(name)
    path = os.path.join(CONTRACTS, "openapi.v2.yaml")
    if not os.path.exists(path):
        r.fail("contracts/openapi.v2.yaml is missing")
        return r
    text = _utf8(path)
    # `#` must stay inside the character class: `$ref: '#/components/...'` is the
    # most common form in this file, and excluding `#` makes those match nothing.
    refs = re.findall(r"\$ref:\s*['\"]?([^'\"\s]+)", text)
    external = [v for v in refs if not v.startswith("#")]
    local = [v for v in refs if v.startswith("#/")]

    for v in external:
        target = os.path.join(os.path.dirname(path), v.split("#")[0])
        if not os.path.exists(target):
            r.fail(f"$ref -> {v} does not exist")

    # Components are `components: -> schemas: -> <Name>:`; collect the names by
    # indentation, which is enough for this file's fixed two-level shape.
    defined = set()
    in_components = in_schemas = False
    comp_indent = schema_indent = -1
    for ln in _utf8(path).split("\n"):
        s = ln.rstrip()
        if not s or s.lstrip().startswith("#"):
            continue
        k = len(s) - len(s.lstrip())
        st = s.strip()
        if st == "components:":
            in_components, comp_indent = True, k
            continue
        if in_components and k <= comp_indent:
            in_components = False
        if in_components and st == "schemas:":
            in_schemas, schema_indent = True, k
            continue
        if in_schemas:
            if k <= schema_indent:
                in_schemas = False
            elif k == schema_indent + 2 and st.endswith(":") and not st.startswith("-"):
                defined.add(st[:-1])
    for v in local:
        name = v.rsplit("/", 1)[-1]
        if name not in defined:
            r.fail(f"$ref -> {v} names no component under components/schemas")

    known = {s["service_id"]
             for s in json.loads(_utf8(os.path.join(CONTRACTS, "service-catalog.v2.json")))["services"]}
    svc = re.findall(r"x-service-id:\s*['\"]?([^'\"\s]+)", text)
    unknown = sorted(set(svc) - known)
    for v in unknown:
        r.fail(f"x-service-id -> {v} is not a service_id in service-catalog.v2.json")

    # An operation with a request body or a 200 response but no schema at all is
    # not this check's business; count only what was resolved, so the note cannot
    # be read as "N operations verified". Same reason the unresolvable id is
    # subtracted below: on a failing run the raw distinct count would print a
    # number that includes the bad value, and "14 catalog services" would be
    # wrong about both the total and the membership.
    resolved = len(set(svc)) - len(unknown)
    r.note = (f"{len(refs)} $ref occurrence(s), {len(external)} naming a file, "
              f"{len(local)} local pointer(s) into {len(defined)} component(s); "
              f"{len(svc)} x-service-id naming {resolved} catalog service(s)")
    if unknown:
        r.note += f", {len(unknown)} not in the catalog"
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
    texts = {}
    for fname, prefix in QC_PLANS:
        path = os.path.join(ROOT, "plans", fname + ".md")
        if not os.path.exists(path):
            r.fail(f"plans/{fname}.md is missing")
            continue
        text = texts[fname] = _utf8(path)
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

    # Every full id a plan cites must exist in the registry of the plan that owns
    # its prefix. The id-pattern test above cannot see this: `SUB.LAYOUT.CPS_
    # EXCEEDED` is perfectly well shaped, it just is not registered anywhere, so
    # a plan that emits it disagrees with the table that is supposed to be its
    # authority. Only 3+-segment mentions count -- `VIS.SAFE_AREA` is a group
    # reference and `SUB.TIMING.CPS` is prose shorthand, neither claims to be an id.
    registered = {}
    for fname, prefix in QC_PLANS:
        if fname in texts:
            registered[prefix] = set(re.findall(
                r"\|\s*`(" + prefix + r"\.[A-Z0-9_]+(?:\.[A-Z0-9_]+)*)`\s*\|", texts[fname]))
        else:
            registered[prefix] = set()
    owner_of = {prefix: fname for fname, prefix in QC_PLANS}
    dangling_seen = set()
    full_refs = 0
    cite = re.compile(r"`((TECH|VIS|AUD|SUB)(?:\.[A-Z0-9_]+){2,})`")
    for fname, _ in QC_PLANS:
        for ident, prefix in cite.findall(texts.get(fname, "")):
            full_refs += 1
            if ident in registered[prefix]:
                continue
            if ident in KNOWN_DANGLING_REFS:
                dangling_seen.add(ident)
                continue
            r.fail(f"plans/{fname}.md cites {ident}, but "
                   f"plans/{owner_of[prefix]}.md does not register it")
    for i in sorted(KNOWN_DANGLING_REFS - dangling_seen):
        r.fail(f"{i} is in KNOWN_DANGLING_REFS but no plan cites it unregistered "
               f"any more; remove the entry")

    r.note = (f"{total} check_id(s), {len(seen_bad)} known non-conforming "
              f"(owner decision, see contracts/README.md gap 4); "
              f"{full_refs} citation(s), {len(dangling_seen)} unresolved "
              f"(owner decision, see check-registry-missing-fields §5.2)")
    return r


# Files inside docs/design-package/ whose MANIFEST.md row can never match, pinned to
# the hash they had when this check was written. All three were already out of step
# in the scaffolding commit (8c94e5f) and none has been touched since, so this is
# import-state, not drift. DESIGN.md is every archived document concatenated into
# one 849-line file with 66 repo-relative links; README.md and contracts/README.md had
# their links rewritten from ../docs/ to ../../docs/ on import. In all three the
# manifest holds the pre-import checksum, so no edit to the archive can reproduce it.
# Clearing them means regenerating MANIFEST.md -- an owner decision, since the
# package is read-only until they say otherwise.
#
# These are pinned to their own hashes rather than merely skipped. A skip list keyed
# on filename alone would let the next edit to any of the three through, and those
# three are the most likely place to hide one: they are the files a careless
# "let me normalise these links" sweep would touch. Pinning means the note's
# "3 known divergences" can only stay at 3 while they stay byte-identical to what
# was reviewed here.
ARCHIVE_KNOWN_DIVERGENT = {
    "DESIGN.md": (74502,
                  "fdb457740855e387e6382ad4243f542d3170ecec5cb80e98719e0d9539f763e2"),
    "README.md": (4067,
                  "06e09aca9bc3f727ef5c79ed6967e0dd1198f3d8300d0b9339349472ae1a68fe"),
    "contracts/README.md": (2175,
                            "72a98b9f82f9c4b6b8ff9e25d19ee43a08562d5803695ca053740f59a1fa9a93"),
}


# The 61 MANIFEST rows that name files imported out of the package. Each row is
# resolved to its real home by the prefixes below, and pinned to the hash it has
# in the tree this set was built from. The check does not compare these against
# MANIFEST.md -- see the docstring for why that is the right call -- but it does
# compare them against this baseline, so a later edit to one of them is still
# caught rather than merely not looked at.
#
# Pinned per file, not counted: a count would be satisfied by any 57 files
# matching, and the four that had already legitimately moved (the two generated
# catalogs, docs/02, and the memory example) would absorb an edit to any of the
# other 57 without the number changing.
IMPORT_PREFIXES = (
    ("examples/", "contracts/examples/"),
    ("diagrams/", "docs/diagrams/"),
    ("layout.html", "apps/studio-web/layout.html"),
)


# The three files inside the package that MANIFEST.md has no row for. Nothing
# compared them, so an edit to any of them -- including to MANIFEST.md itself --
# looked exactly like a clean tree. Pinned per file for the same reason as
# ARCHIVE_IMPORTED_BASELINE: a count of three would be satisfied by any three
# files existing, which is not the same statement as these three being unedited.
ARCHIVE_UNLISTED_BASELINE = {
    '00-阅读顺序.txt': (433,
        'e53d8d41c479040982142cb26bf9a398c32633ea4a57537cc399ee89c12a95b0'),
    'MANIFEST.md': (12978,
        '39fa3ac3e66043b360047a2bb6688e742c3abe8c26bbf95550a9d7a83adfea8d'),
    'contracts/domain-v1-README.md': (651,
        '6dfeacb599efcf688ffd1196b49882dbe3d46e833c855999a78d6f51b8affb6a'),
}

ARCHIVE_IMPORTED_BASELINE = {
    'contracts/admission-record.v2.schema.json': (5020,
        '66477e967cb12d55e62d111ff49d2c0cb2eb4a0bccdb00831cec2ad17eba5272'),  # contracts/admission-record.v2.schema.json
    'contracts/automation-policy.v2.schema.json': (4472,
        'd49d1807836b4c270479cdd85692c81a9e29aba654671fa6f96cf92becc50930'),  # contracts/automation-policy.v2.schema.json
    'contracts/context-pack.v2.schema.json': (3693,
        '03c5dd1e0aa0e541eea78903012f7395ce7219a5e39cd881cecc57445fd3037b'),  # contracts/context-pack.v2.schema.json
    'contracts/domain-v1/README.md': (651,
        '6dfeacb599efcf688ffd1196b49882dbe3d46e833c855999a78d6f51b8affb6a'),  # contracts/domain-v1/README.md
    'contracts/domain-v1/change-plan.v1.schema.json': (6980,
        '29ecd36cda7dfab69be17ae01134900fc7b2f273f18da3f08d9f96fa743f6879'),  # contracts/domain-v1/change-plan.v1.schema.json
    'contracts/domain-v1/event-envelope.v1.schema.json': (1410,
        'f3ad8ac0d88a0d128fbe88a3eb3ea292e22cff82dd7e8087d2435ca38da22988'),  # contracts/domain-v1/event-envelope.v1.schema.json
    'contracts/domain-v1/generation-request.v1.schema.json': (11768,
        '358dcbc9c889821940b2f73f78df003a59aaea87698a15fc54786b078e3c73c3'),  # contracts/domain-v1/generation-request.v1.schema.json
    'contracts/domain-v1/qc-report.v1.schema.json': (7068,
        'b7708a0609c0e004b1a3839d4bba5e3b5717ef1fe4b549151ec26f5fc0f782cf'),  # contracts/domain-v1/qc-report.v1.schema.json
    'contracts/domain-v1/shot-spec.v1.schema.json': (12833,
        'e76a90386587eb7cd0f28d6e148f34ae6eb7d5e7368c05557e9ee20fed4fcdac'),  # contracts/domain-v1/shot-spec.v1.schema.json
    'contracts/enterprise-change-plan.v2.schema.json': (7015,
        'e445d1a9e49bd2ea046b0a4319b4685b22c865c761bc461725c1c4ed780ae352'),  # contracts/enterprise-change-plan.v2.schema.json
    'contracts/episode-manifest.v2.schema.json': (6094,
        '30e8106652717169075266d314ba0be6b39f15d9d7b34041df7f133eab05a4aa'),  # contracts/episode-manifest.v2.schema.json
    'contracts/event-envelope.v2.schema.json': (2333,
        '9f70984c27885afdf3af68e76ba3cac84e11e5611deb5de7f02c08a7a4b3a9eb'),  # contracts/event-envelope.v2.schema.json
    'contracts/event-topics.v2.json': (25073,
        'db2c8a144b0cb4e24d55a9e765dc030c0387e3a269f3ab8959eb2fd263ef504f'),  # contracts/event-topics.v2.json
    'contracts/execution-context.v2.schema.json': (3768,
        '1763c0e45c1b166aa1a47e13a02801153fe9c0041fe6a533d5ee543fa8a504db'),  # contracts/execution-context.v2.schema.json
    'contracts/generation-command.v2.schema.json': (27272,
        'b89e3ab6183753c6bce99379b1c76a70d951ae21498f2bd5dfdecc236cb1ba31'),  # contracts/generation-command.v2.schema.json
    'contracts/mcp-tools.v2.design.json': (11587,
        '56d4e14358cbd2fd85f9f7d30f3166d167ef5bfcfa52267f7d8c9bb0bc73204e'),  # contracts/mcp-tools.v2.design.json
    'contracts/memory-claim.v2.schema.json': (6665,
        '8eac005d62271d188244088173738078966386ddd700641a7e5bbb0031ae4c13'),  # contracts/memory-claim.v2.schema.json
    'contracts/openapi.v2.yaml': (24416,
        'bd0d0878fa271f9e7ea8eb2cf939efaa0842a4f50bb62575c609f467270617ac'),  # contracts/openapi.v2.yaml
    'contracts/service-catalog.v2.json': (43573,
        '320b2a261dbe668fc873a32be0dc8e9829248fba9cfce1af9facca19abca9bec'),  # contracts/service-catalog.v2.json
    'diagrams/01-enterprise-architecture-1.mmd': (1148,
        '34c0d13d987c3c6c58960182f44209c07577a09cb3fa233ababff7b1e38a224e'),  # docs/diagrams/01-enterprise-architecture-1.mmd
    'diagrams/03-admission-saga.mmd': (942,
        '0712d8e22d322181bf44e83274e4881c8f19966d900712e7deeccfcc8f0a63b5'),  # docs/diagrams/03-admission-saga.mmd
    'diagrams/03-deployment-capacity-1.mmd': (433,
        'd648d2dcfbc65a41b9c15ff4ac9b4c264d89e90b904a7dd316d034688df8359a'),  # docs/diagrams/03-deployment-capacity-1.mmd
    'diagrams/04-memory-commit.mmd': (651,
        'ceb4e8279d2bf3acf11963132958275ea1ab7f485bbe72dfb656c87f981a6336'),  # docs/diagrams/04-memory-commit.mmd
    'docs/01-enterprise-architecture.md': (5442,
        'd16e5746d28545048cd9d988826c91797ed5e5f08062a944796c3fe5d7d3f06b'),  # docs/01-enterprise-architecture.md
    'docs/02-service-catalog.md': (5618,
        '0bc3f802527b588b16ea63d46c67b506468a784e0c3d3570e87af0ccd155de93'),  # docs/02-service-catalog.md
    'docs/03-deployment-capacity.md': (4380,
        'cd3cd8ba8622bd177093b910308106763f962020870bbd29bcdd4c313f453781'),  # docs/03-deployment-capacity.md
    'docs/04-data-consistency.md': (5502,
        '98daa321b0082b23e6cd114640e12fdaefcd50fd1f549c57c1f9ac953456f801'),  # docs/04-data-consistency.md
    'docs/05-cross-episode-memory.md': (5338,
        '0aacb8d4ebb2e7cfa550edaf189c00ebf8606073f0852bf6fd0d6a6b806a5b2a'),  # docs/05-cross-episode-memory.md
    'docs/06-automation-governance.md': (3889,
        '023ae3cba4d3b9809ec3c90740cedc26a58ae105a8e7627220f3289993a14cc8'),  # docs/06-automation-governance.md
    'docs/07-production-workflows.md': (3652,
        'd0813ae215cf970f8505abfe6dae2fedc16302c10a2c68618631c270e0f57dfe'),  # docs/07-production-workflows.md
    'docs/08-agents-prompts.md': (2821,
        '0adcbceddfdf080ce6952c745d3827d7272fc874d5b118470365881d13d768e1'),  # docs/08-agents-prompts.md
    'docs/09-model-execution.md': (3474,
        'a95aafb721684495a4755a1189e81694762bc0879907abbd1dd575eab81eac15'),  # docs/09-model-execution.md
    'docs/10-assets.md': (2475,
        'ee878d1f0f43cf4486e264740e9b77604a633a9f53aa8f84b8e5b64103d9c9c9'),  # docs/10-assets.md
    'docs/11-quality-repair.md': (2726,
        'f185f8f3618f898ecbbb5bbd4677f2921c86a73f2d6e2b460d4e3f7bc04f9774'),  # docs/11-quality-repair.md
    'docs/12-budget-scheduling.md': (2974,
        'ae7971ded454246026b67d3cb608cb1f5bd6cd6bbd1b8e5c54962805b7670a88'),  # docs/12-budget-scheduling.md
    'docs/13-api-product.md': (3344,
        '8f8cc48fa1804b11553ea208daa2741eef3f45367b173fb30793737e9a019c6f'),  # docs/13-api-product.md
    'docs/14-enterprise-operations.md': (4476,
        '048e556f42c78922e439753f9ddab0b6ccabc490ccf7fb6f78eeb2abf0531bc6'),  # docs/14-enterprise-operations.md
    'docs/15-fde-business-evaluation.md': (3332,
        '3b4065c871cc0d6ada04d5f047adbb20aad338ab66f27758cfe1c700349eb451'),  # docs/15-fde-business-evaluation.md
    'docs/16-migration-acceptance.md': (7357,
        '4ac74dc45bd8848fffe5d80c4d84cc034114d532d99871b71ac495016bb29a6b'),  # docs/16-migration-acceptance.md
    'docs/17-provider-evidence-baseline.md': (3366,
        '45233dbc91a98eff6770acbd450542afe2af9b66ec3a58841274e7ce2d17230f'),  # docs/17-provider-evidence-baseline.md
    'examples/admission-unknown.json': (2064,
        '666d072b936b49effd29788040ef47caeaecdec875122a1e0f04b4d4633d315f'),  # contracts/examples/admission-unknown.json
    'examples/automation-policy.json': (1205,
        '6cd8bc8aa1edb5dcdbe01c27cff69d4b7cc01489f9ff6faea3170a56671ee692'),  # contracts/examples/automation-policy.json
    'examples/capacity-plan.json': (805,
        '25a9ce9903d257e1e80fb31065917bb46a4e99199b3eecc39d4b2d074db9986b'),  # contracts/examples/capacity-plan.json
    'examples/context-pack.json': (1107,
        '3498c669c1194c3f94c749fb1262c96e2ebabdede8134feed1b607bd3f52dc44'),  # contracts/examples/context-pack.json
    'examples/enterprise-change-plan.json': (3329,
        'cad511d1c9cf167a40de0b4883818c74b92fdd53c5e1cd4079269d085baa548a'),  # contracts/examples/enterprise-change-plan.json
    'examples/episode-manifest.json': (1880,
        'd78217fc3896859b5a3d6ea13a623614744e859fd0715d79d3fe81d64b1c2ee7'),  # contracts/examples/episode-manifest.json
    'examples/execution-context.json': (1071,
        '2ab69b576409bc88518b6042fb6afb621a5b5c8d0356986be8f3d3f4fbbdcdce'),  # contracts/examples/execution-context.json
    'examples/generation-command.image.json': (2181,
        'b3dff465b3ecb221ddaf1cc9ab97054982d0b70d166a489f8fa29d3ebb2ad9f5'),  # contracts/examples/generation-command.image.json
    'examples/generation-command.lipsync.json': (2475,
        'a063ef5ee8c2747fd5f703c2986e8036d3f6aeaca7ba691b7ff28d7408ffe862'),  # contracts/examples/generation-command.lipsync.json
    'examples/generation-command.music.json': (2120,
        '00f996a0494e0f9f972a38be85eb0a6e738abc104124b7d34cc5062c8542e6d7'),  # contracts/examples/generation-command.music.json
    'examples/generation-command.sound_effect.json': (2125,
        '949e8c924519a879751195260090deafd333cdc3f08bb0fd9089f93087698ccb'),  # contracts/examples/generation-command.sound_effect.json
    'examples/generation-command.speech.json': (2128,
        '78918d563b77f5993215773f23a8cd9ed8a2b95e2144e1fe7cd128b55375ea32'),  # contracts/examples/generation-command.speech.json
    'examples/generation-command.text.json': (2185,
        '95bd646fbdc0e188c1243e68120a1d35de585044dc383567d7ea98853a4b84ff'),  # contracts/examples/generation-command.text.json
    'examples/generation-command.training.json': (2341,
        'a5828c67bd12e202188a89dc0d352ba598d93f7c1936c7b4ca3f43b49732e9df'),  # contracts/examples/generation-command.training.json
    'examples/generation-command.understanding.json': (2364,
        'b841ef1256fc3c3a897fc3e4f7d4134d2066fdaa014400a416a8ae1debfe2d25'),  # contracts/examples/generation-command.understanding.json
    'examples/generation-command.video.json': (2492,
        '498632238534fdebdc4ae1913977785e06901a2fb8bc96b69b46e41d6edef271'),  # contracts/examples/generation-command.video.json
    'examples/memory-claim.json': (1248,
        '57df655dcc08aa0d9f87aa73a03d87562267ee8dc9630d17dbaef1b8d6bc73c2'),  # contracts/examples/memory-claim.json
    'examples/memory-committed-event.json': (1892,
        'c3d497dae06b2bb1c8f54995ec598f80a42ed67ad1bb720a3109d02e09d5b2ac'),  # contracts/examples/memory-committed-event.json
    'examples/runtime-safety.yaml': (1049,
        '000ac6e6fd3ea36e166408cd27a2cc8e920d749a19fef602e532c8638b9f67f8'),  # contracts/examples/runtime-safety.yaml
    'examples/service-interactions.yaml': (1278,
        'b9d404ff65b94bcca15cc4ac5f7b289ca997399888297b6e2f84070f5b1defe9'),  # contracts/examples/service-interactions.yaml
    'layout.html': (14709,
        'f59808b3c9bab45c27233754bdb9c40e69c732cbae21c1ce165487336c71a9d7'),  # apps/studio-web/layout.html
}


def check_archive(name):
    """The read-only design package still matches the checksums in its MANIFEST.

    AGENTS.md says `docs/design-package/` is the original input and must not be
    edited -- "改了就没法对照了". Nothing enforced that. The package ships a
    MANIFEST.md of 115 rows, each a path, a byte count and a SHA-256, and none of
    it had ever been recomputed. A silent edit inside the archive would look
    exactly like a clean tree.

    115 rows but only 54 files live inside the package: the rest were imported to
    their real homes on scaffolding (contracts/, docs/, contracts/examples/,
    docs/diagrams/, apps/studio-web/layout.html), and those are expected to keep
    changing. So the 54 in-package rows are compared against MANIFEST.md, and the
    other 61 against ARCHIVE_IMPORTED_BASELINE instead -- comparing them against
    MANIFEST.md would flag every legitimate doc edit and bury the signal, but not
    looking at them at all would leave an edit there invisible.

    The comparison uses the bytes on disk, not `git cat-file`, after confirming
    the working tree is LF here: the manifest was built from LF files and a CRLF
    checkout would otherwise report all 54 as changed.

    The three rows in ARCHIVE_KNOWN_DIVERGENT cannot match by construction, so they
    are pinned to their own hashes and reported separately. Pinned rather than
    skipped for the same reason they are enumerated rather than counted: a bare
    count or a filename-only skip would let a fourth edit hide behind the old
    number, and a fourth edit is exactly what this check exists to catch.
    """
    r = Result(name)
    pkg = os.path.join(ROOT, "docs", "design-package")
    man = os.path.join(pkg, "MANIFEST.md")
    if not os.path.exists(man):
        # MANIFEST.md is itself pinned, so its absence is a finding, not a reason to
        # return quietly: with no manifest to compare against, every other row would
        # go unexamined and the check would still look clean.
        r.fail("docs/design-package/MANIFEST.md is missing; no row can be compared")
        r.note = ("no MANIFEST.md; 0 rows compared, and its own absence is reported "
                  "rather than passed")
        return r
    rows = []
    for line in _utf8(man).splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`\s*\|", line)
        if m:
            rows.append((m.group(1), int(m.group(2)), m.group(3)))

    inside = 0
    inside_ok = 0
    known = 0
    imported = 0
    bad = []
    for rel, size, sha in rows:
        p = os.path.join(pkg, rel)
        if not os.path.exists(p):
            # Imported out of the package on scaffolding -- not compared against
            # MANIFEST.md, but still compared against the pinned baseline, so an
            # edit to one of them does not pass just because it is not looked at.
            # Resolved against ROOT, not the cwd: a bare `rel` made every one of
            # these 61 resolve to "no file at any known home" whenever the suite
            # was launched from anywhere but the repo root, turning the whole
            # imported half of this check red for a reason that is not the tree's.
            dest = os.path.join(ROOT, rel) if os.path.exists(os.path.join(ROOT, rel)) else None
            if dest is None:
                for pre, home in IMPORT_PREFIXES:
                    cand = os.path.join(ROOT, home + rel[len(pre):])
                    if rel.startswith(pre) and os.path.exists(cand):
                        dest = cand
                        break
            want = ARCHIVE_IMPORTED_BASELINE.get(rel)
            if want is None:
                bad.append((rel, "a pinned baseline", "not in ARCHIVE_IMPORTED_BASELINE"))
            elif dest is None:
                bad.append((rel, "an imported file", "no file at any known home"))
            else:
                b = open(dest, "rb").read()
                if (len(b), hashlib.sha256(b).hexdigest()) != want[:2]:
                    bad.append((rel, f"{want[0]}B pin", f"{len(b)}B at {dest}"))
                else:
                    imported += 1
            continue
        inside += 1
        b = open(p, "rb").read()
        h = hashlib.sha256(b).hexdigest()
        if len(b) == size and h == sha:
            inside_ok += 1
            continue
        if rel in ARCHIVE_KNOWN_DIVERGENT:
            # Pinned to the hash reviewed here, not merely skipped -- see the set.
            fsize, fsha = ARCHIVE_KNOWN_DIVERGENT[rel]
            if len(b) == fsize and h == fsha:
                known += 1
            else:
                bad.append((rel, fsize, len(b)))
            continue
        bad.append((rel, size, len(b)))

    # Files present in the package that no MANIFEST row names. Reported and
    # pinned, not silently ignored: this is the one hole left by comparing only
    # what MANIFEST.md lists.
    listed = {rel for rel, _, _ in rows}
    on_disk = set()
    for dirpath, _, filenames in os.walk(pkg):
        for fn in filenames:
            on_disk.add(os.path.relpath(os.path.join(dirpath, fn), pkg).replace(os.sep, "/"))
    # Denominator is the pin set, not whatever happens to be on disk: a deleted file
    # drops out of `on_disk`, and a denominator that shrinks with it would report
    # "2/2 match" for a package that just lost a file.
    unlisted = sorted(ARCHIVE_UNLISTED_BASELINE)
    unlisted_ok = 0
    for rel in unlisted:
        want = ARCHIVE_UNLISTED_BASELINE[rel]
        p = os.path.join(pkg, rel)
        if not os.path.exists(p):
            r.fail(f"{rel}: pinned at {want[0]}B but the file is gone")
            continue
        b = open(p, "rb").read()
        if (len(b), hashlib.sha256(b).hexdigest()) != want:
            r.fail(f"{rel}: pinned at {want[0]}B/SHA-256, file is {len(b)}B")
        else:
            unlisted_ok += 1
    for rel in sorted(on_disk - listed - set(unlisted)):
        # Its own message rather than a `bad` row: `bad` is formatted as
        # "expected {want}B", and "an ARCHIVE_UNLISTED_BASELINE entryB" is nonsense.
        r.fail(f"{rel}: in the package with no MANIFEST row and no "
               f"ARCHIVE_UNLISTED_BASELINE pin")
    for rel in sorted(set(unlisted) & listed):
        r.fail(f"{rel} is pinned as unlisted but MANIFEST.md has a row for it now; "
               f"remove it from ARCHIVE_UNLISTED_BASELINE")
    # A stale entry is itself a finding: if an owner regenerates MANIFEST.md, these
    # three start matching and the set should shrink with it.
    stale = set(ARCHIVE_KNOWN_DIVERGENT) - {rel for rel, _, _ in rows}
    for rel in sorted(stale):
        r.fail(f"{rel} is listed as known-divergent but MANIFEST.md has no row for it")
    for rel, want, got in bad:
        r.fail(f"{rel}: expected {want}B/SHA-256, file is {got}B")
    # Counted directly rather than derived from `len(bad)`: `bad` mixes in-package
    # failures with imported-row failures and orphan pins, so subtracting them back
    # out is error-prone in exactly the way the note is meant to expose.
    r.note = (f"{inside} in-package file(s) checksummed, "
              f"{inside_ok}/{inside} match MANIFEST.md"
              + (f", {known} known divergences (see the set in this file)" if known else "")
              + f"; {len(rows) - inside} row(s) name files imported elsewhere, "
                f"{imported}/{len(rows) - inside} still match the pinned baseline "
                f"and are no longer unexamined"
              + (f"; {unlisted_ok}/{len(unlisted)} file(s) with no MANIFEST row "
                 f"still match their pin" if unlisted else
                 "; no file in the package lacks a MANIFEST row"))
    return r


def check_citerefs(name):
    """A citation into `check_all.py` names the function it means:
    `check_all.py:check_payload_refs:1454-1464`, not `check_all.py:1454-1464`.

    Line numbers rot silently. This file is the one that gets rewritten, so a
    citation that lands in a `check_*` function today lands in a different
    one after the next insertion above it -- and every range-only or
    range-exists test still passes, because the file is as long as it ever
    was. The citations this was written for had done exactly that: six of them
    named ranges that had slid into a neighbouring function, one in
    `contracts/README.md` (a live description of what a check does today) and
    five in the corrections report.

    A test that only asks "is this line inside *some* check_* function" does
    not catch that, and this was verified by mutation: `763-772` slid from
    `check_payload_refs` into `check_interactions` and passed, and `160-170`
    slid into `check_links` and passed. Both were still inside a check. So the
    function name is required, not optional: it is the only part of the
    citation that does not move, and it is what the prose is actually
    referring to. Anonymous `check_all.py:<line>` is a failure telling the
    author to add the name.

    Scope is deliberately just `check_all.py`. Other citations in prose look
    like `plans/S35:1010` or `S38:241` -- shorthand for a plan or design-package
    file, not a path -- so resolving them needs a name map that does not exist
    and would make this check guess.

    Fenced blocks are skipped, so a citation quoted as an example of the wrong
    form belongs in a fence. Never weaken this to make a stale number pass:
    the number is the thing that is wrong.

    Whether the prose and the cited code say the same thing is not decidable
    here and is not attempted; this settles that the named function exists and
    that both ends of the range are inside it.
    """
    r = Result(name)
    py = _utf8(os.path.join(ROOT, "tools", "check_all.py")).splitlines()

    spans = []
    for i, line in enumerate(py):
        if line.startswith("def "):
            spans.append((i + 1, line[4:].split("(")[0]))
    defined = {fname for _, fname in spans}

    def enclosing(n):
        got = None
        for start, fname in spans:
            if start <= n:
                got = fname
            else:
                break
        return got

    def mask_fences(text):
        """Blank fenced blocks but keep the line count, so offsets stay
        aligned with the file on disk."""
        out, inside = [], False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                inside = not inside
                out.append("")
            else:
                out.append("" if inside else line)
        return "\n".join(out)

    # A negative lookbehind on a path character keeps `x/tools/check_all.py`
    # from also matching at the `check_all` offset. The optional name is
    # captured so the anonymous form can be reported with its own message
    # rather than silently ignored.
    cite = re.compile(
        r"(?<![\w./\-])(?:tools/)?check_all\.py:"
        r"(?:(check_[A-Za-z0-9_]+):)?(\d+)(?:-(\d+))?"
    )
    total = 0
    ok = 0
    anonymous = 0
    files = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        if rel.startswith(("docs/design-package/", ".claude/", "_inbox/")):
            continue
        files += 1
        text = mask_fences(_utf8(p))
        for m in cite.finditer(text):
            lo = int(m.group(2))
            hi = int(m.group(3) or m.group(2))
            where = f"{rel}:{text[: m.start()].count(chr(10)) + 1} cites {m.group(0)}"
            total += 1
            n = len(py)
            if not (1 <= lo <= n and 1 <= hi <= n and lo <= hi):
                r.fail(f"{where}: check_all.py has {n} line(s)")
                continue
            fname = m.group(1)
            if not fname:
                anonymous += 1
                r.fail(f"{where}: name the function too, like "
                       f"check_all.py:check_payload_refs:{m.group(2)}")
                continue
            if fname not in defined:
                r.fail(f"{where}: check_all.py defines no {fname}")
                continue
            f_lo, f_hi = enclosing(lo), enclosing(hi)
            if f_lo != fname or f_hi != fname:
                r.fail(f"{where}: lines {lo}-{hi} are inside "
                       f"{f_lo or 'module top'} .. {f_hi or 'module top'}, not {fname}")
                continue
            ok += 1

    r.note = (f"{total} check_all.py citation(s) across {files} md file(s); "
              f"{ok}/{total} name the function that covers their range "
              f"({anonymous} unnamed)")
    return r


def check_planrefs(name):
    """`Snn §X` names a heading in `plans/Snn-*.md`.

    Why this is checkable at all, when `行 N` citations (see the corrections
    report, 补记十一之十三) are not: the attribute is inside the text. `S08
    §6.5` says which plan it means; "行 537" does not, so resolving it needs
    context the citation does not carry. That distinction -- not the presence
    of a number -- is what makes this family auditable.

    Two things this deliberately does NOT do:

    - It DOES fail an integer that is not a heading but is inside the file's
      length, and the failure message says the form was not accepted because
      `Snn §行号` is an open owner decision. That is deliberate, and it took
      two wrong versions to get here:

      v1 failed only ints beyond the file's length. That let `S07 §11` pass:
      S07 stops at §10 but is 1141 lines, so `§11` was read as "line 11" and
      went unjudged -- the exact error this check exists to catch, which
      `plans/S45-audit.md` carried until this round.
      v2 required the integer to be at least the plan's highest top-level
      section (so `S23 §1180`, a real line-number ref, would qualify). Still
      wrong: `S07 §11` is only one past §10, so it slipped through again.
      Both were mutation-tested; bothe survived the mutation meant to catch
      them. There is no arithmetic rule that separates "§11 of a plan ending
      at §10" from "line 1180", because the difference is intent, not shape.
      So the check reports it and says why. Outside the corrections report
      this corpus has zero such refs, so nothing is wrongly failed today;
      if the owner makes the line-number form legal, this branch is where to
      change it, and the note links the decision.
    - It does not check that the cited section *says* the thing the prose
      claims. That was tried and is not decidable: a "same gap as S19 §11 C6"
      row legitimately cites a section that does not contain the identifier the
      citing sentence quotes, because the identifier is the citing document's
      own subject. 17 of 17 such flags were that shape.

    Scope matches check_citerefs: `docs/design-package/` (read-only archive),
    `.claude/` (other sessions' worktrees) and `_inbox/` are skipped, and
    fenced blocks are masked so an example of a wrong form does not count.

    The one file that legitimately quotes wrong refs -- the corrections report,
    whose correction tables put the wrong form in a cell -- is exempted by
    path, not by trying to tell a quoted example from a live citation. Two
    heuristics were tried and both were wrong, measured:
      - masking inline code spans drops 11 real refs, `S10 §5.3 行 625` among
        them, because a ref followed by `行 N` is written in backticks too;
      - masking 「」/『』/“”/‘’ spans catches 3 refs that are all meant to be
        checked, and still misses `S08 §6.5` at :5138, which is in backticks
        and quotes no bracket at all.
    There is no lexical tell, so the exemption is stated rather than inferred.
    That is a real gap: a wrong ref elsewhere in the repo still fails, but a
    wrong ref inside the report does not, and the report grows.

    Headings are read without requiring a space after the number, because this
    repo writes both `## 3. 跨集改稿` and `## 3.跨集改稿`. Requiring the space
    turned 7 correct refs into apparent errors when this was measured.

    The pattern requires `§` to follow `Snn` directly (whitespace only). It
    was widened once to allow up to six intervening characters, to catch
    `S07 的 §6.5` / `S25（§2.6）`. That widening was measured and rejected:
    of the 12 refs it newly flagged in plan files, 11 were not attributions
    at all but self-references with a service name mentioned for context --
    `| 候选兼容性探测（S25 用，§2.6） |` cites S20's own §2.6 in the column
    that describes who calls the endpoint, and S25 has no §2.6. Only one,
    `plans/S47-realtime-projection.md:1077` "S23 方案 §1180", was a genuine
    cross-plan ref, and it means line 1180, not a section. A widened
    attribution predicate reads surrounding prose, not the text of the ref,
    so it is worse than the narrow one.
    """
    r = Result(name)
    fences = re.compile(r"```.*?```", re.S)

    def mask(text):
        inside = False
        out = []
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                inside = not inside
                out.append("")
            else:
                out.append("" if inside else line)
        return "\n".join(out)

    plans = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "plans", "*.md"))):
        base = os.path.basename(p)
        m = re.match(r"(S\d{2})-", base)
        if not m:
            continue
        lines = _utf8(p).split("\n")
        heads = set()
        for line in lines:
            h = re.match(r"^#{1,5}\s*(\d+(?:\.\d+)*)\s*[.、]?", line)
            if h:
                heads.add(h.group(1))
        top = max((int(x) for x in heads if x.isdigit()), default=0)
        plans[m.group(1)] = (heads, len(lines), top)

    # `Snn §A`, then any continuation: `/`, `、`, `,`, `–`, `~`, each optionally
    # re-prefixed with `§`. `S45` writes `S07 §5.1–§5.2`; `contracts/README.md`
    # writes `S23 §6.1`; `plans/` use both.
    ref = re.compile(
        r"(?<![A-Za-z0-9])S(\d{2})\s*§\s*(\d+(?:\.\d+)*)"
        r"((?:\s*(?:[/、,，]|[-–~])\s*§?\s*\d+(?:\.\d+)*)*)"
    )

    # See the docstring: the report writes wrong refs on purpose, in table
    # cells, with no lexical marker that separates them from live ones.
    EXEMPT = {"delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md"}

    refs = 0
    pairs = 0
    heading = 0
    lineno = []
    dangling = []
    exempt_refs = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        if rel.startswith(("docs/design-package/", ".claude/", "_inbox/")):
            continue
        if rel in EXEMPT:
            n = len(ref.findall(mask(_utf8(p))))
            exempt_refs += n
            refs += n  # the total is what README's prose calls "共 N 处"; the
                       # judged count is reported separately below
            continue
        text = mask(_utf8(p))
        for m in ref.finditer(text):
            sid = "S" + m.group(1)
            refs += 1
            line = text[: m.start()].count("\n") + 1
            where = f"{rel}:{line} cites {m.group(0)}"
            if sid not in plans:
                pairs += 1
                dangling.append(f"{where}: plans/ has no {sid}-*.md")
                continue
            heads, nlines, top = plans[sid]
            for x in [m.group(2)] + re.findall(r"\d+(?:\.\d+)*", m.group(3)):
                pairs += 1
                if x in heads or any(h.startswith(x + ".") for h in heads):
                    heading += 1
                elif x.isdigit() and int(x) <= nlines:
                    # Not a heading, but inside the file. Almost always a
                    # mistyped section (`S07 §11` where S07 stops at §10).
                    # It could also be the line-number form, which the owner
                    # has not decided on -- so it is reported as a failure
                    # with that named, not silently accepted. Outside this
                    # report the corpus has no such ref, so nothing is
                    # wrongly failed today.
                    lineno.append(where)
                    r.fail(f"{where}: {sid} has no §{x} (plan is {nlines} "
                           f"lines; if this means line {x}, `Snn §行号` is "
                           f"an open owner decision and not a legal form yet)")
                else:
                    dangling.append(f"{where}: {sid} has no §{x}")

    for d in dangling:
        r.fail(d)

    note = (f"{refs} ref(s) ({refs - exempt_refs} judged), "
            f"{pairs} section pair(s) across {len(plans)} plan file(s); "
            f"{heading}/{pairs} resolve to a heading; "
            f"{exempt_refs} ref(s) in the corrections report not judged")
    if lineno:
        # These are failures, listed here too so the summary says why the run
        # is red without making the reader search the failure lines.
        note += (f"; {len(lineno)} integer ref(s) name no heading and are "
                 f"counted as mistyped sections: " + ", ".join(lineno[:5]))
        if len(lineno) > 5:
            note += f" (+{len(lineno) - 5} more)"
    else:
        note += "; no integer ref names a non-heading"
    r.note = note
    return r




def check_docrefs(name):
    """`docs/NN §X` names a heading in `docs/NN-*.md`.

    The same family as `planrefs`, with one difference worth stating: these
    refs are almost always markdown links, `[docs/07 §1.5](../docs/07-...)`,
    so `links` already proves the path resolves. This settles the other half
    -- that the section number exists -- which no other check looks at.

    488 section pairs across 17 docs. They resolve three ways:
      - 484 to a real heading;
      - 3 as `<top section>.<ordered item>`, `docs/07 §1.5` meaning item 5 of
        §1. That is the same convention the corpus writes out in full as
        `docs/07 §1 步骤 5` (7 in plans/; 2 more elsewhere describe the form), and the text at that position is
        the sentence the citing line quotes, checked by hand. It is accepted
        as a list-item reference and counted separately in the note, not
        silently folded into "resolved to a heading".
      - 1 that names neither: `docs/10 §1`. docs/10 has no numbered heading at
        all (one `# 10` title and seven flat paragraphs), so `§1` can only mean
        "the first paragraph". That form appears exactly once in the corpus and
        the paragraph it names is not the one whose sentence is quoted, so it
        is not treated as a convention. It is reported as a failure with the
        open-decision wording, the way `planrefs` reports `Snn §行号`: the
        owner has not said paragraph numbering is legal, and this check does
        not decide that for them.

    Two heading-reading bugs were made and fixed while measuring, and both are
    the same mistake -- requiring more punctuation than the corpus writes:
      - requiring `.` or `、` after the number cost nothing measurable: every
        one of the 64 numbered headings in docs/ uses `.`, so 0 of the 488
        pairs change verdict under that reading. Kept in the record anyway,
        because "it happened to cost nothing" is not the same as "it was
        right", and the shipped pattern is the one that states the real rule.
      - requiring a space after the number dropped `## 3.跨集改稿`, which puts
        none, and made docs/07 appear to have no §3 -- 7 correct refs (all
        `docs/07 §3`) then looked like misses.
    The pattern accepts all three shapes: `1. X`, `1.X`, `1 X`.

    A level-1 `# NN · title` is the document header, not a section, and is
    skipped by its own regex. Without that, docs/01-09 carry a phantom §"01"
    and docs/10, 17 -- which have a header and no numbered section at all --
    would appear to have exactly one heading, which is the opposite of the
    fact the `docs/10 §1` wording above depends on. No ref uses a
    zero-padded section number today, so this closed a false-pass door that
    was open but never walked through.

    Scope matches `planrefs`: `docs/design-package/` (read-only archive),
    `.claude/` (other sessions' worktrees) and `_inbox/` are skipped, and
    fenced blocks are masked so a quoted wrong form does not count. The
    corrections report is exempt by path for the same reason as there, and
    its refs are still counted in the total, not in the judged count.
    """
    r = Result(name)

    def mask(text):
        inside = False
        out = []
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                inside = not inside
                out.append("")
            else:
                out.append("" if inside else line)
        return "\n".join(out)

    docs = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "docs", "*.md"))):
        m = re.match(r"(\d{2})-", os.path.basename(p))
        if not m:
            continue
        heads = set()
        items = {}
        cur = None
        n = 0
        for line in _utf8(p).split("\n"):
            if re.match(r"^#\s+\d+\s*[·•]", line):
                continue  # `# NN · title` is the doc header, not a section
            h = re.match(r"^#{1,5}\s*(\d+(?:\.\d+)*)[.、]?\s*\S", line)
            if h:
                cur = h.group(1)
                n = 0
                heads.add(cur)
                items.setdefault(cur, 0)
                continue
            if cur is not None and re.match(r"^\d+\.\s", line):
                n += 1
                items[cur] = n
        docs[m.group(1)] = (heads, items)

    # Lookbehind, not a leading `[^A-Za-z0-9_]` character class. The class
    # matches the newline before a ref that starts its own line, so every such
    # ref reported a line number one too small -- 30 of them did, including the
    # only `docs/07 §3` in S07's 4.5 that is not preceded by prose. Lookbehind
    # asserts the same boundary without consuming a character, so the reported
    # line and the quoted text agree. Match set is identical either way; this
    # is purely about the number in the failure message being the right line.
    ref = re.compile(
        r"(?<![A-Za-z0-9_])docs[\s/](\d{1,2})\s*§\s*(\d+(?:\.\d+)*)"
        r"((?:\s*(?:[/、,，]|[-–~])\s*§?\s*\d+(?:\.\d+)*)*)"
    )
    EXEMPT = {"delivery/acceptance/contracts-gap-list-corrections-2026-11-06.md"}

    refs = judged = exempt_refs = 0
    heading = listitem = 0
    unresolved = []
    for p in sorted(glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
        if rel.startswith(("docs/design-package/", ".claude/", "_inbox/")):
            continue
        text = mask(_utf8(p))
        for m in ref.finditer(text):
            n_this = 1
            refs += 1
            if rel in EXEMPT:
                exempt_refs += 1
                continue
            judged += 1
            num = m.group(1).zfill(2)
            line = text[: m.start()].count("\n") + 1
            heads, items = docs[num]
            xs = [m.group(2)] + re.findall(r"\d+(?:\.\d+)*", m.group(3))
            for x in xs:
                if x in heads or any(h.startswith(x + ".") for h in heads):
                    heading += 1
                elif "." in x:
                    top, sub = x.split(".", 1)
                    if top in heads and sub.isdigit() and int(sub) <= items.get(top, 0):
                        listitem += 1
                    else:
                        base = f"{rel}:{line} cites docs/{num} §{x}"
                        unresolved.append((base,
                            base + f": docs/{num}-*.md has no such heading or "
                            f"list item"))
                else:
                    base = f"{rel}:{line} cites docs/{num} §{x}"
                    unresolved.append((base,
                        base + f": docs/{num}-*.md has no §{x}"
                        + ("" if heads else
                           f" (it has no numbered heading at all, so this can "
                           f"only mean paragraph {x}; that form is not "
                           f"established in this corpus and is an open owner "
                           f"decision, like `Snn §行号`)")))
    known_seen = set()
    for key, msg in unresolved:
        if key in KNOWN_UNRESOLVED_DOCREFS:
            known_seen.add(key)
            continue
        r.fail(msg)
    for k in sorted(KNOWN_UNRESOLVED_DOCREFS - known_seen):
        r.fail(f"{k!r} is in KNOWN_UNRESOLVED_DOCREFS but no longer appears "
               f"unresolved; remove the entry")

    # heading + listitem + unresolved must equal the judged pair count, or the
    # note reports a fraction whose denominator does not cover its parts --
    # the same trap `planrefs` fell into by omitting the exempt refs.
    r.note = (f"{refs} ref(s) ({judged} judged), "
              f"{heading + listitem + len(unresolved)} section pair(s) across "
              f"{len(docs)} doc file(s); {heading} resolve to a heading, "
              f"{listitem} to an ordered list item, {len(unresolved)} resolve "
              f"to neither; {exempt_refs} ref(s) in the corrections report not "
              f"judged")
    return r



CHECKS = [
    ("json", check_json, "every contracts/**.json parses"),
    ("payloads", check_payload_refs, "every topic schema_ref/data_schema_ref names a real file"),
    ("openapi", check_openapi_refs, "every $ref in openapi.v2.yaml that names a file resolves"),
    ("topics", check_topic_fields, "every event topic carries the fields its readers use"),
    ("redaction", check_redaction_policy, "redaction policy regexes compile and section refs resolve"),
    ("mcp", check_mcp_tools, "MCP tool scopes agree with their annotations and schema refs resolve"),
    ("checkids", check_check_ids, "check_id values in the QC plans match the schema pattern"),
    ("links", check_links, "every relative markdown link resolves"),
    ("catalog", check_catalog, "service/topic ids agree across catalog, docs/02, topics"),
    ("families", check_families, "action-family structural invariants"),
    ("scaffold", check_scaffold, "generated files match the catalog"),
    ("examples", check_examples, "contracts/examples/ validate against their schemas"),
    ("maturity", check_maturity, "catalog maturity matches whether services/*/src/ has code"),
    ("archive", check_archive, "docs/design-package/ matches the checksums in its MANIFEST"),
    ("safety", check_safety_switches, "runtime-safety.yaml keeps every production switch off"),
    ("interactions", check_interactions, "service-interactions.yaml names real services, topics and authorities"),
    ("plantopics", check_plan_topics, "plans/*.md name declared topics and pair them with their own events"),
    ("readme", check_readme_counts, "contracts/README.md's check-summary counts match the checks"),
    ("docsreadme", check_docs_readme, "docs/README.md's counts about its own directory match the files"),
    ("citerefs", check_citerefs, "prose line citations land on real lines of the file they name"),
    ("planrefs", check_planrefs, "Snn §X in prose names a heading in plans/Snn-*.md"),
    ("docrefs", check_docrefs, "docs/NN §X in prose names a heading in docs/NN-*.md"),
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
    skipped = []
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
        elif r.skipped:
            skipped.append(name)
            print(f"skip  {name:10} {r.note}")
        else:
            print(f"ok    {name:10} {r.note}")

    print()
    if env_problem:
        print("environment problem; at least one check did not run")
        return 2
    if failed:
        return 1
    # A skipped check verified nothing, so it is not counted as passed. A total
    # that includes a check which never executed is the same over-claim as a
    # stale docstring promising broader coverage than it has.
    ran = len(selected) - len(skipped)
    line = f"{ran} check(s) passed"
    if skipped:
        line += f", {len(skipped)} skipped ({', '.join(skipped)}): nothing was verified there"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
