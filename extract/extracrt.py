import pdfplumber
import sys
import docx

PDF_PATH = sys.argv[1]

def extract_pdf_text(path): 
    pages_text = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages_text.append(page.extract_text() or "")
        return "\n".join(pages_text)

def extract_docx_text(path):



try:
    text = extract_pdf_text(PDF_PATH)
    print(text)
    if len(text.strip()) < 100:
        print("error: no readable text found. This may be a scanend image; " "please upload a text-based PDF or a Word File.", file=sys.stderr)
        sys.exit(1)
except FileNotFoundError:
    print(f"error: file not found: {PDF_PATH}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"error: could not read {PDF_PATH} as a PDF ({e})", file=sys.stderr)
    sys.exit(1)