import pdfplumber
import sys
import docx
from pathlib import Path

def extract_pdf_text(path): 
    pages_text = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages_text.append(page.extract_text() or "")
        return "\n".join(pages_text)

def extract_docx_text(path):
    pages_text = []
    doc = docx.Document(path)
    for paragraph in doc.paragraphs:
        pages_text.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join([cell.text for cell in row.cells])
            pages_text.append(row_text)
    
    return "\n".join(pages_text)

if len(sys.argv) != 2:
    print("usage: py extrac.py <resume.pdf or resume.docx>", file=sys.stderr)
    sys.exit(1)

INPUT_PATH = sys.argv[1]

if not Path(INPUT_PATH).exists():
    print(f"error: file not found: {INPUT_PATH}", file=sys.stderr)
    sys.exit(1)

suffix = Path(INPUT_PATH).suffix.lower()

try:
    if suffix == ".pdf":
        text = extract_pdf_text(INPUT_PATH)
    elif suffix == ".docx":
        text = extract_docx_text(INPUT_PATH)
    else:
        print(f"error: unsupported file type '{suffix}': please upload a PDF or Word (.docx) file", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"error: could not read {INPUT_PATH} ({e})", file=sys.stderr)
    sys.exit(1)

if len(text.strip()) < 100:
    print("error: no readable text found. This may be a scanned image; "
          "please upload a text-based PDF or a Word file.", file=sys.stderr)
    sys.exit(1)

print(text)