"""
oxigraph_session.py — runs a generated script in Oxigraph, in-process.

Interface: send, sync, close, stopped. The output lines (echo markers,
TSV answers, statement times) are what execution.py and event_log.py
read. The script's commands are interpreted one by one:

  prefix p: <iri>                declared before every SPARQL statement
  import <file>                  .trig into its named graphs; .ttl into the
                                 default graph (the framework)
  echo <text>                    <text>
  # ...                          ignored
  quit                           closes the session
  anything else                  one SPARQL 1.1 statement: a select prints
                                 a header and one TSV row per answer; every
                                 statement prints its evaluation time

Oxigraph keeps the default graph separate from the named graphs: a
pattern outside GRAPH sees only the framework.
"""

from __future__ import annotations

import time
from pathlib import Path

import pyoxigraph as ox


def _term(t) -> str:
    """A solution term in TSV form (<iri>, "literal"^^<datatype>); "" when unbound."""
    if t is None:
        return ""
    if isinstance(t, ox.NamedNode):
        return f"<{t.value}>"
    if isinstance(t, ox.BlankNode):
        return f"_:{t.value}"
    value = t.value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    if t.language:
        return f'"{value}"@{t.language}'
    if t.datatype is not None and t.datatype.value != "http://www.w3.org/2001/XMLSchema#string":
        return f'"{value}"^^<{t.datatype.value}>'
    return f'"{value}"'


class OxigraphSession:
    """One in-memory Oxigraph store for a whole run. send() queues a stream
    element's commands; sync() executes them and returns their output.
    Imported TriG files in the scratch directory are deleted once loaded.
    An error is reported as an "An error occurred" line, counted by LiveRun."""

    def __init__(self, scratch: Path, timeout: float, audit_log: bool = False):
        self.scratch = scratch
        self.timeout = timeout          # kept for interface parity; statements run in-process
        self.stopped: str | None = None
        self._store = ox.Store()
        self._prefixes: list = []
        self._pending: list = []
        self._log = (scratch / "replay.oxigraph").open("w", encoding="utf-8") if audit_log else None

    def send(self, lines: list) -> None:
        if self.stopped is None:
            self._pending += lines
            if self._log is not None:
                self._log.writelines(line + "\n" for line in lines)

    def _prefixed(self, statement: str) -> str:
        return "\n".join(self._prefixes) + "\n" + statement

    def _import(self, path: Path, out: list) -> None:
        if path.suffix == ".trig":
            self._store.load(path=str(path), format=ox.RdfFormat.TRIG)
            if path.parent == self.scratch:
                path.unlink()
        else:
            self._store.load(path=str(path), format=ox.RdfFormat.TURTLE, to_graph=ox.DefaultGraph())

    def _statement(self, statement: str, out: list) -> None:
        t0 = time.perf_counter()
        if statement.lstrip()[:6].lower() == "select":
            solutions = self._store.query(self._prefixed(statement))
            names = [v.value for v in solutions.variables]
            rows = [[_term(s[name]) for name in names] for s in solutions]
            out.append("\t".join(f"?{name}" for name in names))
            out += ["\t".join(r) for r in rows]
        else:
            self._store.update(self._prefixed(statement))
        out.append(f"Total statement evaluation time: {time.perf_counter() - t0:.3f} s")

    def sync(self) -> list:
        """Execute everything sent since the last sync; return its output."""
        out: list = []
        lines, self._pending = self._pending, []
        for line in lines:
            if self.stopped is not None:
                break
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            word = stripped.split(" ", 1)[0].lower()
            try:
                if word == "prefix":
                    _, name, iri = stripped.split(None, 2)
                    self._prefixes.append(f"PREFIX {name} {iri}")
                elif word == "echo":
                    out.append(stripped[5:])
                elif word == "import":
                    self._import(Path(stripped[len("import "):]), out)
                elif word == "quit":
                    self.stopped = "quit"
                else:
                    self._statement(stripped, out)
            except Exception as e:  # reported as an error line, counted by LiveRun
                out.append(f"An error occurred: {type(e).__name__}: {e}")
                out.append(stripped[:200])
        if self.stopped == "quit":
            self.stopped = None
        return out

    def close(self) -> list:
        out = self.sync()
        if self._log is not None:
            self._log.close()
        return out
