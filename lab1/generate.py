import json
import os
import re
import sys
import time

from google import genai
from google.genai import types, errors

from render import render

MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-flash"]
RETRIES_PER_MODEL = 3


def call_gemini(client, config, description):
    for model in MODELS:
        for attempt in range(RETRIES_PER_MODEL):
            try:
                resp = client.models.generate_content(
                    model=model, contents=description, config=config)
                if resp.text:
                    return resp.text
                print(f"{model}: empty reply, retrying")
            except errors.ServerError as e:                
                wait = 2 ** (attempt + 1)
                print(f"{model}: server error {e.code}, retrying in {wait}s")
                time.sleep(wait)
            except errors.ClientError as e:                 
                if e.code in (404, 429):                    
                    print(f"{model}: error {e.code}, trying the next model")
                    break
                sys.exit(f"Request rejected ({e.code}): {e.message}")
    sys.exit("All models failed. Wait a minute and try again.")


def parse_json(text):
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    return json.loads(text)


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: python generate.py <description.txt> <output.json> [diagram_name]")
    desc_path, json_path = sys.argv[1], sys.argv[2]
    diagram = sys.argv[3] if len(sys.argv) > 3 else "diagram"

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        sys.exit("Set the GEMINI_API_KEY environment variable first.")

    with open("prompt.txt", encoding="utf-8") as f:
        instructions = f.read()
    with open(desc_path, encoding="utf-8") as f:
        description = f.read()

    client = genai.Client()
    config = types.GenerateContentConfig(
        system_instruction=instructions,
        response_mime_type="application/json",
        temperature=0,
    )

    text = call_gemini(client, config, description)
    try:
        data = parse_json(text)
    except json.JSONDecodeError as e:
        print("Gemini did not return valid JSON:", e)
        print(text)
        sys.exit(1)

    if not isinstance(data, dict) or not data.get("entities"):
        print("The JSON has no 'entities' list:")
        print(text)
        sys.exit(1)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    render(json_path, diagram)
    print(f"Saved JSON to {json_path} and diagram to {diagram}.png")


if __name__ == "__main__":
    main()
