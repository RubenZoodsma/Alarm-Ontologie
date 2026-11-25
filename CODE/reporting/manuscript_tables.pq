// manuscript_tables.pq — Power Query for MANUSCRIPT/table_drafts/Table4_occurrence.xlsx
//
// Two queries load MANUSCRIPT/table_drafts/manuscript_tables.csv (written by
// CODE/reporting/manuscript_tables.py) into the sheets the formatted tables
// read from: tblTable3 into Data_T3, tblTable4 into Data. The sheets
// "Table 3" and "Table 4" look values up by column (Unit in A, Key in B,
// Value in C), so each query must land at A1 of its sheet.
//
// ONE-TIME SETUP, per query (first tblTable3 on Data_T3, then tblTable4 on Data):
//   1. On the sheet, select columns A:D and press Delete (clear the contents;
//      do not delete the columns or the sheet: the formulas refer to them).
//   2. Data > Get Data (Power Query) > Blank query. The Power Query editor opens.
//   3. Home > Advanced Editor: replace all text with the query below, with
//      the CSV's full path on the CsvPath line. OK.
//   4. Name the query (Query Settings, right) tblTable3 / tblTable4.
//   5. Home > Close & Load To... > Table > Existing worksheet: =Data_T3!$A$1
//      (or =Data!$A$1). OK.
//
// AFTER THAT: run manuscript_tables.py, then in Excel Data > Refresh All.
// Nothing writes into the workbook any more; do not run scripts that open and
// save it (openpyxl drops Power Query definitions on save).
//
// The Value column is typed with culture "en-US": the CSV uses a '.' decimal
// separator, and a Dutch-locale Excel would otherwise read 90.5 as 905.


// ── tblTable3 (load to =Data_T3!$A$1) ────────────────────────────────────
let
    CsvPath  = "/full/path/to/Alarm-Ontologie/MANUSCRIPT/table_drafts/manuscript_tables.csv",
    Source   = Csv.Document(File.Contents(CsvPath),
                            [Delimiter = ";", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Rows     = Table.SelectRows(Promoted, each [Table] = "Table 3"),
    Columns  = Table.SelectColumns(Rows, {"Unit", "Key", "Value", "Description"}),
    Typed    = Table.TransformColumnTypes(Columns,
                   {{"Unit", type text}, {"Key", type text},
                    {"Value", type number}, {"Description", type text}}, "en-US")
in
    Typed


// ── tblTable4 (load to =Data!$A$1) ───────────────────────────────────────
let
    CsvPath  = "/full/path/to/Alarm-Ontologie/MANUSCRIPT/table_drafts/manuscript_tables.csv",
    Source   = Csv.Document(File.Contents(CsvPath),
                            [Delimiter = ";", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Rows     = Table.SelectRows(Promoted, each [Table] = "Table 4"),
    Columns  = Table.SelectColumns(Rows, {"Unit", "Key", "Value", "Description"}),
    Typed    = Table.TransformColumnTypes(Columns,
                   {{"Unit", type text}, {"Key", type text},
                    {"Value", type number}, {"Description", type text}}, "en-US")
in
    Typed
