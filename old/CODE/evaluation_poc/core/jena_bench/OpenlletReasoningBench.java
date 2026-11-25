// OpenlletReasoningBench.java — clean-slate Openllet (Pellet fork) reasoning
// benchmark, checking whether a genuinely generic OWL 2 DL reasoner can
// replace both owlrl's naive slowness AND Jena's hand-coded-rule workaround.
//
// Unlike JenaReasoningBench.java in this same directory, there are NO
// hand-written chain/subproperty rules here at all — that was the whole
// point of trying this. Openllet is a real OWL 2 DL reasoner (Pellet fork)
// and should read mda:hasOperationState's owl:propertyChainAxiom and the
// sensor-enabled axiom's owl:someValuesFrom/owl:intersectionOf natively from
// the ontology data, the same way owlrl already does correctly — the
// question is only whether it does so fast enough for our repeated small-
// delta reasoning pattern, since tableau-based DL reasoners are usually
// tuned for one-shot classification/consistency checking, not high-
// frequency incremental materialisation.
//
// Same exact test scenario as both prior benchmarks (owlrl, Jena rules) for
// a genuine apples-to-apples comparison: load the four real static TTL
// files, add a minimal Device->FunctionalUnit->Sensor->Signal->
// SignalAnalysis->Metric ABox, reason, check hasSensorOperationState on the
// Sensor and hasOperationState propagated to the Device.
//
// Usage
// -----
//   cd CODE/evaluation_poc/core/jena_bench
//   javac -cp "$HOME/tools/apache-jena-6.2.0/lib/*:$HOME/tools/openllet/*" OpenlletReasoningBench.java
//   java  -cp "$HOME/tools/apache-jena-6.2.0/lib/*:$HOME/tools/openllet/*:." OpenlletReasoningBench

import openllet.jena.PelletReasonerFactory;
import org.apache.jena.rdf.model.*;
import org.apache.jena.reasoner.Reasoner;
import org.apache.jena.util.FileManager;
import org.apache.jena.vocabulary.RDF;

import java.util.StringJoiner;

public class OpenlletReasoningBench {

    static final String MDA = "https://w3id.org/mda/ontology#";
    static final String EX  = "https://example.org/test/";

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
            System.out.printf("  loaded %-40s (%d ms)%n", f.substring(f.lastIndexOf('/') + 1),
                    System.currentTimeMillis() - before);
        }
        System.out.println("static model size: " + staticModel.size() + " triples");

        for (int n : new int[]{0, 1, 2, 3, 4, 5, 6}) {
            Model abox = ModelFactory.createDefaultModel();
            for (int i = 0; i <= n; i++) {
                addChain(abox, "dev" + i, "fu" + i, "sensor" + i, "signal" + i, "analysis" + i, "metric" + i);
            }

            long t0 = System.currentTimeMillis();
            Reasoner reasoner = PelletReasonerFactory.theInstance().create();
            InfModel inf = ModelFactory.createInfModel(reasoner, staticModel.union(abox));

            Resource sensor0 = inf.getResource(EX + "sensor0");
            Resource dev0 = inf.getResource(EX + "dev0");
            Property hasSensorOpState = inf.getProperty(MDA + "hasSensorOperationState");
            Property hasOpState = inf.getProperty(MDA + "hasOperationState");

            StringJoiner sensorSpecific = new StringJoiner(", ");
            inf.listStatements(sensor0, hasSensorOpState, (RDFNode) null)
               .forEachRemaining(s -> sensorSpecific.add(s.getObject().toString()));
            StringJoiner devVals = new StringJoiner(", ");
            inf.listStatements(dev0, hasOpState, (RDFNode) null)
               .forEachRemaining(s -> devVals.add(s.getObject().toString()));

            long elapsed = System.currentTimeMillis() - t0;
            System.out.printf("n_background=%d  elapsed=%6dms  sensor0.hasSensorOpState=[%s]  dev0.hasOpState=[%s]%n",
                    n, elapsed, sensorSpecific, devVals);
            inf.close();
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
