# MDA-POC-RDFox

Proof of concept: patient-centred alarm management by reasoning over a stream of medical device alarms with the Medical Device Alarm (MDA) framework. The engine is Oxigraph, an open-source SPARQL 1.1 store, run in-process.

## Architecture

The engine follows the RDF stream processing model of RSP-QL. Each module in `engine/` has one role:

| Module | RSP-QL role | Responsibility |
|---|---|---|
| `stream.py` | Stream | Replays an alarm corpus as a stream of separate `AlarmArrival` (no end) and `AlarmEnd` elements, in time order |
| `mint.py` | Stream items | Turns an alarm into RDF using the MDA framework (its alarm-type blueprint), minting patient-scoped entities |
| `windows.py` | Window operators | Keeps each alarm's knowledge in the store while it is valid: a transient graph (until its end arrives) and a persistent graph (until the ontology's post-alarm window after its end). A graph is valid exactly while it is in the store |
| `rules.py` | R2R: what holds | The registry of the nine rules, the loader for their `.rq` files, and cheap gates that skip checks that cannot match |
| `actions.py` | R2R: what happens | What a rule's result does: CAT1 stores a flag (withdrawable), CAT2 stores a silence (lifted when its last justification ends), clinical events and CAT3 are episodes with a start and an end |
| `processor.py` | Query processor | Per stream element: close due windows, handle ends (drop, lift silences, update episodes), handle arrivals (insert, evaluate rules, apply their actions). Runs the stream live (`LiveRun`, `run_live`): each element is minted, executed and read before the next; `run_parallel` replays patients in separate worker processes. A `Processor` handles one patient's stream or several interleaved; `run_live` replays patients one after another |
| `execution.py` | Execution | One in-memory Oxigraph store per run (`oxigraph_session.OxigraphSession`), fed in lockstep; reads the results back and times each rule invocation |
| `event_log.py` | R2S | Writes what fired, and which alarms it rests on, as two CSV logs, per element as it happens (`LiveLog`) |
| `paths.py` | — | Where the inputs live |

## Order at one instant

At time t: landmark windows closing at or before t, then all `AlarmEnd`s at t (transient graphs dropped, then CAT2 lifts, then one episode update), then each `AlarmArrival` at t. An alarm's end never precedes its own arrival: a zero-length alarm ends right after arriving. Nothing about an alarm's end is in the store before its `AlarmEnd` arrives.

## Live processing

The engine is a live stream processor. Each stream element — an arrival, or one patient's ends at one instant — is minted, executed in Oxigraph and read before the next element is taken (lockstep, one store per run). Nothing in Python reacts to a query result: every decision is an update inside the store. Each element's output is read as it arrives: both logs gain their rows as firings happen and events end, and the rule table is kept as running counts, so memory does not grow with the run. An engine error is reported with the patient and time of the element that caused it; a framework that fails to load stops the run.

Time is event time, with these conventions:

- **Instants.** Elements with the same timestamp form one instant, processed ends-before-arrivals (see above). Live, that means waiting until an instant is complete — one second at this data's resolution — before processing it.
- **Windows close lazily.** A window due at d closes when the next element arrives, with its exact time d, not on a clock tick. No query runs in between, so no decision differs; an outside observer of the store would see the expired graphs until then.
- **Patients one after another.** `run_live` replays each patient's stream in turn (each batch its own Oxigraph store; with `workers` > 1, patients run in parallel processes); a live unit would interleave all beds in time. The `Processor` supports interleaving, and `validation/clinical_events_cross_patient.py` runs it. `regression.py` also checks that no command can depend on the future, by prefix invariance: at every instant t of every fixture patient, the commands so far equal those of a run that knows only the stream up to t.

With `audit_logging` on (`SETTINGS`, off by default), everything sent to the engine is logged to `replay.oxigraph` in the run's scratch folder, with each imported alarm graph's triples under its `import` line as `#` comments — about 17 KB per alarm, so for debugging only. The alarm graph files are deleted once imported either way.

## Rules and actions

Rule logic lives only in `representation/`, never in Python:

- `representation/rules/<rule>.rq` — a rule's **condition**: a SPARQL graph pattern, with a header giving the natural-language rule, its clause-by-clause reading, the variables it expects bound and binds, and the fixtures that validate it.
- `representation/actions/<action>.rq` — what happens with a result: a complete SPARQL command with a `{{BINDINGS}}` slot (a `VALUES` clause with this moment's alarm, time or patient) and a `{{CONDITION}}` slot (a rule).
- No standing Datalog and no materialisation: rules read the framework's axioms where they are stated (CAT2a follows `inference.ttl`'s `mda:approximates` restrictions through `rdfs:subClassOf*`).

Files use standard `PREFIX` lines; `rules.py` checks they agree and the script declares them once (the session prepends them to each statement). `regression.py` fails if query text appears in the Python sources.

## Running

Both scripts are configured through a `SETTINGS` dict at the top of the file; there are no command-line arguments.

- **Regression test** (fabricated, known-outcome patients in `DATA/CAT_evaluation/events_data.csv`):
  ```
  cd engine && python3 regression.py
  ```
  Ends with `N/91 checks matched expected outcome` and writes both logs to `engine/_scratch/`.
- **Real-corpus run**: `python3 poc_main.py`. Choose the dataset, number of patients, seed, rules, batch size and number of parallel `workers` in `SETTINGS`. Logs go to `_scratch/`. Ends with a per-rule table: invocations, hits, hit rate and Oxigraph time per invocation (rounded to 1 ms).

Validation scripts in `validation/`:
- `log_rebuild_check.py`: every logged row comes out again when only the alarms the logs name for it are replayed through the engine; the criterion is the rule file itself (run `regression.py` or `poc_main.py` first).
- `clinical_events_cross_patient.py`: five patients interleaved in one stream and one store produce no cross-patient events.
- `compare_logs.py`: two runs log the same firings and events (alarms matched by ALARM_ID).

## Requirements

- `pyoxigraph` (open source; `pip install pyoxigraph`), the SPARQL 1.1 engine. RDFox was used until October 2026; the rules and actions are standard SPARQL 1.1.
- Python 3.9+ with `rdflib`; `pandas` for the real corpus.
