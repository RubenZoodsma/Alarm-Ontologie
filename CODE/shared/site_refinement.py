"""
site_refinement.py — map (base metric, site) to the metric it then is.

The annotation names what a method measures (the base metric) and where
(Sensor.AnatomicalPosition). inference.ttl's "Site refinements" table states,
per base metric, which sites change its meaning:

    metric:ArterialBloodPressure mda:hasSiteRefinement
        [ mda:atSite anatomicalPosition:PulmonaryArtery ;
          mda:refinedMetric metric:PulmonaryArteryPressure ] .

Generic in three ways: any base metric can have refinements (the table is
data); a site is any value of a sensor property marked
mda:refinesParticularIdentity in ontology.ttl (anatomical position today,
laterality would need no code); and sites are matched by subsumption
(rdfs:subClassOf* in the vocabulary), the most specific entry winning.

Rules (decided 2026-11-25):
  - a base without refinements: unaffected, its site only tells sensors apart;
  - a base with refinements and no site: the base itself;
  - a base with refinements and a site the table covers: the refined metric;
  - a base with refinements and a site the table does not cover, from a
    scheme the table uses for that base (e.g. ArterialBloodPressure at
    Central): an error (C2) — the base and site contradict each other.

check_inheritance() is C3: a refined metric that is a subclass of its base
inherits the base's mda:approximates, so it must not state another one.

Used by CODE/framework_build/build_framework.py.
"""

from collections import defaultdict

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDFS

MDA = Namespace("https://w3id.org/mda/ontology#")


def _scheme(concept: URIRef) -> str:
    """The vocabulary scheme of a concept: its IRI up to the last '/'."""
    return str(concept).rsplit("/", 1)[0]


def _local(concept) -> str:
    return str(concept).rsplit("/", 1)[-1].rsplit("#", 1)[-1]


class SiteRefinementError(ValueError):
    pass


class SiteTable:
    def __init__(self, inference: Graph, onto: Graph, tbox: Graph):
        """inference: the table; onto: which sensor properties carry a site;
        tbox: the class hierarchy (vocabulary + curated + inference) for site
        subsumption and the inheritance check."""
        self.tbox = tbox
        self.entries: dict = defaultdict(list)      # base -> [(site, refined)]
        for base, node in inference.subject_objects(MDA.hasSiteRefinement):
            site = inference.value(node, MDA.atSite)
            refined = inference.value(node, MDA.refinedMetric)
            if site is None or refined is None:
                raise SiteRefinementError(
                    f"site refinement of {_local(base)} lacks mda:atSite or mda:refinedMetric")
            self.entries[base].append((site, refined))
        self.site_properties = {p for p, v in onto.subject_objects(MDA.refinesParticularIdentity)
                                if str(v).lower() == "true"}
        # the classes their values belong to (e.g. mda:AnatomicalPosition):
        # an annotation column may bind a site as a node of that class
        self.site_classes = {r for p in self.site_properties for r in onto.objects(p, RDFS.range)}
        self.used: dict = defaultdict(int)          # (base, site, refined) -> rows

    def _subsumes(self, general: URIRef, specific: URIRef) -> bool:
        """specific rdfs:subClassOf* general."""
        if general == specific:
            return True
        return general in set(self.tbox.transitive_objects(specific, RDFS.subClassOf))

    def resolve(self, base, sites) -> URIRef:
        """The metric `base` is when measured at `sites` (the row's values of
        the site-carrying sensor properties). Raises SiteRefinementError on a
        site the table does not cover for this base (C2) or on two entries
        that match equally well."""
        entries = self.entries.get(base)
        if base is None or not entries:
            return base
        table_schemes = {_scheme(s) for s, _ in entries}
        relevant = [q for q in sites if _scheme(q) in table_schemes]
        if not relevant:
            self.used[(base, None, base)] += 1
            return base
        matches = [(s, r) for s, r in entries for q in relevant if self._subsumes(s, q)]
        if not matches:
            raise SiteRefinementError(
                f"{_local(base)} at {', '.join(_local(q) for q in relevant)}: no site refinement "
                f"covers this site (allowed: {', '.join(sorted(_local(s) for s, _ in entries))}) — "
                f"the base metric and the site contradict each other, or the table lacks an entry")
        most_specific = [(s, r) for s, r in matches
                         if not any(o != s and self._subsumes(s, o) for o, _ in matches)]
        refined = {r for _, r in most_specific}
        if len(refined) > 1:
            raise SiteRefinementError(
                f"{_local(base)} at {', '.join(_local(q) for q in relevant)}: ambiguous, matches "
                + ", ".join(f"{_local(s)} -> {_local(r)}" for s, r in most_specific))
        site, result = most_specific[0]
        self.used[(base, site, result)] += 1
        return result

    def _approximates(self, concept, seen=()) -> set:
        found = set()
        for sup in self.tbox.objects(concept, RDFS.subClassOf):
            if (sup, OWL.onProperty, MDA.approximates) in self.tbox:
                found |= set(self.tbox.objects(sup, OWL.hasValue))
            elif isinstance(sup, URIRef) and sup not in seen:
                found |= self._approximates(sup, seen + (concept,))
        return found

    def check_inheritance(self) -> list:
        """C3. Problems as text: a refined subclass of its base stating a
        property of its own besides the inherited one; a replacing metric
        (not a subclass) without a property of its own."""
        problems = []
        for base, entries in self.entries.items():
            base_props = self._approximates(base)
            for site, refined in entries:
                if refined == base:
                    continue
                props = self._approximates(refined)
                if self._subsumes(base, refined):
                    if props - base_props:
                        problems.append(
                            f"{_local(refined)} (site {_local(site)}) is a subclass of {_local(base)} "
                            f"but states {', '.join(sorted(_local(p) for p in props - base_props))} besides the "
                            f"inherited {', '.join(sorted(_local(p) for p in base_props))}: either it keeps the "
                            f"base's property (drop its own) or it replaces the base (drop the subclass edge)")
                elif not props:
                    problems.append(
                        f"{_local(refined)} (site {_local(site)}) replaces {_local(base)} (not a subclass) "
                        f"but approximates no property: add its mda:approximates axiom")
        return problems

    def report(self) -> list:
        """Build-log lines: per base, how many rows each site resolved to."""
        lines = []
        for base in sorted(self.entries, key=str):
            uses = [(site, refined, n) for (b, site, refined), n in self.used.items() if b == base]
            if not uses:
                lines.append(f"{_local(base)}: no row")
                continue
            parts = [f"{_local(site) if site is not None else '(no site)'} -> {_local(refined)} x{n}"
                     for site, refined, n in sorted(uses, key=lambda u: (str(u[0]), str(u[1])))]
            lines.append(f"{_local(base)}: " + "; ".join(parts))
        return lines
