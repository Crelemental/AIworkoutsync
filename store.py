"""Reading and writing the year JSONL files in output/."""
import os
import json

OUTPUT_DIR = "/root/fitness-sync/output"


def activities_path(year):
    return os.path.join(OUTPUT_DIR, f"{year}-YTD-backlog.jsonl")


def wellness_path(year):
    return os.path.join(OUTPUT_DIR, f"{year}-daily-wellness.jsonl")


def read_jsonl(path):
    records = []
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def write_jsonl(path, records):
    """Compact one-record-per-line JSON, written to a temp file and swapped in so push.sh never copies a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)
