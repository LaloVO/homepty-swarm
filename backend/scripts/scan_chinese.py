#!/usr/bin/env python3
"""Scan all .py files in backend for Chinese characters and report them."""
import os
import re
import sys

chinese_re = re.compile(r'[\u4e00-\u9fa5]')
results = {}

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for root, dirs, files in os.walk(base):
    dirs[:] = [d for d in dirs if d not in ['__pycache__', '.git', 'venv', 'node_modules', 'scripts']]
    for fname in files:
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(root, fname)
        try:
            with open(fpath, encoding='utf-8') as f:
                lines = f.readlines()
            hits = [(i+1, l.rstrip()) for i, l in enumerate(lines) if chinese_re.search(l)]
            if hits:
                results[fpath] = hits
        except Exception as e:
            sys.stderr.write(f'ERROR {fpath}: {e}\n')

out_lines = []
for fpath, hits in sorted(results.items()):
    rel = os.path.relpath(fpath, base)
    out_lines.append(f'\n=== {rel} ({len(hits)} lines) ===')
    for lineno, line in hits:
        out_lines.append(f'  L{lineno}: {line[:150]}')

report = '\n'.join(out_lines)
# Write to file to avoid encoding issues on console
report_path = os.path.join(base, 'scripts', 'chinese_scan_report.txt')
with open(report_path, 'w', encoding='utf-8') as f:
    f.write(report)
print(f'Report written to {report_path}')
print(f'Files with Chinese: {len(results)}')
for fpath, hits in sorted(results.items()):
    rel = os.path.relpath(fpath, base)
    print(f'  {rel}: {len(hits)} lines')
