"""
regression.py — every verification case in MDA-POC/validation/cases/
(one YAML file per case: its alarms, expected conclusions and rationale;
see cases.py for the format), plus engine-wide invariants: no
lookahead (prefix invariance, no engine needed), no rule logic in Python,
blueprint identity (every distinct annotated alarm resolves to a
blueprint of its own), framework copies in sync (MDA-POC/data/ holds the
framework files the build wrote, and the scaffold), and minting
completeness (every blueprint node and edge is grounded once).

Prints a PASS/FAIL line per assertion and writes
  validation/results/cases_results.csv   one row per assertion
  validation/results/cases_run.json      when, on which commit and files
and both engine logs to _scratch/.

  cd MDA-POC/engine && python3 regression.py
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import cases as C
import rules as RU
import event_log as EL
import mint as M
import stream as S
from execution import RuleStats, summarize_invocations
from paths import ACTIONS_DIR, ENGINE_DIR, POC_DIR, RULES_DIR
from processor import Processor, run_live

SETTINGS = {
    # Write every command sent to the engine to _scratch/replay.* (debugging).
    "audit_logging": False,
    # Case ids to run (e.g. ["fig2_scenario"]); None runs every case.
    "only": None,
    # Write validation/results/ (switch off while iterating on one case).
    "write_results": True,
}


def rule_logic_outside_rule_files() -> list:
    """Python lines that look like rule logic: a select, a FILTER, a
    negation, or a pattern over the flag/silence predicates. Rules live in
    representation/rules/ and actions in representation/actions/ (rules.py);
    Python only binds and orders them. Graph drops and the priority INSERT
    DATA (windows.py) are window mechanics and do not match. Comments and
    docstring lines are skipped."""
    pattern = re.compile(r"(?i:select\s+(distinct\s+)?\?)|FILTER\s*\(|NOT EXISTS|silencedBy>|flaggedLikelyFalsePositive>")
    hits = []
    for path in sorted(ENGINE_DIR.glob("*.py")):
        if path.name in ("regression.py", "cases.py"):
            continue
        in_docstring = False
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.count('"""') % 2 == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            if pattern.search(stripped.split("  #", 1)[0]):
                hits.append(f"{path.name}:{i}: {stripped[:100]}")
    return hits


def lookahead_by_construction() -> list:
    """Structural guards: an arrival carries no end, and no rule or action
    file uses validity metadata."""
    problems = []
    if "end" in {f.name for f in dataclasses.fields(S.AlarmArrival)}:
        problems.append("stream.AlarmArrival has an end field")
    for folder in (RULES_DIR, ACTIONS_DIR):
        for path in sorted(folder.glob("*.rq")):
            if "validUntil" in path.read_text():
                problems.append(f"{path.name}: validUntil")
    return problems


UNKNOWN_END = datetime.max


def _instants(elements: list) -> list:
    """Stream elements grouped by instant, in order: [(t, [element, ...])]."""
    grouped: list = []
    for element in elements:
        if grouped and grouped[-1][0] == element.time:
            grouped[-1][1].append(element)
        else:
            grouped.append((element.time, [element]))
    return grouped


def _normalised(lines: list) -> list:
    """Script lines with each TriG import replaced by the file's content,
    so two runs compare on what they mint, not on file paths."""
    return [Path(line[len("import "):]).read_text() if line.startswith("import ") else line
            for line in lines]


def script_depends_on_future(kb, groups: dict, scratch: Path) -> list:
    """Prefix invariance. For every patient and every instant t, the script
    emitted up to t by a run over the whole stream must equal the script of
    a run that knows only the stream up to t: arrivals after t removed,
    ends after t unknown. Any difference means something after t leaked
    into what the engine did at or before t. The whole script covers the
    minted triples, every rule invocation and every window drop; the engine
    executes it deterministically, so the store and every decision up to t
    are identical too."""
    rule_names = list(RU.RULES)
    problems = []
    for patient, events in groups.items():
        base = scratch / "lookahead" / M._clean(patient)
        (base / "full").mkdir(parents=True)
        full = Processor(kb, base / "full", rule_names)
        boundaries = []
        for t, elements in _instants(S.replay_stream(events)):
            full.feed(elements)
            boundaries.append((t, len(full.lines)))
        full_script = _normalised(full.lines)

        for n, (t, emitted) in enumerate(boundaries):
            known = [e if e.end <= t else dataclasses.replace(e, end=UNKNOWN_END)
                     for e in events if e.start <= t]
            (base / f"cut{n}").mkdir()
            cut = Processor(kb, base / f"cut{n}", rule_names)
            cut.feed([element for element in S.replay_stream(known) if element.time <= t])
            if _normalised(cut.lines) != full_script[:emitted]:
                problems.append(f"{patient}: the script up to {t.isoformat()} depends on the stream after it")
                break
    shutil.rmtree(scratch / "lookahead")
    return problems


def _git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=POC_DIR, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _hash(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


# MDA-POC/data/ file -> the file it copies, relative to the repository root
# (synced by hand). entities.ttl is the scaffold entity_seed.py writes from
# kg_generated.ttl: a stale copy makes minting stop at the first individuated
# node whose concept it lacks.
FRAMEWORK_COPIES = {
    "ontology.ttl": "FRAMEWORK/ONTOLOGY/ontology.ttl",
    "inference.ttl": "FRAMEWORK/KNOWLEDGE_BASE/inference.ttl",
    "kg_generated.ttl": "FRAMEWORK/KNOWLEDGE_BASE/kg_generated.ttl",
    "vocab_generated.ttl": "FRAMEWORK/VOCABULARY/vocab_generated.ttl",
    "clinicalEvent_vocab.ttl": "FRAMEWORK/VOCABULARY/seed/clinicalEvent_vocab.ttl",
    "priority_rank.ttl": "FRAMEWORK/VOCABULARY/priority_rank.ttl",
    "entities.ttl": "DATA/POC_EVENTS/entities.ttl",
}


def _same_graph(a: Path, b: Path) -> bool:
    """True when two Turtle files hold the same graph: isomorphic, blank-node
    labels and serialisation aside (RDFC-1.0 canonicalisation, pyoxigraph)."""
    import pyoxigraph as ox
    datasets = []
    for path in (a, b):
        ds = ox.Dataset(ox.parse(path=str(path), format=ox.RdfFormat.TURTLE))
        ds.canonicalize(ox.CanonicalizationAlgorithm.RDFC_1_0)
        datasets.append(ds)
    return datasets[0] == datasets[1]


def framework_copy_issues() -> list:
    """The POC must reason over the framework as built: each copy in
    MDA-POC/data/ holds the same graph as its FRAMEWORK/ source (identical
    bytes, or isomorphic: a rebuild may renumber blank nodes)."""
    root = M.DATA_DIR.parent.parent
    issues = []
    for copy, source in FRAMEWORK_COPIES.items():
        a, b = M.DATA_DIR / copy, root / source
        if not a.exists() or not b.exists():
            issues.append(f"MDA-POC/data/{copy} or {source} is missing")
        elif a.read_bytes() != b.read_bytes() and not _same_graph(a, b):
            issues.append(f"MDA-POC/data/{copy} differs from {source}: copy it")
    return issues


def minting_completeness_issues(kb) -> list:
    """Every blueprint, minted for one (patient, device), grounds each of its
    nodes below the device exactly once, and each edge between them exactly
    once. Counted from the catalogue's blank nodes, not from the walk that
    minting uses, so a walk that drops a node (one per class, one child per
    property) or a scaffold that cuts a chain fails here. Referential nodes
    are vocabulary IRIs, not blank nodes, and are not counted. Also checks
    the graph contract: the condition graph holds only states (no node
    types, no edges between nodes), each on a node the background declares."""
    from rdflib import BNode
    from rdflib.namespace import RDF
    issues = []
    for t in sorted(set(kb.type_index.values()), key=str):
        devices = [d for msg in kb.catalogue.objects(t, M.MDA.hasMessage)
                   for d in kb.catalogue.objects(msg, M.MDA.triggeredBy)]
        if len(devices) != 1:
            continue
        nodes, edges, todo = set(), set(), [devices[0]]
        while todo:
            n = todo.pop()
            for p, o in kb.catalogue.predicate_objects(n):
                if isinstance(o, BNode):
                    edges.add((n, p, o))
                    if o not in nodes:
                        nodes.add(o)
                        todo.append(o)
        try:
            bg, cond = M.ground_chain(kb, M.archetype_structure(kb, t), "P", "D", {})
        except Exception as e:      # an error here is the finding
            issues.append(f"{M._local(t)}: {e}")
            continue
        g = bg + cond
        grounded = {s for s, _, _ in g.triples((None, RDF.type, None))}
        links = [(s, p, o) for s, p, o in g if p != RDF.type and str(o).startswith(str(M.ENTITY))]
        if len(grounded) != len(nodes) or len(links) != len(edges):
            issues.append(f"{M._local(t)}: {len(grounded)}/{len(nodes)} nodes, {len(links)}/{len(edges)} edges grounded")
        # Contract: structure persists, states are transient. The condition
        # graph holds only states on nodes the background graph declares.
        structural = [(s, p, o) for s, p, o in cond if p == RDF.type or str(o).startswith(str(M.ENTITY))]
        if structural:
            issues.append(f"{M._local(t)}: {len(structural)} node/edge triple(s) in the condition graph, "
                          f"e.g. {M._local(structural[0][1])}")
        undeclared = {s for s, _, _ in cond} - grounded - {M.device_iri("P", "D")}
        if undeclared:
            issues.append(f"{M._local(t)}: state on a node the background graph does not declare: "
                          f"{M._local(next(iter(undeclared)))}")
    return issues


def blueprint_identity_issues(kb) -> list:
    """Every distinct alarm of the selected corpus that is annotated (not
    EXCL) resolves, through its gateway device name, label and priority, to a
    blueprint; no two distinct alarms share one; every blueprint is reached.
    Guards against a lookup on fewer fields (label only, or label + priority)
    silently merging alarms of different devices or priorities."""
    import pandas as pd
    from types import SimpleNamespace
    corpus = pd.read_csv(M.DEVICE_MODELS.parent.parent / "CORPUS" / "Annotation_corpus_99.csv",
                         sep=";", encoding="utf-8-sig", dtype=str)
    ann = pd.read_csv(M.DEVICE_MODELS.parent / "consensus" / "p99_annotated.csv", sep=";", dtype=str)
    excl = {M.blueprint_key(m, l, p) for l, p, m, s in
            zip(ann.alarmLabel, ann.alarmPriority, ann["Device.Type"], ann.reviewStatus)
            if str(s).strip() == "EXCL"}
    issues, seen = [], {}
    for g, l, p in {(g, l, p) for g, l, p in zip(corpus.Alarm_device, corpus.Alarm_label, corpus.Alarm_priority)}:
        if M.blueprint_key(kb.model_of.get(g, ""), l, p) in excl:
            continue
        iri = M.type_of(kb, SimpleNamespace(device_name=g, label=l, priority=p))
        if iri is None:
            issues.append(f"no blueprint: {g} | {l.strip()} | {p}")
        elif iri in seen:
            issues.append(f"shared blueprint {iri}: {seen[iri]} and {g} | {l.strip()} | {p}")
        else:
            seen[iri] = f"{g} | {l.strip()} | {p}"
    unreached = set(kb.type_index.values()) - set(seen)
    issues += [f"blueprint not reached by any annotated alarm: {i}" for i in sorted(unreached)]
    return sorted(issues)


def run():
    """Run the cases, print a PASS/FAIL line per assertion, write the results."""
    scratch = ENGINE_DIR / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)

    kb = M.load_kb()
    cases = C.load_cases(kb=kb, only=SETTINGS["only"])
    runnable = [c for c in cases if c.status != "draft"]
    in_scope = {c.id: c.events for c in runnable}

    # The replayed alarms as an events file, for validation/log_rebuild_check.py.
    with (scratch / "cases_events.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["patientID", "label", "device_id", "start", "end", "alarm_id", "device_name", "priority"])
        writer.writerows([e.patient, e.label, e.device_id, e.start.isoformat(), e.end.isoformat(), e.alarm_id,
                          e.device_name, e.priority]
                         for evs in in_scope.values() for e in evs)

    alarms = EL.alarm_index(e for evs in in_scope.values() for e in evs)
    log = EL.LiveLog(in_scope, alarms, scratch / "clinical_events.csv", scratch / "rule_firings.csv")
    stats = RuleStats()
    complete = run_live(kb, in_scope, scratch, log, stats, audit_log=SETTINGS["audit_logging"])
    log.close()
    if not complete:
        print("*** INCOMPLETE RUN: the engine stopped or reported errors (see above). ***")
    summarize_invocations(stats)

    rows = []   # case, rule, purpose, status, review, n, assertion, result, observed
    for case in runnable:
        results = C.evaluate(case, C.observe(case, log.firings, log.records))
        case_ok = all(ok for _a, ok, _o in results)
        for n, (assertion, ok, observed) in enumerate(results, start=1):
            if case.status == "expected-fail":
                result = "XPASS" if case_ok else ("XFAIL" if not ok else "pass")
            else:
                result = "PASS" if ok else "FAIL"
            rows.append([case.id, case.rule, case.purpose, case.status, case.review, n, assertion, result,
                         observed])
            if result != "pass":
                print(f"[{result}] {case.id} #{n}: {assertion}" + ("" if ok else f"\n    observed: {observed}"))

    problems = lookahead_by_construction() + script_depends_on_future(kb, in_scope, scratch)
    hits = rule_logic_outside_rule_files()
    identity = blueprint_identity_issues(kb)
    copies = framework_copy_issues()
    completeness = minting_completeness_issues(kb)
    for name, issues in (("no lookahead: at every instant, the script so far is independent of the stream "
                          "after it", problems), ("no rule logic in Python sources", hits),
                         ("blueprint identity: every distinct annotated alarm (device, label, priority) "
                          "resolves to a blueprint of its own", identity),
                         ("framework copies: MDA-POC/data/ equals the FRAMEWORK/ and scaffold files", copies),
                         ("minting completeness: every blueprint grounds each of its nodes and edges once",
                          completeness)):
        result = "FAIL" if issues else "PASS"
        rows.append(["(engine)", "engine", "invariant", "active", "done", 0, name, result,
                     "; ".join(issues) or "none"])
        print(f"[{result}] {name}" + "".join(f"\n    {i}" for i in issues))

    active = [r for r in rows if r[3] == "active"]
    passed = sum(r[7] == "PASS" for r in active)
    xfail = sorted({r[0] for r in rows if r[7] == "XFAIL"})
    xpass = sorted({r[0] for r in rows if r[7] == "XPASS"})
    drafts = [c.id for c in cases if c.status == "draft"]
    n_cases = len({r[0] for r in active if r[0] != "(engine)"})
    print(f"\n{passed}/{len(active)} assertions passed in {n_cases} active cases "
          f"(including {len(active) - sum(r[0] != '(engine)' for r in active)} engine invariants)")
    if xfail or xpass:
        print(f"expected-fail: {len(xfail)} still failing, {len(xpass)} now passing {xpass}")
    if drafts:
        print(f"draft, not run: {drafts}")

    if SETTINGS["write_results"]:
        out = C.RESULTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        with (out / "cases_results.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["case", "rule", "purpose", "status", "review", "n", "assertion", "result",
                             "observed"])
            writer.writerows(rows)
        framework = [M.ONTOLOGY, M.VOCAB, M.CLINICAL_EVENT_VOCAB, M.INFERENCE, M.CATALOGUE, M.ENTITIES]
        meta = {
            "run": datetime.now().isoformat(timespec="seconds"),
            "commit": _git("rev-parse", "--short", "HEAD"),
            "uncommitted_changes": bool(_git("status", "--porcelain", "--", ".")),
            "framework_hash": _hash(framework),
            "rules_hash": _hash(list(RULES_DIR.glob("*.rq")) + list(ACTIONS_DIR.glob("*.rq"))),
            "cases_hash": _hash(list(C.CASES_DIR.glob("*.yaml"))),
            "only": SETTINGS["only"],
            "complete": complete,
            "active_cases": n_cases,
            "assertions": len(active),
            "passed": passed,
            "expected_fail": xfail,
            "unexpected_pass": xpass,
            "draft": drafts,
        }
        (out / "cases_run.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"results written to {out}")


if __name__ == "__main__":
    run()
