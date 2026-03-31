Overview of the 8 CQs as operational specifications
## Technical CQs

CQ	What we wish to achieve	Expected input	Expected output
T1. Technical equivalence	
_Identify alarms that are semantically identical at the technical level, regardless of vendor wording._

    This tests whether technical alarm meaning can be normalized into shared technical assertion patterns.	A technical concept–attribute–value pattern, or alternatively the full set of alarms to be grouped by such patterns.	A set of alarms that instantiate the same technical assertion pattern.

T2. Sensor alarm propagation	
_Determine whether a sensor-level technical problem propagates to multiple downstream metrics._ 

    This tests whether the ontology captures technical dependency structure in the measurement chain.	A sensor-level alarm, or a sensor-level technical assertion indicating failure / disconnection / invalidity.	The set of affected or invalidated metrics that depend on that sensor.

## Clinical CQs
CQ	What we wish to achieve	Expected input	Expected output
C1. Clinical equivalence	
_Identify alarms that are semantically identical at the clinical level, regardless of device or measurement origin._
    This tests whether alarms can be normalized into shared clinical meaning.	A clinical concept–attribute–value pattern, or alternatively the full set of alarms to be grouped by such patterns.	A set of alarms that instantiate the same clinical assertion pattern.

C2. Clinical process coupling	
_Determine which distinct clinical assertions can be grouped under a common physiological process._
    This tests whether the ontology supports abstraction from property-level abnormalities to process-level interpretation.	A physiological process. Optionally: a set of alarms or clinical assertions to be evaluated against that process.	The set of linked clinical assertions and/or alarms associated with that process.

## Mixed CQs
CQ	What we wish to achieve	Expected input	Expected output
M1. Technical-to-clinical mapping	
_Determine how a technical assertion is translated into clinical meaning._ 
    This tests whether the ontology explicitly captures the latent knowledge bridge from measurement space to physiology space.	A technical concept–attribute–value pattern, or a technical assertion derived from an alarm.	The linked clinical assertion or set of clinical assertions corresponding to that technical assertion.

M2. Alternative interpretation paths	
_Determine whether one technical assertion can lead to multiple distinct clinical interpretations, and keep those interpretations explicit rather than collapsing them prematurely._ 
    This tests whether the ontology can represent ambiguity or competing hypotheses.	A technical assertion.	A set of distinct interpretation paths, each ending in a different clinical assertion or interpretation context.

M3. Cross-domain convergence	
_Determine which different technical assertions converge onto the same clinical meaning._ 
    This tests whether heterogeneous technical origins can be reduced into a common clinical feature.	A clinical concept–attribute–value pattern.	The set of technical assertions that map to that clinical assertion.

M4. End-to-end feature derivation	
_Derive the reduced ontology-based representation of an alarm for downstream reasoning or modeling._ 
    This tests whether the ontology can be used as a semantic compression mechanism over raw alarms.	A single alarm.	A compact feature set derived from that alarm, potentially including technical features, clinical features, and higher-level process features.