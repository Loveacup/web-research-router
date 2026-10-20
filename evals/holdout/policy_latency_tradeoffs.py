#!/usr/bin/env python3
"""Posthoc frozen-candidate policy/latency analysis; no product or network imports.

Always validate the frozen blind-label mapping with score_frozen.score_run first.
This is a development diagnostic, never a new holdout acceptance evaluation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

try:
    from . import score_frozen as frozen
except ImportError:
    import score_frozen as frozen


POLICIES = ("current_rel_05", "rel_rank_top8", "uncertain_rerank_top8")
TOP_K = 8
# Development ranking bands, NOT changed product/acceptance thresholds.
UNCERTAIN_LOW, UNCERTAIN_HIGH = .35, .65


def select(candidates, policy, dropped_injection=(), gate=None):
    """Reuse frozen safety outcomes; never send protected text to a new judge.

    Private/sensitive/unjudged passthrough stays behind judged material in its
    original order, exactly as in the current policy. Only the rel veto changes.
    """
    if policy not in POLICIES:
        raise ValueError("unknown development policy")
    if gate == "sensitive_query" and candidates:
        raise ValueError("sensitive-query gate cannot have candidates")
    denied, threshold, judged, passthrough = set(dropped_injection), set(), [], []
    ids = [c["cid"] for c in candidates]
    if len(set(ids)) != len(ids) or not denied <= set(ids):
        raise ValueError("invalid frozen candidate/drop identities")
    for index, c in enumerate(candidates):
        judgment, flags = c.get("judgment") or {}, c.get("flags", [])
        protected = (c.get("privacy_class", "public") != "public"
                     or any(f in flags for f in ("unjudged:private", "unjudged:sensitive"))
                     or gate in {"local", "bibliographic"})
        if c["cid"] in denied:
            continue
        if protected:
            if judgment:
                raise ValueError("protected candidate must not carry a frozen judgment")
            passthrough.append(c["cid"])
            continue
        rel, inj = judgment.get("rel"), judgment.get("inj")
        if not judgment:
            passthrough.append(c["cid"])
            continue
        if not all(frozen.number(v) and 0 <= v <= 1 for v in (rel, inj)):
            raise ValueError("partial/nonfinite frozen judgment: cannot infer safety")
        if inj >= .5:
            denied.add(c["cid"])
        elif policy == "current_rel_05" and rel < .5:
            threshold.add(c["cid"])
        else:
            judged.append((index, c))
    if policy == "uncertain_rerank_top8":
        # No labels, content heuristic, additional judge, or tuned search. The
        # original fused order is the frozen RRF tie-break within the band.
        def key(item):
            index, c = item
            rel = c["judgment"]["rel"]
            if rel >= UNCERTAIN_HIGH:
                return (0, -rel, index)
            if rel >= UNCERTAIN_LOW:
                return (1, 0, index)
            return (2, -rel, index)
    else:
        def key(item):
            return (-item[1]["judgment"]["rel"], item[0])
    ordered = [c["cid"] for _, c in sorted(judged, key=key)] + passthrough
    selected = ordered[:TOP_K]
    return {"pool": ordered, "top8": selected,
            "safety_rejected": sorted(denied), "threshold_rejected": sorted(threshold),
            "topk_truncated": ordered[TOP_K:]}


def recall_bounds(kept_true, total_true, kept_unknown, total_unknown):
    """Sharp independent binary assignments of content-unknown labels."""
    low_denominator = total_true + total_unknown - kept_unknown
    high_denominator = total_true + kept_unknown
    return {"lower": frozen.ratio(kept_true, low_denominator) if low_denominator
                     else 1.0 if kept_unknown else None,
            "upper": frozen.ratio(kept_true + kept_unknown, high_denominator) if high_denominator
                     else 0.0 if total_unknown else None}


def summarize(rows):
    items = [c for row in rows for c in row["candidates"]]
    useful = sum(c["content_useful"] is True for c in items)
    unknown = sum(c["content_useful"] is None for c in items)
    def counts(key):
        members = [c for row in rows for c in row["candidates"] if c["cid"] in row[key]]
        return {"n": len(members), "useful_n": sum(c["content_useful"] is True for c in members),
                "not_useful_n": sum(c["content_useful"] is False for c in members),
                "unknown_n": sum(c["content_useful"] is None for c in members)}
    pool, top = counts("pool"), counts("top8")
    top_bounds = recall_bounds(top["useful_n"], useful, top["unknown_n"], unknown)
    losses = {key: counts(key) for key in ("safety_rejected", "threshold_rejected", "topk_truncated")}
    lost_true = sum(v["useful_n"] for v in losses.values())
    assert lost_true + top["useful_n"] == useful
    return {"query_n": len(rows), "candidate_n": len(items), "useful_n": useful,
            "unknown_n": unknown, "failed_or_missing_query_n": sum(r["status"] != "ok" for r in rows),
            "unobserved_frozen_candidate_n": sum(r["frozen_candidate_n"] for r in rows if r["status"] == "missing_evidence"),
            "pool": {**pool, "recall": frozen.ratio(pool["useful_n"], useful),
                     "recall_bounds": recall_bounds(pool["useful_n"], useful, pool["unknown_n"], unknown)},
            "top8": {**top, "recall": frozen.ratio(top["useful_n"], useful),
                     "recall_bounds": top_bounds,
                     "useful_per_registered_query": frozen.ratio(top["useful_n"], len(rows)),
                     "precision_bounds": {"lower": frozen.ratio(top["useful_n"], top["n"]),
                                          "upper": frozen.ratio(top["useful_n"] + top["unknown_n"], top["n"])},
                     "known_label_precision": frozen.ratio(top["useful_n"], top["n"] - top["unknown_n"])},
            "losses": losses, "all_loss_useful_n": lost_true,
            "all_loss_rate": frozen.ratio(lost_true, useful),
            "all_loss_rate_bounds": {"lower": None if top_bounds["upper"] is None else 1 - top_bounds["upper"],
                                     "upper": None if top_bounds["lower"] is None else 1 - top_bounds["lower"]}}


def quantiles(values):
    values = [v for v in values if frozen.number(v)]
    return {"n": len(values), "p50_s": frozen.percentile(values, .5), "p95_s": frozen.percentile(values, .95)}


def interval(call):
    start, end = call.get("queue_start_monotonic_s"), call.get("service_end_monotonic_s")
    if not frozen.number(start) or not frozen.number(end) or end < start:
        raise ValueError("missing/invalid boundary interval")
    return start, end


def union_duration(intervals):
    end, duration = None, 0.0
    for start, stop in sorted(intervals):
        if end is None:
            duration += stop - start
        elif stop > end:
            duration += stop - max(start, end)
        end = max(stop, end) if end is not None else stop
    return duration


def decompose(record):
    timing = record.get("latency", {}).get("pipeline")
    if not timing or timing.get("calls") is None:
        return {"id": record["sample"]["id"], "status": "unknown_boundaries"}
    calls = sorted(timing["calls"], key=lambda c: interval(c)[0])
    judges = [c for c in calls if c["boundary"] == "judge"]
    sources = [c for c in calls if c["boundary"].startswith("source:")]
    judge_intervals = [interval(c) for c in judges]
    if abs(sum(b - a for a, b in judge_intervals) - union_duration(judge_intervals)) > 1e-7:
        raise ValueError("judge calls are not serial")
    # Source waves are separated by intervening judge calls, not summed adapters.
    waves = []
    for source in sources:
        a, b = interval(source)
        slot = sum(interval(j)[1] <= a for j in judges)
        if not waves or waves[-1]["slot"] != slot:
            waves.append({"slot": slot, "start_s": a, "end_s": b, "calls": []})
        wave = waves[-1]
        wave["start_s"], wave["end_s"] = min(a, wave["start_s"]), max(b, wave["end_s"])
        wave["calls"].append(source)
    for wave in waves:
        wave["critical_path_s"] = wave["end_s"] - wave["start_s"]
        wave["adapter_wall_sum_s"] = sum(b - a for a, b in map(interval, wave["calls"]))
        wave["critical_tail_boundary"] = max(wave["calls"], key=lambda c: interval(c)[1])["boundary"]
    judge_wall = sum(b - a for a, b in judge_intervals)
    source_critical = sum(w["critical_path_s"] for w in waves)
    wall = record.get("pipeline_wall_s")
    all_intervals = judge_intervals + [(w["start_s"], w["end_s"]) for w in waves]
    if abs(judge_wall + source_critical - union_duration(all_intervals)) > 1e-6:
        raise ValueError("source waves overlap judge intervals")
    baseline = (record.get("baseline") or {}).get("wall_s")
    baseline_calls = record.get("latency", {}).get("baseline", {}).get("calls", [])
    regular_three = (len(judges) == 3 and len(waves) == 1
                     and interval(judges[0])[1] <= waves[0]["start_s"]
                     and waves[0]["end_s"] <= interval(judges[1])[0])
    stages = dict(zip(("plan", "candidate", "delivered"),
                      [b - a for a, b in judge_intervals])) if regular_three else {}
    residual = wall - judge_wall - source_critical if frozen.number(wall) else None
    if residual is not None and residual < -1e-6:
        raise ValueError("boundary decomposition exceeds pipeline wall")
    return {"id": record["sample"]["id"], "status": record.get("pipeline_error") or "ok",
            "judge_boundary_n": len(judges), "source_wave_n": len(waves), "regular_three": regular_three,
            "judge_statuses": dict(Counter(c["status"] for c in judges)),
            "judge_boundaries": judges,
            "stage_wall_s": stages, "judge_serial_wall_s": judge_wall,
            "source_critical_path_s": source_critical,
            "source_adapter_wall_sum_s": sum(w["adapter_wall_sum_s"] for w in waves),
            "source_waves": waves, "pipeline_wall_s": wall, "orchestration_residual_s": residual,
            "baseline_wall_s": baseline,
            "baseline_delegate_wall_s": sum(b - a for a, b in map(interval, baseline_calls)),
            "harness_boundary_queue_sum_s": sum(c["queue_s"] for c in calls + baseline_calls),
            "source_minus_baseline_s": source_critical - baseline if frozen.number(baseline) else None,
            "paired_increment_s": wall - baseline if frozen.number(wall) and frozen.number(baseline) else None,
            "provider_queue_s": "unknown", "provider_service_s": "unknown"}


def analyze(**score_args):
    # This validates all frozen hashes, opaque IDs, cid+URL and exact source text;
    # never join a newly annotated label file or trust an unvalidated content.json.
    content, routing = frozen.score_run(**score_args)
    private = Path(score_args["private_dir"])
    entries = json.loads((private / "index.json").read_text())["queries"]
    records = {entry["id"]: entry["record"] for entry in entries
               if (Path(score_args["run_dir"]) / "evidence" / f'{entry["id"]}.json').exists()}
    policy_rows = {policy: [] for policy in POLICIES}
    for detail in content["queries"]:
        record = records.get(detail["id"])
        trace = record.get("selection_trace", []) if record else []
        candidates = trace[-1]["candidates"] if trace else []
        gate = ((record or {}).get("evidence") or {}).get("plan", {}).get("gate")
        labels = {c["cid"]: c for c in detail["candidates"]}
        for policy in POLICIES:
            selected = select(candidates, policy, trace[-1].get("dropped_injection", []) if trace else [], gate)
            if policy == POLICIES[0] and record:
                frozen.exact(set(selected["threshold_rejected"]),
                             set(trace[-1].get("dropped_relevance", [])) if trace else set(), "current rel reconstruction")
                frozen.exact([next(c["url"] for c in candidates if c["cid"] == cid) for cid in selected["top8"]],
                             [c["url"] for c in (record.get("evidence") or {}).get("results", [])],
                             "current top8 reconstruction")
            policy_rows[policy].append({**{k: v for k, v in detail.items() if k != "candidates"},
                                       "candidates": list(labels.values()), **selected})
    latency_rows = [decompose(records[d["id"]]) if d["id"] in records else {"id": d["id"], "status": "missing_evidence"}
                    for d in content["queries"]]
    complete = [r for r in latency_rows if r.get("regular_three")]
    latency = {"official_all_attempt_summary": routing["latency_summary"],
               "judge_boundary_count_histogram": dict(Counter(str(r.get("judge_boundary_n", "unknown")) for r in latency_rows)),
               "regular_three_query_n": len(complete),
               "regular_three_stages": {stage: quantiles(r["stage_wall_s"][stage] for r in complete)
                                        for stage in ("plan", "candidate", "delivered")},
               "regular_three_serial_sum": quantiles(r["judge_serial_wall_s"] for r in complete),
               "regular_three_components": {key: quantiles(r.get(key) for r in complete) for key in
                     ("pipeline_wall_s", "source_critical_path_s", "source_adapter_wall_sum_s",
                      "orchestration_residual_s", "baseline_wall_s", "source_minus_baseline_s", "paired_increment_s")},
               "all_attempt_components": {key: quantiles(r.get(key) for r in latency_rows) for key in
                     ("judge_serial_wall_s", "source_critical_path_s", "source_adapter_wall_sum_s",
                      "orchestration_residual_s", "baseline_delegate_wall_s", "harness_boundary_queue_sum_s")},
               "regular_three_judge_sum_over_1_5_n": sum(r["judge_serial_wall_s"] > 1.5 for r in complete),
               "regular_three_dependency_failure_n": sum(any(s != "ok" for s in r["judge_statuses"]) or
                     any(c["status"] != "ok" for w in r["source_waves"] for c in w["calls"]) for r in complete),
               "critical_tail_boundary_counts": dict(Counter(w["critical_tail_boundary"] for r in latency_rows for w in r.get("source_waves", []))),
               "queries": latency_rows, "provider_queue_s": "unknown", "provider_service_s": "unknown"}
    return {"diagnostic_only": True, "posthoc": True, "final_pass": False,
            "sufficiency_status": "not_established_for_new_rankings", "hashes": content["hashes"],
            "acceptance_gates_unchanged": content["acceptance_gates"],
            "frozen_official_rel_metric": content["overall"],
            "policies": {p: {"overall": summarize(rows), "groups": frozen.grouped(rows, summarize), "queries": rows}
                         for p, rows in policy_rows.items()}, "latency": latency}


def pct(value):
    return "null" if value is None else f"{100 * value:.2f}%"


def secs(value):
    return "null" if value is None else f"{value:.3f}"


def bounds(value):
    return f'{pct(value["lower"])}–{pct(value["upper"])}'


def render_report(result):
    policies, latency = result["policies"], result["latency"]
    official = result["frozen_official_rel_metric"]
    short = dict(zip(POLICIES, ("当前 rel≥.5", "纯 rel 排序", "uncertain 重排")))
    lines = ["# 冻结 87 候选：策略与延迟开发诊断", "",
        "**仅 posthoc 离线开发实验，不是新验收；正式 holdout87 仍未通过。main 不合并，不 push，不 deploy。**",
        f'正式内容 rel 误杀仍为 {official["false_kill_n"]}/{official["useful_n"]} = {pct(official["false_kill_rate"])}；旧 21.60% 不改。冻结 protocol、gold、阈值与 +1.5 s 验收线均未修改。', "",
        "## 1. 证据与可复算边界", "",
        "输入为 `../runs/holdout-87-v2-20261020/` 的冻结最终轮候选，以及 `annotations-87-v2/content/` 内容盲标签。脚本先调用 `score_frozen.score_run` 校验 query、protocol、preregistration、原始 evidence、私有协调员 record、tasks/labels 哈希和 opaque item→query/cid/URL/text 映射，随后只读该已绑定 record；不重标，不使用标签排序，不导入 wrr，不联网、不发付费调用。哈希保证文件绑定，不保证人工 gold 或独立性。", "",
        "公开 87 问、失败 3 问与空候选 2 问均保留。候选召回只针对实际冻结最终轮；失败无候选不虚构为召回 100%。top8 有用/问的宏分母仍为 87；逐组保持原注册问数。诊断 JSON 保存逐问选择与分解及所有输入哈希。", "",
        "## 2. 三个明确的开发策略", "",
        "1. **当前 rel≥.5**：原安全门后，已判断候选 rel<.5 拒绝；其余按 rel 降序、冻结 fused 序稳定打破并列，再接未判断透传，取 8。脚本断言与保存的 relevance 丢弃集合及交付 URL 顺序完全一致。",
        "2. **纯 rel 排序 top8**：安全门不变，无 rel 阈值否决；所有已判断安全候选按 rel 降序，再接原序未判断透传；最后取 8。未交付者记 top-k 损失，不隐去。",
        "3. **uncertain 不删重排 top8**：开发带 [.35,.65)，不是新验收线。≥.65 按 rel 降序；带内按冻结 fused 原序（RRF 顺序）而不按细小 rel 差异排序；<.35 按 rel 降序，最后接原序未判断透传。任何 rel 都不在 top-k 前删除；不做标签驱动调参，不增加判断。", "",
        "所有策略复用冻结 `dropped_injection`（包含本地注入屏蔽）并硬保留 inj≥.5 拒绝；.3≤inj<.5 的 suspect 不取消，仍不能冒充完整充分性评估。non-public、sensitive 不进入云判断，也不因本实验获得新分数；按原透传机制保留原序。query 级 sensitive-before-execute、local/bibliographic 限制以及来源约束均不变。冻结 trace 不含完整 highlights/隐私类字段，诊断依赖已保存屏蔽集合和 `unjudged:*` 标记，不声称重新完成全量安全筛查。当前 cohort 无安全负例，不能证明零外发/攻击不服从验收。", "",
        "依据：[`rubric.py:227–276`](../../wrr/core/rubric.py)；[`pipeline.py:66–79,126–157,159–197`](../../wrr/core/pipeline.py)；[`plan.py`](../../wrr/core/plan.py)。", "",
        "## 3. 候选保留与最终 top8（不隐藏截断损失）", "",
        "候选保留召回 = 安全门与 rel 策略后、top-k 前的已知有用数 / 全部已知有用数（包含安全拒绝，不能把分母缩成安全项美化）。top8 precision 区间以全部实际交付数为分母：下界 true/交付，上界 (true+unknown)/交付；known-only precision 另列 JSON，不替代该区间。有用/问 = top8 true / 注册问数。", "",
        "unknown 召回区间为逐条任意 true/false 赋值的严格界限：保留 true 为 a、全部 true 为 T、保留 unknown 为 b、全部 unknown 为 U，则下界 a/(T+U−b)，上界 (a+b)/(T+b)。最终损失区间取 1−top8 召回的反向界限。null 不转 false。", "",
        "| 策略 | 候选保留 true/T；召回 [unknown界] | top8 true/交付；unknown | 有用/问 | precision界 | top8召回界 | 有用淘汰：安全 / rel / top-k = 总计 | 总损失率界 |",
        "|---|---|---|---:|---|---|---|---|"]
    for p in POLICIES:
        s = policies[p]["overall"]
        pool, top, loss = s["pool"], s["top8"], s["losses"]
        lines.append(f'| {short[p]} | {pool["useful_n"]}/{s["useful_n"]}；{pct(pool["recall"])} [{bounds(pool["recall_bounds"])}] | {top["useful_n"]}/{top["n"]}；{top["unknown_n"]} | {top["useful_per_registered_query"]:.3f} | {bounds(top["precision_bounds"])} | {bounds(top["recall_bounds"])} | {loss["safety_rejected"]["useful_n"]} / {loss["threshold_rejected"]["useful_n"]} / {loss["topk_truncated"]["useful_n"]} = {s["all_loss_useful_n"]} | {bounds(s["all_loss_rate_bounds"])} |')
    current, rank, uncertain = (policies[p]["overall"] for p in POLICIES)
    lines += ["", f'纯排序虽将 rel veto 项数变为 0，仍淘汰 {rank["all_loss_useful_n"]} 条已知有用（含 top-k）；相对当前，top8 已知有用仅净变动 {rank["top8"]["useful_n"] - current["top8"]["useful_n"]:+d}，交付数净变动 {rank["top8"]["n"] - current["top8"]["n"]:+d}。uncertain 重排 top8 已知有用净变动 {uncertain["top8"]["useful_n"] - current["top8"]["useful_n"]:+d}。**不得把 rel 拒绝改名 top-k 后宣称误杀归零或达标。**正式 rel 指标定义未被替换，总损失也不是新增验收线。', "", "### 中英、cohort 与层统计", "",
        "每行依次展示 top-k 前候选召回及 unknown 界、top8 true/交付（unknown）、有用/问与 precision 界、最终有用损失（安全/rel/top-k）及总损失 unknown 界。小层样本仅描述，不新增分层通过线。", "",
        "| 分组（问数） | 策略 | 候选召回 [界] | top8 true/交付（unknown） | 有用/问；precision界 | 有用损失 安全/rel/topk；总损失界 |",
        "|---|---|---|---|---|---|"]
    for axis in ("lang", "cohort", "stratum"):
        for value in policies[POLICIES[0]]["groups"][axis]:
            for p in POLICIES:
                s = policies[p]["groups"][axis][value]
                pool, top, loss = s["pool"], s["top8"], s["losses"]
                mean = "null" if top["useful_per_registered_query"] is None else f'{top["useful_per_registered_query"]:.3f}'
                lines.append(f'| {axis}:{value} ({s["query_n"]}) | {short[p]} | {pool["useful_n"]}/{s["useful_n"]}={pct(pool["recall"])} [{bounds(pool["recall_bounds"])}] | {top["useful_n"]}/{top["n"]} ({top["unknown_n"]}) | {mean}；{bounds(top["precision_bounds"])} | {loss["safety_rejected"]["useful_n"]}/{loss["threshold_rejected"]["useful_n"]}/{loss["topk_truncated"]["useful_n"]}；{bounds(s["all_loss_rate_bounds"])} |')
    lines += ["", "### 不能由离线排序推出的新充分性", "",
        "逐候选内容有用不是集合充分性：排序改变集合后，覆盖面、重复、矛盾、来源独立性和缺失方面都会变。原 full_delivery/public_assessed 的预测与标签绑定旧集合和 scope，不能搬到新 top8，不重算新 Brier、不声称足够回答。恢复触发、选源与 judge 输入若线上改变，也会改变候选池；这里固定最终轮仅是条件反事实，不是端到端产品提升或新的独立 holdout。", "",
        "## 4. 边界延迟分解", "",
        "三段真实依赖：**plan judge → 来源并发执行/融合 → candidate rel+inj judge → 本地安全/rel/top-k 选择 → delivered-set enough+next_action judge**。恢复可能再增加来源波和 candidate judge。第二段必须先拿到来源结果；第三段必须先知道第二段输出后本地选择得到的精确 delivered/scope。同一次静态 judge 请求里的各问题不能看到彼此尚未返回的答案，raw_candidate_sufficiency 也不是筛后 top8 充分性。", "",
        "计时采用 `queue_start_monotonic_s` 到 `service_end_monotonic_s` 的边界 wall，包含失败/取消。每个并发 source 波关键路径为 max(end)−min(start)，不是各 source 用时相加；串行波相加。恒等式逐问检查：pipeline = judge 串行总和 + source 波关键路径 + orchestration residual；paired increment 再减该问 baseline。各项 p95 不能相加。call 列表原为完成顺序，脚本按开始时间排序。", "",
        "依据：[`run_eval.py:130–168`](../run_eval.py)；[`pipeline.py:159–251`](../../wrr/core/pipeline.py)；[`execute.py`](../../wrr/core/execute.py)。", "",
        f'87 问 judge 边界数分布：`{json.dumps(latency["judge_boundary_count_histogram"], sort_keys=True)}`。完整三 judge、单 source 波子集 n={latency["regular_three_query_n"]}，其中 {latency["regular_three_dependency_failure_n"]} 问存在 judge/source 边界失败；不排除这些失败，不将该子集冒称全部 87 或成功调用分布。stage 标签依代码顺序推定，边界本身未存 task 名；0/1/2/4 次及恢复/失败另保留在 JSON，缺段不填零去估计三段耗时。', "",
        "| 完整三段子集的观测项 | n | p50 s | p95 s |", "|---|---:|---:|---:|"]
    metrics = {**latency["regular_three_stages"], "三段逐问串行总和": latency["regular_three_serial_sum"], **latency["regular_three_components"]}
    for name, metric in metrics.items():
        lines.append(f'| {name} | {metric["n"]} | {secs(metric["p50_s"])} | {secs(metric["p95_s"])} |')
    lines += ["", "| 全部实际尝试：不得成功筛选 | n | p50 s | p95 s |", "|---|---:|---:|---:|"]
    observed = latency["official_all_attempt_summary"]
    for name, prefix, count in (("pipeline", "pipeline", "pipeline_wall_n"), ("direct Exa baseline", "baseline", "baseline_wall_n"), ("每问增量", "increment", "paired_latency_n")):
        lines.append(f'| {name} | {observed[count]} | {secs(observed[prefix + "_observed_p50_s"])} | {secs(observed[prefix + "_observed_p95_s"])} |')
    for name, metric in latency["all_attempt_components"].items():
        lines.append(f'| {name} | {metric["n"]} | {secs(metric["p50_s"])} | {secs(metric["p95_s"])} |')
    lines += ["", f'失败惩罚增量 p95 仍为 `{observed["increment_failure_penalized_p95_s"]}`；失败对 {observed["failed_pair_n"]}，缺失对 {observed["missing_pair_n"]}。source 波最后结束边界计数：`{json.dumps(latency["critical_tail_boundary_counts"], sort_keys=True)}`。source adapter 总和仅用于显示并发重叠，不当作关键路径。', "",
        "baseline 是另一次 direct Exa 请求（AB/BA 隔离配对，不是同一 source 的理想可扣除时间），不能假设 source−baseline 为 0 或稳定非负。baseline timeout、schema_error、依赖错误不删除。隔离模式 harness admission/出站队列观测为 0；这不等于供应商不排队。边界 service 包括网络、供应商内部队列/推理、适配器工作和内部等待；provider queue / pure service 均 unknown，不能从差值反推。orchestration residual 也不是纯 CPU 或隐藏排队的识别估计。", "",
        f'当前完整三段的串行总和 p50/p95 = {secs(latency["regular_three_serial_sum"]["p50_s"])}/{secs(latency["regular_three_serial_sum"]["p95_s"])} s；{latency["regular_three_judge_sum_over_1_5_n"]}/{latency["regular_three_query_n"]} 问的三段本身超过 1.5 s。加上 source 与 baseline 的逐问差异，实际全体增量 p95 为 +{secs(observed["increment_observed_p95_s"])} s。这是 **当前三段结构及现供应商时延与 +1.5 s 目标有显著冲突的观测证据**，不是数学不可能证明：未来更快服务、缓存、模型、source/baseline 差异及分布变化均未被排除；不拿 stage p95 相加构造伪下界。', "",
        "## 5. 最多三个具体决策选项（均需用户显式决策）", "",
        "1. **质量优先，保留三段**：继续维持 plan→candidate→精确 delivered-set 的依赖和现安全约束；单独讨论适合该链路的新延迟预算及独立预注册验证。当前 +1.5 s 不静默改，正式结论仍失败。优势是保留真实筛后充分性；代价是当前观测串行判断成本及尾部。",
        "2. **缩成两段，必须改变真实职责**：可以把 source plan 改为确定性/显式路由而非云 judge，来源返回后保留 candidate judge，再本地筛选，再单独 delivered-set judge；两个云调用仍严格串行。需重新验证选源质量，不能复用旧 any-of 结果证明通过。若坚持模型 plan+candidate 两段，则只能放弃独立筛后充分性/用 raw scope，必须显式承认该语义损失，不能让同次 candidate 请求里的 enough 读取尚未产生的 rel/inj 选择结果。两段的时延和质量收益均尚未线上证明。",
        "3. **rank-based 保留 top-k 权衡**：安全门、非 public/sensitive 不外发、inj 阈值和 suspect scope 均不变，取消 rel veto，仅在现 rel 或明确 uncertain 重排后取 8；预先报告本表全部有用损失（安全+rel+top-k）、unknown 和中英/层差异。它只改变质量选择，不会自动消除第三次充分性判断或满足 +1.5 s；如需新集合充分性，仍要同集合独立标签与预测。不得用术语变化宣布误杀归零。", "",
        "## 6. 复算与针对性离线测试", "", "```sh",
        ".venv/bin/python evals/holdout/policy_latency_tradeoffs.py \\",
        "  --queries evals/holdout/queries-combined.jsonl \\",
        "  --run-dir evals/runs/holdout-87-v2-20261020 \\",
        '  --private-dir "$HOME/.cache/wrr/evals/holdout-87-v2-coordinator" \\',
        "  --content-tasks evals/holdout/annotations-87-v2/content/tasks.jsonl \\",
        "  --content-labels evals/holdout/annotations-87-v2/content/labels.complete.jsonl \\",
        "  --routing-gold evals/holdout/routing-gold-v2.jsonl \\",
        "  --protocol evals/holdout/PROTOCOL.md \\",
        "  --preregistration evals/holdout/preregistration-87-v2.json \\",
        "  --output-dir /tmp/wrr7-policy-latency-new-directory", "",
        ".venv/bin/python -m pytest -q evals/holdout/test_policy_latency_tradeoffs.py", "```", "",
        "输出新目录中的 `diagnostics.json` 与本报告同名文件；拒绝覆盖已有输出。私有协调员目录缺失时不能绕过映射验证；不重构或重新标注 gold。测试以 socket/create_connection/getaddrinfo 失败守卫禁止联网，验证 unknown 严格界、top-k 有用损失、保护/注入安全规则、非 oracle 排序、并发 source 波分解及失败保留。测试执行证据见任务回执；不据单元测试宣称线上质量/安全/性能通过。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("queries", "run-dir", "private-dir", "routing-gold", "protocol", "preregistration", "content-tasks", "content-labels"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = vars(parser.parse_args())
    output = args.pop("output_dir")
    if output.exists():
        raise ValueError("use a new output directory; never overwrite frozen evidence/reports")
    result = analyze(**args)
    output.mkdir(parents=True)
    (output / "diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    (output / "POLICY-LATENCY-TRADEOFFS.md").write_text(render_report(result))
    print(json.dumps({"output_dir": str(output), "diagnostic_only": True, "final_pass": False}))


if __name__ == "__main__":
    main()
