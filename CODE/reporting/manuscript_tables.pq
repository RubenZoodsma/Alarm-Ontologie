// manuscript_tables.pq — Power Query for MANUSCRIPT/table_drafts/Table4_occurrence.xlsx
//
// Loads MANUSCRIPT/table_drafts/manuscript_tables.csv (written by
// CODE/reporting/manuscript_tables.py) into the two data sheets the
// formatted tables read from. The sheets "Table 3" and "Table 4" look values
// up by column (Unit in A, Key in B, Value in C), so the queries must land at
// A1 of the same sheets with these four columns in this order.
//
// One-time setup (Excel: Data > Get Data > From Other Sources > Blank Query >
// Advanced Editor), four queries, named exactly as below:
//
//   1. ManuscriptTablesPath   paste QUERY 1, put in the CSV's full path.
//                             Close & Load To... > Only Create Connection.
//   2. ManuscriptTables       paste QUERY 2. Only Create Connection.
//   3. tblTable3              paste QUERY 3. Before loading: on sheet Data_T3,
//                             clear the old table's contents (Clear All; do
//                             not delete columns or the sheet, the formulas
//                             refer to them). Then Close & Load To... > Table >
//                             Existing worksheet: =Data_T3!$A$1.
//   4. tblTable4              paste QUERY 4. Same, on sheet Data: =Data!$A$1.
//
// After that: run manuscript_tables.py, then Data > Refresh All. Nothing
// writes into the workbook any more; do not run scripts that open and save it
// (openpyxl drops Power Query definitions on save).
//
// Numbers are written with a '.' decimal separator; the Value column is typed
// with culture "en-US" so a Dutch-locale Excel reads 90.5 as 90,5 and not 905.


// ── QUERY 1: ManuscriptTablesPath ────────────────────────────────────────
"/full/path/to/Alarm-Ontologie/MANUSCRIPT/table_drafts/manuscript_tables.csv"
    meta [IsParameterQuery = true, Type = "Text", IsParameterQueryRequired = true]


// ── QUERY 2: ManuscriptTables ────────────────────────────────────────────
let
    Source   = Csv.Document(File.Contents(ManuscriptTablesPath),
                            [Delimiter = ";", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed    = Table.TransformColumnTypes(Promoted,
                   {{"Table", type text}, {"Unit", type text}, {"Key", type text},
                    {"Value", type number}, {"Description", type text}}, "en-US")
in
    Typed


// ── QUERY 3: tblTable3 ───────────────────────────────────────────────────
let
    Rows = Table.SelectRows(ManuscriptTables, each [Table] = "Table 3"),
    Out  = Table.SelectColumns(Rows, {"Unit", "Key", "Value", "Description"})
in
    Out


// ── QUERY 4: tblTable4 ───────────────────────────────────────────────────
let
    Rows = Table.SelectRows(ManuscriptTables, each [Table] = "Table 4"),
    Out  = Table.SelectColumns(Rows, {"Unit", "Key", "Value", "Description"})
in
    Out
