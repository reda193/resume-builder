"""Show how each page of one or more PDFs is read: one column or two.

Usage: py check_columns.py <file.pdf> [more.pdf ...]
"""
import sys

import pdfplumber

import columns

for path in sys.argv[1:]:
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            lines = columns.group_into_lines(page.extract_words())
            gutter = columns.find_gutter(lines, page.width) if lines else None
            layout = f"two columns (gutter at x={gutter:.0f})" if gutter else "one column"
            print(f"{path}, page {number}: {layout}")