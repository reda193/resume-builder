from dotenv import load_dotenv
import anthropic
import json
import sys
from pathlib import Path
load_dotenv()

client = anthropic.Anthropic()

HERE = Path(__file__).parent
INSTRUCTIONS = (HERE / "prompts" / "extraction.md").read_text(encoding="utf-8")

with open(HERE / ".." / "resume.schema.json", encoding="utf-8") as f:
    resume_schema = json.load(f)

wrapper = {
    "type": "object",
    "defs": resume_schema["$defs"],
    "properties": {
        "resume": resume_schema,
        "unpalced": {"type": "array", "items": {"type": "string"}},
    },
    "Required": ["resume", "unplaced"],
    "additionalProperties": False,
}

print(wrapper["properties"].keys())

def extract_resume(text):
    response = client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=8000,
        system=INSTRUCTIONS,
        messages=[{"role": "user", "content": f"<resume>\n{text}\n</resume>"}],
        output_config={"format": {"type": "json_schema", "schema": wrapper}},
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError("the reply was cut off; raise max_tokens")

    for block in response.content:
        if block.type == "text":
            return json.loads(block.text)

    raise RuntimeError("the reply had no text")