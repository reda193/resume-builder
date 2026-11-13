"""Turn an uploaded resume (PDF or Word) into resume JSON.

Usage: py extract.py <resume.pdf or resume.docx> <output.json>

The work is done by extract_from_file, which takes the file's contents as
bytes, so the command line and the API share it. It raises an exception
instead of exiting, with one class per kind of problem, so a caller can tell
them apart: UnsupportedFileError, UnreadableFileError and AIExtractionError.

Steps:
  1. Read the text out of the file.
  2. AI call 1: extract the main resume. Sections the schema doesn't cover
     come back in "unplaced", grouped by heading.
  3. AI call 2 (once per unplaced group): turn it into a custom section.
  4. Add the custom sections to the resume, in their original position.
  5. Remove empty values, check against the full schema, and save.
"""
import io
import json
import re
import sys
from pathlib import Path

import anthropic
import docx
import jsonschema
import pdfplumber
from dotenv import load_dotenv

import columns

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
client = anthropic.Anthropic()

MODEL = "claude-haiku-5-5"
EXTRACTION_INSTRUCTIONS = (HERE / "prompts" / "extraction.md").read_text(encoding="utf-8")
CUSTOM_SECTION_INSTRUCTIONS = (HERE / "prompts" / "custom_section.md").read_text(encoding="utf-8")

with open(HERE / ".." / "resume.schema.json", encoding="utf-8") as f:
    RESUME_SCHEMA = json.load(f)

BUILTIN_SECTIONS = ["summary", "education", "experience", "projects", "skills"]


# ---------------------------------------------------------------------------
# Schemas for the AI
# ---------------------------------------------------------------------------

# Rules the API can't enforce while generating. The full schema still checks
# them afterward, in validate_resume.
NOT_SUPPORTED = {"maxLength", "minLength", "maxItems", "uniqueItems", "pattern",
                 "$schema", "$id", "format"}


def simplify(node):
    """Copy a schema for the AI: keep $refs (each definition is written once),
    remove rules the API can't use, and make every field required
    (the AI writes "" or [] when a field doesn't apply)."""
    if isinstance(node, list):
        return [simplify(item) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return {"$ref": node["$ref"]}
    if "anyOf" in node:
        return {"type": "string"}
    result = {}
    for key, value in node.items():
        if key in NOT_SUPPORTED or key == "required":
            continue
        if key == "minItems" and value > 1:
            continue
        if key in ("properties", "$defs"):
            result[key] = {name: simplify(sub) for name, sub in value.items()}
            if key == "properties":
                result["required"] = list(value.keys())
        else:
            result[key] = simplify(value)
    return result


def build_resume_wrapper():
    """Schema for AI call 1: the resume (without custom sections, which made
    the schema too large) plus the unplaced groups."""
    resume_part = simplify(RESUME_SCHEMA)
    del resume_part["properties"]["custom_sections"]
    resume_part["required"].remove("custom_sections")

    defs = resume_part.pop("$defs")
    for name in ("custom_section", "custom_entry", "custom_id"):
        del defs[name]

    unplaced_group = {
        "type": "object",
        "properties": {
            "heading": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "string"}},
            "after": {"type": "string"},
        },
        "required": ["heading", "lines", "after"],
        "additionalProperties": False,
    }

    return {
        "type": "object",
        "$defs": defs,
        "properties": {
            "resume": resume_part,
            "unplaced": {"type": "array", "items": unplaced_group},
        },
        "required": ["resume", "unplaced"],
        "additionalProperties": False,
    }


def build_custom_section_schema():
    """Schema for AI call 2: one custom section's layout and content.
    No id or title: the code sets those from the heading."""
    all_defs = simplify(RESUME_SCHEMA)["$defs"]
    section = all_defs["custom_section"]
    for field in ("id", "title"):
        del section["properties"][field]
        section["required"].remove(field)

    # Only the definitions this schema points to.
    needed = ("custom_entry", "bullet", "text", "date", "skill_group")
    section["$defs"] = {name: all_defs[name] for name in needed}
    return section


RESUME_WRAPPER = build_resume_wrapper()
CUSTOM_SECTION_SCHEMA = build_custom_section_schema()


# ---------------------------------------------------------------------------
# Calling the AI
# ---------------------------------------------------------------------------

def ask_ai(instructions, content, schema):
    """Send one request and return the reply as a dictionary."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=15000,
        system=instructions,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError("the reply was cut off; raise max_tokens")
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to process this document")

    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)

    raise RuntimeError("the reply had no text")


def extract_resume(text):
    """AI call 1: the main resume, plus unplaced groups."""
    return ask_ai(EXTRACTION_INSTRUCTIONS, f"<resume>\n{text}\n</resume>", RESUME_WRAPPER)


def extract_custom_section(group):
    """AI call 2: one unplaced group as a custom section's layout and content."""
    lines = "\n".join(group["lines"])
    content = f"<section>\nHeading: {group['heading']}\n{lines}\n</section>"
    return ask_ai(CUSTOM_SECTION_INSTRUCTIONS, content, CUSTOM_SECTION_SCHEMA)


# ---------------------------------------------------------------------------
# Adding custom sections to the resume
# ---------------------------------------------------------------------------

def make_custom_id(heading, used_ids):
    """'Volunteer Experience' -> 'custom-volunteer-experience', unique."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"
    candidate = f"custom-{slug}"
    number = 2
    while candidate in used_ids:
        candidate = f"custom-{slug}-{number}"
        number += 1
    return candidate


def insert_after(order, item, after):
    """Put item into the order list just after `after`.
    "" means first; an unknown `after` means last."""
    if after == "":
        order.insert(0, item)
    elif after in order:
        order.insert(order.index(after) + 1, item)
    else:
        order.append(item)


def add_custom_sections(resume, unplaced):
    """AI call 2 for each unplaced group, then add the results to the resume.
    Returns the titles of the sections added."""
    custom_sections = resume.setdefault("custom_sections", [])
    order = resume.setdefault("section_order", [])
    used_ids = {section["id"] for section in custom_sections}
    ids_by_heading = {}
    added = []

    for group in unplaced:
        heading = group["heading"].strip()
        # Many resumes write headings in capitals ("VOLUNTEER EXPERIENCE"). The template
        # styles section titles itself, so store them in normal capitalization.
        if heading.isupper():
            heading = heading.title()
        content = extract_custom_section(group)

        section_id = make_custom_id(heading, used_ids)
        used_ids.add(section_id)
        ids_by_heading[heading.lower()] = section_id

        custom_sections.append({"id": section_id, "title": heading, **content})

        # "after" is a built-in section name or an earlier unplaced heading.
        after = group["after"].strip()
        after = ids_by_heading.get(after.lower(), after.lower())
        insert_after(order, section_id, after)
        added.append(heading)

    return added


# ---------------------------------------------------------------------------
# Cleaning and checking
# ---------------------------------------------------------------------------

def remove_empty(value):
    """Remove the "" and [] values the AI used for fields that don't apply."""
    if isinstance(value, dict):
        cleaned = {k: remove_empty(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in ("", [], {})}
    if isinstance(value, list):
        cleaned = [remove_empty(v) for v in value]
        return [v for v in cleaned if v not in ("", [], {})]
    if isinstance(value, str):
        return value.strip()
    return value


def validate_resume(resume):
    """Check against the full schema, plus the rules JSON Schema can't express.
    Returns a list of problems (empty when everything is fine)."""
    validator = jsonschema.Draft202012Validator(
        RESUME_SCHEMA, format_checker=jsonschema.FormatChecker())
    problems = []
    for error in validator.iter_errors(resume):
        where = "/".join(str(part) for part in error.absolute_path) or "(top level)"
        message = error.message if len(error.message) <= 120 else error.message[:117] + "..."
        problems.append(f"{where}: {message}")

    custom_ids = [section.get("id") for section in resume.get("custom_sections", [])]
    if len(custom_ids) != len(set(custom_ids)):
        problems.append("custom_sections: two sections have the same id")
    for name in resume.get("section_order", []):
        if name.startswith("custom-") and name not in custom_ids:
            problems.append(f"section_order: '{name}' has no matching custom section")
        elif not name.startswith("custom-") and name not in BUILTIN_SECTIONS:
            problems.append(f"section_order: unknown section '{name}'")
    for section_id in custom_ids:
        if section_id not in resume.get("section_order", []):
            problems.append(f"custom_sections: '{section_id}' is missing from section_order")
    return problems


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UnsupportedFileError(ValueError):
    """The file is not a PDF or a Word (.docx) file."""


class UnreadableFileError(ValueError):
    """The file couldn't be read, or has no text in it, like a scanned image."""


class AIExtractionError(RuntimeError):
    """The AI call failed, or its answer couldn't be used."""


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

SUPPORTED_TYPES = (".pdf", ".docx")
MIN_TEXT_LENGTH = 100


def extract_pdf_text(source):
    """Return the text of a PDF, reading two-column pages one column at a time.

    Args:
        source: A file path, or a file-like object such as io.BytesIO.

    Returns:
        The text of every page, joined with newlines.
    """
    pages_text = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            pages_text.append(columns.page_text(page))
    return "\n".join(pages_text)


def extract_docx_text(source):
    """Return the text of a Word document: its paragraphs, then its tables.

    Args:
        source: A file path, or a file-like object such as io.BytesIO.

    Returns:
        The text, joined with newlines. Table cells are separated by " | ".
    """
    parts = []
    doc = docx.Document(source)
    for paragraph in doc.paragraphs:
        parts.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join([cell.text for cell in row.cells]))
    return "\n".join(parts)


def read_text(data, filename):
    """Read the text out of an uploaded file's contents.

    Args:
        data: The file's contents, as bytes.
        filename: The file's name, used only for its extension.

    Returns:
        The file's text.

    Raises:
        UnsupportedFileError: If the file isn't a PDF or a .docx file.
        UnreadableFileError: If the file can't be opened, or has too little
            text to be a resume (usually a scanned image).
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_TYPES:
        raise UnsupportedFileError(
            f"unsupported file type '{suffix or 'none'}': please upload a PDF or Word (.docx) file")
    try:
        if suffix == ".pdf":
            text = extract_pdf_text(io.BytesIO(data))
        else:
            text = extract_docx_text(io.BytesIO(data))
    except Exception as e:
        raise UnreadableFileError(f"the file could not be read ({e})") from e
    if len(text.strip()) < MIN_TEXT_LENGTH:
        raise UnreadableFileError("no readable text found. This may be a scanned image; "
                                  "please upload a text-based PDF or a Word file.")
    return text


def extract_from_file(data, filename):
    """Turn an uploaded resume into resume JSON.

    Args:
        data: The file's contents, as bytes.
        filename: The file's name, used only for its extension.

    Returns:
        A dictionary with three keys: "resume", the resume as a dictionary;
        "added_sections", the headings that were added as custom sections;
        and "warnings", ways the resume doesn't fully match the schema. The
        resume is returned even when there are warnings, so the person can fix
        it in the app.

    Raises:
        UnsupportedFileError: If the file isn't a PDF or a .docx file.
        UnreadableFileError: If the file can't be read or has no text.
        AIExtractionError: If the AI call fails or its answer can't be used.
    """
    text = read_text(data, filename)
    try:
        answer = extract_resume(text)
        resume = remove_empty(answer["resume"])
        unplaced = [group for group in answer["unplaced"] if group["lines"]]
        added = add_custom_sections(resume, unplaced)
        resume = remove_empty(resume)
    except Exception as e:
        raise AIExtractionError(f"extraction failed ({e})") from e
    return {"resume": resume, "added_sections": added, "warnings": validate_resume(resume)}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def fail(message):
    """Print an error and exit. Only the command line uses this."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    """Run extraction from the command line: read a file, extract it, and save the JSON."""
    if len(sys.argv) != 3:
        fail("usage: py extract.py <resume.pdf or resume.docx> <output.json>")

    input_path, output_path = sys.argv[1], sys.argv[2]

    try:
        data = Path(input_path).read_bytes()
    except OSError as e:
        fail(f"could not open {input_path} ({e})")

    try:
        result = extract_from_file(data, input_path)
    except (UnsupportedFileError, UnreadableFileError, AIExtractionError) as e:
        fail(str(e))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result["resume"], f, indent=2, ensure_ascii=False)
    print(f"Wrote: {output_path}")

    if result["added_sections"]:
        print("Added as custom sections: " + ", ".join(result["added_sections"]))

    if result["warnings"]:
        print("Warning: the result doesn't fully match the schema:", file=sys.stderr)
        for problem in result["warnings"]:
            print(f"  - {problem}", file=sys.stderr)


if __name__ == "__main__":
    main()