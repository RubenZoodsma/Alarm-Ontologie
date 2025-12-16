#!/usr/bin/env python3
"""
Base Concepts Analysis for Alarm Ontology

Analyzes the 13 base concepts and calculates:
- Number of data properties
- Number of object properties  
- Number of instances
- Coverage (number of alarms referencing each concept)

Author: Generated for Alarm Ontology Research
Date: December 2025
"""

import rdflib
import json
from pathlib import Path
from collections import defaultdict

class BaseConceptAnalyzer:
    """Analyzes base concepts in the Alarm Ontology."""
    
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
    
    def get_concept_uri(self, concept_name):
        """Get the full URI for a concept name."""
        # Try different namespaces
        possible_uris = [
            self.namespaces['common'][concept_name],
            self.namespaces['device'][concept_name],
            self.namespaces['clinical'][concept_name]
        ]
        
        # Check which URI actually exists in the ontology
        for uri in possible_uris:
            if (uri, self.namespaces['rdf'].type, self.namespaces['owl'].Class) in self.full_graph or \
               (uri, self.namespaces['rdf'].type, self.namespaces['rdfs'].Class) in self.full_graph:
                return uri
        
        # If not found as exact match, look for partial matches
        for s in self.full_graph.subjects():
            if concept_name in str(s):
                return s
        
        return None
    
    def count_properties_for_concept(self, concept_name):
        """Count data and object properties for a given concept."""
        concept_uri = self.get_concept_uri(concept_name)
        if not concept_uri:
            return 0, 0, []
        
        data_properties = []
        object_properties = []
        
        # Find properties that have this concept as domain
        for prop in self.full_graph.subjects():
            # Check if it's a property
            prop_types = list(self.full_graph.objects(prop, self.namespaces['rdf'].type))
            
            is_data_prop = any('DatatypeProperty' in str(pt) for pt in prop_types)
            is_object_prop = any('ObjectProperty' in str(pt) for pt in prop_types)
            
            if is_data_prop or is_object_prop:
                # Check if domain includes our concept
                domains = list(self.full_graph.objects(prop, self.namespaces['rdfs'].domain))
                for domain in domains:
                    if str(domain) == str(concept_uri) or concept_name in str(domain):
                        if is_data_prop:
                            data_properties.append(str(prop))
                        elif is_object_prop:
                            object_properties.append(str(prop))
        
        return len(data_properties), len(object_properties), data_properties + object_properties
    
    def count_instances_for_concept(self, concept_name):
        """Count instances of a given concept."""
        concept_uri = self.get_concept_uri(concept_name)
        if not concept_uri:
            # Try to find instances by name pattern
            instances = []
            for s in self.full_graph.subjects():
                if concept_name.lower() in str(s).lower():
                    instances.append(str(s))
            return len(instances), instances
        
        # Find direct instances
        instances = list(self.full_graph.subjects(self.namespaces['rdf'].type, concept_uri))
        
        # Also find instances by name pattern (since some might not have explicit typing)
        pattern_instances = []
        for s in self.full_graph.subjects():
            s_str = str(s).lower()
            concept_lower = concept_name.lower()
            if concept_lower in s_str or any(variant in s_str for variant in [
                concept_lower + '_', concept_lower.rstrip('s') + '_', 
                concept_lower + 'sensor', concept_lower + 'channel'
            ]):
                if s not in instances:
                    pattern_instances.append(s)
        
        all_instances = instances + pattern_instances
        return len(all_instances), [str(i) for i in all_instances]
    
    def calculate_alarm_coverage(self, concept_name):
        """Calculate how many alarms reference this concept."""
        concept_instances = set()
        
        # Get all instances of this concept
        _, instance_list = self.count_instances_for_concept(concept_name)
        for instance in instance_list:
            concept_instances.add(instance.split('#')[-1] if '#' in instance else instance.split('/')[-1])
        
        # Also add the concept name itself
        concept_instances.add(concept_name)
        
        # Find AlertConditionDescription instances
        acd_alarms = set()
        for s in self.full_graph.subjects():
            s_str = str(s)
            if 'ACD_' in s_str or 'AlertConditionDescription' in s_str:
                acd_alarms.add(s)
        
        # Count how many ACDs reference instances of this concept
        referencing_acds = set()
        
        for acd in acd_alarms:
            # Look for conditions referenced by this ACD
            for p, o in self.full_graph.predicate_objects(acd):
                if 'requiresCondition' in str(p) or 'impliesCondition' in str(p):
                    # This ACD references condition 'o'
                    # Now check if this condition references our concept
                    for cond_p, cond_o in self.full_graph.predicate_objects(o):
                        if 'aboutEntity' in str(cond_p):
                            entity_name = str(cond_o).split('#')[-1] if '#' in str(cond_o) else str(cond_o).split('/')[-1]
                            
                            # Check if this entity matches our concept instances
                            if entity_name in concept_instances or \
                               any(concept_part in entity_name for concept_part in [concept_name, concept_name.lower()]):
                                referencing_acds.add(acd)
                                break
        
        return len(referencing_acds), list(referencing_acds)
    
    def analyze_all_concepts(self):
        """Analyze all base concepts and return comprehensive results."""
        print("\n" + "="*80)
        print("BASE CONCEPTS ANALYSIS")
        print("="*80)
        
        self.load_ontology_files()
        
        results = {}
        
        print(f"\n{'Concept':<20} {'Data Props':<12} {'Object Props':<12} {'Instances':<12} {'Coverage':<10}")
        print("-" * 80)
        
        for concept in self.base_concepts:
            print(f"\nAnalyzing {concept}...")
            
            # Count properties
            data_props, object_props, prop_list = self.count_properties_for_concept(concept)
            
            # Count instances  
            instance_count, instance_list = self.count_instances_for_concept(concept)
            
            # Calculate alarm coverage
            coverage_count, coverage_list = self.calculate_alarm_coverage(concept)
            
            results[concept] = {
                'data_properties': data_props,
                'object_properties': object_props,
                'total_properties': data_props + object_props,
                'property_list': prop_list,
                'instances': instance_count,
                'instance_list': instance_list,
                'alarm_coverage': coverage_count,
                'coverage_list': [str(c) for c in coverage_list]
            }
            
            print(f"{concept:<20} {data_props:<12} {object_props:<12} {instance_count:<12} {coverage_count:<10}")
        
        # Print detailed results
        print(f"\n{'='*80}")
        print("DETAILED ANALYSIS")
        print(f"{'='*80}")
        
        for concept, data in results.items():
            print(f"\n🔍 {concept.upper()}")
            print(f"   Data Properties: {data['data_properties']}")
            print(f"   Object Properties: {data['object_properties']}")
            print(f"   Total Properties: {data['total_properties']}")
            print(f"   Instances: {data['instances']}")
            print(f"   Alarm Coverage: {data['alarm_coverage']}")
            
            if data['property_list']:
                print(f"   Properties: {', '.join(data['property_list'][:3])}{'...' if len(data['property_list']) > 3 else ''}")
            
            if data['instance_list']:
                print(f"   Sample Instances: {', '.join([i.split('#')[-1] if '#' in i else i.split('/')[-1] for i in data['instance_list'][:3]])}{'...' if len(data['instance_list']) > 3 else ''}")
        
        # Generate summary statistics
        print(f"\n{'='*80}")
        print("SUMMARY STATISTICS")
        print(f"{'='*80}")
        
        total_data_props = sum(d['data_properties'] for d in results.values())
        total_object_props = sum(d['object_properties'] for d in results.values())
        total_instances = sum(d['instances'] for d in results.values())
        total_coverage = sum(d['alarm_coverage'] for d in results.values())
        
        print(f"Total Data Properties across all concepts: {total_data_props}")
        print(f"Total Object Properties across all concepts: {total_object_props}")
        print(f"Total Instances across all concepts: {total_instances}")
        print(f"Total Alarm Coverage instances: {total_coverage}")
        
        # Identify most/least covered concepts
        coverage_sorted = sorted(results.items(), key=lambda x: x[1]['alarm_coverage'], reverse=True)
        print(f"\nMost covered concept: {coverage_sorted[0][0]} ({coverage_sorted[0][1]['alarm_coverage']} alarms)")
        print(f"Least covered concept: {coverage_sorted[-1][0]} ({coverage_sorted[-1][1]['alarm_coverage']} alarms)")
        
        # Generate CSV output
        csv_output = "Concept,Data Properties,Object Properties,Total Properties,Instances,Alarm Coverage\n"
        for concept, data in results.items():
            csv_output += f"{concept},{data['data_properties']},{data['object_properties']},{data['total_properties']},{data['instances']},{data['alarm_coverage']}\n"
        
        # Save results
        output_file = self.base_path / 'analysis' / 'base_concepts_analysis.json'
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)
        
        csv_file = self.base_path / 'analysis' / 'base_concepts_summary.csv'
        with open(csv_file, 'w') as f:
            f.write(csv_output)
        
        print(f"\n💾 Detailed results saved to: {output_file}")
        print(f"💾 CSV summary saved to: {csv_file}")
        
        return results

def main():
    """Main execution function."""
    base_path = "/Users/rzoodsm2/Documents/GIT-repositories/Alarm-Ontologie"
    analyzer = BaseConceptAnalyzer(base_path)
    results = analyzer.analyze_all_concepts()
    return results

if __name__ == "__main__":
    main()