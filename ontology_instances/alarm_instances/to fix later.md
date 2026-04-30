## Stuff to fix in the ontology:

- Cond_ArterialBloodPressureProperty_ObservationState_Monitored --> Should be observation state of the process, not the range of properties




PM: Check ECG ST-afwijkingen en interpretatie zowel op epistemisch als ontologisch domein
PM: Link relations of groups (ecg-leads, ABPMetricGroup) transitively to their children
PM: differentiate between cardiacCycle vs cardiacContraction?
PM: Check technisch verschil operationalState vs functionalState -> onderscheid 'aanwezig, doet het niet' vs 'niet aanwezig'
PM: Check hierarchie settings e.d. van een device

PM: check prefix :: licentie / aanmelding via website Margherita
PM: naming conventions check (has-__ vs actief werkwoord). 


Eruit halen:
- SensorInterface + MeasurementProcess ? 
    _eigenlijk nergens gebruikt - behalve voor backward compatibility met SDC ; maar inhoudelijk zit er geen voordeel aan._