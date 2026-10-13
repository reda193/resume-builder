"""Turn an extraction result into a profile.

Usage: py make_profile.py <resume.json> <profile.json>
"""
import json
import sys

from user_profile import resume_to_profile, validate_profile

with open(sys.argv[1], encoding="utf-8") as f:
    resume = json.load(f)

profile = resume_to_profile(resume)

problems = validate_profile(profile)
for problem in problems:
    print("problem:", problem)

with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(profile, f, indent=2, ensure_ascii=False)
print("Wrote:", sys.argv[2])