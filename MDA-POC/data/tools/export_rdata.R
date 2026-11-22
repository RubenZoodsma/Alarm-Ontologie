# export_rdata.R — convert the full corpus (DATA/POC_EVENTS/DATA_LOCKED.rData)
# to the POC's CSV shape: patientID;label;device_id;start;end;alarm_id;
# device_name;priority;department,
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
#   device_naam            -> device_name: the gateway's device name (e.g. Caleo);
#                             with label and priority it identifies the alarm's
#                             blueprint (device model + label + priority,
#                             FRAMEWORK/PLAN_blueprint_identity.md). Checked
#                             against DATA/ANNOTATION/device_models.csv.
#   priority column        -> priority (Hoog / Medium / Laag / Unknown, as in
#                             the corpus); the first of PRIORITY_COLUMNS present
#   department column      -> department: NICU or PICU (Table 4 is per unit);
#                             the first of DEPARTMENT_COLUMNS present, its
#                             values mapped by pattern (stops on any it
#                             cannot map, listing them)
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

# Device name and priority. Unverified against the source until the first run
# with the locked data (plan step A5): stop, listing the source's columns, if
# either is missing, and stop on device names the mapping does not know.
DEVICE_COLUMN    <- "device_naam"
PRIORITY_COLUMNS <- c("prioriteit", "priority", "alarm_prioriteit", "Alarm_priority")
if (!DEVICE_COLUMN %in% names(d)) {
  stop("No column '", DEVICE_COLUMN, "'. Columns: ", paste(names(d), collapse = ", "))
}
priority_column <- intersect(PRIORITY_COLUMNS, names(d))[1]
if (is.na(priority_column)) {
  stop("No priority column (tried ", paste(PRIORITY_COLUMNS, collapse = ", "),
       "). Columns: ", paste(names(d), collapse = ", "))
}
models_path <- file.path(dirname(normalizePath(output_path, mustWork = FALSE)),
                         "..", "ANNOTATION", "device_models.csv")
if (!file.exists(models_path)) {
  models_path <- "DATA/ANNOTATION/device_models.csv"
}
models <- read.csv2(models_path, stringsAsFactors = FALSE)
names_in_source <- unique(trimws(as.character(d[[DEVICE_COLUMN]])))
unknown <- setdiff(names_in_source, models$gateway_name)
if (length(unknown) > 0) {
  stop("Device names missing from device_models.csv: ", paste(unknown, collapse = ", "))
}
DEPARTMENT_COLUMNS <- c("afdeling", "department", "Alarm_department", "unit", "ward", "afdeling_naam")
department_column <- intersect(DEPARTMENT_COLUMNS, names(d))[1]
if (is.na(department_column)) {
  stop("No department column (tried ", paste(DEPARTMENT_COLUMNS, collapse = ", "),
       "). Columns: ", paste(names(d), collapse = ", "))
}
raw_department <- trimws(as.character(d[[department_column]]))
unit_of <- function(x) {
  x <- tolower(x)
  ifelse(grepl("neonat|nicu", x), "NICU",
         ifelse(grepl("paed|pediat|kinder|picu|(^|[^a-z])kic([^a-z]|$)", x), "PICU", NA))
}
department <- unit_of(raw_department)
if (any(is.na(department))) {
  stop("Department values not mapped to NICU/PICU: ",
       paste(unique(raw_department[is.na(department)]), collapse = ", "))
}
cat(sprintf("departments (%s): %s\n", department_column,
            paste(sprintf("%s -> %s", unique(raw_department), unit_of(unique(raw_department))), collapse = "; ")))
priorities <- sort(unique(trimws(as.character(d[[priority_column]]))))
cat(sprintf("device names: %s\npriorities (%s): %s\n",
            paste(sort(names_in_source), collapse = ", "), priority_column,
            paste(priorities, collapse = ", ")))

events <- data.frame(
  patientID = as.character(d$patientID),
  label     = trimws(as.character(d$conditie)),
  device_id = paste(d$bed_naam, d$device_naam, sep = "_"),
  start     = format(d$alarm_start, "%Y-%m-%dT%H:%M:%S"),
  end       = format(d$alarm_eind, "%Y-%m-%dT%H:%M:%S"),
  alarm_id  = seq_len(nrow(d)),
  device_name = trimws(as.character(d[[DEVICE_COLUMN]])),
  priority  = trimws(as.character(d[[priority_column]])),
  department = department,
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
