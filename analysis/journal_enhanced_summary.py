#!/usr/bin/env python3
"""
Enhanced Alarm Ontology Journal Statistics Generator

Creates formatted output suitable for medical journal publication including
LaTeX tables and comprehensive metrics analysis.

Author: Generated for Alarm Ontology Research  
Date: December 2025
"""

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import the main analyzer
from journal_summary_statistics import AlarmOntologyAnalyzer

def generate_latex_table(results):
    """Generate LaTeX table for journal publication."""
    
    # Calculate additional metrics
    instance_class_ratio = f"{results['instances']['total_individuals'] / results['core_ontology']['total_classes']:.1f}"
    
    latex_table = f"""\\begin{{table}}[htbp]
\\centering
\\caption{{Alarm Ontology Quantitative Characteristics}}
\\label{{tab:ontology_metrics}}
\\begin{{tabular}}{{|l|r|l|}}
\\hline
\\textbf{{Metric Category}} & \\textbf{{Count}} & \\textbf{{Description}} \\\\
\\hline
\\multicolumn{{3}}{{|c|}}{{\\textbf{{Ontological Structure}}}} \\\\
\\hline
Core Classes & {results['core_ontology']['total_classes']} & Fundamental concept definitions \\\\
Object Properties & {results['core_ontology']['object_properties']} & Relationships between entities \\\\
Data Properties & {results['core_ontology']['data_properties']} & Literal value assignments \\\\
Predefined States & {results['core_ontology']['predefined_states']} & Standardized state vocabulary \\\\
\\hline
\\multicolumn{{3}}{{|c|}}{{\\textbf{{Instance Population}}}} \\\\
\\hline
Total Individuals & {results['instances']['total_individuals']} & Complete instance coverage \\\\
Clinical Instances & {results['instances']['clinical_instances']} & Patient and physiological entities \\\\
Device Instances & {results['instances']['device_instances']} & Medical equipment and sensors \\\\
Alarm Instances & {results['instances']['alarm_instances']} & Alert and condition specifications \\\\
Instance-to-Class Ratio & {instance_class_ratio}:1 & Population density metric \\\\
\\hline
\\multicolumn{{3}}{{|c|}}{{\\textbf{{Alarm Modeling}}}} \\\\
\\hline
Condition Specifications & {results['alarm_modeling']['total_conditions']} & Entity-attribute-value requirements \\\\
Asserted Conditions & {results['alarm_modeling']['asserted_conditions']} & Explicitly defined requirements \\\\
Inferred Conditions & {results['alarm_modeling']['inferred_conditions']} & Computationally derived requirements \\\\
Alert Condition States & {results['alarm_modeling']['alert_condition_states']} & Reasoning-level alarm objects \\\\
\\hline
\\multicolumn{{3}}{{|c|}}{{\\textbf{{Domain Coverage}}}} \\\\
\\hline
Physiological Domains & {results['domain_coverage']['physiological_domains']} & Clinical monitoring areas \\\\
Device Categories & {results['domain_coverage']['device_types']} & Medical equipment types \\\\
SNOMED CT Codes & {results['interoperability']['entities_with_snomed_codes']} & Standardized clinical terms \\\\
Modular Namespaces & {results['interoperability']['ontology_modules']} & Architectural organization \\\\
\\hline
\\multicolumn{{3}}{{|c|}}{{\\textbf{{Computational Metrics}}}} \\\\
\\hline
RDF Triples & {results['complexity']['total_triples']:,} & Complete knowledge base size \\\\
Unique Subjects & {results['complexity']['unique_subjects']} & Distinct entities modeled \\\\
Unique Predicates & {results['complexity']['unique_predicates']} & Relationship vocabulary size \\\\
Avg. Branching Factor & {results['complexity']['average_branching_factor']} & Structural complexity metric \\\\
Ontology Density & {results['complexity']['ontology_density']} & Information density ratio \\\\
\\hline
\\end{{tabular}}
\\end{{table}}"""
    
    return latex_table

def generate_medical_journal_summary():
    """Generate comprehensive medical journal summary with enhanced formatting."""
    
    print("="*80)
    print("ALARM ONTOLOGY - ENHANCED MEDICAL JOURNAL ANALYSIS")
    print("="*80)
    
    # Initialize analyzer
    base_path = "/Users/rzoodsm2/Documents/GIT-repositories/Alarm-Ontologie"
    analyzer = AlarmOntologyAnalyzer(base_path)
    
    # Generate results
    results = analyzer.generate_journal_summary()
    
    # Additional medical-specific metrics
    print("\n📋 ADDITIONAL MEDICAL JOURNAL METRICS")
    print("="*50)
    
    # Calculate clinical relevance metrics
    clinical_percentage = round((results['instances']['clinical_instances'] / results['instances']['total_individuals']) * 100, 1)
    device_percentage = round((results['instances']['device_instances'] / results['instances']['total_individuals']) * 100, 1)
    alarm_percentage = round((results['instances']['alarm_instances'] / results['instances']['total_individuals']) * 100, 1)
    
    # Condition modeling efficiency
    total_conditions = results['alarm_modeling']['total_conditions']
    asserted_conditions = results['alarm_modeling']['asserted_conditions']
    inferred_conditions = results['alarm_modeling']['inferred_conditions']
    inference_ratio = round(inferred_conditions / asserted_conditions, 2) if asserted_conditions > 0 else 0
    
    print(f"   • Clinical Domain Coverage: {clinical_percentage}%")
    print(f"   • Device Domain Coverage: {device_percentage}%") 
    print(f"   • Alarm Domain Coverage: {alarm_percentage}%")
    print(f"   • Condition Inference Multiplier: {inference_ratio}x")
    print(f"   • Knowledge Representation Efficiency: {results['complexity']['ontology_density']} triples/entity")
    
    # Generate LaTeX table
    latex_table = generate_latex_table(results)
    
    print(f"\n📊 LATEX TABLE FOR JOURNAL")
    print("="*50)
    print(latex_table)
    
    # Save LaTeX table to file
    latex_file = f"{base_path}/analysis/ontology_metrics_table.tex"
    with open(latex_file, 'w') as f:
        f.write(latex_table)
    print(f"\n💾 LaTeX table saved to: {latex_file}")
    
    # Enhanced journal abstract
    print(f"\n📝 ENHANCED JOURNAL ABSTRACT PARAGRAPH")
    print("="*70)
    
    enhanced_abstract = f"""
BACKGROUND: Clinical alarm systems in intensive care environments generate frequent false alarms, compromising patient safety and staff workflow. Ontological modeling can provide formal semantic foundations for intelligent alarm interpretation.

METHODS: We developed a comprehensive alarm ontology comprising {results['core_ontology']['total_classes']} core classes, {results['core_ontology']['total_properties']} properties ({results['core_ontology']['object_properties']} object, {results['core_ontology']['data_properties']} data), and {results['core_ontology']['predefined_states']} predefined states. The ontology instantiates {results['instances']['total_individuals']} individuals across clinical ({results['instances']['clinical_instances']}, {clinical_percentage}%), device ({results['instances']['device_instances']}, {device_percentage}%), and alarm-specific ({results['instances']['alarm_instances']}, {alarm_percentage}%) domains.

RESULTS: The alarm modeling approach distinguishes {total_conditions} condition specifications into {asserted_conditions} explicitly asserted and {inferred_conditions} computationally inferred requirements (inference multiplier: {inference_ratio}x). Domain coverage encompasses {results['domain_coverage']['physiological_domains']} major physiological monitoring areas and {results['domain_coverage']['device_types']} medical device categories. Standards alignment includes {results['interoperability']['entities_with_snomed_codes']} SNOMED CT-coded entities organized across {results['interoperability']['ontology_modules']} modular namespaces.

CONCLUSIONS: The ontology provides formal semantic foundations for alarm interpretation with {results['complexity']['total_triples']:,} RDF triples (density: {results['complexity']['ontology_density']} triples/entity), supporting automated reasoning over clinical alarm scenarios in intensive care environments.
"""
    
    print(enhanced_abstract)
    
    # Save enhanced abstract
    abstract_file = f"{base_path}/analysis/journal_abstract.txt"
    with open(abstract_file, 'w') as f:
        f.write(enhanced_abstract)
    print(f"\n💾 Enhanced abstract saved to: {abstract_file}")
    
    print("\n" + "="*80)
    print("ANALYSIS COMPLETE - Files ready for journal submission!")
    print("="*80)
    
    return results, latex_table, enhanced_abstract

if __name__ == "__main__":
    generate_medical_journal_summary()