#!/usr/bin/env python3
"""
Baby Activity Report Generator

Generates a comprehensive report from:
  1. Activity log CSV (feeds, pumps, diapers, sleep from manual tracking)
  2. Sleep monitor JSONL (camera-based bassinet monitoring)

Usage:
  report.py --range 24h          # Last 24 hours
  report.py --range 7d           # Last 7 days
  report.py --from 2026-03-25 --to 2026-03-31
  report.py --range 24h --section sleep   # Only sleep section
  report.py --range 7d --format json      # JSON output
"""

import argparse
import re
import sys
from datetime import timedelta
from pathlib import Path

# Ensure lib/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.output import generate_report, generate_json_report
from lib.loaders import parse_time_range


def main():
    parser = argparse.ArgumentParser(description='Baby Activity Report Generator')
    parser.add_argument('--range', help='Time range: e.g. 24h, 7d, 2w')
    parser.add_argument('--from', dest='start', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--to', dest='end', help='End date (YYYY-MM-DD)')
    parser.add_argument('--section', help='Only show specific section: sleep, feeding, pumping, diapers, weight')
    parser.add_argument('--format', default='text', choices=['text', 'json'], help='Output format')
    parser.add_argument('--csv', help='Path to activity CSV (overrides default)')
    args = parser.parse_args()

    start, end = parse_time_range(args)

    if args.format == 'json':
        print(generate_json_report(start, end, csv_path=args.csv))
    else:
        sections = [args.section] if args.section else None
        print(generate_report(start, end, sections, csv_path=args.csv))


if __name__ == '__main__':
    main()
