# export_locked_dataset.R — bridges the real SAS-ICU alarm corpus into the
# semicolon-CSV schema op_knowledge.py's load_events() expects
# (patientID;label;device_id;start;end), so the existing Python pipeline
# needs no changes to run against it.
#
# Source is DATA_LOCKED.rData's `data` object: 13,820,542 rows x 21 cols,
# matching the manuscript's ~14 million alarms / ~3300 patients. Static
# location, not passed on the command line — this file lives on a specific
# mounted volume, not in the repo, and is never meant to move with it.
#
# Column mapping, and why:
#   patientID  -> patientID                    (as-is)
#   label      -> trimws(conditie)             ("DEVICE - message", the exact
#                                                format kg_generated.ttl's
#                                                type_index keys on; trimws
#                                                because some source values
#                                                carry a trailing space, e.g.
#                                                "PHILIPSMONITOR - Asystolie ")
#   device_id  -> paste(device_type, bed_naam) (see below)
#   start      -> alarm_start
#   end        -> alarm_eind
#
# device_id is NOT device_type + patientID. events_seed.py's own docstring
# states the framework's device-identity principle explicitly: "ONE DEVICE
# PER TYPE, NOT PER PATIENT... a device is not possessed by the patient it
# happens to be monitoring... a real physical unit moves between patients."
# device_naam/device_type alone are device MODELS (10 distinct values across
# 3299 patients) — too coarse to be an instance id. bed_naam (187 distinct
# beds) is the right anchor instead: ICU monitors are bedside-fixed
# infrastructure, so "this physical monitor, wherever it lives" is correctly
# modelled as (device_type, bed_naam), reused across whichever patients pass
# through that bed over time — not re-minted per patient.
#
# Usage
# -----
#   Rscript export_locked_dataset.R                  # all patients (full ~14M-row corpus)
#   Rscript export_locked_dataset.R 1                 # first patient only (sorted patientID), for feasibility timing
#   Rscript export_locked_dataset.R 10                 # first 10 patients
#
# Patient selection is deterministic (sorted patientID, take first N), not
# random, so re-running the same N always reproduces the same patient(s).

LOCKED_DATASET_PATH <- "/Volumes/KIND/rzoodsm2_MIGRATED/Home/PhD/2023/1 - SAS_ICU/B - UMC review/Scripts/final_vis/DATA_LOCKED.rData"

# Resolved from Rscript's own --file= argument, not the working directory —
# this script always lives at CODE/evaluation_poc/data/ within the repo, so
# the output path is derived from where IT is, regardless of where it's run
# from.
.script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
SCRIPT_PATH <- normalizePath(sub("^--file=", "", .script_arg))
REPO_ROOT <- dirname(dirname(dirname(dirname(SCRIPT_PATH))))  # data -> evaluation_poc -> CODE -> repo root
OUT_PATH <- file.path(REPO_ROOT, "DATA", "LOCKED_CORPUS", "events_data.csv")

args <- commandArgs(trailingOnly = TRUE)
n_patients <- if (length(args) >= 1) as.integer(args[1]) else NA_integer_

cat(sprintf("[load] %s\n", LOCKED_DATASET_PATH))
load(LOCKED_DATASET_PATH)  # brings in `data`, among many other objects

stopifnot(exists("data"), is.data.frame(data))
cat(sprintf("[loaded] %d rows x %d cols\n", nrow(data), ncol(data)))

if (!is.na(n_patients)) {
  selected_patients <- sort(unique(data$patientID))[seq_len(min(n_patients, length(unique(data$patientID))))]
  data <- data[data$patientID %in% selected_patients, ]
  cat(sprintf("[filter] restricted to %d patient(s): %s -> %d rows\n",
              length(selected_patients), paste(selected_patients, collapse = ", "), nrow(data)))
}

events <- data.frame(
  patientID = as.character(data$patientID),
  label     = trimws(data$conditie),
  device_id = paste(data$device_type, data$bed_naam, sep = "_"),
  start     = format(data$alarm_start, "%Y-%m-%dT%H:%M:%S"),
  end       = format(data$alarm_eind, "%Y-%m-%dT%H:%M:%S"),
  stringsAsFactors = FALSE
)

# Drop rows with no end time (open/unresolved alarms) — load_events'
# parse_ts has no notion of an ongoing alarm, and every existing rule/CQ
# assumes a closed [start, end] interval.
before <- nrow(events)
events <- events[!is.na(data$alarm_eind), ]
if (before != nrow(events)) {
  cat(sprintf("[filter] dropped %d row(s) with no end time -> %d rows\n", before - nrow(events), nrow(events)))
}

dir.create(dirname(OUT_PATH), recursive = TRUE, showWarnings = FALSE)
write.table(events, OUT_PATH, sep = ";", row.names = FALSE, quote = FALSE, na = "")
cat(sprintf("[write] %d rows -> %s\n", nrow(events), OUT_PATH))
