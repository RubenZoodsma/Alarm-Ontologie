## File to validate the ontology excel sheet for correctness and completeness

## Load in the file
import pandas as pd

# Read the excel file, specifically the 3rd sheet
df = pd.read_excel("AlarmOntoClassification.xlsx", sheet_name='Analyse_Subset')
# Display the first few rows to understand its structure
#print(df.head())

## Print the unique values in each row to understand the data better
for column in df.columns:
    unique_values = df[column].unique()
    #print(f"Column: {column}, Unique Values: {unique_values}")

## Print unique values in each ROW
for index, row in df.iterrows():
    unique_values = row.unique()
    ## remove Nan
    unique_values = [val for val in unique_values if pd.notna(val)]
  #  if unique_values:
  #      print(f"Row {index}, type {unique_values[0]} , Unique Values: {unique_values}")

    ## Isolate the unique values where '.hasName' is in unique_values[0]
    has_name_values = [val for val in unique_values if '.hasName' in str(val)]
    if has_name_values:
        print(f"Row {index}, name: {unique_values[0]}, Unique Values: {unique_values[1:-1]}")
        ## Save to a csv file
        with open("validated_ontology_names.csv", "a") as f:
            f.write(f"{unique_values[0]},{','.join(map(str, unique_values[1:-1]))}\n")
