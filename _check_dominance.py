import pandas as pd, numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parent
df = pd.read_csv(ROOT / '20260311_dataSample.csv')
df['alarm_start'] = pd.to_datetime(df['alarm_start'], format='mixed', errors='raise')
df['alarm_eind']  = pd.to_datetime(df['alarm_eind'], format='mixed', errors='raise')
df['date'] = df['alarm_start'].dt.date
target_patient_id, target_date = df.groupby(['patientID','date']).size().idxmax()
df = df[(df['patientID']==target_patient_id) & (df['date']==target_date)].copy()
df = df[(df['alarm_eind']-df['alarm_start']) <= pd.Timedelta(minutes=30)].copy()

ev = pd.read_csv(ROOT / 'conditie_unique_values.csv')[['conditie','tech_evidence','clin_evidence']]
df = df.merge(ev, on='conditie', how='left')
df['tech_ev'] = df['tech_evidence'].fillna(3) / 10.0
df['clin_ev'] = df['clin_evidence'].fillna(3) / 10.0
total = df['tech_ev'] + df['clin_ev']
df['tech_ratio'] = df['tech_ev'] / total.replace(0, np.nan)

uniq = df[['conditie','tech_ev','clin_ev','tech_ratio']].drop_duplicates('conditie').sort_values('tech_ratio')
print(uniq.to_string())
print()
print('clin-dominated (<0.4):', (df['tech_ratio']<0.4).sum(), 'alarm rows')
print('neutral (0.4-0.6):',    ((df['tech_ratio']>=0.4)&(df['tech_ratio']<=0.6)).sum(), 'alarm rows')
print('tech-dominated (>0.6):', (df['tech_ratio']>0.6).sum(), 'alarm rows')
