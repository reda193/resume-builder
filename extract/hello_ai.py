from dotenv import load_dotenv
import anthropic

load_dotenv()

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-haiku-5-5",
    max_tokens=200,
    messages=[
        {"role": "user", "content": "Say hello."}
    ],
)

for block in response.content:
    if block.type == "text":
        print(block.text)