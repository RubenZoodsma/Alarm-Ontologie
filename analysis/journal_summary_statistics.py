#!/usr/bin/env python3
"""
Alarm Ontology Journal Summary Statistics Generator

This script analyzes the Alarm Ontology and generates quantitative characteristics
suitable for reporting in a medical journal paper. It provides comprehensive
metrics about the ontology's structure, coverage, and modeling approach.

Author: Generated for Alarm Ontology Research
Date: December 2025
"""

import rdflib
import glob
import os
from collections import defaultdict, Counter
import json
from pathlib import Path

class AlarmOntologyAnalyzer:
    """Analyzes alarm ontology files and generates journal-ready statistics."""
    
    def __init__(self, base_path):
        self.base_path = Path(base_path)
        self.core_graph = rdflib.Graph()
        self.instance_graph = rdflib.Graph()
        self.full_graph = rdflib.Graph()
        
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
            self.core_graph.bind(prefix, namespace)
            self.instance_graph.bind(prefix, namespace)
            self.full_graph.bind(prefix, namespace)
    
    def load_ontology_files(self):
        """Load all ontology files into appropriate graphs."""
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
                    self.core_graph.parse(str(full_path))
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
                        self.instance_graph.parse(str(ttl_file))
                        self.full_graph.parse(str(ttl_file))
                        print(f"✓ Loaded {ttl_file.name}")
                    except Exception as e:
                        print(f"✗ Error loading {ttl_file}: {e}")
    
    def analyze_core_ontology_structure(self):
        """Analyze the structure of the core ontology."""
        print("\n=== CORE ONTOLOGY ANALYSIS ===")
        
        # Count classes
        classes = set()
        for s in self.core_graph.subjects(self.namespaces['rdf'].type, self.namespaces['owl'].Class):
            classes.add(s)
        for s in self.core_graph.subjects(self.namespaces['rdf'].type, self.namespaces['rdfs'].Class):
            classes.add(s)
        
        # Count properties
        object_properties = set(self.core_graph.subjects(self.namespaces['rdf'].type, self.namespaces['owl'].ObjectProperty))
        data_properties = set(self.core_graph.subjects(self.namespaces['rdf'].type, self.namespaces['owl'].DatatypeProperty))
        
        # Count individuals/states
        individuals = set()
        for s in self.core_graph.subjects(self.namespaces['rdf'].type, None):
            for o in self.core_graph.objects(s, self.namespaces['rdf'].type):
                if str(o).endswith('#State') or 'State' in str(o):
                    individuals.add(s)
        
        return {
            'total_classes': len(classes),
            'object_properties': len(object_properties), 
            'data_properties': len(data_properties),
            'total_properties': len(object_properties) + len(data_properties),
            'predefined_states': len(individuals),
            'classes_list': [str(c) for c in classes],
            'object_properties_list': [str(p) for p in object_properties],
            'data_properties_list': [str(p) for p in data_properties]
        }
    
    def analyze_instance_coverage(self):
        """Analyze instance coverage across domains."""
        print("\n=== INSTANCE COVERAGE ANALYSIS ===")
        
        # Count total individuals
        individuals = set()
        for s in self.instance_graph.subjects():
            if isinstance(s, rdflib.URIRef):
                individuals.add(s)
        
        # Categorize by domain
        clinical_instances = set()
        device_instances = set() 
        alarm_instances = set()
        
        for individual in individuals:
            individual_str = str(individual)
            if 'clinical' in individual_str.lower() or any(term in individual_str.lower() 
                for term in ['patient', 'property', 'organ', 'system']):
                clinical_instances.add(individual)
            elif 'device' in individual_str.lower() or any(term in individual_str.lower()
                for term in ['sensor', 'monitor', 'channel', 'metric', 'signal']):
                device_instances.add(individual)
            elif 'alarm' in individual_str.lower() or any(term in individual_str.lower()
                for term in ['condition', 'acd', 'acs']):
                alarm_instances.add(individual)
        
        return {
            'total_individuals': len(individuals),
            'clinical_instances': len(clinical_instances),
            'device_instances': len(device_instances), 
            'alarm_instances': len(alarm_instances),
            'individuals_list': [str(i) for i in individuals]
        }
    
    def analyze_alarm_modeling_approach(self):
        """Analyze the alarm modeling approach and conditions."""
        print("\n=== ALARM MODELING ANALYSIS ===")
        
        # Count AlertConditionDescription instances using direct RDF pattern matching
        acd_count = 0
        acs_count = 0
        condition_count = 0
        
        # Look for AlertConditionDescription
        for s, p, o in self.full_graph:
            if p == self.namespaces['rdf'].type:
                o_str = str(o)
                if 'AlertConditionDescription' in o_str:
                    acd_count += 1
                elif 'AlertConditionState' in o_str:
                    acs_count += 1
                elif 'Condition' in o_str and 'Alert' not in o_str:
                    condition_count += 1
        
        # Alternative approach: count by looking for specific patterns
        if acd_count == 0:
            # Look for ACD pattern in subject names
            for s in self.full_graph.subjects():
                s_str = str(s)
                if 'ACD_' in s_str or 'AlertConditionDescription' in s_str:
                    acd_count += 1
                elif 'ACS_' in s_str or 'AlertConditionState' in s_str:
                    acs_count += 1
                elif 'Cond_' in s_str:
                    condition_count += 1
        
        # Analyze condition types (asserted vs inferred)
        asserted_conditions = 0
        inferred_conditions = 0
        
        # Look for requiresCondition and impliesCondition patterns
        requires_condition = 0
        implies_condition = 0
        
        for s, p, o in self.full_graph:
            p_str = str(p)
            if 'requiresCondition' in p_str:
                requires_condition += 1
                asserted_conditions += 1
            elif 'impliesCondition' in p_str:
                implies_condition += 1 
                inferred_conditions += 1
        
        # If we still don't find proper counts, count by analyzing condition comments
        if asserted_conditions == 0 and inferred_conditions == 0:
            for s in self.full_graph.subjects():
                if 'Cond_' in str(s):
                    # Check comments for asserted vs inferred
                    for comment in self.full_graph.objects(s, self.namespaces['rdfs'].comment):
                        comment_str = str(comment).lower()
                        if 'asserted' in comment_str or 'explicit' in comment_str:
                            asserted_conditions += 1
                        elif 'inferred' in comment_str or 'derived' in comment_str:
                            inferred_conditions += 1
                        else:
                            # Default to asserted if no clear indication
                            asserted_conditions += 1
                        break
        
        return {
            'alert_condition_descriptions': acd_count,
            'alert_condition_states': acs_count,
            'total_conditions': condition_count,
            'asserted_conditions': asserted_conditions,
            'inferred_conditions': inferred_conditions,
            'condition_modeling_ratio': f"{asserted_conditions}:{inferred_conditions}" if inferred_conditions > 0 else f"{asserted_conditions}:0",
            'requires_condition_relations': requires_condition,
            'implies_condition_relations': implies_condition
        }
    
    def analyze_domain_coverage(self):
        """Analyze coverage across medical domains."""
        print("\n=== DOMAIN COVERAGE ANALYSIS ===")
        
        # Analyze physiological properties covered
        physiological_terms = [
            'temperature', 'pressure', 'saturation', 'rate', 'rhythm', 
            'co2', 'oxygen', 'ecg', 'cardiac', 'respiratory'
        ]
        
        covered_domains = set()
        all_entities = [str(s) for s in self.full_graph.subjects()]
        
        for entity in all_entities:
            entity_lower = entity.lower()
            for term in physiological_terms:
                if term in entity_lower:
                    covered_domains.add(term.title())
        
        # Analyze device types
        device_types = [
            'monitor', 'sensor', 'transducer', 'electrode', 'probe', 
            'oximeter', 'capnography', 'ventilator'
        ]
        
        covered_devices = set()
        for entity in all_entities:
            entity_lower = entity.lower()
            for device in device_types:
                if device in entity_lower:
                    covered_devices.add(device.title())
        
        return {
            'physiological_domains': len(covered_domains),
            'device_types': len(covered_devices),
            'covered_physiological_domains': list(covered_domains),
            'covered_device_types': list(covered_devices)
        }
    
    def analyze_interoperability_features(self):
        """Analyze interoperability and standards alignment."""
        print("\n=== INTEROPERABILITY ANALYSIS ===")
        
        # Check for MDC codes (IEEE 11073)
        mdc_properties = list(self.full_graph.subjects(self.namespaces['common'].hasMDCCode, None))
        mdc_codes = list(self.full_graph.objects(None, self.namespaces['common'].hasMDCCode))
        
        # Check for SNOMED codes
        snomed_properties = list(self.full_graph.subjects(self.namespaces['common'].hasSNOMEDCode, None))
        snomed_codes = list(self.full_graph.objects(None, self.namespaces['common'].hasSNOMEDCode))
        
        # Analyze modular architecture
        namespaces_used = set()
        for s, p, o in self.full_graph:
            if isinstance(s, rdflib.URIRef):
                ns = str(s).split('#')[0] + '#'
                namespaces_used.add(ns)
        
        return {
            'entities_with_mdc_codes': len(mdc_properties),
            'total_mdc_codes': len(mdc_codes),
            'entities_with_snomed_codes': len(snomed_properties),
            'total_snomed_codes': len(snomed_codes),
            'ontology_modules': len([ns for ns in namespaces_used if 'Alarm-Ontologie' in ns])
        }
    
    def generate_complexity_metrics(self):
        """Generate complexity and expressivity metrics."""
        print("\n=== COMPLEXITY METRICS ===")
        
        total_triples = len(self.full_graph)
        total_subjects = len(set(self.full_graph.subjects()))
        total_predicates = len(set(self.full_graph.predicates()))
        total_objects = len(set(self.full_graph.objects()))
        
        # Calculate average branching factor
        subject_counts = Counter()
        for s, p, o in self.full_graph:
            subject_counts[s] += 1
        
        avg_branching = sum(subject_counts.values()) / len(subject_counts) if subject_counts else 0
        
        return {
            'total_triples': total_triples,
            'unique_subjects': total_subjects,
            'unique_predicates': total_predicates, 
            'unique_objects': total_objects,
            'average_branching_factor': round(avg_branching, 2),
            'ontology_density': round(total_triples / total_subjects, 2) if total_subjects > 0 else 0
        }
    
    def generate_journal_summary(self):
        """Generate comprehensive summary for journal publication."""
        print("\n" + "="*60)
        print("ALARM ONTOLOGY - JOURNAL SUMMARY STATISTICS")
        print("="*60)
        
        # Load all files
        self.load_ontology_files()
        
        # Perform all analyses
        core_stats = self.analyze_core_ontology_structure()
        instance_stats = self.analyze_instance_coverage()
        alarm_stats = self.analyze_alarm_modeling_approach()
        domain_stats = self.analyze_domain_coverage()
        interop_stats = self.analyze_interoperability_features()
        complexity_stats = self.generate_complexity_metrics()
        
        # Format results for journal
        print(f"\n📊 ONTOLOGY STRUCTURE METRICS")
        print(f"   • Core Classes: {core_stats['total_classes']}")
        print(f"   • Object Properties: {core_stats['object_properties']}")
        print(f"   • Data Properties: {core_stats['data_properties']}")
        print(f"   • Total Properties: {core_stats['total_properties']}")
        print(f"   • Predefined States: {core_stats['predefined_states']}")
        
        print(f"\n🏥 INSTANCE COVERAGE")
        print(f"   • Total Individuals: {instance_stats['total_individuals']}")
        print(f"   • Clinical Instances: {instance_stats['clinical_instances']}")
        print(f"   • Device Instances: {instance_stats['device_instances']}")
        print(f"   • Alarm Instances: {instance_stats['alarm_instances']}")
        
        print(f"\n🚨 ALARM MODELING APPROACH")
        print(f"   • Alert Condition Descriptions: {alarm_stats['alert_condition_descriptions']}")
        print(f"   • Alert Condition States: {alarm_stats['alert_condition_states']}")
        print(f"   • Total Conditions: {alarm_stats['total_conditions']}")
        print(f"   • Asserted Conditions: {alarm_stats['asserted_conditions']}")
        print(f"   • Inferred Conditions: {alarm_stats['inferred_conditions']}")
        print(f"   • Asserted:Inferred Ratio: {alarm_stats['condition_modeling_ratio']}")
        print(f"   • Requires Condition Relations: {alarm_stats['requires_condition_relations']}")
        print(f"   • Implies Condition Relations: {alarm_stats['implies_condition_relations']}")
        
        print(f"\n🔬 DOMAIN COVERAGE")
        print(f"   • Physiological Domains: {domain_stats['physiological_domains']}")
        print(f"   • Device Types: {domain_stats['device_types']}")
        print(f"   • Covered Domains: {', '.join(domain_stats['covered_physiological_domains'])}")
        print(f"   • Covered Devices: {', '.join(domain_stats['covered_device_types'])}")
        
        print(f"\n🔗 INTEROPERABILITY FEATURES")
        print(f"   • Entities with MDC Codes: {interop_stats['entities_with_mdc_codes']}")
        print(f"   • Total MDC Codes: {interop_stats['total_mdc_codes']}")
        print(f"   • Entities with SNOMED Codes: {interop_stats['entities_with_snomed_codes']}")
        print(f"   • Ontology Modules: {interop_stats['ontology_modules']}")
        
        print(f"\n📈 COMPLEXITY METRICS")
        print(f"   • Total Triples: {complexity_stats['total_triples']:,}")
        print(f"   • Unique Subjects: {complexity_stats['unique_subjects']}")
        print(f"   • Unique Predicates: {complexity_stats['unique_predicates']}")
        print(f"   • Average Branching Factor: {complexity_stats['average_branching_factor']}")
        print(f"   • Ontology Density: {complexity_stats['ontology_density']}")
        
        # Generate journal-ready summary paragraph
        print(f"\n📝 JOURNAL-READY SUMMARY PARAGRAPH:")
        print("="*60)
        
        # Calculate key ratios for journal reporting
        instance_to_class_ratio = round(instance_stats['total_individuals'] / core_stats['total_classes'], 1)
        clinical_coverage_percent = round((instance_stats['clinical_instances'] / instance_stats['total_individuals']) * 100, 1)
        device_coverage_percent = round((instance_stats['device_instances'] / instance_stats['total_individuals']) * 100, 1)
        
        summary = f"""
ONTOLOGY CHARACTERISTICS: The Alarm Ontology comprises {core_stats['total_classes']} core classes, {core_stats['total_properties']} properties ({core_stats['object_properties']} object properties, {core_stats['data_properties']} data properties), and {core_stats['predefined_states']} predefined states. The ontology instantiates {instance_stats['total_individuals']} individuals with an instance-to-class ratio of {instance_to_class_ratio}:1, distributed across clinical ({instance_stats['clinical_instances']}, {clinical_coverage_percent}%), device ({instance_stats['device_instances']}, {device_coverage_percent}%), and alarm-specific ({instance_stats['alarm_instances']}) domains.

ALARM MODELING: The approach employs {alarm_stats['alert_condition_descriptions']} Alert Condition Descriptions and {alarm_stats['alert_condition_states']} Alert Condition States, operationalized through {alarm_stats['total_conditions']} condition specifications. The modeling distinguishes between {alarm_stats['asserted_conditions']} explicitly asserted conditions and {alarm_stats['inferred_conditions']} computationally inferred conditions (ratio {alarm_stats['condition_modeling_ratio']}), connected via {alarm_stats['requires_condition_relations']} "requires" and {alarm_stats['implies_condition_relations']} "implies" relations.

CLINICAL COVERAGE: Domain coverage encompasses {domain_stats['physiological_domains']} major physiological domains ({', '.join(domain_stats['covered_physiological_domains'])}) and {domain_stats['device_types']} medical device categories ({', '.join(domain_stats['covered_device_types'])}), supporting comprehensive intensive care monitoring scenarios.

INTEROPERABILITY: Standards alignment includes {interop_stats['entities_with_snomed_codes']} entities with SNOMED CT codes and {interop_stats['entities_with_mdc_codes']} with IEEE 11073 MDC codes, organized across {interop_stats['ontology_modules']} modular namespaces for maintainability and reuse.

COMPUTATIONAL CHARACTERISTICS: The complete ontology contains {complexity_stats['total_triples']:,} RDF triples with {complexity_stats['unique_subjects']} unique subjects and {complexity_stats['unique_predicates']} unique predicates, yielding an average branching factor of {complexity_stats['average_branching_factor']} and ontology density of {complexity_stats['ontology_density']}.
"""
        print(summary)
        
        # Save detailed results to JSON
        results = {
            'core_ontology': core_stats,
            'instances': instance_stats, 
            'alarm_modeling': alarm_stats,
            'domain_coverage': domain_stats,
            'interoperability': interop_stats,
            'complexity': complexity_stats,
            'journal_summary': summary.strip()
        }
        
        output_file = self.base_path / 'analysis' / 'journal_summary_results.json'
        output_file.parent.mkdir(exist_ok=True)
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        
        print(f"\n💾 Detailed results saved to: {output_file}")
        print("="*60)
        
        return results

def main():
    """Main execution function."""
    # Set base path to ontology directory
    base_path = "/Users/rzoodsm2/Documents/GIT-repositories/Alarm-Ontologie"
    
    # Initialize analyzer
    analyzer = AlarmOntologyAnalyzer(base_path)
    
    # Generate and print journal summary
    results = analyzer.generate_journal_summary()
    
    return results

if __name__ == "__main__":
    main()