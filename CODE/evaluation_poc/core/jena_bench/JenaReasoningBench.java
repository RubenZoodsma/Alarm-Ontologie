// JenaReasoningBench.java — clean-slate Apache Jena reasoning benchmark.
//
// Not wired into the real pipeline (op_knowledge.py) at all — this exists to
// answer one question with real numbers: does swapping the reasoning engine
// away from owlrl actually fix the ~5.3s-per-call cost profiled against it,
// while Oxford Semantic Technologies' RDFox license key is still pending.
//
// Mirrors the EXACT isolated test scenario already run against owlrl (see
// the conversation this file comes from): load the four real static TTL
// files, add a minimal Device->FunctionalUnit->Sensor->Signal->
// SignalAnalysis->Metric ABox, reason, and check whether the sensor-enabled
// axiom (FRAMEWORK/KNOWLEDGE_BASE/inference.ttl) correctly derives
// hasSensorOperationState on the Sensor AND propagates to hasOperationState
// on the Device — the same two things checked against owlrl, for a true
// apples-to-apples comparison, not just "Jena feels faster."
//
// Why this needs FOUR hand-written rules Jena's own owl-fb.rules ruleset
// does NOT provide: checked directly (grep) before writing this — Jena's
// fullest bundled OWL ruleset supports owl:someValuesFrom/owl:intersectionOf
// (what our sensor-enabled axiom itself uses) but has zero rules for
// owl:propertyChainAxiom, an OWL 2 construct predating Jena's classic rule
// engine. mda:hasOperationState's propagation (ontology.ttl) is exactly
// four two-hop chains, each trivially expressible as a native Jena forward
// rule — not a workaround, just Jena's normal rule syntax standing in for
// an OWL 2 feature its stock ruleset never implemented.
//
// Usage
// -----
//   cd CODE/evaluation_poc/core/jena_bench
//   javac -cp "$HOME/tools/apache-jena-6.2.0/lib/*" JenaReasoningBench.java
//   java  -cp "$HOME/tools/apache-jena-6.2.0/lib/*:." JenaReasoningBench

import org.apache.jena.rdf.model.*;
import org.apache.jena.reasoner.Reasoner;
import org.apache.jena.reasoner.rulesys.GenericRuleReasoner;
import org.apache.jena.reasoner.rulesys.Rule;
import org.apache.jena.util.FileManager;
import org.apache.jena.vocabulary.RDF;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.StringJoiner;

public class JenaReasoningBench {

    static final String MDA = "https://w3id.org/mda/ontology#";
    static final String EX  = "https://example.org/test/";

    // The four chains, stated directly in ontology.ttl as
    // mda:hasOperationState's own owl:propertyChainAxiom list — reproduced
    // here as Jena rules, not reinterpreted or approximated.
    // The three subproperties of mda:hasOperationState (ontology.ttl's own
    // rdfs:subPropertyOf declarations) — forced forward, see the long comment
    // at their point of use for why this can't be left to owl-fb.rules alone.
    static final String[] SUBPROPERTY_RULES = {
        "[subprop1: (?s <" + MDA + "hasSensorOperationState> ?o) -> (?s <" + MDA + "hasOperationState> ?o)]",
        "[subprop2: (?s <" + MDA + "hasDeviceOperationState> ?o) -> (?s <" + MDA + "hasOperationState> ?o)]",
        "[subprop3: (?s <" + MDA + "hasComponentOperationState> ?o) -> (?s <" + MDA + "hasOperationState> ?o)]",
    };

    static final String[] CHAIN_RULES = {
        "[chain1: (?x <" + MDA + "producesSignal> ?y) (?y <" + MDA + "hasOperationState> ?z) -> (?x <" + MDA + "hasOperationState> ?z)]",
        "[chain2: (?x <" + MDA + "hasSensor> ?y) (?y <" + MDA + "hasOperationState> ?z) -> (?x <" + MDA + "hasOperationState> ?z)]",
        "[chain3: (?x <" + MDA + "hasFunctionalUnit> ?y) (?y <" + MDA + "hasOperationState> ?z) -> (?x <" + MDA + "hasOperationState> ?z)]",
        "[chain4: (?d <" + MDA + "administers> ?m) (?d <" + MDA + "hasOperationState> ?z) -> (?m <" + MDA + "hasOperationState> ?z)]",
    };

    public static void main(String[] args) throws Exception {
        String repoRoot = args.length > 0 ? args[0]
            : "/Users/rzoodsm2/Library/CloudStorage/OneDrive-UMCUtrecht/BACKUP_23:02:26/GIT-repositories/Alarm-Ontologie";

        Model staticModel = ModelFactory.createDefaultModel();
        String[] staticFiles = {
            repoRoot + "/FRAMEWORK/ONTOLOGY/ontology.ttl",
            repoRoot + "/FRAMEWORK/VOCABULARY/vocab_generated.ttl",
            repoRoot + "/FRAMEWORK/KNOWLEDGE_BASE/inference.ttl",
            repoRoot + "/DATA/POC_EVENTS/entities.ttl",
        };
        for (String f : staticFiles) {
            long before = System.currentTimeMillis();
            FileManager.getInternal().readModel(staticModel, f);
            System.out.printf("  loaded %-70s (%d ms)%n", f.substring(f.lastIndexOf('/') + 1),
                    System.currentTimeMillis() - before);
        }
        System.out.println("static model size: " + staticModel.size() + " triples");

        // owl-fb.rules: Jena's fullest bundled OWL ruleset (forward+backward),
        // confirmed to include someValuesFrom/intersectionOf handling — the
        // constructs our sensor-enabled axiom actually uses. (owl.rules, the
        // pure-forward variant, was tried first as a fix for the issue explained
        // below, but its parser requires builtin functors — e.g. "contradiction"
        // in conflict1 — that Rule.parseRules doesn't register outside Jena's own
        // internal loading path; not usable standalone here.)
        //
        // owl-fb.rules is hybrid forward+backward — confirmed by direct isolated
        // test (see conversation) that its RDFS subPropertyOf entailment is only
        // visible to a direct query (listStatements finds it fine), never
        // materialised into the FORWARD working memory our own chain rules scan
        // over — so a custom forward chain rule never sees a subproperty-derived
        // fact as a premise, even though a direct query for that exact same fact
        // succeeds. SUBPROPERTY_RULES below sidesteps this: three explicit
        // forward rules for the three OperationState subproperties specifically
        // (see ontology.ttl's own rdfs:subPropertyOf declarations), guaranteeing
        // a forward-materialised generic hasOperationState fact for our chain
        // rules to consume, without depending on owl-fb.rules' own (partly
        // backward) subPropertyOf handling for this specific path. Loaded via
        // the classloader directly (Rule.rulesFromURL's "classpath:" scheme
        // isn't recognised by this Jena version) rather than a URL.
        List<Rule> rules = new ArrayList<>(Rule.parseRules(readClasspathResource("/etc/owl-fb.rules")));
        for (String r : SUBPROPERTY_RULES) {
            rules.addAll(Rule.parseRules(r));
        }
        for (String r : CHAIN_RULES) {
            rules.addAll(Rule.parseRules(r));
        }
        System.out.println("total rules loaded: " + rules.size());

        GenericRuleReasoner reasoner = new GenericRuleReasoner(rules);
        reasoner.setOWLTranslation(true);
        reasoner.setTransitiveClosureCaching(true);

        // For each n_background, a fresh reasoning pass over the SAME static
        // model plus a small synthetic ABox — mirrors the owlrl scaling test
        // exactly (n_background = how many OTHER Sensor->...->Metric chains
        // sit alongside the one being checked).
        for (int n : new int[]{0, 1, 2, 3, 4, 5, 6}) {
            Model abox = ModelFactory.createDefaultModel();
            for (int i = 0; i <= n; i++) {
                addChain(abox, "dev" + i, "fu" + i, "sensor" + i, "signal" + i, "analysis" + i, "metric" + i);
            }

            long t0 = System.currentTimeMillis();
            InfModel inf = ModelFactory.createInfModel(reasoner, staticModel.union(abox));
            // InfModel is lazy — force materialisation the same way
            // owlrl.DeductiveClosure.expand() is eager, by actually reading
            // the derived facts, not just binding the reasoner.
            long derivedCount = inf.size();
            long elapsed = System.currentTimeMillis() - t0;

            Resource sensor0 = inf.getResource(EX + "sensor0");
            Resource fu0 = inf.getResource(EX + "fu0");
            Resource dev0 = inf.getResource(EX + "dev0");
            Property hasSensorOpState = inf.getProperty(MDA + "hasSensorOperationState");
            Property hasOpState = inf.getProperty(MDA + "hasOperationState");

            StringJoiner sensorSpecific = new StringJoiner(", ");
            inf.listStatements(sensor0, hasSensorOpState, (RDFNode) null)
               .forEachRemaining(s -> sensorSpecific.add(s.getObject().toString()));
            StringJoiner sensorGeneric = new StringJoiner(", ");
            inf.listStatements(sensor0, hasOpState, (RDFNode) null)
               .forEachRemaining(s -> sensorGeneric.add(s.getObject().toString()));
            StringJoiner fuVals = new StringJoiner(", ");
            inf.listStatements(fu0, hasOpState, (RDFNode) null)
               .forEachRemaining(s -> fuVals.add(s.getObject().toString()));
            StringJoiner devVals = new StringJoiner(", ");
            inf.listStatements(dev0, hasOpState, (RDFNode) null)
               .forEachRemaining(s -> devVals.add(s.getObject().toString()));

            System.out.printf("n_background=%d  total=%6d  elapsed=%5dms  sensor0.hasSensorOpState=[%s]  sensor0.hasOpState(generic)=[%s]  fu0.hasOpState=[%s]  dev0.hasOpState=[%s]%n",
                    n, derivedCount, elapsed, sensorSpecific, sensorGeneric, fuVals, devVals);
            inf.close();
        }
    }

    static String readClasspathResource(String path) throws Exception {
        try (InputStream in = JenaReasoningBench.class.getResourceAsStream(path)) {
            if (in == null) {
                throw new IllegalStateException("Resource not found on classpath: " + path);
            }
            StringBuilder sb = new StringBuilder();
            BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            String line;
            while ((line = r.readLine()) != null) {
                sb.append(line).append('\n');
            }
            return sb.toString();
        }
    }

    static void addChain(Model m, String dev, String fu, String sensor, String signal, String analysis, String metric) {
        Resource devR = m.createResource(EX + dev);
        Resource fuR = m.createResource(EX + fu);
        Resource sensorR = m.createResource(EX + sensor);
        Resource signalR = m.createResource(EX + signal);
        Resource analysisR = m.createResource(EX + analysis);
        Resource metricR = m.createResource(EX + metric);

        m.add(devR, RDF.type, m.createResource(MDA + "Device"));
        m.add(devR, m.createProperty(MDA + "hasFunctionalUnit"), fuR);
        m.add(fuR, RDF.type, m.createResource(MDA + "FunctionalUnit"));
        m.add(fuR, m.createProperty(MDA + "hasSensor"), sensorR);
        m.add(sensorR, RDF.type, m.createResource(MDA + "Sensor"));
        m.add(sensorR, m.createProperty(MDA + "sensorProducesSignal"), signalR);
        m.add(signalR, RDF.type, m.createResource(MDA + "Signal"));
        m.add(signalR, m.createProperty(MDA + "analyzedBy"), analysisR);
        m.add(analysisR, RDF.type, m.createResource(MDA + "SignalAnalysis"));
        m.add(analysisR, m.createProperty(MDA + "producesMetric"), metricR);
        m.add(metricR, RDF.type, m.createResource(MDA + "Metric"));
    }
}
