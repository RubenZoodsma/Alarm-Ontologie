# Medical Device Alarm (MDA) framework

Code, data and results accompanying the manuscript on the Medical Device Alarm (MDA) framework: an ontology, vocabulary and knowledge base that represent what an ICU medical device alarm conveys, technically and clinically, and a proof of concept (MDA-POC) that reasons over a stream of alarms with it.

The ontology is published at <https://w3id.org/mda/ontology>; the documentation site is built from `docs/`.

## Repository layout

| Folder | Contents |
|---|---|
| `FRAMEWORK/` | The framework: `ONTOLOGY/ontology.ttl` and its SOSA/SSN alignment `ONTOLOGY/alignment_sosa.ttl`; `KNOWLEDGE_BASE/` (`inference.ttl` bridge axioms, `clinicalEvents.ttl`, `kg_generated.ttl` alarm-type blueprints); `VOCABULARY/` (`vocab_generated.ttl`, `priority_rank.ttl`, hand-written `seed/`) |
| `DATA/` | Inputs: `CORPUS/Annotation_corpus_99.csv` (the selected alarm corpus), `ANNOTATION/consensus/p99_annotated.csv` (the expert annotation), `ANNOTATION/device_models.csv`, `POC_EVENTS/` (local CSV cache of the locked real-world dataset, not included) |
| `CODE/data_prep/` | Corpus selection (Pareto cut-off), the corpus recount, and the import of returned annotation workbooks |
| `CODE/framework_build/` | `build_framework.py` (annotation → knowledge base and vocabulary), `entity_seed.py` (scaffold for the POC) |
| `CODE/evaluation_cq/` | Competency questions CQ1–7 (`cq_QA.py`) and their report page |
| `CODE/evaluation_qa/` | Published site pages: per-blueprint graphs, vocabulary browser |
| `CODE/shared/` | Helpers shared by the scripts above |
| `MDA-POC/` | The proof of concept: a stream reasoner on Oxigraph. Self-contained; see [`MDA-POC/README.md`](MDA-POC/README.md) |
| `EVALUATION/CQs/post_enrichment/` | The SPARQL queries of CQ1–7 |
| `MANUSCRIPT/scripts/` | Scripts that produce manuscript artefacts: Tables 3/4, corpus table, filter comparison, Figure 1, Pareto figures, CQ appendix, duplicate-blueprint check. Their outputs (`MANUSCRIPT/*_drafts/`) are not in the repository |
| `docs/` | GitHub Pages site, rebuilt by `.github/workflows/rebuild-docs.yml` |

## Pipeline

1. **Corpus selection.** `CODE/data_prep/build_corpus_cutoffs.py`: per department × device × priority, the alarm labels that together make up 99% of the alarm volume → `DATA/CORPUS/Annotation_corpus_99.csv`.
2. **Annotation.** Each distinct alarm (device model + label + priority) is annotated with what it conveys → `DATA/ANNOTATION/consensus/p99_annotated.csv` (returned workbooks are converted back with `CODE/data_prep/import_annotation_xlsx.py`; the export script is retired to `old/`).
3. **Framework build.** `python3 CODE/framework_build/build_framework.py` → `FRAMEWORK/KNOWLEDGE_BASE/kg_generated.ttl` and `FRAMEWORK/VOCABULARY/vocab_generated.ttl`. Then `python3 MDA-POC/data/tools/entity_seed.py` → `MDA-POC/data/entities.ttl` (the POC's entity scaffold).
4. **Competency questions CQ1–7.** `cd CODE/evaluation_cq && python3 cq_QA.py` → `EVALUATION/CQs/cq_report.md`.
5. **MDA-POC.** The POC reads the framework in place from `FRAMEWORK/`, its only copy. Run `cd MDA-POC/engine && python3 regression.py` (it fails when the scaffold from step 3 is stale). The reasoning competency questions CQ8–10 are answered by the POC's rules; the mapping is in [`MDA-POC/README.md`](MDA-POC/README.md#competency-questions).
6. **Manuscript tables.** `python3 MANUSCRIPT/scripts/manuscript_tables.py` computes every number of Table 3 (framework statistics) and Table 4 (from a real-data POC run) into one CSV, `MANUSCRIPT/table_drafts/manuscript_tables.csv`; the workbook `Table4_occurrence.xlsx` loads it with Power Query (`MANUSCRIPT/scripts/manuscript_tables.pq`). Figure scripts are next to it.

## Installation

```
pip install -r requirements.txt
```

Python 3.9 or later. Java is needed only for the HermiT consistency check in `manuscript_tables.py` (skip it with `--no-hermit`); see `requirements.txt` for the other non-Python tools.

## Reproducibility and data availability

| Result | Reproducible from this repository |
|---|---|
| Ontology, vocabulary, knowledge base (step 3) | Yes |
| CQ1–7 (step 4) | Yes |
| CQ8–10: MDA-POC verification cases (step 5) | Yes |
| MDA-POC rule coverage (`MANUSCRIPT/scripts/rule_coverage.py`) | Yes |
| Framework statistics table | No: needs the full alarm log (`DATA/CORPUS/Total_corpus.csv`), not included |
| Table 4 and the delay-filter comparison | No: needs the patient-level alarm data; the scripts are included, the data are not |

The alarm data come from ICU patients and are not shared. The repository contains only alarm labels, expert annotations and per-label aggregate alarm counts.

## License

The code is released under the MIT License (`LICENSE`). The ontology, vocabulary and knowledge base (`FRAMEWORK/`), including the SOSA/SSN alignment, are released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), as declared in each ontology header.
