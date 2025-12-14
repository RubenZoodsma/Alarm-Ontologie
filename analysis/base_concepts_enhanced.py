#!/usr/bin/env python3
"""
Enhanced Base Concepts Analysis with LaTeX Output

Creates formatted tables and enhanced analysis for the 13 base concepts
with data properties, object properties, instances, and alarm coverage.

Author: Generated for Alarm Ontology Research
Date: December 2025
"""

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from base_concepts_analysis import BaseConceptAnalyzer

def generate_latex_table(results):
    """Generate LaTeX table for base concepts analysis."""
    
    latex_table = f"""\\begin{{table}}[htbp]
\\centering
\\caption{{Base Concepts Analysis: Properties, Instances, and Alarm Coverage}}
\\label{{tab:base_concepts}}
\\begin{{tabular}}{{|l|r|r|r|r|r|}}
\\hline
\\textbf{{Concept}} & \\textbf{{Data}} & \\textbf{{Object}} & \\textbf{{Total}} & \\textbf{{Instances}} & \\textbf{{Coverage}} \\\\
& \\textbf{{Props}} & \\textbf{{Props}} & \\textbf{{Props}} & & \\textbf{{(Alarms)}} \\\\
\\hline"""
    
    # Sort by total properties for better presentation
    sorted_concepts = sorted(results.items(), key=lambda x: x[1]['total_properties'], reverse=True)
    
    for concept, data in sorted_concepts:
        latex_table += f"""
{concept} & {data['data_properties']} & {data['object_properties']} & {data['total_properties']} & {data['instances']} & {data['alarm_coverage']} \\\\"""
    
    # Add totals row
    total_data = sum(d['data_properties'] for d in results.values())
    total_object = sum(d['object_properties'] for d in results.values())
    total_props = sum(d['total_properties'] for d in results.values())
    total_instances = sum(d['instances'] for d in results.values())
    total_coverage = sum(d['alarm_coverage'] for d in results.values())
    
    latex_table += f"""
\\hline
\\textbf{{TOTALS}} & \\textbf{{{total_data}}} & \\textbf{{{total_object}}} & \\textbf{{{total_props}}} & \\textbf{{{total_instances}}} & \\textbf{{{total_coverage}}} \\\\
\\hline
\\end{{tabular}}
\\end{{table}}"""
    
    return latex_table

def generate_coverage_analysis(results):
    """Generate detailed coverage analysis."""
    
    # Categorize concepts by coverage
    high_coverage = [(k, v) for k, v in results.items() if v['alarm_coverage'] >= 10]
    medium_coverage = [(k, v) for k, v in results.items() if 1 <= v['alarm_coverage'] < 10]
    no_coverage = [(k, v) for k, v in results.items() if v['alarm_coverage'] == 0]
    
    analysis = f"""
COVERAGE ANALYSIS:

HIGH COVERAGE (≥10 alarms):
{len(high_coverage)} concepts: {', '.join([c[0] for c in high_coverage])}

MEDIUM COVERAGE (1-9 alarms):
{len(medium_coverage)} concepts: {', '.join([c[0] for c in medium_coverage])}

NO COVERAGE (0 alarms):
{len(no_coverage)} concepts: {', '.join([c[0] for c in no_coverage])}

COVERAGE DISTRIBUTION:
- Concepts with alarm coverage: {len(high_coverage) + len(medium_coverage)}/13 ({((len(high_coverage) + len(medium_coverage))/13)*100:.1f}%)
- Average coverage per concept: {sum(v['alarm_coverage'] for v in results.values())/len(results):.1f} alarms
- Coverage efficiency: {sum(v['alarm_coverage'] for v in results.values())} total alarm references across concepts
"""
    
    return analysis

def generate_property_analysis(results):
    """Generate property distribution analysis."""
    
    # Sort by total properties
    by_properties = sorted(results.items(), key=lambda x: x[1]['total_properties'], reverse=True)
    
    analysis = f"""
PROPERTY DISTRIBUTION ANALYSIS:

TOP 5 CONCEPTS BY TOTAL PROPERTIES:
"""
    for i, (concept, data) in enumerate(by_properties[:5], 1):
        analysis += f"{i}. {concept}: {data['total_properties']} ({data['data_properties']} data + {data['object_properties']} object)\n"
    
    analysis += f"""
PROPERTY TYPES DISTRIBUTION:
- Data Properties: {sum(v['data_properties'] for v in results.values())} ({(sum(v['data_properties'] for v in results.values())/sum(v['total_properties'] for v in results.values()))*100:.1f}% of total)
- Object Properties: {sum(v['object_properties'] for v in results.values())} ({(sum(v['object_properties'] for v in results.values())/sum(v['total_properties'] for v in results.values()))*100:.1f}% of total)

CONCEPTS WITH MOST DATA PROPERTIES:
"""
    by_data_props = sorted(results.items(), key=lambda x: x[1]['data_properties'], reverse=True)
    for i, (concept, data) in enumerate(by_data_props[:3], 1):
        if data['data_properties'] > 0:
            analysis += f"{i}. {concept}: {data['data_properties']} data properties\n"
    
    analysis += f"""
CONCEPTS WITH MOST OBJECT PROPERTIES:
"""
    by_object_props = sorted(results.items(), key=lambda x: x[1]['object_properties'], reverse=True)
    for i, (concept, data) in enumerate(by_object_props[:3], 1):
        if data['object_properties'] > 0:
            analysis += f"{i}. {concept}: {data['object_properties']} object properties\n"
    
    return analysis

def generate_enhanced_analysis():
    """Generate comprehensive enhanced analysis."""
    
    print("="*90)
    print("ENHANCED BASE CONCEPTS ANALYSIS")
    print("="*90)
    
    # Run base analysis
    base_path = "/Users/rzoodsm2/Documents/GIT-repositories/Alarm-Ontologie"
    analyzer = BaseConceptAnalyzer(base_path)
    results = analyzer.analyze_all_concepts()
    
    # Generate LaTeX table
    latex_table = generate_latex_table(results)
    print(f"\n📊 LATEX TABLE FOR PUBLICATION")
    print("="*50)
    print(latex_table)
    
    # Generate coverage analysis
    coverage_analysis = generate_coverage_analysis(results)
    print(f"\n🎯 COVERAGE ANALYSIS")
    print("="*50)
    print(coverage_analysis)
    
    # Generate property analysis
    property_analysis = generate_property_analysis(results)
    print(f"\n🔧 PROPERTY ANALYSIS")
    print("="*50)
    print(property_analysis)
    
    # Generate key insights
    print(f"\n💡 KEY INSIGHTS")
    print("="*50)
    
    # Calculate key metrics
    total_concepts = len(results)
    concepts_with_coverage = len([v for v in results.values() if v['alarm_coverage'] > 0])
    avg_properties = sum(v['total_properties'] for v in results.values()) / total_concepts
    avg_instances = sum(v['instances'] for v in results.values()) / total_concepts
    avg_coverage = sum(v['alarm_coverage'] for v in results.values()) / total_concepts
    
    # Find extremes
    max_instances = max(results.items(), key=lambda x: x[1]['instances'])
    min_instances = min(results.items(), key=lambda x: x[1]['instances'])
    max_properties = max(results.items(), key=lambda x: x[1]['total_properties'])
    max_coverage = max(results.items(), key=lambda x: x[1]['alarm_coverage'])
    
    insights = f"""
STATISTICAL SUMMARY:
• {concepts_with_coverage}/{total_concepts} concepts ({(concepts_with_coverage/total_concepts)*100:.1f}%) have alarm coverage
• Average properties per concept: {avg_properties:.1f}
• Average instances per concept: {avg_instances:.1f}
• Average alarm coverage: {avg_coverage:.1f}

EXTREMES:
• Most instantiated: {max_instances[0]} ({max_instances[1]['instances']} instances)
• Least instantiated: {min_instances[0]} ({min_instances[1]['instances']} instances)
• Most properties: {max_properties[0]} ({max_properties[1]['total_properties']} properties)
• Highest coverage: {max_coverage[0]} ({max_coverage[1]['alarm_coverage']} alarms)

MODELING PATTERNS:
• {len([v for v in results.values() if v['data_properties'] > 0])}/13 concepts have data properties
• {len([v for v in results.values() if v['object_properties'] > 0])}/13 concepts have object properties
• {len([v for v in results.values() if v['instances'] > 0])}/13 concepts have instances
• Core device concepts (Sensor, Metric, DeviceChannel) show highest alarm integration
"""
    
    print(insights)
    
    # Save all outputs
    latex_file = f"{base_path}/analysis/base_concepts_table.tex"
    with open(latex_file, 'w') as f:
        f.write(latex_table)
    
    analysis_file = f"{base_path}/analysis/base_concepts_detailed_analysis.txt"
    with open(analysis_file, 'w') as f:
        f.write("BASE CONCEPTS ENHANCED ANALYSIS\n")
        f.write("="*50 + "\n\n")
        f.write(coverage_analysis + "\n")
        f.write(property_analysis + "\n")
        f.write("KEY INSIGHTS\n")
        f.write("="*20 + "\n")
        f.write(insights)
    
    print(f"\n💾 LaTeX table saved to: {latex_file}")
    print(f"💾 Detailed analysis saved to: {analysis_file}")
    print("="*90)
    
    return results, latex_table, coverage_analysis, property_analysis, insights

if __name__ == "__main__":
    generate_enhanced_analysis()