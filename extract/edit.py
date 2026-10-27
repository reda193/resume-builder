"""Edit one tailored resume with a request in plain words.

Usage:
    py edit.py <resume.json> <profile.json> "<request>" [--lines 2] [--pages 1] [--yes] [--no-build]
    py edit.py <resume.json> --undo [--no-build]

Options:
    --lines     Lines per bullet on the page (1 or 2), for the length check.
    --pages     The page limit to warn about after rebuilding.
    --yes       Apply the changes without asking for confirmation.
    --no-build  Don't rebuild the PDF after saving.
    --undo      Restore the version of the resume from before the last edit.

Only the resume changes; the profile is read for facts but never modified.
The AI returns specific changes, each pointing at one place in the resume,
and every change is checked before it is shown: an edited bullet may only add
facts from the original bullet or the request, and an added bullet may only
use facts from the request or the profile. Before saving, the previous
version is kept in <resume>.history, so every edit can be undone.
"""
import argparse
import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from checks import new_numbers, new_terms, style_problems
from extract import remove_empty, validate_resume
from tailor import CHARS_FOR_LINES, profile_outline, run_build
from user_profile import profile_to_resume

load_dotenv()
client = anthropic.Anthropic()

MODEL = "claude-haiku-5-5"
HERE = Path(__file__).parent
INSTRUCTIONS = ((HERE / "prompts" / "editing.md").read_text(encoding="utf-8")
                + "\n\n# Writing style guide\n\n"
                + (HERE / "prompts" / "writing-style.md").read_text(encoding="utf-8"))
FIX_ROUNDS = 2

ACTIONS = ["edit_bullet", "remove_bullet", "add_bullet", "remove_project",
           "add_skill", "remove_skill", "set_section_order"]
BULLET_PATH = re.compile(r"^/(experience/\d+/roles/\d+|projects/\d+)/bullets/\d+$")
OWNER_PATH = re.compile(r"^/(experience/\d+/roles/\d+|projects/\d+)$")
PROJECT_PATH = re.compile(r"^/projects/\d+$")
SKILL_GROUP_PATH = re.compile(r"^/skills/\d+$")
PATH_RULES = {
    "edit_bullet": BULLET_PATH, "remove_bullet": BULLET_PATH, "add_bullet": OWNER_PATH,
    "remove_project": PROJECT_PATH, "add_skill": SKILL_GROUP_PATH, "remove_skill": SKILL_GROUP_PATH,
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "action": {"enum": ACTIONS},
                "path": {"type": "string"},
                "text": {"type": "string"},
                "order": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action", "path", "text", "order"],
            "additionalProperties": False,
        }},
        "message": {"type": "string"},
    },
    "required": ["changes", "message"],
    "additionalProperties": False,
}


def resume_outline(resume):
    """Format a resume as an outline that shows the path of every editable item.

    Args:
        resume: A resume dictionary matching resume.schema.json.

    Returns:
        The outline as a single string.
    """
    lines = [f"section_order: {', '.join(resume['section_order'])}"]
    for c, company in enumerate(resume.get("experience", [])):
        for r, role in enumerate(company["roles"]):
            dates = " – ".join(d for d in (role.get("start"), role.get("end")) if d)
            lines.append(f"[/experience/{c}/roles/{r}] {role['title']}, {company['company']}, {dates}")
            for b, bullet in enumerate(role.get("bullets", [])):
                lines.append(f"    [/experience/{c}/roles/{r}/bullets/{b}] {bullet}")
    for p, project in enumerate(resume.get("projects", [])):
        lines.append(f"[/projects/{p}] {project['name']} ({', '.join(project.get('tech', []))})")
        for b, bullet in enumerate(project.get("bullets", [])):
            lines.append(f"    [/projects/{p}/bullets/{b}] {bullet}")
    for g, group in enumerate(resume.get("skills", [])):
        lines.append(f"[/skills/{g}] {group['category']}: {', '.join(group['items'])}")
    return "\n".join(lines)


def get_item(resume, path):
    """Return the value at a path like /experience/0/roles/1/bullets/2.

    Args:
        resume: A resume dictionary.
        path: A path made of keys and list positions, separated by slashes.

    Returns:
        The value at that path, or None if the path doesn't exist.
    """
    value = resume
    for part in path.strip("/").split("/"):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def ask_for_changes(messages):
    """Send a conversation to the AI and return its list of changes.

    Args:
        messages: The conversation so far.

    Returns:
        The AI's answer as a dictionary matching EDIT_SCHEMA.

    Raises:
        RuntimeError: If the reply was cut off, refused, or had no text.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=INSTRUCTIONS,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": EDIT_SCHEMA}},
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the reply was cut off")
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)
    raise RuntimeError("the reply had no text")


def change_problems(change, resume, profile, request, limit):
    """Check one change for wrong paths, invented facts, length and style.

    Args:
        change: One change from the AI's answer.
        resume: The resume the change applies to.
        profile: The person's profile, the only allowed source of new facts
            besides the request.
        request: The person's request, in their own words.
        limit: The character limit for a bullet.

    Returns:
        A list of problems, phrased so the AI can act on them.
    """
    action, path, text = change["action"], change["path"], change["text"]
    label = f"{action} at {path}" if path else action

    if action == "set_section_order":
        if sorted(change["order"]) != sorted(resume["section_order"]):
            return [f"{label}: the order must contain exactly these sections: "
                    f"{', '.join(resume['section_order'])}"]
        return []

    if not PATH_RULES[action].match(path) or get_item(resume, path) is None:
        return [f"{label}: there is no matching item at this path"]

    if action in ("remove_bullet", "remove_project"):
        return []

    if action == "remove_skill":
        names = [name.lower() for name in get_item(resume, path)["items"]]
        return [] if text.lower() in names else [f"{label}: there is no skill called '{text}' in this group"]

    profile_content = json.dumps(profile_to_resume(profile), ensure_ascii=False)
    if action == "add_skill":
        known = text.lower() in profile_content.lower() or text.lower() in request.lower()
        return [] if known else [f"{label}: '{text}' is not in the profile or the request"]

    source = get_item(resume, path) + " " + request if action == "edit_bullet" else profile_content + " " + request
    problems = []
    if len(text) > limit:
        problems.append(f"{label}: {len(text)} characters, the limit is {limit}. Shorten it.")
    for number in new_numbers(text, source):
        problems.append(f"{label}: the number {number} is not in the original or the request. Remove it.")
    for term in new_terms(text, source):
        problems.append(f"{label}: '{term}' is not in the original or the request. Remove it.")
    for tell in style_problems(text):
        problems.append(f"{label}: contains '{tell}', which the style guide bans")
    return problems


def find_problems(answer, resume, profile, request, limit):
    """Check every change in the AI's answer.

    Args:
        answer: The AI's answer, matching EDIT_SCHEMA.
        resume: The resume the changes apply to.
        profile: The person's profile.
        request: The person's request.
        limit: The character limit for a bullet.

    Returns:
        A list of problems for all changes; empty if every change passes.
    """
    problems = []
    for change in answer["changes"]:
        problems += change_problems(change, resume, profile, request, limit)
    return problems


def apply_changes(resume, changes):
    """Apply a list of changes to a copy of the resume.

    Edits and additions are applied first, then removals, so removing an item
    never shifts the position of another item a change refers to.

    Args:
        resume: A resume dictionary.
        changes: Changes that have already passed the checks.

    Returns:
        The edited copy, with emptied lists and skill groups removed.
    """
    edited = copy.deepcopy(resume)
    removed = object()

    def parent_and_index(path):
        """Return the list holding the item at path, and the item's position in it."""
        parts = path.strip("/").split("/")
        return get_item(edited, "/".join(parts[:-1])), int(parts[-1])

    for change in changes:
        action, path, text = change["action"], change["path"], change["text"]
        if action == "edit_bullet":
            items, index = parent_and_index(path)
            items[index] = text
        elif action == "add_bullet":
            get_item(edited, path).setdefault("bullets", []).append(text)
        elif action == "add_skill":
            get_item(edited, path)["items"].append(text)
        elif action == "set_section_order":
            edited["section_order"] = list(change["order"])

    for change in changes:
        action, path, text = change["action"], change["path"], change["text"]
        if action in ("remove_bullet", "remove_project"):
            items, index = parent_and_index(path)
            items[index] = removed
        elif action == "remove_skill":
            group = get_item(edited, path)
            group["items"] = [name for name in group["items"] if name.lower() != text.lower()]

    for company in edited.get("experience", []):
        for role in company["roles"]:
            role["bullets"] = [b for b in role.get("bullets", []) if b is not removed]
    edited["projects"] = [p for p in edited.get("projects", []) if p is not removed]
    for project in edited["projects"]:
        project["bullets"] = [b for b in project.get("bullets", []) if b is not removed]
    edited["skills"] = [g for g in edited.get("skills", []) if g["items"]]
    return remove_empty(edited)


def describe(change, resume):
    """Describe one change in plain words, with the text before and after.

    Args:
        change: One change from the AI's answer.
        resume: The resume before the change.

    Returns:
        A short multi-line description.
    """
    action, path, text = change["action"], change["path"], change["text"]
    if action == "edit_bullet":
        return f"Edit {path}\n    before: {get_item(resume, path)}\n    after:  {text}"
    if action == "remove_bullet":
        return f"Remove {path}\n    {get_item(resume, path)}"
    if action == "add_bullet":
        owner = get_item(resume, path)
        return f"Add a bullet to {owner.get('title') or owner.get('name')}\n    {text}"
    if action == "remove_project":
        return f"Remove the project {get_item(resume, path)['name']}"
    if action == "add_skill":
        return f"Add '{text}' to {get_item(resume, path)['category']}"
    if action == "remove_skill":
        return f"Remove '{text}' from {get_item(resume, path)['category']}"
    return f"Reorder sections: {', '.join(change['order'])}"


def history_folder(resume_path):
    """Return the folder holding earlier versions of a resume."""
    return Path(str(resume_path) + ".history")


def save_backup(resume_path):
    """Copy the current resume into its history folder before it changes.

    Args:
        resume_path: The resume JSON file.

    Returns:
        The path of the backup.
    """
    folder = history_folder(resume_path)
    folder.mkdir(exist_ok=True)
    backup = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    backup.write_text(Path(resume_path).read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def undo(resume_path):
    """Restore the most recent backup of a resume, and remove it from the history.

    Args:
        resume_path: The resume JSON file.

    Returns:
        True if a version was restored, False if there was none to restore.
    """
    backups = sorted(history_folder(resume_path).glob("*.json"))
    if not backups:
        return False
    Path(resume_path).write_text(backups[-1].read_text(encoding="utf-8"), encoding="utf-8")
    backups[-1].unlink()
    return True


def rebuild(resume_path, pages):
    """Rebuild the resume's PDF and report its page count.

    Args:
        resume_path: The resume JSON file. The PDF is written next to it.
        pages: The page limit to warn about.
    """
    try:
        report = run_build(resume_path, Path(resume_path).with_suffix(".pdf"))
    except (RuntimeError, ValueError) as e:
        print(f"Warning: the PDF was not rebuilt ({e})", file=sys.stderr)
        return
    print(f"Rebuilt: {Path(resume_path).with_suffix('.pdf')} ({report['pages']} page(s))")
    for problem in report["problems"]:
        print(f"Warning: {problem}", file=sys.stderr)
    if report["pages"] > pages:
        print(f"Warning: the resume is now {report['pages']} pages, over the limit of {pages}", file=sys.stderr)


def main():
    """Run a chat edit from the command line: ask, check, confirm, save, and rebuild."""
    parser = argparse.ArgumentParser(description="Edit a tailored resume with a request in plain words.")
    parser.add_argument("resume")
    parser.add_argument("profile", nargs="?")
    parser.add_argument("request", nargs="?")
    parser.add_argument("--lines", type=int, choices=[1, 2], default=2, help="lines per bullet")
    parser.add_argument("--pages", type=int, default=1, help="page limit to warn about")
    parser.add_argument("--yes", action="store_true", help="apply without asking")
    parser.add_argument("--no-build", action="store_true", help="don't rebuild the PDF")
    parser.add_argument("--undo", action="store_true", help="restore the version before the last edit")
    args = parser.parse_args()

    if args.undo:
        if not undo(args.resume):
            print("Nothing to undo.")
            return
        print(f"Restored the previous version of {args.resume}")
        if not args.no_build:
            rebuild(args.resume, args.pages)
        return

    if not args.profile or not args.request:
        parser.error("a profile and a request are required, unless you use --undo")

    try:
        with open(args.resume, encoding="utf-8") as f:
            resume = json.load(f)
        with open(args.profile, encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    limit = CHARS_FOR_LINES[args.lines]
    request = (f"<request>\n{args.request}\n</request>\n\n"
               f"<settings>\nEach bullet: at most {limit} characters. One line holds about "
               f"{CHARS_FOR_LINES[1]} characters.\n</settings>\n\n"
               f"<resume>\n{resume_outline(resume)}\n</resume>\n\n"
               f"<profile>\n{profile_outline(profile)}\n</profile>")
    messages = [{"role": "user", "content": request}]

    try:
        answer = ask_for_changes(messages)
        problems = find_problems(answer, resume, profile, args.request, limit)
        for _ in range(FIX_ROUNDS):
            if not problems:
                break
            messages += [
                {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
                {"role": "user", "content": "The checks found these problems. Return the full answer again "
                                            "with them fixed:\n- " + "\n- ".join(problems)},
            ]
            answer = ask_for_changes(messages)
            problems = find_problems(answer, resume, profile, args.request, limit)
    except Exception as e:
        print(f"error: editing failed ({e})", file=sys.stderr)
        sys.exit(1)

    good = [c for c in answer["changes"] if not change_problems(c, resume, profile, args.request, limit)]
    rejected = [c for c in answer["changes"] if c not in good]
    if good or not rejected:
        print(answer["message"])
    for change in rejected:
        print(f"Skipped a suggested change that failed the checks: {change['action']} at {change['path']}",
              file=sys.stderr)
        for problem in change_problems(change, resume, profile, args.request, limit):
            print(f"  - {problem}", file=sys.stderr)
    if not good:
        print("No changes were made." if rejected else "No changes to make.")
        return

    print()
    for number, change in enumerate(good, start=1):
        print(f"{number}. {describe(change, resume)}")
    print()

    if not args.yes and input("Apply these changes? [y/N] ").strip().lower() != "y":
        print("Nothing changed.")
        return

    edited = apply_changes(resume, good)
    invalid = validate_resume(edited)
    if invalid:
        print("error: the edited resume would not be valid, so nothing was saved:", file=sys.stderr)
        for problem in invalid:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)

    save_backup(args.resume)
    with open(args.resume, "w", encoding="utf-8") as f:
        json.dump(edited, f, indent=2, ensure_ascii=False)
    print(f"Saved: {args.resume} (undo with: py edit.py {args.resume} --undo)")
    if not args.no_build:
        rebuild(args.resume, args.pages)


if __name__ == "__main__":
    main()