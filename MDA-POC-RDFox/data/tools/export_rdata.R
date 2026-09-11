# export_rdata.R — convert the full 14M-alarm corpus (DATA/POC_EVENTS/
# DATA_LOCKED.rData) into this project's own patientID;label;device_id;
# start;end CSV shape, optionally sampling to N patients along the way.
#
# WHY THIS IS AN R SCRIPT, NOT PYTHON: the .rData file is a full saved R
# WORKSPACE (60+ objects — functions, other analysis data.frames, etc.),
# not a single clean data.frame, and its strings are not valid UTF-8
# (confirmed directly: reading it with Python's pyreadr — the standard
# pure-Python .rData reader — fails with `UnicodeDecodeError` on real
# label text; the pure-Python `rdata` package fails even earlier, on the
# workspace's embedded compiled-function bytecode, before reaching any
# data at all). R has no such problem reading its own native format and
# encoding, so conversion happens once here rather than fighting either
# Python library's limitations.
#
# The 13.8M-row `data` object's own columns don't match this project's
# CSV shape directly:
#   patientID  -> patient (as character; kept as the raw hospital ID)
#   conditie   -> label. TRIMMED: confirmed directly that ~4.18M of
#                 13.8M rows (~30%) carry stray leading/trailing
#                 whitespace (e.g. "PHILIPSMONITOR - Asystolie " with a
#                 trailing space) that would otherwise silently fail
#                 exact-string lookup against kg_generated.ttl's
#                 catalogue (mint.py's kb.type_index) for no reason
#                 related to whether the label is actually known.
#   bed_naam + device_naam -> device_id. Neither column alone is a safe
#                 device-instance identifier: bed_naam (only 49 distinct
#                 beds) is reused across many different physical devices
#                 AT the same bed (a monitor and a ventilator share one
#                 bed) — using it alone would make mint.py's ground_chain
#                 assert two different device TYPES onto one Device IRI.
#                 device_naam alone (10 distinct) is reused across many
#                 different beds/patients over time, which is fine on its
#                 own (see mint.py's own MINTING-section header on
#                 patient-scoped IRIs) but doesn't distinguish two
#                 different physical units of the same model at two
#                 different beds. The pair does.
#   alarm_start/alarm_eind -> start/end. `format()` below deliberately
#                 does NOT pass an explicit tz= override: confirmed
#                 directly (attr(d$alarm_start, "tzone") == "" and
#                 Sys.timezone() == "Europe/Amsterdam" on the machine
#                 this was authored on) that the timestamps already carry
#                 no explicit zone and this session's own local zone
#                 already matches the hospital's — printing in session-
#                 local time is correct here, not an oversight.
#
# 505 distinct `conditie` values exist in the real data; kg_generated.
# ttl's catalogue currently knows 89 AlarmType labels. Rows whose label
# isn't in the catalogue are NOT dropped here — mint.py's own functions
# already handle an unresolved label by minting nothing for it
# (background_for_key/condition_for_event/alarm_message all return empty
# graphs), and poc_entry.py already warns about exactly this. Filtering
# here would hide real corpus coverage gaps instead of surfacing them.
#
# Usage: Rscript export_rdata.R <input.rData> <output.csv> <n_patients: 'all' or an integer> <seed>

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 4) {
  stop("Usage: Rscript export_rdata.R <input.rData> <output.csv> <n_patients: 'all' or integer> <seed>")
}
input_path  <- args[1]
output_path <- args[2]
n_patients  <- args[3]
seed        <- as.integer(args[4])

e <- new.env()
load(input_path, envir = e)
d <- get("data", envir = e)

events <- data.frame(
  patientID = as.character(d$patientID),
  label     = trimws(as.character(d$conditie)),
  device_id = paste(d$bed_naam, d$device_naam, sep = "_"),
  start     = format(d$alarm_start, "%Y-%m-%dT%H:%M:%S"),
  end       = format(d$alarm_eind, "%Y-%m-%dT%H:%M:%S"),
  stringsAsFactors = FALSE
)

if (n_patients != "all") {
  n <- as.integer(n_patients)
  set.seed(seed)
  all_patients <- unique(events$patientID)
  chosen <- sample(all_patients, min(n, length(all_patients)))
  events <- events[events$patientID %in% chosen, ]
}

write.table(events, output_path, sep = ";", row.names = FALSE, quote = FALSE)
cat(sprintf("Exported %d rows, %d patient(s) to %s\n",
            nrow(events), length(unique(events$patientID)), output_path))
