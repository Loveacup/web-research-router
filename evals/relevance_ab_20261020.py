#!/usr/bin/env python3
"""One-shot diagnostic only: 6 fixed public batches, 12 Jev POSTs maximum.

Does not run search, alter product rubric/thresholds, or write frozen labels.
Existing output directory prevents a second attempt. No retries or redirects.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wrr.core import rubric
from wrr.core.judge import JudgeError, noul, resolve_judge
from wrr.core.runtime import prepare_environment

RUN = ROOT / "evals/runs/20261020T-eval"
OUT = ROOT / "evals/runs/20261020T-relevance-ab"
# Validation content is not used to formulate or revise the proposed wording.
SPLITS = {"development": ("001", "023", "038"), "validation": ("028", "046", "019")}
ADDITION = (
    " For a request to find a paper, repository, or official documentation, "
    "an identifiable matching target also counts even when the snippet is truncated before the answer. "
    "Specific evidence for one explicitly requested aspect counts; the candidate need not answer the whole query. "
    "Respect requested source/type constraints; do not infer absent facts or count topic overlap alone."
)
HYPOTHESES = [
    "Locator intent is narrower in rubric.true than frozen useful labels; adding identifiable targets should recover exact papers/docs.",
    "Per-candidate relevance may be confused with full-query sufficiency; explicit partial-aspect credit should recover multi-part evidence.",
    "Prefix-only snippets omit requested details; wording alone cannot recover absent evidence safely.",
    "Frozen labels include off-source, ambiguous-title and generic materials; some counted false rejections are not model mistakes.",
    "Boundary-only explanation is insufficient because many rejected useful scores are far below 0.5.",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def freeze():
    labels = {r["id"]: r for r in map(json.loads, (ROOT / "evals/labels.jsonl").read_text().splitlines())}
    inventory = json.loads((RUN / "manifest.json").read_text())["inventory"]
    kinds = {s["id"]: s["kind"] for s in inventory}
    batches = []
    inputs = {"evals/labels.jsonl": digest(ROOT / "evals/labels.jsonl"),
              "wrr/core/rubric.py": digest(ROOT / "wrr/core/rubric.py"),
              "evals/relevance_ab_20261020.py": digest(Path(__file__))}
    for split, suffixes in SPLITS.items():
        for suffix in suffixes:
            qid = "wrr7-" + suffix
            path = RUN / "evidence" / (qid + ".json")
            e = json.loads(path.read_text())
            assert e["sample"]["stratum"] != "sensitive_negative"
            inputs[str(path.relative_to(ROOT))] = digest(path)
            gold = {r["cid"]: r["useful"] for r in labels[qid]["candidate_labels"]}
            trace = e["selection_trace"][-1]
            # Only candidates historically submitted to the public judge; never
            # forward sensitive/unjudged or suspected-injection material.
            eligible = [c for c in trace["candidates"] if not c["flags"]
                        and isinstance(c["judgment"].get("rel"), (float, int))
                        and c["judgment"].get("inj", 0) < 0.3]
            rejected = [c for c in eligible if gold[c["cid"]] and c["cid"] in trace["dropped_relevance"]]
            normal = [c for c in eligible if gold[c["cid"]] and c["judgment"]["rel"] >= 0.5]
            negative = [c for c in eligible if not gold[c["cid"]]]
            assert len(rejected) >= 2 and normal and negative, qid
            picked = rejected[:2] + normal[:1] + negative[:1]
            candidates = []
            for c in picked:
                source = c["contributions"][0]["source_id"]
                assert kinds[source] != "local"
                assert c["url"].startswith(("https://", "http://"))
                candidates.append({"id": c["cid"], "title": c["title"], "url": c["url"],
                                   "snippet": c["snippet"], "clipped": False,
                                   "source_kind": kinds[source]})
            batches.append({"id": qid, "split": split,
                            "state": {"request": {"query": e["sample"]["query"], "today": "2026-10-20"},
                                      "candidates": candidates},
                            "labels": {c["cid"]: gold[c["cid"]] for c in picked},
                            "historical_rel": {c["cid"]: c["judgment"]["rel"] for c in picked}})
    return {"created_at": datetime.now(timezone.utc).isoformat(), "max_requests": 12,
            "threshold": 0.5, "hypotheses_before_requests": HYPOTHESES,
            "new_true_addition": ADDITION, "input_sha256": inputs,
            "candidate_selection": "first two frozen-useful rel-rejected, first frozen-useful rel-kept, first frozen-not-useful eligible in final cid order",
            "state_limitations": "Four-candidate diagnostic batches, not original full context. Historical clipped/source_kind not serialized; inferred public source kind from first contribution and clipped=false (500-char source limit, no core 2000-char clipping).",
            "batches": batches}


def questions(batch, variant):
    # Build the same complete candidate question set; change only rel.true.
    from types import SimpleNamespace
    qs = rubric.candidate_questions([SimpleNamespace(cid=c["id"]) for c in batch["state"]["candidates"]])
    if variant == "new":
        for key, value in list(qs.items()):
            if key.startswith("rel."):
                qs[key] = noul(value.instructions, true=value.criteria["true"] + ADDITION,
                               false=value.criteria["false"])
    return qs


async def main():
    # Freeze/validate locally before taking the exclusive network-attempt lock.
    frozen = freeze()
    OUT.mkdir(exist_ok=False)
    dump(OUT / "manifest.json", frozen)
    _, _, env = prepare_environment()
    if not env.get("TYPESAFE_API_KEY"):
        default = Path.home() / ".config/wrr/env.d/default.env"
        if default.is_file():
            _, _, env = prepare_environment(env_path=str(default))
    judge = resolve_judge(env=env)
    if judge.backend != "typesafe":
        dump(OUT / "availability.json", {"available": False, "reason": "no_authorized_typesafe_backend"})
        print("No authorized Jev backend; offline manifest retained.")
        return
    # Isolate rubric quality from historical shared-queue deadlines. No retries.
    judge.soft_budget = 15.0
    count = 0
    records = []
    for index, batch in enumerate(frozen["batches"]):
        for variant in (("old", "new") if index % 2 == 0 else ("new", "old")):
            assert count < 12
            count += 1
            record = {"attempt": count, "id": batch["id"], "split": batch["split"], "variant": variant}
            dump(OUT / (f"attempt-{count:02d}.json"), record | {"status": "started"})
            started = time.monotonic()
            try:
                response = await judge.judge(batch["state"], questions(batch, variant))
                record.update(status="ok", model=response.model, backend=response.backend,
                              usage=response.usage, posts=response.posts,
                              rel={key[4:]: answer.p for key, answer in response.answers.items() if key.startswith("rel.")},
                              inj={key[4:]: answer.p for key, answer in response.answers.items() if key.startswith("inj.")})
            except JudgeError as error:
                record.update(status="error", reason=error.reason, posts=error.posts)
            record["wall_s"] = time.monotonic() - started
            dump(OUT / (f"attempt-{count:02d}.json"), record)
            records.append(record)
            print(batch["id"], variant, record["status"], flush=True)
    summary = {"attempts": count, "successful": sum(r["status"] == "ok" for r in records),
               "usage": dict(sum((Counter(r.get("usage", {})) for r in records), Counter())), "splits": {}}
    for split in SPLITS:
        summary["splits"][split] = {}
        for variant in ("old", "new"):
            s = Counter()
            for batch in frozen["batches"]:
                if batch["split"] != split:
                    continue
                result = next(r for r in records if r["id"] == batch["id"] and r["variant"] == variant)
                if result["status"] != "ok":
                    continue
                for cid, useful in batch["labels"].items():
                    keep = result["rel"][cid] >= 0.5
                    s["useful" if useful else "not_useful"] += 1
                    s["kept"] += int(keep)
                    s["false_rejected"] += int(useful and not keep)
                    s["false_kept"] += int(not useful and keep)
            summary["splits"][split][variant] = dict(s)
    dump(OUT / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
