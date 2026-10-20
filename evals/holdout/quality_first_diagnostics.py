#!/usr/bin/env python3
"""Recompute choice A and gross top8 changes through the frozen offline mapping.

Consumer interface: CLI writes quality-first-diagnostics.json and
QUALITY-FIRST-DECISION.md to a new directory. No product/network imports.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import policy_latency_tradeoffs as tradeoffs
except ImportError:
    import policy_latency_tradeoffs as tradeoffs


REL_BANDS = (("[0,0.35)", 0., .35), ("[0.35,0.4)", .35, .4),
             ("[0.4,0.5)", .4, .5), ("[0.5,1]", .5, 1.))
POLICY_NAMES = {"rel_rank_top8": "纯 rel 排序", "uncertain_rerank_top8": "uncertain 分层重排"}


def label_counts(items):
    return {"n": len(items), "useful_n": sum(c["content_useful"] is True for c in items),
            "not_useful_n": sum(c["content_useful"] is False for c in items),
            "unknown_n": sum(c["content_useful"] is None for c in items)}


def rel_band(item):
    rel = item["rel"]
    if rel is None:
        return "unjudged"
    for name, low, high in REL_BANDS:
        if low <= rel < high or rel == high == 1.:
            return name
    raise ValueError("invalid validated rel")


def useful_outcomes(items):
    useful = [c for c in items if c["content_useful"] is True and c["original_rel_rejected"]]
    return {"n": len(useful), "entered_top8_n": sum(c["alternative_delivered"] for c in useful),
            "tail_n": sum(c["alternative_tail"] for c in useful),
            "other_rejected_n": sum(not c["alternative_delivered"] and not c["alternative_tail"] for c in useful)}


def summarize_changes(rows):
    items = [c for row in rows for c in row["candidates"]]
    def delivery(members):
        before = [c for c in members if c["original_delivered"]]
        after = [c for c in members if c["alternative_delivered"]]
        added = [c for c in after if not c["original_delivered"]]
        removed = [c for c in before if not c["alternative_delivered"]]
        return {"before": label_counts(before), "after": label_counts(after),
                "added": label_counts(added), "removed": label_counts(removed),
                "net_useful_n": label_counts(added)["useful_n"] - label_counts(removed)["useful_n"],
                "net_delivered_n": len(added) - len(removed)}
    bands = {}
    for name in [b[0] for b in REL_BANDS] + ["unjudged"]:
        members = [c for c in items if c["rel_band"] == name]
        bands[name] = {"candidates": label_counts(members),
                       "rel_rejected": label_counts([c for c in members if c["original_rel_rejected"]]),
                       "useful_rel_rejected": useful_outcomes(members), "delivered": delivery(members)}
    return {"query_n": len(rows), "candidate_n": len(items),
            "failed_or_missing_query_n": sum(r["status"] != "ok" for r in rows),
            "rel_rejected": label_counts([c for c in items if c["original_rel_rejected"]]),
            "useful_rel_rejected": useful_outcomes(items), "rel_bands": bands,
            "delivered": delivery(items),
            "queries_with_useful_gain_n": sum(r["net_useful_n"] > 0 for r in rows),
            "queries_with_useful_loss_n": sum(r["net_useful_n"] < 0 for r in rows),
            "queries_with_same_useful_n": sum(r["net_useful_n"] == 0 for r in rows)}


def analyze(**score_args):
    # Reuse the existing score_frozen hash/opaque-id/text validation AND its
    # assertion that the reconstructed current top8 exactly matches delivery.
    frozen = tradeoffs.analyze(**score_args)
    current = frozen["policies"][tradeoffs.POLICIES[0]]["queries"]
    comparisons = {}
    for policy in tradeoffs.POLICIES[1:]:
        alternatives = {r["id"]: r for r in frozen["policies"][policy]["queries"]}
        rows = []
        for original in current:
            alternative = alternatives[original["id"]]
            before, after = set(original["top8"]), set(alternative["top8"])
            rejected, tail = set(original["threshold_rejected"]), set(alternative["topk_truncated"])
            items = []
            for candidate in original["candidates"]:
                cid = candidate["cid"]
                items.append({**candidate, "rel_band": rel_band(candidate),
                              "original_rel_rejected": cid in rejected,
                              "original_delivered": cid in before, "alternative_delivered": cid in after,
                              "alternative_tail": cid in tail,
                              "alternative_rank": alternative["pool"].index(cid) + 1 if cid in alternative["pool"] else None})
            # Keep stable frozen candidate order in per-query replacement lists.
            added = [c for c in items if c["alternative_delivered"] and not c["original_delivered"]]
            removed = [c for c in items if c["original_delivered"] and not c["alternative_delivered"]]
            rows.append({**{k: original[k] for k in ("id", "lang", "cohort", "stratum", "status")},
                         "original_top8": original["top8"], "alternative_top8": alternative["top8"],
                         "candidates": items, "added": added, "removed": removed,
                         "net_useful_n": label_counts(added)["useful_n"] - label_counts(removed)["useful_n"]})
        comparisons[policy] = {"overall": summarize_changes(rows),
                               "groups": tradeoffs.frozen.grouped(rows, summarize_changes), "queries": rows}
    return {"diagnostic_only": True, "posthoc": True, "final_pass": False,
            "effect_status": "potential_conditional_ranking_effect_not_observed_product_effect",
            "sufficiency_status": frozen["sufficiency_status"], "hashes": frozen["hashes"],
            "acceptance_gates_unchanged": frozen["acceptance_gates_unchanged"],
            "frozen_official_rel_metric": frozen["frozen_official_rel_metric"],
            "decision": {"choice": "A", "retain_jev_stages": ["plan", "candidate", "delivered"],
                         "new_latency_budget_s": None, "latency_budget_status": "requires_new_evidence_and_user_decision",
                         "new_formal_evaluation_requires_new_query_cohort": True,
                         "old_87_status": "exposed_development_replay_only"},
            "policy_summaries": {p: {k: v for k, v in result.items() if k != "queries"}
                                 for p, result in frozen["policies"].items()},
            "comparisons": comparisons}


def render_report(result):
    official = result["frozen_official_rel_metric"]
    lines = ["# 质量优先决定 A：冻结候选的开发反事实诊断", "",
        "## 1. 已授权决定与未授权事项", "",
        "用户选择 **A：质量优先**。保留 **plan → candidate → delivered 三段 Jev**，candidate 之后本地选择，再对精确 delivered 集合独立判断；试验不确定项保留并分层重排。此记录不修改产品、README 或 RELEASE，也不合 main、不 push、不 deploy。", "",
        "原 **+1.5 s 验收失败不改历史**；原 protocol、preregistration、gold、指标定义及验收线保持冻结。用户未授权新的具体延迟 budget，`new_latency_budget_s = null`；**不可自动设 +6 s / +8 s**，须待新方案质量/延迟证据与用户明确决策，不把当前观测分位数反写成通过线。", "",
        "安全、隐私、site 约束和 calls 上限不变；不增加网络、供应商或 judge 调用。复用已冻结的注入屏蔽与安全判断，不声称本诊断重做全量安全审查。", "",
        "旧 87 问已暴露，现仅是开发回放，不是新的正式留出。新的正式评估需 **新的 query cohort**，事前注册候选方案、质量/延迟指标和经用户授权的 budget，再运行独立验证；不能复用 87 问宣称新正式验收通过。", "",
        "## 2. 固定映射与解释边界", "",
        "`quality_first_diagnostics.py` 调用现有 `policy_latency_tradeoffs.analyze`：先由 `score_frozen.score_run` 校验冻结 query/protocol/preregistration、evidence、私有协调员 record、tasks/labels 哈希及 opaque item→query/cid/URL/原文映射，再校验当前 rel 丢弃和 top8 与原交付一致。标签只用于统计，绝不参与排序，不重新标注或修改 gold。JSON 保存输入哈希与每问逐候选排名、原拒绝、进入/留尾、替入/替出。", "",
        "本报告中的旧标签（oldlabel）保持 true / false / unknown 三态，unknown 不转 false、不猜补；数量列分别报告已知有用、已知无用和 unknown。哈希绑定不代表人工 gold、盲标独立性或事实正确性已经证实。", "",
        "仅在冻结最终轮候选上比较现有脚本的两种策略：纯 rel 安全候选降序；uncertain 使用 [.35,.65) 开发带，≥.65 降序、带内冻结 fused 顺序、<.35 降序，再接原序未判断透传，取最终 top8。不是当前产品候选生成/恢复链路的回放，也不是对新实现的端到端证明。", "",
        "**所有变化只是反事实排序下的潜在影响，不称事实无害或已实现收益。** 原集合 enough/scope/Brier 不可移用到新集合；仍须新集合充分性证据。rel 拒绝改名 top-k 损失不等于验收达标。留在 tail 的有用项仍未交付，不得称作已救回。", "",
        f'冻结正式 rel 误杀保持 {official["false_kill_n"]}/{official["useful_n"]}；历史旧 21.60% 及其结论不改写。以下把原 useful rel 拒绝作为固定目标集合，不把“候选保留”混同“最终交付”。', "",
        "## 3. 原 useful rel 拒绝的最终去向与毛替换", "",
        "替入/替出均按 query+cid 身份比较；净有用变化 = 已知有用替入 − 已知有用替出。数量不证明来源覆盖、去重或充分性改善。", "",
        "| 策略 | 原有用 rel 拒绝 | 进入 top8 | 仍在 tail | 其他未交付 | 有用替入 | 有用替出 | 净有用变化 | unknown 替入/替出 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for policy, comparison in result["comparisons"].items():
        s = comparison["overall"]
        target, delivered = s["useful_rel_rejected"], s["delivered"]
        lines.append(f'| {POLICY_NAMES[policy]} | {target["n"]} | {target["entered_top8_n"]} | {target["tail_n"]} | {target["other_rejected_n"]} | {delivered["added"]["useful_n"]} | {delivered["removed"]["useful_n"]} | {delivered["net_useful_n"]:+d} | {delivered["added"]["unknown_n"]}/{delivered["removed"]["unknown_n"]} |')
    lines += ["", "## 4. rel 区间与分层（不隐藏替出）", "",
        "每个层的三个主区间是 [0,.35)、[.35,.4)、[.4,.5)，左闭右开；.35/.4 归入右侧，.5 不属于原 rel 拒绝。为完整解释所有替出，另列 [.5,1] 和 unjudged。`原拒绝 T/F/U` 仅是原 threshold_rejected 内旧标签；`进入/留尾` 仅是其中已知有用项；`有用前→后；入/出；净` 则覆盖该区间全部候选的最终交付，不只是原拒绝项。语言、cohort、stratum 沿原分组，不能跨轴求和；失败/空问保留在 JSON 注册分母。", "",
        "| 分组（问数） | 策略 | rel 区间 | 原拒绝 T/F/U | 有用拒绝进入/留尾 | 有用前→后；入/出；净 | unknown 入/出 |", "|---|---|---|---:|---:|---|---:|"]
    for policy, comparison in result["comparisons"].items():
        groups = [("overall", comparison["overall"])]
        groups += [(f"{axis}:{value}", summary) for axis, values in comparison["groups"].items() for value, summary in values.items()]
        for group, summary in groups:
            for band, s in summary["rel_bands"].items():
                rejected, target, d = s["rel_rejected"], s["useful_rel_rejected"], s["delivered"]
                lines.append(f'| {group} ({summary["query_n"]}) | {POLICY_NAMES[policy]} | {band} | {rejected["useful_n"]}/{rejected["not_useful_n"]}/{rejected["unknown_n"]} | {target["entered_top8_n"]}/{target["tail_n"]} | {d["before"]["useful_n"]}→{d["after"]["useful_n"]}；{d["added"]["useful_n"]}/{d["removed"]["useful_n"]}；{d["net_useful_n"]:+d} | {d["added"]["unknown_n"]}/{d["removed"]["unknown_n"]} |')
    lines += ["", "## 5. 离线复算与 consumer 测试", "", "```sh",
        ".venv/bin/python evals/holdout/quality_first_diagnostics.py \\",
        "  --queries evals/holdout/queries-combined.jsonl \\",
        "  --run-dir evals/runs/holdout-87-v2-20261020 \\",
        '  --private-dir "$HOME/.cache/wrr/evals/holdout-87-v2-coordinator" \\',
        "  --content-tasks evals/holdout/annotations-87-v2/content/tasks.jsonl \\",
        "  --content-labels evals/holdout/annotations-87-v2/content/labels.complete.jsonl \\",
        "  --routing-gold evals/holdout/routing-gold-v2.jsonl \\",
        "  --protocol evals/holdout/PROTOCOL.md \\",
        "  --preregistration evals/holdout/preregistration-87-v2.json \\",
        "  --output-dir /tmp/wrr-quality-first-new-directory", "",
        ".venv/bin/python -m pytest -q evals/holdout/test_quality_first_diagnostics.py", "```", "",
        "输出 `quality-first-diagnostics.json` 与 `QUALITY-FIRST-DECISION.md`；输出目录已存在则拒绝覆盖，不能写回冻结数据。私有协调员目录不可用时不得绕过映射校验。永久测试仅从 CLI 写出的 JSON/Markdown 读取消费者可见统计，用真实冻结包验证区间边界、gross 替入/替出、tail、unknown、分层、不可覆盖和标签哈希拒绝；socket 守卫禁止联网。复算不等于测试通过；执行证据由实际任务回执单列。", ""]
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
    (output / "quality-first-diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    (output / "QUALITY-FIRST-DECISION.md").write_text(render_report(result))
    print(json.dumps({"output_dir": str(output), "diagnostic_only": True, "final_pass": False}))


if __name__ == "__main__":
    main()
