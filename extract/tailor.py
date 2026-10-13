"""Tailor a profile to one job posting.

Usage:
    py tailor.py <profile.json> <job.txt> <output.json> [--lines 2] [--bullets 3] [--projects 2]
                 [--group-roles]

Options:
    --lines     Lines per bullet on the page (1 or 2).
    --bullets   Most bullets per job and per project.
    --projects  Most projects.
    --group-roles  Show several roles at the same company under one company
                   heading. By default, every role gets its own entry.

Outputs:
    <output.json>              The tailored resume, ready for the Go renderer.
    <output>.cover-letter.txt  The cover letter.

The AI only picks items by id and rewrites bullet wording. Company names, job
titles, dates and locations are copied from the profile by code, so the AI
cannot change them. Every rewritten bullet is checked against its original,
and any bullet that still fails the checks falls back to its original wording.

Constants:
    CHARS_FOR_LINES: Character limit per bullet for each lines-per-bullet
        setting, measured in Jake's template (one line holds about 108
        characters) with a safety margin.
    FIX_ROUNDS: How many times the AI may fix problems the checks found.
"""
import argparse
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from checks import new_numbers, new_terms, numbers_in, style_problems
from extract import remove_empty, validate_resume
from user_profile import group_roles, profile_to_resume, separate_roles

load_dotenv()
client = anthropic.Anthropic()

MODEL = "claude-haiku-5-5"
HERE = Path(__file__).parent
INSTRUCTIONS = ((HERE / "prompts" / "tailoring.md").read_text(encoding="utf-8")
                + "\n\n# Writing style guide\n\n"
                + (HERE / "prompts" / "writing-style.md").read_text(encoding="utf-8"))

CHARS_FOR_LINES = {1: 100, 2: 210}
FIX_ROUNDS = 2
BUILTIN_SECTIONS = ["education", "experience", "projects", "skills"]

TAILORED_BULLET = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The id of the original bullet in the profile."},
        "text": {"type": "string", "description": "The bullet, reworded for this job."},
        "rank": {"type": "integer", "description": "1 is the strongest match for this job."},
    },
    "required": ["id", "text", "rank"],
    "additionalProperties": False,
}

REQUIREMENT = {
    "type": "object",
    "properties": {
        "requirement": {"type": "string", "description": "One thing the posting asks for, in its own words."},
        "bullet_ids": {"type": "array", "items": {"type": "string"},
                       "description": "Ids of every profile bullet that shows evidence of it."},
        "skill_ids": {"type": "array", "items": {"type": "string"},
                      "description": "Ids of every profile skill that relates to it."},
    },
    "required": ["requirement", "bullet_ids", "skill_ids"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {"type": "array", "items": REQUIREMENT,
                         "description": "Filled in first: the posting's requirements and the evidence for each."},
        "section_order": {"type": "array", "items": {"type": "string"}},
        "roles": {"type": "array", "items": {
            "type": "object",
            "properties": {"role_id": {"type": "string"},
                           "bullets": {"type": "array", "items": TAILORED_BULLET}},
            "required": ["role_id", "bullets"], "additionalProperties": False}},
        "projects": {"type": "array", "items": {
            "type": "object",
            "properties": {"project_id": {"type": "string"},
                           "bullets": {"type": "array", "items": TAILORED_BULLET}},
            "required": ["project_id", "bullets"], "additionalProperties": False}},
        "skills": {"type": "array", "items": {"type": "string"}, "description": "Skill ids, most relevant first."},
        "cover_letter": {
            "type": "object",
            "properties": {"company": {"type": "string"}, "team": {"type": "string"}, "role": {"type": "string"},
                           "paragraphs": {"type": "array", "items": {"type": "string"}}},
            "required": ["company", "team", "role", "paragraphs"], "additionalProperties": False},
        "notes": {
            "type": "object",
            "properties": {"strong_matches": {"type": "array", "items": {"type": "string"}},
                           "gaps": {"type": "array", "items": {"type": "string"}}},
            "required": ["strong_matches", "gaps"], "additionalProperties": False},
    },
    "required": ["requirements", "section_order", "roles", "projects", "skills", "cover_letter", "notes"],
    "additionalProperties": False,
}


def profile_outline(profile):
    """Format a profile as a readable outline with ids, for the AI.

    The outline is shorter than the profile's JSON and easier for the AI to
    read. Education and custom sections are listed for context only, since
    tailoring copies them unchanged.

    Args:
        profile: A profile dictionary matching profile.schema.json.

    Returns:
        The outline as a single string.
    """
    lines = []
    if profile.get("experience"):
        lines.append("EXPERIENCE")
        for company in profile["experience"]:
            lines.append(f"  {company['company']} ({company.get('location', '')})")
            for role in company["roles"]:
                dates = " – ".join(d for d in (role.get("start"), role.get("end")) if d)
                lines.append(f"    [{role['id']}] {role['title']}, {dates}")
                for bullet in role.get("bullets", []):
                    lines.append(f"      [{bullet['id']}] {bullet['text']}")
    if profile.get("projects"):
        lines.append("PROJECTS")
        for project in profile["projects"]:
            tech = ", ".join(project.get("tech", []))
            lines.append(f"  [{project['id']}] {project['name']} ({tech})")
            for bullet in project.get("bullets", []):
                lines.append(f"      [{bullet['id']}] {bullet['text']}")
    if profile.get("education"):
        lines.append("EDUCATION (always included, as is)")
        for entry in profile["education"]:
            lines.append(f"  {entry.get('degree', '')}, {entry['school']}, {entry.get('end', '')}")
    if profile.get("skills"):
        lines.append("SKILLS")
        for group in profile["skills"]:
            items = ", ".join(f"[{skill['id']}] {skill['name']}" for skill in group["items"])
            lines.append(f"  {group['category']}: {items}")
    for section in profile.get("custom_sections", []):
        lines.append(f"{section['title'].upper()} (custom section {section['id']}, always included, as is)")
    return "\n".join(lines)


def ask_ai(messages):
    """Send a conversation to the AI and return its structured answer.

    Args:
        messages: The conversation so far, as a list of message dictionaries.

    Returns:
        The AI's answer as a dictionary matching OUTPUT_SCHEMA.

    Raises:
        RuntimeError: If the reply was cut off, refused, or had no text.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=INSTRUCTIONS,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the reply was cut off; raise max_tokens")
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to tailor this resume")
    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)
    raise RuntimeError("the reply had no text")


def index_profile(profile):
    """Build lookup tables for the items in a profile.

    Args:
        profile: A profile dictionary matching profile.schema.json.

    Returns:
        A tuple (roles, projects, bullets, skills) of dictionaries keyed by id.
        roles, projects and skills map ids to the profile's items. bullets maps
        each bullet id to a tuple (owner id, original text), where the owner is
        the role or project the bullet belongs to.
    """
    roles, projects, bullets, skills = {}, {}, {}, {}
    for company in profile.get("experience", []):
        for role in company["roles"]:
            roles[role["id"]] = role
            for bullet in role.get("bullets", []):
                bullets[bullet["id"]] = (role["id"], bullet["text"])
    for project in profile.get("projects", []):
        projects[project["id"]] = project
        for bullet in project.get("bullets", []):
            bullets[bullet["id"]] = (project["id"], bullet["text"])
    for group in profile.get("skills", []):
        for skill in group["items"]:
            skills[skill["id"]] = skill
    return roles, projects, bullets, skills


def find_problems(answer, profile, settings):
    """Check the AI's answer against the profile, the settings and the style guide.

    Every bullet and skill id cited as evidence in the requirements map must
    exist in the profile. Each chosen bullet is compared with its own original:
    it must not add numbers or tool names, must fit the length limit, and must
    not contain banned phrases. The cover letter must not contain numbers absent
    from the profile, banned phrases or exclamation marks.

    Args:
        answer: The AI's answer, matching OUTPUT_SCHEMA.
        profile: The profile the answer was based on.
        settings: A dictionary with "lines", "bullets" and "projects".

    Returns:
        A list of problems, each phrased so the AI can act on it. Empty if the
        answer passes every check.
    """
    roles, projects, bullets, skills = index_profile(profile)
    limit = CHARS_FOR_LINES[settings["lines"]]
    problems = []
    used = set()

    def check_bullets(owner_id, owner_kind, items, max_count):
        """Check the bullets chosen for one role or project, adding to problems.

        Args:
            owner_id: The id of the role or project.
            owner_kind: "role" or "project", used in messages.
            items: The tailored bullets chosen for it.
            max_count: The most bullets allowed.
        """
        if len(items) > max_count:
            problems.append(f"{owner_kind} {owner_id}: {len(items)} bullets, the limit is {max_count}")
        for item in items:
            bid, text = item["id"], item["text"]
            if bid not in bullets:
                problems.append(f"bullet {bid}: no bullet with this id exists in the profile")
                continue
            if bullets[bid][0] != owner_id:
                problems.append(f"bullet {bid}: belongs to {bullets[bid][0]}, not {owner_id}")
            if bid in used:
                problems.append(f"bullet {bid}: used more than once")
            used.add(bid)
            original = bullets[bid][1]
            if len(text) > limit:
                problems.append(f"bullet {bid}: {len(text)} characters, the limit is {limit}. Shorten it.")
            for number in new_numbers(text, original):
                problems.append(f"bullet {bid}: the number {number} is not in the original bullet. Remove it.")
            for term in new_terms(text, original):
                problems.append(f"bullet {bid}: '{term}' is not in the original bullet. Remove it "
                                f"unless the original says the same thing in other words.")
            for tell in style_problems(text):
                problems.append(f"bullet {bid}: contains '{tell}', which the style guide bans")

    for requirement in answer["requirements"]:
        for bid in requirement["bullet_ids"]:
            if bid not in bullets:
                problems.append(f"requirement '{requirement['requirement']}': bullet {bid} does not exist "
                                f"in the profile")
        for sid in requirement["skill_ids"]:
            if sid not in skills:
                problems.append(f"requirement '{requirement['requirement']}': skill {sid} does not exist "
                                f"in the profile")

    for entry in answer["roles"]:
        if entry["role_id"] not in roles:
            problems.append(f"role {entry['role_id']}: no role with this id exists in the profile")
        check_bullets(entry["role_id"], "role", entry["bullets"], settings["bullets"])
    if len(answer["projects"]) > settings["projects"]:
        problems.append(f"{len(answer['projects'])} projects, the limit is {settings['projects']}")
    for entry in answer["projects"]:
        if entry["project_id"] not in projects:
            problems.append(f"project {entry['project_id']}: no project with this id exists in the profile")
        check_bullets(entry["project_id"], "project", entry["bullets"], settings["bullets"])

    for sid in answer["skills"]:
        if sid not in skills:
            problems.append(f"skill {sid}: no skill with this id exists in the profile")

    custom_ids = [section["id"] for section in profile.get("custom_sections", [])]
    for name in answer["section_order"]:
        if name not in BUILTIN_SECTIONS and name not in custom_ids:
            problems.append(f"section_order: '{name}' is not a section of this profile")

    profile_numbers = numbers_in(json.dumps(profile_to_resume(profile), ensure_ascii=False))
    for i, paragraph in enumerate(answer["cover_letter"]["paragraphs"], start=1):
        for number in sorted(numbers_in(paragraph) - profile_numbers):
            problems.append(f"cover letter paragraph {i}: the number {number} is not in the profile. Remove it.")
        for tell in style_problems(paragraph):
            problems.append(f"cover letter paragraph {i}: contains '{tell}', which the style guide bans")
        if "!" in paragraph:
            problems.append(f"cover letter paragraph {i}: no exclamation marks")
    return problems


def tailor(profile, job, settings):
    """Ask the AI to tailor a profile, then let it fix any problems the checks find.

    Args:
        profile: A profile dictionary matching profile.schema.json.
        job: The job posting, as plain text.
        settings: A dictionary with "lines", "bullets" and "projects".

    Returns:
        A tuple (answer, problems): the AI's final answer, and the problems
        still present after FIX_ROUNDS rounds of fixing.
    """
    limit = CHARS_FOR_LINES[settings["lines"]]
    request = (
        f"<settings>\nEach bullet: at most {limit} characters ({settings['lines']} line(s) on the page).\n"
        f"At most {settings['bullets']} bullets per job and per project. At most {settings['projects']} projects.\n"
        f"</settings>\n\n<profile>\n{profile_outline(profile)}\n</profile>\n\n<job_posting>\n{job}\n</job_posting>"
    )
    messages = [{"role": "user", "content": request}]
    answer = ask_ai(messages)
    problems = find_problems(answer, profile, settings)

    for _ in range(FIX_ROUNDS):
        if not problems:
            break
        print(f"Fixing {len(problems)} problem(s)...", file=sys.stderr)
        messages += [
            {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
            {"role": "user", "content": "The checks found these problems. Return the full answer again "
                                        "with them fixed, changing nothing else:\n- " + "\n- ".join(problems)},
        ]
        answer = ask_ai(messages)
        problems = find_problems(answer, profile, settings)
    return answer, problems


def assemble(profile, answer, settings):
    """Build the tailored resume from the profile and the AI's choices.

    Facts (companies, titles, dates, locations) come from the profile; only the
    choice of items and the bullet wording come from the AI. Every job in the
    profile stays on the resume, even if the AI left it out, so the work history
    has no unexplained gaps. Each role gets its own entry, unless
    settings["group_roles"] is true, in which case consecutive roles at the same
    company share one company heading. Skills keep their profile groups, ordered by the
    AI's relevance ranking. Education and custom sections are copied unchanged,
    and any section the AI forgot to list in section_order is appended.

    Args:
        profile: A profile dictionary matching profile.schema.json.
        answer: The AI's answer, matching OUTPUT_SCHEMA.
        settings: A dictionary with "lines", "bullets", "projects" and, optionally,
            "group_roles".

    Returns:
        A resume dictionary matching resume.schema.json, with empty values removed.
    """
    roles, projects, bullets, skills = index_profile(profile)
    limit = CHARS_FOR_LINES[settings["lines"]]

    def safe_text(item):
        """Return a bullet's rewritten text, or its original if the rewrite fails a check.

        Args:
            item: A tailored bullet from the AI's answer.

        Returns:
            The text to use, or None if the bullet id isn't in the profile.
        """
        original = bullets.get(item["id"], (None, None))[1]
        text = item["text"]
        if original is None:
            return None
        honest = not new_numbers(text, original) and not new_terms(text, original)
        if honest and len(text) <= limit and not style_problems(text):
            return text
        return original

    def chosen_bullets(owner_id, items):
        """Return the texts of a role's or project's chosen bullets, best rank first.

        Args:
            owner_id: The id of the role or project.
            items: The tailored bullets the AI chose for it.

        Returns:
            Up to settings["bullets"] bullet texts.
        """
        picked = [item for item in items if bullets.get(item["id"], (None,))[0] == owner_id]
        picked.sort(key=lambda item: item["rank"])
        texts = [safe_text(item) for item in picked[:settings["bullets"]]]
        return [text for text in texts if text]

    chosen_roles = {entry["role_id"]: entry for entry in answer["roles"] if entry["role_id"] in roles}
    experience = []
    for company in profile.get("experience", []):
        company_roles = []
        for role in company["roles"]:
            items = chosen_roles.get(role["id"], {"bullets": []})["bullets"]
            company_roles.append({
                "title": role["title"], "start": role.get("start", ""), "end": role.get("end", ""),
                "bullets": chosen_bullets(role["id"], items),
            })
        experience.append({"company": company["company"], "location": company.get("location", ""),
                           "roles": company_roles})
    experience = group_roles(experience) if settings.get("group_roles") else separate_roles(experience)

    chosen_projects = [entry for entry in answer["projects"] if entry["project_id"] in projects]
    project_list = []
    for entry in chosen_projects[:settings["projects"]]:
        project = projects[entry["project_id"]]
        project_list.append({
            "name": project["name"], "tech": project.get("tech", []), "link": project.get("link", ""),
            "start": project.get("start", ""), "end": project.get("end", ""),
            "bullets": chosen_bullets(project["id"], entry["bullets"]),
        })

    chosen_skill_ids = [sid for sid in answer["skills"] if sid in skills]
    ranked_groups = []
    for group in profile.get("skills", []):
        group_ids = [sid for sid in chosen_skill_ids if sid in {skill["id"] for skill in group["items"]}]
        if group_ids:
            best_position = chosen_skill_ids.index(group_ids[0])
            names = [skills[sid]["name"] for sid in group_ids]
            ranked_groups.append((best_position, {"category": group["category"], "items": names}))
    skill_groups = [group for _, group in sorted(ranked_groups, key=lambda pair: pair[0])]

    custom_ids = [section["id"] for section in profile.get("custom_sections", [])]
    order = [name for name in answer["section_order"] if name in BUILTIN_SECTIONS or name in custom_ids]
    for name in profile["section_order"]:
        if name not in order:
            order.append(name)

    base = profile_to_resume(profile)
    resume = {
        "schema_version": "1.0",
        "basics": profile["basics"],
        "section_order": order,
        "education": base.get("education", []),
        "experience": experience,
        "projects": project_list,
        "skills": skill_groups,
        "custom_sections": base.get("custom_sections", []),
    }
    return remove_empty(resume)


def cover_letter_text(answer, profile):
    """Format the AI's cover letter as plain text, with a greeting and sign-off.

    Args:
        answer: The AI's answer, matching OUTPUT_SCHEMA.
        profile: The profile, for the sign-off name.

    Returns:
        The full letter as a string.
    """
    letter = answer["cover_letter"]
    body = "\n\n".join(letter["paragraphs"])
    return f"Dear Hiring Team at {letter['company']},\n\n{body}\n\nSincerely,\n{profile['basics']['name']}\n"


def main():
    """Run tailoring from the command line: read inputs, tailor, and write the outputs."""
    parser = argparse.ArgumentParser(description="Tailor a profile to a job posting.")
    parser.add_argument("profile")
    parser.add_argument("job")
    parser.add_argument("output")
    parser.add_argument("--lines", type=int, choices=[1, 2], default=2, help="lines per bullet")
    parser.add_argument("--bullets", type=int, default=3, help="most bullets per job")
    parser.add_argument("--projects", type=int, default=2, help="most projects")
    parser.add_argument("--group-roles", action="store_true",
                        help="show several roles at the same company under one company heading")
    args = parser.parse_args()
    settings = {"lines": args.lines, "bullets": args.bullets, "projects": args.projects,
                "group_roles": args.group_roles}

    try:
        with open(args.profile, encoding="utf-8") as f:
            profile = json.load(f)
        job = Path(args.job).read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        answer, problems = tailor(profile, job, settings)
    except Exception as e:
        print(f"error: tailoring failed ({e})", file=sys.stderr)
        sys.exit(1)

    resume = assemble(profile, answer, settings)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(resume, f, indent=2, ensure_ascii=False)
    letter_path = Path(args.output).with_suffix(".cover-letter.txt")
    letter_path.write_text(cover_letter_text(answer, profile), encoding="utf-8")
    print(f"Wrote: {args.output}")
    print(f"Wrote: {letter_path}")

    covered = [r for r in answer["requirements"] if r["bullet_ids"]]
    print(f"\nRequirements with evidence: {len(covered)} of {len(answer['requirements'])}")
    for requirement in answer["requirements"]:
        mark = "+" if requirement["bullet_ids"] else "-"
        evidence = ", ".join(requirement["bullet_ids"]) or "no evidence"
        print(f"  {mark} {requirement['requirement']} ({evidence})")

    print("\nStrong matches:")
    for item in answer["notes"]["strong_matches"]:
        print(f"  + {item}")
    print("Gaps:")
    for item in answer["notes"]["gaps"]:
        print(f"  - {item}")

    if problems:
        print("\nWarning: these problems remained after fixing; affected bullets use their original wording:",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    for problem in validate_resume(resume):
        print(f"Warning: {problem}", file=sys.stderr)


if __name__ == "__main__":
    main()