#!/usr/bin/env python3
"""Tune-only, network-denied loss anatomy; no selector changes or new judgments.

PYTHONPATH=. .venv/bin/python evals/holdout/loss_anatomy_tune.py
PYTHONPATH=. .venv/bin/python evals/holdout/loss_anatomy_tune.py --assert-public-url
The assertion checks the ordinary public URL regression after the privacy fix.
Only aggregate counts, query/candidate IDs and public hostnames are emitted.
"""
from __future__ import annotations

# rank_tuning_v1 installs its socket deny guard before importing product code.
from evals.holdout import rank_tuning_v1 as rt

import argparse
from collections import Counter
import inspect
import json
import re
from urllib.parse import unquote, urlsplit

from wrr import search_filter as screen

PUBLIC_REPRO = "https://github.com/OpenResearchTools/router-v2/documentation"


def decoded_probe(text):
    probe = screen._normalize(text)
    for _ in range(3):
        decoded = screen._normalize(unquote(probe))
        if decoded == probe:
            break
        probe = decoded
    return probe


def entropy_hits(probe):
    return [match for match in screen._ENTROPY.finditer(probe)
            if any(c.isupper() for c in match.group())
            and any(c.islower() for c in match.group())
            and any(c.isdigit() for c in match.group())]


def non_entropy_checker():
    # Diagnostic ablation only. Never used to select or expose any candidate.
    source = inspect.getsource(screen._sensitive)
    old = "for match in _ENTROPY.finditer(_entropy_probe(probe)):"
    assert source.count(old) == 1
    source = source.replace(old, "for match in ():")
    namespace = dict(vars(screen))
    exec(compile(source, "<offline-entropy-isolation>", "exec"), namespace)
    return namespace["_sensitive"]


def legacy_checker():
    """Replay only the pre-fix entropy boundary, without changing other gates."""
    namespace = dict(vars(screen), _entropy_probe=lambda probe: probe)
    exec(compile(inspect.getsource(screen._sensitive), "<offline-legacy-entropy>", "exec"), namespace)
    return namespace["_sensitive"]


def cross_path_entropy(candidate, other_sensitive, old_sensitive):
    url = candidate["url"]
    if not screen._public_url(url) or not old_sensitive(url) or other_sensitive(url):
        return False
    probe = decoded_probe(url)
    parsed = urlsplit(probe)
    start = probe.find(parsed.path, probe.find("://") + 3) if parsed.path else -1
    end = start + len(parsed.path)
    matches = entropy_hits(probe)
    if not matches or start < 0:
        return False
    for match in matches:
        path_part = probe[max(match.start(), start):min(match.end(), end)]
        pieces = [part for part in path_part.split("/") if part]
        if ("/" not in path_part or match.end() > end or len(pieces) < 2
                or any(len(part) >= 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", part)
                       for part in pieces)):
            return False
    return True


def cause_of(candidate, hard_ids):
    judgment = candidate.get("judgment") or {}
    flags = candidate.get("flags", [])
    if candidate["cid"] in hard_ids and not judgment:
        return "c_local_hard"
    if judgment.get("inj", 0) >= .5:
        return "c_inj_hard"
    if "unjudged:sensitive" in flags:
        return "a_sensitive"
    if not judgment:
        return "b_degraded" if "unjudged:degraded" in flags else "b_overflow"
    if judgment.get("inj", 0) >= .3:
        return "d_suspect"
    if judgment.get("rel", 0) < .35:
        return "e_low_rel"
    return "ranked_high" if judgment["rel"] >= .65 else "ranked_uncertain"


def analyze():
    inputs, hashes, index = rt.setup()
    split = rt.load(rt.BASE / "split-v1.json")
    assert len(split["tune"]) == 45
    rows, records = rt.subset_rows(split["tune"], inputs, index)
    baseline = rt.replay(rows, records, rt.BASELINE)
    other_sensitive = non_entropy_checker()
    old_sensitive = legacy_checker()
    items, queries = [], []
    for row in rows:
        sid = row["id"]
        record = records[sid] or {}
        trace = (record.get("selection_trace") or [{}])[-1]
        raw = trace.get("candidates", [])
        hard_ids = set(trace.get("dropped_injection", []))
        selected_order = baseline["selections"][sid]["top8"]
        selected = set(selected_order)
        labels = {c["cid"]: c["content_useful"] for c in row["candidates"]}
        current = []
        for candidate in raw:
            judgment = candidate.get("judgment") or {}
            sensitive = "unjudged:sensitive" in candidate.get("flags", [])
            path_hit = sensitive and cross_path_entropy(candidate, other_sensitive, old_sensitive)
            item = {
                "id": sid, "lang": row["lang"], "cid": candidate["cid"],
                "host": (urlsplit(candidate["url"]).hostname or "").lower(),
                "sources": sorted({c["source_id"] for c in candidate.get("contributions", [])}),
                "useful": labels[candidate["cid"]], "delivered": candidate["cid"] in selected,
                "cause": cause_of(candidate, hard_ids), "sensitive": sensitive,
                "path_entropy": path_hit,
                "newly_screenable_visible": sensitive
                    and candidate.get("privacy_class", "public") == "public"
                    and screen._public_url(candidate["url"])
                    and any(old_sensitive(candidate[k]) for k in ("title", "url", "snippet"))
                    and all(not screen._sensitive(candidate[k]) for k in ("title", "url", "snippet")),
                "missing_jev_scores": sensitive and not judgment,
                "path_only_visible": path_hit and all(not screen._sensitive(candidate[k])
                                                       for k in ("title", "snippet")),
                "suspect": .3 <= judgment.get("inj", 0) < .5,
                "low_rel": bool(judgment) and judgment.get("rel", 0) < .35,
                "clip_possible": len(candidate["title"]) == 300 or len(candidate["snippet"]) == 2000,
                "same_host_repeat": False,
            }
            current.append(item)
        by_cid = {item["cid"]: item for item in current}
        seen_hosts = set()
        for cid in selected_order:
            item = by_cid[cid]
            item["same_host_repeat"] = item["host"] in seen_hosts
            seen_hosts.add(item["host"])
        items.extend(current)
        useful = sum(item["useful"] is True for item in current)
        kept = sum(item["useful"] is True and item["delivered"] for item in current)
        mapped = {**row, "candidates": [dict(c, delivered=c["cid"] in selected) for c in row["candidates"]]}
        capacity = rt.capacity.query_quality(mapped, 8)
        queries.append({
            "id": sid, "lang": row["lang"], "status": row["status"],
            "U_known": useful, "u_known": kept, "delivered_n": len(selected),
            "known_opportunity": min(8, useful), "known_only_deficit": min(8, useful) - kept,
            "loss_bounds": capacity["loss_bounds"],
            "raw_known_misses": dict(Counter(item["cause"] for item in current
                                              if item["useful"] is True and not item["delivered"])),
            "path_only_visible_known_misses": sum(item["path_only_visible"] and item["useful"] is True
                                                   and not item["delivered"] for item in current),
            "delivered_false": sum(item["useful"] is False and item["delivered"] for item in current),
            "delivered_unknown": sum(item["useful"] is None and item["delivered"] for item in current),
            "missed_unknown": sum(item["useful"] is None and not item["delivered"] for item in current),
            "repeat_slots": sum(item["same_host_repeat"] for item in current),
            "repeat_false": sum(item["same_host_repeat"] and item["useful"] is False for item in current),
        })
    causes = sorted({item["cause"] for item in items} | {"b_overflow", "d_suspect"})
    groups = {}
    for lang in ("all", "en", "zh"):
        subset = [item for item in items if lang == "all" or item["lang"] == lang]
        qsubset = [q for q in queries if lang == "all" or q["lang"] == lang]
        missed = [item for item in subset if item["useful"] is True and not item["delivered"]]
        delivered = [item for item in subset if item["delivered"]]
        groups[lang] = {
            "query_n": len(qsubset), "candidate_n": len(subset), "raw_useful_missed": len(missed),
            "known_only_deficit": sum(q["known_only_deficit"] for q in qsubset),
            "known_opportunity": sum(q["known_opportunity"] for q in qsubset),
            "cause_counts": {cause: {
                "raw_known_useful_missed": sum(item["cause"] == cause for item in missed),
                "raw_miss_share": sum(item["cause"] == cause for item in missed) / len(missed),
                "known_only_marginal_capacity_ceiling": sum(min(q["known_only_deficit"],
                    sum(item["cause"] == cause and item["id"] == q["id"] for item in missed)) for q in qsubset),
            } for cause in causes},
            "path_entropy_missed": sum(item["path_entropy"] for item in missed),
            "path_only_visible_missed": sum(item["path_only_visible"] for item in missed),
            "path_only_visible_candidate_n": sum(item["path_only_visible"] for item in subset),
            "path_only_known_capacity_ceiling": sum(min(q["known_only_deficit"],
                q["path_only_visible_known_misses"]) for q in qsubset),
            "newly_screenable_visible_n": sum(item["newly_screenable_visible"] for item in subset),
            "newly_screenable_queries": sum(any(item["newly_screenable_visible"] and item["id"] == q["id"]
                                                for item in subset) for q in qsubset),
            "newly_screenable_missing_jev_n": sum(item["newly_screenable_visible"] and item["missing_jev_scores"]
                                                   for item in subset),
            "newly_screenable_known_useful_n": sum(item["newly_screenable_visible"] and item["useful"] is True
                                                    for item in subset),
            "newly_screenable_known_missed_n": sum(item["newly_screenable_visible"] for item in missed),
            "newly_screenable_known_capacity_ceiling": sum(min(q["known_only_deficit"],
                sum(item["newly_screenable_visible"] and item["id"] == q["id"] for item in missed))
                for q in qsubset),
            "repeat_slots": sum(item["same_host_repeat"] for item in delivered),
            "repeat_false": sum(item["same_host_repeat"] and item["useful"] is False for item in delivered),
            "repeat_useful": sum(item["same_host_repeat"] and item["useful"] is True for item in delivered),
            "repeat_unknown": sum(item["same_host_repeat"] and item["useful"] is None for item in delivered),
            "single_source_missed": sum(len(item["sources"]) == 1 for item in missed),
            "single_source_delivered": sum(len(item["sources"]) == 1 for item in delivered),
            "clip_possible_missed": sum(item["clip_possible"] for item in missed),
            "delivered_n": len(delivered),
            "delivered_false": sum(item["useful"] is False for item in delivered),
            "delivered_unknown": sum(item["useful"] is None for item in delivered),
            "noise_by_cause": dict(Counter(item["cause"] for item in delivered if item["useful"] is False)),
            "source_counts": {source: {
                "delivered": sum(source in item["sources"] for item in delivered),
                "false": sum(source in item["sources"] and item["useful"] is False for item in delivered),
                "unknown": sum(source in item["sources"] and item["useful"] is None for item in delivered),
                "useful_missed": sum(source in item["sources"] for item in missed),
            } for source in ("deepseek", "exa", "academic")},
        }
    return {"split_sha256": rt.SPLIT_HASH, "hashes": hashes,
            "scope": "tune45 only; no validation mapping/scoring or policy changes",
            "baseline": {"overall": baseline["overall"], "by_lang": baseline["by_lang"]},
            "privacy_fix_limits": "Frozen title/url/snippet screenability only; highlights are not frozen here. "
                "Missing Jev rel/inj scores prohibit counterfactual ranking or measured quality gains; "
                "capacity ceilings are theoretical, not improvement estimates.",
            "groups": groups, "queries": queries, "annotations": items,
            "network_attempt_n": len(rt.NETWORK_ATTEMPTS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assert-public-url", action="store_true")
    args = parser.parse_args()
    if args.assert_public_url:
        print(json.dumps({"synthetic_public_url": PUBLIC_REPRO,
                          "public_url": screen._public_url(PUBLIC_REPRO),
                          "sensitive_actual": screen._sensitive(PUBLIC_REPRO),
                          "sensitive_expected": False,
                          "network_attempt_n": len(rt.NETWORK_ATTEMPTS)}), flush=True)
        assert not screen._sensitive(PUBLIC_REPRO), "public URL ordinary path entropy false positive"
    else:
        print(json.dumps(analyze(), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
