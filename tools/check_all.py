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
      decision. `_tmp`-style scratch directories would also hide there.
    """
    r = Result(name)
    fence = re.compile(r"```.*?```", re.S)
    inline = re.compile(r"`[^`\n]*`")
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
    r.note = (f"{total} local link(s)"
              f"; docs/design-package/ ({skipped_files} md, {skipped_links} link(s)) "
              f"is read-only archive, not checked"
              + (f" -- {len(skipped_broken)} broken there" if skipped_broken else ""))
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
        summary = ", ".join(f"{n} in {d}/" for d, n in sorted(by_dir.items()))
        r.note += (f"  [{len(unloaded)} schema file(s) never loaded by the validator: "
                   f"{summary}]")

    # contracts/README.md quotes a count of examples in its nine-check summary,
    # and that count went 57 -> 63 -> 66 while the directory kept growing to 72,
    # because nothing tied the sentence to the files. The validator's own tally is
    # the source of truth; check that it closes over the directory so the number
    # can be copied from a green line instead of remembered.
    on_disk = len(glob.glob(os.path.join(CONTRACTS, "examples", "*.json")))
    stems = {os.path.basename(p).split(".")[0]
             for p in glob.glob(os.path.join(CONTRACTS, "examples", "*.json"))}
    mapped = {s for s in stems if s in mod.SCHEMA_OF}
    tally = re.search(r"(\d+) example\(s\) valid, (\d+) failing, (\d+) without a schema",
                      verbose.getvalue())
    if tally:
        r.note += (f"  {tally.group(1)} valid, {tally.group(3)} without a schema; "
                   f"{on_disk} file(s) on disk, {len(stems)} stem(s), {len(mapped)} mapped")
    else:
        r.note += (f"  {on_disk} file(s) on disk, {len(stems)} stem(s), "
                   f"{len(mapped)} mapped")
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
    ("check_id", r"(\d+) 个\s*\n?`check_id` 是否符合", "checkids", r"(\d+) check_id"),
    ("links", r"`links`（(\d+) 条相对链接", "links", r"(\d+) local link"),
    ("stated invariant", r"自己声明的\s*\n?(\d+) 条 invariant", "families", r"(\d+) stated invariant"),
    ("scaffold", r"`scaffold`（(\d+) 个生成物", "scaffold", r"(\d+) generated file"),
    ("example", r"`examples`（\*\*(\d+)\*\* 个示例", "examples", r"(\d+) valid"),
    ("unmapped example", r"另 (\d+) 个刻意不映射", "examples", r"(\d+) without a schema"),
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
    start = head + opener.end()
    para = readme[start:readme.find("\n\n", start)]

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
              + (f"; {counts_note}" if counts_note else ""))
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


CHECKS = [
    ("json", check_json, "every contracts/**.json parses"),
    ("payloads", check_payload_refs, "every topic schema_ref/data_schema_ref names a real file"),
    ("checkids", check_check_ids, "check_id values in the QC plans match the schema pattern"),
    ("links", check_links, "every relative markdown link resolves"),
    ("catalog", check_catalog, "service/topic ids agree across catalog, docs/02, topics"),
    ("families", check_families, "action-family structural invariants"),
    ("scaffold", check_scaffold, "generated files match the catalog"),
    ("examples", check_examples, "contracts/examples/ validate against their schemas"),
    ("maturity", check_maturity, "catalog maturity matches whether services/*/src/ has code"),
    ("readme", check_readme_counts, "contracts/README.md's check-summary counts match the checks"),
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
