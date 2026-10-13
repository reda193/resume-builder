"""The profile: everything about one user, with an id on every item.

  resume_to_profile(resume)   an extraction result (resume JSON) -> a new profile
  validate_profile(profile)   check against profile.schema.json, plus rules it can't express
  profile_to_resume(profile)  every item in the profile -> resume JSON (no selection yet)

Tailoring will build on profile_to_resume: instead of every item, it will use the
items the AI picked by id, in the AI's order.
"""
import json
from pathlib import Path

import jsonschema

HERE = Path(__file__).parent
with open(HERE / ".." / "profile.schema.json", encoding="utf-8") as f:
    PROFILE_SCHEMA = json.load(f)

MAX_JOBS = 15      # roles, counted across all companies
MAX_SKILLS = 75    # skills, counted across all groups
BUILTIN_SECTIONS = ["summary", "education", "experience", "projects", "skills"]


# ---------------------------------------------------------------------------
# Resume -> profile
# ---------------------------------------------------------------------------

class IdMaker:
    """Hands out ids like "b1", "b2", "r1": one counter per prefix."""

    def __init__(self):
        self.counters = {}

    def new(self, prefix):
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]}"


def resume_to_profile(resume, source="upload"):
    """Turn an extraction result into a profile: same content, with ids and a source."""
    ids = IdMaker()

    def bullets(texts):
        return [{"id": ids.new("b"), "text": text, "source": source} for text in texts]

    def skill_groups(groups, prefix="sg"):
        return [{"id": ids.new(prefix), "category": group["category"],
                 "items": [{"id": ids.new("sk"), "name": name, "source": source} for name in group["items"]]}
                for group in groups]

    def with_id(item, prefix, list_fields=()):
        """Copy item with a new id; turn the listed text fields into bullet objects."""
        copy = {"id": ids.new(prefix)}
        for key, value in item.items():
            copy[key] = bullets(value) if key in list_fields else value
        return copy

    profile = {
        "schema_version": "1.0",
        "basics": resume["basics"],
        "section_order": resume["section_order"],
    }
    if resume.get("experience"):
        profile["experience"] = [
            {"id": ids.new("co"), **{k: v for k, v in company.items() if k != "roles"},
             "roles": [with_id(role, "r", ["bullets"]) for role in company["roles"]]}
            for company in resume["experience"]]
    if resume.get("education"):
        profile["education"] = [with_id(entry, "ed", ["details"]) for entry in resume["education"]]
    if resume.get("projects"):
        profile["projects"] = [with_id(project, "pr", ["bullets"]) for project in resume["projects"]]
    if resume.get("skills"):
        profile["skills"] = skill_groups(resume["skills"])
    if resume.get("custom_sections"):
        sections = []
        for section in resume["custom_sections"]:
            copy = {k: v for k, v in section.items() if k not in ("entries", "items", "categories")}
            if section.get("entries"):
                copy["entries"] = [with_id(entry, "e", ["bullets"]) for entry in section["entries"]]
            if section.get("items"):
                copy["items"] = [{"id": ids.new("li"), "text": text, "source": source} for text in section["items"]]
            if section.get("categories"):
                copy["categories"] = skill_groups(section["categories"])
            sections.append(copy)
        profile["custom_sections"] = sections
    return profile


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def all_ids(value):
    """Every "id" in the profile, except custom section ids (which use their own format)."""
    found = []
    if isinstance(value, dict):
        if "id" in value and not str(value["id"]).startswith("custom-"):
            found.append(value["id"])
        for child in value.values():
            found += all_ids(child)
    elif isinstance(value, list):
        for child in value:
            found += all_ids(child)
    return found


def validate_profile(profile):
    """Problems with the profile, as a list of messages (empty when it's fine)."""
    validator = jsonschema.Draft202012Validator(PROFILE_SCHEMA, format_checker=jsonschema.FormatChecker())
    problems = []
    for error in validator.iter_errors(profile):
        where = "/".join(str(part) for part in error.absolute_path) or "(top level)"
        if error.validator == "maxItems":
            message = f"{len(error.instance)} items, the limit is {error.validator_value}"
        elif error.validator == "maxLength":
            message = f"{len(error.instance)} characters, the limit is {error.validator_value}"
        else:
            message = error.message if len(error.message) <= 120 else error.message[:117] + "..."
        problems.append(f"{where}: {message}")

    ids = all_ids(profile)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        problems.append(f"ids used more than once: {', '.join(duplicates)}")

    jobs = sum(len(company["roles"]) for company in profile.get("experience", []))
    if jobs > MAX_JOBS:
        problems.append(f"experience: {jobs} jobs, the limit is {MAX_JOBS}")

    skills = sum(len(group["items"]) for group in profile.get("skills", []))
    if skills > MAX_SKILLS:
        problems.append(f"skills: {skills} skills, the limit is {MAX_SKILLS}")

    custom_ids = [section["id"] for section in profile.get("custom_sections", [])]
    for name in profile.get("section_order", []):
        if name.startswith("custom-") and name not in custom_ids:
            problems.append(f"section_order: '{name}' has no matching custom section")
        elif not name.startswith("custom-") and name not in BUILTIN_SECTIONS:
            problems.append(f"section_order: unknown section '{name}'")
    return problems


# ---------------------------------------------------------------------------
# Profile -> resume
# ---------------------------------------------------------------------------

def profile_to_resume(profile):
    """Every item in the profile as resume JSON: ids and sources removed."""

    def texts(items):
        return [item["text"] for item in items]

    def skill_groups(groups):
        return [{"category": group["category"], "items": [skill["name"] for skill in group["items"]]}
                for group in groups]

    def without_id(item, list_fields=()):
        return {key: (texts(value) if key in list_fields else value)
                for key, value in item.items() if key not in ("id", "source")}

    resume = {
        "schema_version": profile["schema_version"],
        "basics": profile["basics"],
        "section_order": profile["section_order"],
    }
    if profile.get("experience"):
        resume["experience"] = [
            {**{k: v for k, v in company.items() if k not in ("id", "roles")},
             "roles": [without_id(role, ["bullets"]) for role in company["roles"]]}
            for company in profile["experience"]]
    if profile.get("education"):
        resume["education"] = [without_id(entry, ["details"]) for entry in profile["education"]]
    if profile.get("projects"):
        resume["projects"] = [without_id(project, ["bullets"]) for project in profile["projects"]]
    if profile.get("skills"):
        resume["skills"] = skill_groups(profile["skills"])
    if profile.get("custom_sections"):
        sections = []
        for section in profile["custom_sections"]:
            copy = {k: v for k, v in section.items() if k not in ("entries", "items", "categories")}
            if section.get("entries"):
                copy["entries"] = [without_id(entry, ["bullets"]) for entry in section["entries"]]
            if section.get("items"):
                copy["items"] = texts(section["items"])
            if section.get("categories"):
                copy["categories"] = skill_groups(section["categories"])
            sections.append(copy)
        resume["custom_sections"] = sections
    return resume


def separate_roles(experience):
    """Give every role its own experience entry, repeating the company name.

    Args:
        experience: A resume experience list, where a company may hold several roles.

    Returns:
        A new experience list with exactly one role per entry, in the same order.
    """
    return [{"company": company["company"], "location": company.get("location", ""), "roles": [role]}
            for company in experience for role in company["roles"]]


def group_roles(experience):
    """Merge consecutive experience entries at the same company into one entry.

    Only neighbouring entries are merged, so someone who left a company and
    returned later keeps separate entries for each stint. Company names are
    compared ignoring case and surrounding spaces; the first entry's name and
    location are kept.

    Args:
        experience: A resume experience list.

    Returns:
        A new experience list with consecutive same-company roles grouped.
    """
    grouped = []
    for company in experience:
        same_as_previous = (grouped and grouped[-1]["company"].strip().lower()
                            == company["company"].strip().lower())
        if same_as_previous:
            grouped[-1]["roles"] = grouped[-1]["roles"] + company["roles"]
        else:
            grouped.append({**company, "roles": list(company["roles"])})
    return grouped