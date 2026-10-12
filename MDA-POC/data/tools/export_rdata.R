# export_rdata.R — convert the full corpus (DATA/POC_EVENTS/DATA_LOCKED.rData)
# to the POC's CSV shape: patientID;label;device_id;start;end;alarm_id,
# optionally sampling N patients.
#
# R, not Python: the file is a whole R workspace with non-UTF-8 strings,
# which the Python readers (pyreadr, rdata) fail on.
#
# Column mapping from the `data` object:
#   patientID              -> patientID
#   conditie               -> label, TRIMMED (~30% carry stray whitespace
#                             that would miss the catalogue lookup)
#   bed_naam + device_naam -> device_id; neither alone identifies a device
#                             (a bed holds several devices; a device name
#                             recurs across beds)
#   row number             -> alarm_id: the source has no identifier, and
#                             patient + device + start collide for 45.5% of
#                             rows. Assigned before sampling.
#   alarm_start/alarm_eind -> start/end, in local time (the timestamps
#                             carry no zone; Europe/Amsterdam)
#
# Unknown labels are kept: poc_main.py reports them as coverage gaps.
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
  alarm_id  = seq_len(nrow(d)),
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
