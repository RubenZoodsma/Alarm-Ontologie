import re
path = 'ontology_instances/alarm_instances/1_ACD_MonitorAlarms.ttl'
lines = open(path).readlines()
for i, l in enumerate(lines, 1):
    core = re.sub(r'\s*##.*', '', l.rstrip())
    if re.search(r',\s*[;.]$', core):
        print(f'{i:4d}: {l.rstrip()}')
