"""Turns an uploaded resume (PDF or Word) into resume JSON.

Usage: py extract.py <resume.pdf or resume.docx> <output.json>

Here's how it works:
  1. Pull the text out of the file. Two-column PDFs are read one column
     at a time, so the text stays in a sensible order.
  2. Ask the AI to copy the resume into our format. Anything it can't fit
     (like volunteer work or certifications) comes back in "unplaced",
     grouped by its heading.
  3. Ask the AI once more for each of those groups, to turn it into a
     custom section.
  4. Put the custom sections back where they were in the original resume.
  5. Tidy up the empty values and check everything against the schema.

The real work happens in extract_from_file. It takes the file as bytes, so
both this command and the API can use it. When something goes wrong it
raises an error instead of quitting, and each kind of problem has its own
error class, so the caller can tell them apart and respond the right way.
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

# The API can't enforce these rules while the AI is writing its answer, so we
# leave them out of the AI's copy of the schema. Nothing is lost:
# validate_resume still checks them against the full schema afterward.
NOT_SUPPORTED = {"maxLength", "minLength", "maxItems", "uniqueItems", "pattern",
                 "$schema", "$id", "format"}


def simplify(node):
    """Make a copy of the schema that the AI can work with.

    It keeps the $refs, so each definition only appears once, drops the rules
    the API can't use, and makes every field required. The AI writes "" or []
    for fields that don't apply, and we clean those up later.
    """
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
    """Build the schema for the first AI call: the resume, plus anything left over.

    Custom sections aren't in it, because including them made the schema too
    big for the API. Instead, the AI puts those sections in "unplaced", and a
    second call handles each one.
    """
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
    """Build the schema for the second AI call: one custom section's layout and content.

    It leaves out the id and title, since our code sets those from the
    section's heading.
    """
    all_defs = simplify(RESUME_SCHEMA)["$defs"]
    section = all_defs["custom_section"]
    for field in ("id", "title"):
        del section["properties"][field]
        section["required"].remove(field)

    # Only bring along the definitions this schema actually uses.
    needed = ("custom_entry", "bullet", "text", "date", "skill_group")
    section["$defs"] = {name: all_defs[name] for name in needed}
    return section


RESUME_WRAPPER = build_resume_wrapper()
CUSTOM_SECTION_SCHEMA = build_custom_section_schema()


# ---------------------------------------------------------------------------
# Talking to the AI
# ---------------------------------------------------------------------------

def ask_ai(instructions, content, schema):
    """Send one request to the AI and give back its answer as a dictionary."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=15000,
        system=instructions,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError("the AI's answer was cut off before it finished")
    if response.stop_reason == "refusal":
        raise RuntimeError("the AI declined to process this document")

    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)

    raise RuntimeError("the AI's answer was empty")


def extract_resume(text):
    """First AI call: copy the resume into our format, and set aside what doesn't fit."""
    return ask_ai(EXTRACTION_INSTRUCTIONS, f"<resume>\n{text}\n</resume>", RESUME_WRAPPER)


def extract_custom_section(group):
    """Second AI call: turn one leftover group into a custom section."""
    lines = "\n".join(group["lines"])
    content = f"<section>\nHeading: {group['heading']}\n{lines}\n</section>"
    return ask_ai(CUSTOM_SECTION_INSTRUCTIONS, content, CUSTOM_SECTION_SCHEMA)


# ---------------------------------------------------------------------------
# Adding custom sections to the resume
# ---------------------------------------------------------------------------

def make_custom_id(heading, used_ids):
    """Turn a heading into a unique id, like 'Volunteer Experience' -> 'custom-volunteer-experience'."""
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "section"
    candidate = f"custom-{slug}"
    number = 2
    while candidate in used_ids:
        candidate = f"custom-{slug}-{number}"
        number += 1
    return candidate


def insert_after(order, item, after):
    """Put an item into the section order, right after another one.

    An empty `after` puts it first. If we don't recognize `after`, it goes
    at the end, so nothing ever gets lost.
    """
    if after == "":
        order.insert(0, item)
    elif after in order:
        order.insert(order.index(after) + 1, item)
    else:
        order.append(item)


def add_custom_sections(resume, unplaced):
    """Turn each leftover group into a custom section and add it to the resume.

    Each section goes back where it appeared in the original resume.

    Returns:
        The titles of the sections we added.
    """
    custom_sections = resume.setdefault("custom_sections", [])
    order = resume.setdefault("section_order", [])
    used_ids = {section["id"] for section in custom_sections}
    ids_by_heading = {}
    added = []

    for group in unplaced:
        heading = group["heading"].strip()
        # Lots of resumes write headings in all caps ("VOLUNTEER EXPERIENCE").
        # The template styles titles on its own, so we store them normally.
        if heading.isupper():
            heading = heading.title()
        content = extract_custom_section(group)

        section_id = make_custom_id(heading, used_ids)
        used_ids.add(section_id)
        ids_by_heading[heading.lower()] = section_id

        custom_sections.append({"id": section_id, "title": heading, **content})

        # "after" is either a built-in section, like "experience", or the
        # heading of a leftover group we've already added.
        after = group["after"].strip()
        after = ids_by_heading.get(after.lower(), after.lower())
        insert_after(order, section_id, after)
        added.append(heading)

    return added


# ---------------------------------------------------------------------------
# Cleaning up and checking
# ---------------------------------------------------------------------------

def remove_empty(value):
    """Strip out the "" and [] values the AI wrote for fields that don't apply."""
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
    """Check the resume against the full schema, plus a few rules the schema can't express.

    Returns:
        A list of problems. It's empty when everything looks good.
    """
    validator = jsonschema.Draft202012Validator(
        RESUME_SCHEMA, format_checker=jsonschema.FormatChecker())
    problems = []
    for error in validator.iter_errors(resume):
        where = "/".join(str(part) for part in error.absolute_path) or "(top level)"
        message = error.message if len(error.message) <= 120 else error.message[:117] + "..."
        problems.append(f"{where}: {message}")

    custom_ids = [section.get("id") for section in resume.get("custom_sections", [])]
    if len(custom_ids) != len(set(custom_ids)):
        problems.append("custom_sections: two sections share the same id")
    for name in resume.get("section_order", []):
        if name.startswith("custom-") and name not in custom_ids:
            problems.append(f"section_order: '{name}' doesn't match any custom section")
        elif not name.startswith("custom-") and name not in BUILTIN_SECTIONS:
            problems.append(f"section_order: '{name}' isn't a section we know about")
    for section_id in custom_ids:
        if section_id not in resume.get("section_order", []):
            problems.append(f"custom_sections: '{section_id}' is missing from section_order")
    return problems


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UnsupportedFileError(ValueError):
    """The file isn't a PDF or a Word (.docx) file."""


class UnreadableFileError(ValueError):
    """We couldn't read the file, or there's no text in it (like a scanned image)."""


class AIExtractionError(RuntimeError):
    """The AI call failed, or we couldn't use what it sent back."""


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

SUPPORTED_TYPES = (".pdf", ".docx")
MIN_TEXT_LENGTH = 100


def extract_pdf_text(source):
    """Get the text out of a PDF, reading two-column pages one column at a time.

    Args:
        source: A path to the file, or the file itself in memory (like io.BytesIO).

    Returns:
        The text of every page, one after another.
    """
    pages_text = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            pages_text.append(columns.page_text(page))
    return "\n".join(pages_text)


def extract_docx_text(source):
    """Get the text out of a Word document: its paragraphs first, then its tables.

    Args:
        source: A path to the file, or the file itself in memory (like io.BytesIO).

    Returns:
        The document's text. Table cells are separated by " | ".
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
    """Get the text out of an uploaded file.

    Args:
        data: The file's contents, as bytes.
        filename: The file's name. We only look at its extension.

    Returns:
        The text in the file.

    Raises:
        UnsupportedFileError: If it isn't a PDF or a .docx file.
        UnreadableFileError: If we can't open it, or it has too little text to
            be a resume. That usually means it's a scanned image.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_TYPES:
        kind = f"{suffix} files" if suffix else "files without an extension"
        raise UnsupportedFileError(f"we can't read {kind}. Please upload a PDF or a Word (.docx) file.")
    try:
        if suffix == ".pdf":
            text = extract_pdf_text(io.BytesIO(data))
        else:
            text = extract_docx_text(io.BytesIO(data))
    except Exception as e:
        raise UnreadableFileError(f"we couldn't open this file. It may be damaged ({e}).") from e
    if len(text.strip()) < MIN_TEXT_LENGTH:
        raise UnreadableFileError("we couldn't find any text in this file. It might be a scanned "
                                  "image. Please upload a PDF with selectable text, or a Word file.")
    return text


def extract_from_file(data, filename):
    """Turn an uploaded resume into resume JSON.

    Args:
        data: The file's contents, as bytes.
        filename: The file's name. We only look at its extension.

    Returns:
        A dictionary with three things in it:
            "resume": the resume itself.
            "added_sections": the headings we turned into custom sections.
            "warnings": anything that doesn't quite match the schema.
        We send the resume back even when there are warnings, so the person
        can fix it up in the app.

    Raises:
        UnsupportedFileError: If it isn't a PDF or a .docx file.
        UnreadableFileError: If we can't read it, or it has no text.
        AIExtractionError: If the AI call fails, or we can't use its answer.
    """
    text = read_text(data, filename)
    try:
        answer = extract_resume(text)
        resume = remove_empty(answer["resume"])
        unplaced = [group for group in answer["unplaced"] if group["lines"]]
        added = add_custom_sections(resume, unplaced)
        resume = remove_empty(resume)
    except Exception as e:
        raise AIExtractionError(f"something went wrong while reading your resume ({e})") from e
    return {"resume": resume, "added_sections": added, "warnings": validate_resume(resume)}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def fail(message):
    """Print an error and quit. Only the command line uses this; the API never should."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    """Run it from the command line: read a file, extract it, and save the JSON."""
    if len(sys.argv) != 3:
        fail("usage: py extract.py <resume.pdf or resume.docx> <output.json>")

    input_path, output_path = sys.argv[1], sys.argv[2]

    try:
        data = Path(input_path).read_bytes()
    except OSError as e:
        fail(f"couldn't open {input_path} ({e})")

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
        print("Heads up: a few things don't quite match the schema:", file=sys.stderr)
        for problem in result["warnings"]:
            print(f"  - {problem}", file=sys.stderr)


if __name__ == "__main__":
    main()