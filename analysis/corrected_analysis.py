#!/usr/bin/env python3
"""
Corrected Base Concepts Analysis for Alarm Ontology

This version properly analyzes:
- Actual properties used by instances (not schema definitions)
- Actual instances (not pattern matches)
- Proper alarm coverage calculation

Author: Generated for Alarm Ontology Research
Date: December 2025
"""

import rdflib
import json
from pathlib import Path
from collections import defaultdict

class CorrectedBaseConceptAnalyzer:
    """Corrected analyzer for base concepts in the Alarm Ontology."""
    
    def __init__(self, base_path):
        self.base_path = Path(base_path)
        self.full_graph = rdflib.Graph()
        
        # Base concepts to analyze
        self.base_concepts = [
            'Patient', 'PhysiologicalSystem', 'Organ', 'PhysiologicalProcess', 
            'PhysiologicalProperty', 'PhysicalDevice', 'FunctionalUnit', 
            'DeviceChannel', 'Metric', 'MeasurementProcess', 'Signal', 
            'Sensor', 'SensorInterface'
        ]
        
        # Namespace definitions
        self.namespaces = {
            'common': rdflib.Namespace('https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/common#'),
            'device': rdflib.Namespace('https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/device#'),
            'clinical': rdflib.Namespace('https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/clinical#'),
            'instances': rdflib.Namespace('https://github.com/RubenZoodsma/Alarm-Ontologie/ontology/instances#'),
            'owl': rdflib.OWL,
            'rdfs': rdflib.RDFS,
            'rdf': rdflib.RDF
        }
        
        # Bind namespaces
        for prefix, namespace in self.namespaces.items():
            self.full_graph.bind(prefix, namespace)
    
    def load_ontology_files(self):
        """Load all ontology files into the graph."""
        print("Loading ontology files...")
        
        # Load core ontology files
        core_files = [
            'ontology_core/alarmOnto_common.ttl',
            'ontology_core/alarmOnto_clinical.ttl', 
            'ontology_core/alarmOnto_device.ttl'
        ]
        
        for file_path in core_files:
            full_path = self.base_path / file_path
            if full_path.exists():
                try:
                    self.full_graph.parse(str(full_path))
                    print(f"✓ Loaded {file_path}")
                except Exception as e:
                    print(f"✗ Error loading {file_path}: {e}")
        
        # Load instance files
        instance_dirs = ['clinical_instances', 'device_instances', 'alarm_instances']
        for dir_name in instance_dirs:
            instance_dir = self.base_path / 'ontology_instances' / dir_name
            if instance_dir.exists():
                for ttl_file in instance_dir.glob('*.ttl'):
                    try:
                        self.full_graph.parse(str(ttl_file))
                        print(f"✓ Loaded {ttl_file.name}")
                    except Exception as e:
                        print(f"✗ Error loading {ttl_file}: {e}")
    
    def get_concept_class_uri(self, concept_name):
        """Get the class URI for a concept."""
        # Try different namespaces - both new GitHub URIs and old example.org URIs
        possible_uris = [
            self.namespaces['device'][concept_name],
            self.namespaces['clinical'][concept_name],
            self.namespaces['common'][concept_name],
            # Also try old namespace patterns
            rdflib.URIRef(f"http://example.org/alarmOnto/clinical#{concept_name}"),
            rdflib.URIRef(f"http://example.org/alarmOnto/device#{concept_name}"),
            rdflib.URIRef(f"http://example.org/alarmOnto/common#{concept_name}")
        ]
        
        # Check which URI actually exists as a class
        for uri in possible_uris:
            if (uri, self.namespaces['rdf'].type, self.namespaces['owl'].Class) in self.full_graph:
                return uri
        
        return None
    
    def find_instances_of_concept(self, concept_name):
        """Find all instances that are explicitly typed as the concept."""
        class_uri = self.get_concept_class_uri(concept_name)
        if not class_uri:
            print(f"  ⚠️ No class definition found for {concept_name}")
            return []
        
        # Find instances by rdf:type
        instances = []
        for instance in self.full_graph.subjects(self.namespaces['rdf'].type, class_uri):
            instances.append(instance)
        
        print(f"  Found {len(instances)} instances of type {class_uri}")
        return instances
    
    def analyze_instance_properties(self, instances):
        """Analyze the actual properties used by a set of instances."""
        data_properties = set()
        object_properties = set()
        
        for instance in instances:
            # Get all properties used by this instance
            for predicate, obj in self.full_graph.predicate_objects(instance):
                # Skip RDF structural properties
                if str(predicate) in [
                    str(self.namespaces['rdf'].type),
                    str(self.namespaces['rdfs'].label),
                    str(self.namespaces['owl'].sameAs)
                ]:
                    continue
                
                # Check if this is a data property or object property
                if self.is_data_property(predicate):
                    data_properties.add(str(predicate))
                elif self.is_object_property(predicate):
                    object_properties.add(str(predicate))
        
        return list(data_properties), list(object_properties)
    
    def is_data_property(self, property_uri):
        """Check if a property is a data property."""
        # Check if it's explicitly defined as a data property
        if (property_uri, self.namespaces['rdf'].type, self.namespaces['owl'].DatatypeProperty) in self.full_graph:
            return True
        
        # Check if the object is a literal (indicating data property)
        for s, p, o in self.full_graph.triples((None, property_uri, None)):
            if isinstance(o, rdflib.Literal):
                return True
        
        return False
    
    def is_object_property(self, property_uri):
        """Check if a property is an object property."""
        # Check if it's explicitly defined as an object property  
        if (property_uri, self.namespaces['rdf'].type, self.namespaces['owl'].ObjectProperty) in self.full_graph:
            return True
        
        # Check if the object is a URI (indicating object property)
        for s, p, o in self.full_graph.triples((None, property_uri, None)):
            if isinstance(o, rdflib.URIRef):
                return True
        
        return False
    
    def calculate_alarm_coverage_for_concept(self, concept_name, instances):
        """Calculate alarm coverage for a specific concept."""
        referencing_alarms = set()
        
        # Get instance URIs as strings for comparison
        instance_strs = [str(inst) for inst in instances]
        instance_names = [str(inst).split('#')[-1] if '#' in str(inst) else str(inst).split('/')[-1] 
                         for inst in instances]
        
        # Find conditions that reference instances of this concept
        for s, p, o in self.full_graph.triples((None, None, None)):
            if 'aboutEntity' in str(p):
                entity_str = str(o)
                entity_name = entity_str.split('#')[-1] if '#' in entity_str else entity_str.split('/')[-1]
                
                # Check direct instance matches
                if entity_str in instance_strs or entity_name in instance_names:
                    condition_uri = s
                    for alarm_s, alarm_p, alarm_o in self.full_graph.triples((None, None, condition_uri)):
                        if 'requiresCondition' in str(alarm_p) or 'impliesCondition' in str(alarm_p):
                            referencing_alarms.add(alarm_s)
        
        # Check all condition subjects for concept name patterns
        for s in self.full_graph.subjects():
            condition_name = str(s).split('#')[-1] if '#' in str(s) else str(s).split('/')[-1]
            
            # Check if this condition name contains our concept pattern
            if condition_name.startswith('Cond_'):
                concept_matches = False
                
                # Specific pattern matching for each concept
                if concept_name == "PhysiologicalProcess":
                    if any(pattern in condition_name for pattern in [
                        'Process', 'VentilationProcess', 'Ventilation'
                    ]):
                        concept_matches = True
                elif concept_name == "PhysiologicalSystem":
                    if any(pattern in condition_name for pattern in [
                        'System', 'PulmonarySystem', 'CardiovascularSystem'
                    ]):
                        concept_matches = True
                elif concept_name == "MeasurementProcess":
                    if 'MeasurementProcess' in condition_name or 'Measurement' in condition_name:
                        concept_matches = True
                elif concept_name == "PhysicalDevice":
                    if any(pattern in condition_name for pattern in [
                        'Monitor', 'Device', 'PhilipsMonitor'
                    ]):
                        concept_matches = True
                else:
                    # General pattern matching for other concepts
                    patterns = [concept_name, concept_name.lower()]
                    if any(pattern and pattern in condition_name for pattern in patterns):
                        concept_matches = True
                
                if concept_matches:
                    # Find which alarms use this condition
                    for alarm_s, alarm_p, alarm_o in self.full_graph.triples((None, None, s)):
                        if 'requiresCondition' in str(alarm_p) or 'impliesCondition' in str(alarm_p):
                            referencing_alarms.add(alarm_s)
        
        return len(referencing_alarms), list(referencing_alarms)
    
    def analyze_concept(self, concept_name):
        """Analyze a single concept comprehensively."""
        print(f"\n🔍 Analyzing {concept_name}...")
        
        # Find actual instances
        instances = self.find_instances_of_concept(concept_name)
        instance_count = len(instances)
        
        if instance_count == 0:
            print(f"  ⚠️ No instances found, checking for semantic references...")
        
        # Analyze properties used by these instances
        if instance_count > 0:
            data_props, object_props = self.analyze_instance_properties(instances)
        else:
            data_props, object_props = [], []
        
        # Calculate alarm coverage (includes semantic pattern matching)
        coverage_count, coverage_list = self.calculate_alarm_coverage_for_concept(concept_name, instances)
        
        # Debug output for concepts with unexpected coverage
        if concept_name == "PhysiologicalProcess" and coverage_count > 0:
            print(f"  🔍 Debug: Found conditions matching {concept_name}:")
            for s in self.full_graph.subjects():
                condition_name = str(s).split('#')[-1] if '#' in str(s) else str(s).split('/')[-1]
                if condition_name.startswith('Cond_') and ('Process' in condition_name or 'Ventilation' in condition_name):
                    print(f"    - {condition_name}")
        
        print(f"  📊 Results: {len(data_props)} data props, {len(object_props)} object props, {instance_count} instances, {coverage_count} alarms")
        
        return {
            'data_properties': len(data_props),
            'object_properties': len(object_props),
            'total_properties': len(data_props) + len(object_props),
            'property_details': {'data': data_props, 'object': object_props},
            'instances': instance_count,
            'instance_list': [str(inst) for inst in instances],
            'alarm_coverage': coverage_count,
            'coverage_list': [str(c) for c in coverage_list]
        }
    
    def analyze_all_concepts(self):
        """Analyze all base concepts."""
        print("\n" + "="*90)
        print("CORRECTED BASE CONCEPTS ANALYSIS")
        print("="*90)
        
        self.load_ontology_files()
        
        # Count total number of unique alarms for percentage calculation
        total_alarms = set()
        for s, p, o in self.full_graph.triples((None, None, None)):
            if ('requiresCondition' in str(p) or 'impliesCondition' in str(p)) and 'ACD_' in str(s):
                total_alarms.add(s)
        total_alarm_count = len(total_alarms)
        
        results = {}
        
        print(f"\nTotal unique alarms found: {total_alarm_count}\n")
        print(f"{'Concept':<20} {'Data':<8} {'Object':<8} {'Total':<8} {'Instances':<12} {'Coverage':<12} {'%':<8}")
        print("-" * 98)
        
        for concept in self.base_concepts:
            result = self.analyze_concept(concept)
            coverage_pct = (result['alarm_coverage'] / total_alarm_count * 100) if total_alarm_count > 0 else 0
            result['coverage_percentage'] = coverage_pct
            results[concept] = result
            
            print(f"{concept:<20} {result['data_properties']:<8} {result['object_properties']:<8} "
                  f"{result['total_properties']:<8} {result['instances']:<12} {result['alarm_coverage']:<12} {coverage_pct:<7.1f}%")
        
        return results

def main():
    """Main execution function."""
    base_path = "/Users/rzoodsm2/Documents/GIT-repositories/Alarm-Ontologie"
    analyzer = CorrectedBaseConceptAnalyzer(base_path)
    results = analyzer.analyze_all_concepts()
    
    print(f"\n{'='*90}")
    print("DETAILED VERIFICATION")
    print(f"{'='*90}")
    
    for concept, data in results.items():
        if data['instances'] > 0:
            print(f"\n✅ {concept}:")
            print(f"   Instances ({data['instances']}): {[inst.split('#')[-1] if '#' in inst else inst.split('/')[-1] for inst in data['instance_list']]}")
            if data['property_details']['data']:
                print(f"   Data Properties ({len(data['property_details']['data'])}): {[prop.split('#')[-1] for prop in data['property_details']['data']]}")
            if data['property_details']['object']:
                print(f"   Object Properties ({len(data['property_details']['object'])}): {[prop.split('#')[-1] for prop in data['property_details']['object']]}")
            print(f"   Alarm Coverage: {data['alarm_coverage']} unique alarms ({data['coverage_percentage']:.1f}%)")
            if data['coverage_list']:
                alarm_names = [alarm.split('#')[-1] if '#' in alarm else alarm.split('/')[-1] for alarm in data['coverage_list'][:3]]
                print(f"   Sample Alarms: {', '.join(alarm_names)}{'...' if len(data['coverage_list']) > 3 else ''}")
    
    return results

if __name__ == "__main__":
    main()