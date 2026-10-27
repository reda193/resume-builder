from dotenv import load_dotenv
import anthropic
import json
import sys
from pathlib import Path
load_dotenv()

client = anthropic.Anthropic()

HERE = Path(__file__).parent
INSTRUCTIONS = (HERE / "prompts" / "extraction.md").read_text(encoding="utf-8")



print(wrapper["properties"].keys())



