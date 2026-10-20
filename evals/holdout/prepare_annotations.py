#!/usr/bin/env python3
"""Prepare staged holdout annotation packets using frozen local files only.

Only distribute the requested stage's output directory, never coordinator files.
This module deliberately imports neither the product nor the live eval harness.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import re


REVIEW = {"annotator": "assistant", "model": "unknown", "user_reviewed": False}
GATES = {
    "source_recall_min": 0.8,
    "rel_false_kill_rate_max": 0.10,
    "same_scope_enough_brier_max": 0.2,
    "isolated_paired_increment_p95_s_max": 1.5,
    "regular_judge_posts_max": 3,
    "recovery_judge_posts_max": 4,
    "external_egress_attempts_max": 0,
    "real_independent_queries_min": 60,
    "user_query_audit_fraction_min": 0.20,
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def save_rows(path, rows):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def new_directory(path):
    path = Path(path)
    if path.exists():
        raise ValueError(f"use a new output directory: {path}")
    path.mkdir(parents=True)


def text(candidate):
    result = {key: candidate[key] for key in ("title", "url", "snippet")}
    if any(not isinstance(value, str) for value in result.values()):
        raise ValueError("candidate title/url/snippet must be strings")
    return result


def review_row(task_id):
    return {"task_id": task_id, "annotation_status": "pending", "review": dict(REVIEW)}


def preregister(queries_path, protocol_path, output_path, *, seed=20261020):
    """Bind the protocol before live execution; never imply sample authenticity."""
    output_path = Path(output_path)
    if output_path.exists():
        raise ValueError("preregistration must be a new file")
    rows = jsonl(queries_path)
    ids = [row["id"] for row in rows]
    normalized = [" ".join(row["query"].casefold().split()) for row in rows]
    if len(ids) != len(set(ids)) or len(normalized) != len(set(normalized)):
        raise ValueError("duplicate query or id")
    public = [row for row in rows if row["stratum"] not in {"sensitive_negative", "敏感负例"}]
    real = [row for row in public if row["synthetic"] is False and row["source"] in {"omp", "hermes"}]
    if len(real) < GATES["real_independent_queries_min"]:
        raise ValueError("formal preregistration requires at least 60 real independent queries")
    result = {
        "holdout_protocol_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(protocol_path), "queries_sha256": sha256(queries_path),
        "selected_ids": sorted(ids), "real_public_query_count": len(real),
        "gates": GATES, "seed": seed, "latency_mode": "isolated-paired",
        "query_concurrency": 1, "harness_retries": 0, "repetitions": 1,
        "deadline_s": 15, "count": 8,
        "independence_status": "requires provenance and prior-exposure audit; count is not proof",
        "user_audit_status": "pending", "final_pass": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save(output_path, result)
    return result


def prepare_content(queries_path, run_dir, output_dir, private_dir, *, seed=20261020):
    output_dir, private_dir = Path(output_dir), Path(private_dir)
    if (private_dir.resolve().is_relative_to(output_dir.resolve())
            or output_dir.resolve().is_relative_to(private_dir.resolve())):
        raise ValueError("coordinator directory must be outside every blind packet")
    if output_dir.exists() or private_dir.exists():
        raise ValueError("use new output and coordinator directories")
    rows = jsonl(queries_path)
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("duplicate query id")
    rng = random.Random(seed)
    tasks, labels, queries = [], [], []
    for number, row in enumerate(rows, 1):
        sid = row["id"]
        if Path(sid).name != sid or sid in {".", ".."}:
            raise ValueError("query id must be a filename-safe identifier")
        record_path = Path(run_dir) / "evidence" / f"{sid}.json"
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        if record is not None and (record["sample"]["id"] != sid or record["sample"]["query"] != row["query"]):
            raise ValueError(f"frozen query mismatch: {sid}")
        sensitive = row["stratum"] in {"sensitive_negative", "敏感负例"}
        entry = {"id": sid, "record": record, "record_sha256": sha256(record_path) if record else None,
                 "status": "missing_evidence" if record is None else record.get("pipeline_error") or "ok"}
        queries.append(entry)
        if sensitive:
            entry["annotation_policy"] = "local safety audit only; never distribute or send to a model"
            continue
        candidates = record["selection_trace"][-1]["candidates"] if record and record["selection_trace"] else []
        identities = [(c["cid"], c["url"]) for c in candidates]
        if len({cid for cid, _ in identities}) != len(identities):
            raise ValueError(f"duplicate final candidate id: {sid}")
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        task_id = f"q{number:06d}"
        task = {"task_id": task_id, "query": row["query"], "candidates": []}
        mapping = []
        for item_number, candidate in enumerate(shuffled, 1):
            item_id = f"i{item_number:06d}"
            task["candidates"].append({"item_id": item_id, **text(candidate)})
            mapping.append({"item_id": item_id, "cid": candidate["cid"], "url": candidate["url"]})
        tasks.append(task)
        labels.append({**review_row(task_id), "candidate_labels": [
            {"item_id": c["item_id"], "content_useful": None, "rationale": ""} for c in task["candidates"]]})
        entry.update(task_id=task_id, mapping=mapping)
    rng.shuffle(tasks)
    new_directory(output_dir)
    new_directory(private_dir)
    save_rows(output_dir / "tasks.jsonl", tasks)
    save_rows(output_dir / "labels.template.jsonl", labels)
    save(private_dir / "index.json", {
        "schema_version": 1, "seed": seed, "queries_sha256": sha256(queries_path),
        "content_tasks_path": str((output_dir / "tasks.jsonl").resolve()),
        "content_tasks_sha256": sha256(output_dir / "tasks.jsonl"), "queries": queries,
    })


def freeze_content(private_dir, labels_path):
    private_dir, labels_path = Path(private_dir), Path(labels_path)
    freeze_path = private_dir / "content-freeze.json"
    if freeze_path.exists():
        raise ValueError("content is already frozen; use a new annotation run for changes")
    index = json.loads((private_dir / "index.json").read_text())
    if sha256(index["content_tasks_path"]) != index["content_tasks_sha256"]:
        raise ValueError("content tasks changed")
    rows = jsonl(labels_path)
    expected = {row["task_id"]: row for row in index["queries"] if "task_id" in row}
    if len(rows) != len(expected) or {row["task_id"] for row in rows} != set(expected):
        raise ValueError("content task coverage or identity mismatch")
    for row in rows:
        if row.get("annotation_status") != "complete":
            raise ValueError("complete every content task before freezing")
        review = row.get("review", {})
        if (not isinstance(review.get("annotator"), str) or not review["annotator"].strip()
                or not isinstance(review.get("model"), str) or not review["model"].strip()
                or type(review.get("user_reviewed")) is not bool):
            raise ValueError("explicit annotator/model/user-review metadata required")
        expected_items = {item["item_id"] for item in expected[row["task_id"]]["mapping"]}
        items = row["candidate_labels"]
        if len(items) != len(expected_items) or {item["item_id"] for item in items} != expected_items:
            raise ValueError("content candidate coverage mismatch")
        for item in items:
            value = item.get("content_useful")
            if value is not None and type(value) is not bool:
                raise ValueError("content_useful must be true, false, or null")
            if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
                raise ValueError("every content decision, including unknown, needs a rationale")
    save(freeze_path, {
        "labels_path": str(labels_path.resolve()), "labels_sha256": sha256(labels_path),
        "content_tasks_sha256": index["content_tasks_sha256"],
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "human_gold": False, "user_audit_status": "pending",
    })


def frozen_index(private_dir):
    private_dir = Path(private_dir)
    freeze_path = private_dir / "content-freeze.json"
    if not freeze_path.exists():
        raise ValueError("freeze content labels before revealing another annotation stage")
    index = json.loads((private_dir / "index.json").read_text())
    freeze = json.loads(freeze_path.read_text())
    if (sha256(freeze["labels_path"]) != freeze["labels_sha256"]
            or sha256(index["content_tasks_path"]) != freeze["content_tasks_sha256"]
            or index["content_tasks_sha256"] != freeze["content_tasks_sha256"]):
        raise ValueError("frozen annotation content changed")
    return index


def stage_directory(private_dir, output_dir, index):
    output_dir, private_dir = Path(output_dir), Path(private_dir)
    content_dir = Path(index["content_tasks_path"]).parent
    if any(output_dir.resolve().is_relative_to(path.resolve())
           or path.resolve().is_relative_to(output_dir.resolve()) for path in (private_dir, content_dir)):
        raise ValueError("stage output must be outside coordinator and other blind packets")
    new_directory(output_dir)
    return output_dir


def source_constraint_template(query):
    # Flag literal unresolved requirements, never infer ownership from domains.
    official = bool(re.search(r"\bofficial\b|官方", query, re.IGNORECASE))
    nonstandard_site = bool(re.search(r"(?<![\w:])site\.[\w.-]+", query, re.IGNORECASE))
    return {
        "mode": "ambiguous" if nonstandard_site else None,
        "target_domains": [],
        "rationale": "Nonstandard site spelling; not a hard site: constraint." if nonstandard_site else "",
        "unadjudicated_requirements": [
            {"requirement": "official", "status": "unadjudicated", "target_domains": []}
        ] if official else [],
    }


def prepare_source(private_dir, output_dir):
    index = frozen_index(private_dir)
    tasks = jsonl(index["content_tasks_path"])
    labels = [{**review_row(task["task_id"]), "source_constraint": source_constraint_template(task["query"]),
        "candidate_labels": [{"item_id": item["item_id"], "source_match": None, "rationale": ""}
                             for item in task["candidates"]]} for task in tasks]
    output_dir = stage_directory(private_dir, output_dir, index)
    save_rows(output_dir / "tasks.jsonl", tasks)
    save_rows(output_dir / "labels.template.jsonl", labels)


def prepare_sufficiency(private_dir, output_dir):
    index = frozen_index(private_dir)
    tasks, labels, bindings, unknown = [], [], [], []
    rng = random.Random(index["seed"] + 1)
    queries = {task["task_id"]: task["query"] for task in jsonl(index["content_tasks_path"])}
    for entry in index["queries"]:
        if "task_id" not in entry:
            continue
        record = entry["record"]
        evidence = record.get("evidence") if record else None
        evidence = evidence or {}
        query = queries[entry["task_id"]]
        delivered = evidence.get("results", [])
        prediction = evidence.get("quality", {}).get("sufficiency", {})
        scope = prediction.get("scope")
        scopes = [("full_delivery", delivered, False)]
        if prediction.get("enough") is None:
            unknown.append({"id": entry["id"], "scope": scope, "reason": "missing prediction"})
        elif scope == "delivered":
            scopes[0] = ("full_delivery", delivered, True)
        elif scope == "public_only":
            assessed = [c for c in delivered if c.get("judgment") and "inj:suspect" not in c.get("flags", [])]
            if len(assessed) == len(delivered):
                scopes[0] = ("full_delivery", delivered, True)
            else:
                scopes.append(("public_assessed", assessed, True))
        else:
            unknown.append({"id": entry["id"], "scope": scope,
                            "reason": "exact assessed input not reconstructible from saved evidence"})
        for scope_key, candidates, matches_prediction in scopes:
            task_id = f"s{len(tasks) + 1:06d}"
            texts = [text(candidate) for candidate in candidates]
            rng.shuffle(texts)
            tasks.append({"task_id": task_id, "query": query, "candidates": texts})
            labels.append({**review_row(task_id), "enough": None, "rationale": "", "missing_aspects": []})
            bindings.append({"task_id": task_id, "id": entry["id"], "scope_key": scope_key,
                             "matches_prediction": matches_prediction,
                             "prediction_scope": scope, "candidate_urls": [c["url"] for c in candidates]})
    rng.shuffle(tasks)
    output_dir = stage_directory(private_dir, output_dir, index)
    save_rows(output_dir / "tasks.jsonl", tasks)
    save_rows(output_dir / "labels.template.jsonl", labels)
    save(Path(private_dir) / "sufficiency-index.json", {
        "tasks_sha256": sha256(output_dir / "tasks.jsonl"),
        "bindings": bindings, "unknown_predictions": unknown,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pre = subparsers.add_parser("preregister")
    pre.add_argument("--queries", type=Path, required=True)
    pre.add_argument("--protocol", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    pre.add_argument("--seed", type=int, default=20261020)
    content = subparsers.add_parser("content")
    content.add_argument("--queries", type=Path, required=True)
    content.add_argument("--run-dir", type=Path, required=True)
    content.add_argument("--output-dir", type=Path, required=True)
    content.add_argument("--private-dir", type=Path, required=True)
    content.add_argument("--seed", type=int, default=20261020)
    freeze = subparsers.add_parser("freeze-content")
    freeze.add_argument("--private-dir", type=Path, required=True)
    freeze.add_argument("--labels", type=Path, required=True)
    for name in ("source", "sufficiency"):
        stage = subparsers.add_parser(name)
        stage.add_argument("--private-dir", type=Path, required=True)
        stage.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "preregister":
        preregister(args.queries, args.protocol, args.output, seed=args.seed)
    elif args.command == "content":
        prepare_content(args.queries, args.run_dir, args.output_dir, args.private_dir, seed=args.seed)
    elif args.command == "freeze-content":
        freeze_content(args.private_dir, args.labels)
    elif args.command == "source":
        prepare_source(args.private_dir, args.output_dir)
    else:
        prepare_sufficiency(args.private_dir, args.output_dir)


if __name__ == "__main__":
    main()
