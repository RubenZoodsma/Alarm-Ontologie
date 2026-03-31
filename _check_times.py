import pandas as pd
df = pd.read_csv("burden_output.csv", parse_dates=["time"])
pid = df["patientID"].iloc[0]
sub = df[df["patientID"] == pid].sort_values("time")
print("min time:", sub["time"].min())
print("max time:", sub["time"].max())
print("total rows:", len(sub))
day_start = sub["time"].min().floor("D")
end_6h = day_start + pd.Timedelta(hours=6)
print("day_start:", day_start)
print("end_6h:", end_6h)
clipped = sub[(sub["time"] >= day_start) & (sub["time"] <= end_6h)]
print("rows in 6h window:", len(clipped))
print("first 3 times:", sub["time"].head(3).tolist())
