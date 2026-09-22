# MDA-POC-RDFox

Proof of concept: patient-centred alarm management by reasoning over a stream of medical device alarms with the Medical Device Alarm (MDA) framework. The reasoning engine is RDFox.

## Architecture

The engine follows the RDF stream processing model of RSP-QL. Each module in `engine/` has one role:

| Module | RSP-QL role | Responsibility |
|---|---|---|
| `stream.py` | Stream | Replays an alarm corpus as a stream of separate `AlarmArrival` (no end) and `AlarmEnd` elements, in time order |
| `mint.py` | Stream items | Turns an alarm into RDF using the MDA framework (its alarm-type blueprint), minting patient-scoped entities |
| `windows.py` | Window operators | Keeps each alarm's knowledge in the store while it is valid: a transient graph (until its end arrives) and a persistent graph (until the ontology's post-alarm window after its end). A graph is valid exactly while it is in the store |
| `rules.py` | R2R: what holds | The registry of the nine rules, the loader for their `.rq` files, and cheap gates that skip checks that cannot match |
| `actions.py` | R2R: what happens | What a rule's result does: CAT1 stores a flag (withdrawable), CAT2 stores a silence (lifted when its last justification ends), clinical events and CAT3 are episodes with a start and an end |
| `processor.py` | Query processor | Per stream element: close due windows, handle ends (drop, lift silences, update episodes), handle arrivals (insert, evaluate rules, apply their actions). Works on one patient's stream or several interleaved |
| `execution.py` | Execution | Runs the generated script in RDFox and reads the results back |
| `event_log.py` | R2S | Writes what fired, and which alarms it rests on, as two CSV logs |
| `paths.py` | — | Where the inputs live |

## Order at one instant

At time t: landmark windows closing at or before t, then all `AlarmEnd`s at t (transient graphs dropped, then CAT2 lifts, then one episode update), then each `AlarmArrival` at t. An alarm's end never precedes its own arrival: a zero-length alarm ends right after arriving. Nothing about an alarm's end is in the store before its `AlarmEnd` arrives; `regression.py` checks this structurally.

## Rules and actions

Rule logic lives only in `representation/`, never in Python:

- `representation/rules/<rule>.rq` — a rule's **condition**: a SPARQL graph pattern, with a header giving the natural-language rule, its clause-by-clause reading, the variables it expects bound and binds, and the fixtures that validate it.
- `representation/actions/<action>.rq` — what happens with a result: a complete SPARQL command with a `{{BINDINGS}}` slot (a `VALUES` clause with this moment's alarm, time or patient) and a `{{CONDITION}}` slot (a rule).
- No standing Datalog and no materialisation: rules read the framework's axioms where they are stated (CAT2a follows `inference.ttl`'s `mda:approximates` restrictions through `rdfs:subClassOf*`).

Files use standard `PREFIX` lines; `rules.py` checks they agree and the script declares them once (the RDFox shell rejects a `PREFIX` clause inside a command). `regression.py` fails if query text appears in the Python sources.

## Running

Both scripts are configured through a `SETTINGS` dict at the top of the file; there are no command-line arguments.

- **Regression test** (fabricated, known-outcome patients in `DATA/CAT_evaluation/events_data.csv`):
  ```
  cd engine && python3 regression.py
  ```
  Ends with `N/91 checks matched expected outcome` and writes both logs to `engine/_scratch/`.
- **Real-corpus run**: `python3 poc_main.py`. Choose the dataset, number of patients, seed, rules and batch size in `SETTINGS`. Logs go to `_scratch/`.

Validation scripts in `validation/`:
- `log_rebuild_check.py`: every logged row comes out again when only the alarms the logs name for it are replayed through the engine; the criterion is the rule file itself (run `regression.py` or `poc_main.py` first).
- `clinical_events_cross_patient.py`: five patients interleaved in one stream and one store produce no cross-patient events.
- `compare_logs.py`: two runs log the same firings and events (alarms matched by ALARM_ID).

## Requirements

- RDFox 7.6b at `~/Downloads/RDFox-macOS-arm64-7.6b/RDFox`, with the licence at `<repo root>/RDFox.lic` (see `engine/paths.py`).
- Python 3.9+ with `rdflib`; `pandas` for the real corpus.
