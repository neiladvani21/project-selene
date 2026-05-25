"""
test_models.py — Run the reporter against multiple OpenRouter models in parallel
and save outputs to /rover/output/model_comparison/ for side-by-side review.

DELETE THIS FILE after picking a model.

Usage (from inside rover container):
    python test_models.py

Or from host (requires map.json already generated):
    LLM_API_KEY=sk-or-... python rover/test_models.py

Models to compare — edit this list freely:
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

# Add rover dir to path so we can import reporter helpers
sys.path.insert(0, os.path.dirname(__file__))
from reporter import build_mermaid, build_priority_table, build_prompt

MAP_PATH = "/rover/output/map.json"
OUTPUT_DIR = "/rover/output/model_comparison"

MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-v4-flash:free",
    # "nvidia/nemotron-3-super-120b-a12b:free",  # uncomment to add more
    # "openai/gpt-oss-120b:free",
]


def safe_filename(model: str) -> str:
    return model.replace("/", "_").replace(":", "_")


def run_model(model: str, prompt: str, api_key: str) -> dict:
    start = time.time()
    print(f"[{model}] starting...", flush=True)
    try:
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content
        elapsed = round(time.time() - start, 1)
        print(f"[{model}] done in {elapsed}s ({len(content)} chars)", flush=True)
        return {"model": model, "content": content, "elapsed": elapsed, "error": None}
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        print(f"[{model}] ERROR after {elapsed}s: {e}", flush=True)
        return {"model": model, "content": None, "elapsed": elapsed, "error": str(e)}


def main():
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("ERROR: LLM_API_KEY not set", flush=True)
        sys.exit(1)

    print(f"[test_models] reading {MAP_PATH}", flush=True)
    try:
        with open(MAP_PATH) as f:
            map_data = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {MAP_PATH} not found — run mapping first (POST /map)", flush=True)
        sys.exit(1)

    print("[test_models] building prompt", flush=True)
    mermaid = build_mermaid(map_data)
    priority_table, findings = build_priority_table(map_data)
    prompt = build_prompt(map_data, mermaid, priority_table, findings)
    print(f"[test_models] prompt: {len(prompt)} chars", flush=True)

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    print(f"[test_models] running {len(MODELS)} models in parallel", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=len(MODELS)) as pool:
        futures = {pool.submit(run_model, m, prompt, api_key): m for m in MODELS}
        for future in as_completed(futures):
            results.append(future.result())

    # Write individual report files
    for r in results:
        fname = safe_filename(r["model"])
        out_path = f"{OUTPUT_DIR}/{fname}.md"
        if r["content"]:
            with open(out_path, "w") as f:
                f.write(f"<!-- model: {r['model']} | elapsed: {r['elapsed']}s -->\n\n")
                f.write(r["content"])
            print(f"[test_models] wrote {out_path}", flush=True)
        else:
            with open(out_path, "w") as f:
                f.write(f"# ERROR\n\nModel: {r['model']}\nError: {r['error']}\n")
            print(f"[test_models] wrote error log to {out_path}", flush=True)

    # Write a summary index
    summary_path = f"{OUTPUT_DIR}/README.md"
    with open(summary_path, "w") as f:
        f.write("# Model Comparison\n\n")
        f.write("| Model | Time | Output Size | File |\n")
        f.write("|-------|------|-------------|------|\n")
        for r in sorted(results, key=lambda x: x["elapsed"]):
            size = f"{len(r['content'])} chars" if r["content"] else "ERROR"
            fname = safe_filename(r["model"]) + ".md"
            f.write(f"| {r['model']} | {r['elapsed']}s | {size} | [{fname}](./{fname}) |\n")
        f.write(f"\nPrompt length: {len(prompt)} chars\n")
    print(f"[test_models] summary written to {summary_path}", flush=True)
    print("[test_models] done — open output/model_comparison/ to compare", flush=True)


if __name__ == "__main__":
    main()
