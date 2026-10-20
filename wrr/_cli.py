#!/usr/bin/env python3
"""wrr-cli: Web Research Router 的命令行入口。

独立 CLI：search 驱动 core evidence 流水线；fetch/similar 沿用原有 router，
不经过宿主运行时或 tool handler。

用法::

    wrr-cli.py search "查询词" [--provider exa] [--count 10] [--exa-mode deep] [--lang zh-CN]
    wrr-cli.py fetch  "https://..." [--provider exa] [--max-chars 5000]
    wrr-cli.py similar "https://..." [--count 10]
    wrr-cli.py test  [--provider exa]

通用选项：--json（机器可读）/ --env PATH（指定 .env）/ -q 静默元信息。

设计取舍：
  - search 通过 inventory → adapters → judge → pipeline，不复制路由规则。
  - --sources 显式指定来源；--provider 在 search 中是单来源快捷方式。
  - fetch/similar 保持原 router 语义。
  - 仅依赖标准库 + wrr 自身；不引入 click/typer，.env 用内置极简解析器。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys

# ── 作为包内模块运行时，wrr 包由 pip 安装保证可 import；不需要手动 sys.path 注入 ──


# ── .env 加载（极简，零依赖）──────────────────────────────────────────


def load_env(explicit: str | None = None) -> str | None:
    """Load one authorized env file without overriding the process environment.

    --env wins over WRR_ENV; otherwise use WRR's own default.env. An explicitly
    selected missing/unreadable file is an error, never a credential fallback.
    """
    selected = explicit or os.environ.get("WRR_ENV")
    path = selected or os.path.join(
        os.path.expanduser("~"), ".config", "wrr", "env.d", "default.env",
    )
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, val = line.partition("=")
                key = key.strip()
                if key:
                    values.setdefault(key, val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        if selected:
            raise
        return None
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return path


# ── 输出辅助 ─────────────────────────────────────────────────────────
def _eprint(*a) -> None:
    print(*a, file=sys.stderr)


def _emit_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))



def cmd_host_fetch_result(ns) -> int:
    """Evaluate one accepted host-native fetch return from bounded JSON stdin."""
    from wrr.host_handoff import HostHandoffError, evaluate_host_fetch

    try:
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise HostHandoffError("host return input exceeds the supported limit")
        payload = json.loads(raw.decode("utf-8"))
        result = evaluate_host_fetch(payload)
    except (HostHandoffError, json.JSONDecodeError, UnicodeError):
        _emit_json({
            "operation": "host_fetch_return",
            "ok": False,
            "error": "invalid_host_return",
            "detail": "Host return failed closed.",
        })
        return 2
    _emit_json(result)
    return 0 if result["ok"] else 1

def cmd_host_fetch_eligibility(ns) -> int:
    """Evaluate the shared public-target policy without loading provider config."""
    from wrr.errors import EngineError, FetchPolicyError
    from wrr.fetch_transport import validate_public_target

    try:
        asyncio.run(validate_public_target(ns.url))
    except FetchPolicyError:
        _emit_json({
            "operation": "host_fetch_eligibility",
            "ok": True,
            "eligible": False,
            "reason": "policy_denied",
        })
        return 0
    except EngineError:
        _emit_json({
            "operation": "host_fetch_eligibility",
            "ok": False,
            "eligible": False,
            "error": "eligibility_unavailable",
        })
        return 1
    _emit_json({
        "operation": "host_fetch_eligibility",
        "ok": True,
        "eligible": True,
    })
    return 0


def _result_to_payload(operation: str, result) -> object:
    """RouterResult.payload → 可 JSON 化结构（search/similar 为 list，extract 为 dict）。"""
    from .schemas import ExtractResult

    payload = result.payload
    if isinstance(payload, list):
        items = []
        for item in payload:
            value = dataclasses.asdict(item)
            if not value.get("fusion_sources"):
                value.pop("fusion_sources", None)
            if value.get("rrf_score") is None:
                value.pop("rrf_score", None)
            items.append(value)
        return items
    if isinstance(payload, ExtractResult):
        return payload.to_dict()
    if dataclasses.is_dataclass(payload):
        return dataclasses.asdict(payload)
    return payload


def _chain_to_list(result) -> list:
    return [dataclasses.asdict(s) for s in result.fallback_chain]


# ── 运行单个动作（统一异常 → 退出码）─────────────────────────────────
async def _run(operation: str, options, provider: str | None):
    from wrr.registry import get_registry
    from wrr.router import route

    registry = get_registry()
    return await route(operation, options, registry, explicit_provider=provider)


def _dispatch(operation: str, options, provider, ident, as_json, quiet, formatter,
              *, host_fallback_requested: bool = False):
    """执行 + 渲染 + 退出码。返回 0/1。"""
    from wrr.errors import AllEnginesFailedError, TerminalReason, WRRError
    from wrr.formatters import failed_quality_payload, quality_payload
    try:
        result = asyncio.run(_run(operation, options, provider))
    except AllEnginesFailedError as e:
        if as_json:
            payload = {"operation": operation, "ok": False,
                       "error": "all_engines_failed", "detail": str(e),
                       "quality": failed_quality_payload()}
            if (host_fallback_requested
                    and e.terminal_reason is TerminalReason.FETCH_POLICY_DENIED):
                payload["host_fallback"] = {
                    "status": "denied",
                    "reason": TerminalReason.FETCH_POLICY_DENIED.value,
                }
            if e.diagnostics is not None:
                payload["diagnostics"] = e.diagnostics.to_dict()
            _emit_json(payload)
        else:
            _eprint(f"✗ 所有引擎失败（{operation}）：\n{e}")
            _eprint("  提示：检查 API key（EXA_API_KEY / BRAVE_API_KEY / SEARXNG_URL）、"
                    "网络连通性，或换 --provider 试单引擎。")
        return 1
    except WRRError as e:
        if as_json:
            _emit_json({"operation": operation, "ok": False,
                        "error": type(e).__name__, "detail": str(e),
                        "quality": failed_quality_payload()})
        else:
            _eprint(f"✗ {type(e).__name__}: {e}")
        return 1

    if as_json:
        payload = {
            "operation": operation,
            "ok": True,
            "provider": result.actual_provider,
            "fallback_chain": _chain_to_list(result),
            "result": _result_to_payload(operation, result),
            "quality": quality_payload(result),
        }
        if hasattr(result, "diagnostics") and result.diagnostics is not None:
            payload["diagnostics"] = result.diagnostics.to_dict()
        _emit_json(payload)
    else:
        if not quiet:
            degraded = result.degraded_from if hasattr(result, "degraded_from") else None
            tag = f"  (降级自 {degraded})" if degraded else ""
            _eprint(f"● provider={result.actual_provider}{tag}")
        # formatters 返回 Hermes 工具响应信封 {"success","content","details"}；
        # 人类可读模式只取其中的 markdown content。解析失败则原样输出。
        rendered = formatter(result, ident)
        try:
            env = json.loads(rendered)
            print(env.get("content", rendered) if isinstance(env, dict) else rendered)
        except (json.JSONDecodeError, TypeError):
            print(rendered)
    return 0


# ── 子命令实现 ───────────────────────────────────────────────────────
def cmd_search(ns) -> int:
    from wrr import config
    from wrr.core.inventory import build_inventory
    from wrr.core.runtime import prepare_environment, search_evidence
    from wrr.core.sources import build_adapters

    if ns.rpc:
        from wrr.core.rpc import stdio_rpc
        try:
            runtime, snapshot, env = prepare_environment(
                runtime=ns.runtime, env_path=ns.env, trust_project=ns.trust_project,
            )
            def inventory_factory(host_capabilities):
                inventory = build_inventory(runtime=runtime, env=snapshot,
                                            trust_project=ns.trust_project,
                                            host_capabilities=host_capabilities)
                return inventory, build_adapters(inventory, env=env, exa_mode=ns.exa_mode)
            return asyncio.run(stdio_rpc(inventory_factory=inventory_factory, env=env))
        except (ValueError, OSError):
            print('{"error":{"reason":"configuration_error"}}', flush=True)
            return 2
    if ns.query is None:
        _eprint("✗ search 需要 query；或使用 --rpc 从 stdin 接收请求")
        return 2

    if ns.route_mode is not None:
        _eprint("✗ --route-mode 已移除；请使用自动选源或 --sources a,b")
        return 2
    if ns.mode is not None:
        _eprint("✗ --mode 已移除；Exa search type 请使用 --exa-mode")
        return 2
    if not 1 <= ns.count <= config.MAX_SEARCH_COUNT:
        _eprint(f"✗ --count 须在 1..{config.MAX_SEARCH_COUNT}")
        return 2
    explicit = None
    if ns.sources is not None:
        explicit = [sid.strip() for sid in ns.sources.split(",")]
        if not explicit or any(not sid for sid in explicit):
            _eprint("✗ --sources 须为逗号分隔的非空来源 ID")
            return 2
    if ns.provider:
        if explicit is not None and explicit != [ns.provider]:
            _eprint("✗ --provider 与 --sources 不能指定不同来源")
            return 2
        explicit = [ns.provider]
    try:
        evidence = asyncio.run(search_evidence(
            ns.query, runtime=ns.runtime, env_path=ns.env,
            trust_project=ns.trust_project, exa_mode=ns.exa_mode,
            count=ns.count, sources=explicit,
        ))
    except (ValueError, OSError) as error:
        # Configuration errors are not source runs and must not masquerade as
        # successful evidence. Avoid serializing exception text or credentials.
        _eprint(f"✗ 搜索配置不可用（{type(error).__name__}）；检查 --sources 和 inventory")
        return 2
    if ns.json:
        _emit_json(evidence.to_dict())
    else:
        if not ns.quiet:
            quality = evidence.quality
            _eprint(f"● sources={','.join(evidence.plan.sources)} "
                    f"executed={quality.executed} relevance={quality.relevance} "
                    f"judge.calls={evidence.judge.calls}")
        for index, candidate in enumerate(evidence.results, 1):
            print(f"{index}. {candidate.title}\n   {candidate.url}\n   {candidate.snippet}")
    if evidence.quality.executed == "cancelled":
        return 130
    return 1 if evidence.quality.executed == "failed" else 0


def cmd_fetch(ns) -> int:
    from wrr.schemas import ExtractOptions
    from wrr.formatters import format_extract
    from wrr import config
    if not ns.url.lower().startswith(("http://", "https://")):
        _eprint("✗ url 须以 http:// 或 https:// 开头")
        return 2
    if ns.max_chars < 1 or ns.max_chars > config.MAX_MAX_CHARACTERS:
        _eprint(f"✗ --max-chars 须在 1..{config.MAX_MAX_CHARACTERS}")
        return 2
    opts = ExtractOptions(url=ns.url, max_characters=ns.max_chars,
                          provider=ns.provider, allow_private_origin=ns.allow_private_origin)
    return _dispatch(
        "extract", opts, ns.provider, ns.url, ns.json, ns.quiet, format_extract,
        host_fallback_requested=ns.host_fallback == "request",
    )


def cmd_similar(ns) -> int:
    from wrr.schemas import SimilarOptions
    from wrr.formatters import format_similar
    from wrr import config
    if not ns.url.lower().startswith(("http://", "https://")):
        _eprint("✗ url 须以 http:// 或 https:// 开头")
        return 2
    if ns.count < 1 or ns.count > config.MAX_SEARCH_COUNT:
        _eprint(f"✗ --count 须在 1..{config.MAX_SEARCH_COUNT}")
        return 2
    opts = SimilarOptions(url=ns.url, count=ns.count, provider=ns.provider)
    return _dispatch("similar", opts, ns.provider, ns.url,
                     ns.json, ns.quiet, format_similar)


def cmd_test(ns) -> int:
    """冒烟测试或单元测试入口。"""
    from wrr.schemas import ExtractOptions, SimilarOptions
    from wrr.errors import WRRError

    if ns.what == "unit":
        return cmd_test_unit(ns)

    prov = ns.provider
    cases = [
        ("extract", ExtractOptions(url="https://example.com",
                                   max_characters=500, provider=prov)),
        # similar 仅 Exa 支持；显式 provider 非 exa 时跳过
        ("similar", SimilarOptions(url="https://example.com",
                                   count=3, provider=prov)),
    ]

    report = []
    overall_ok = True
    for op, opts in cases:
        if op == "similar" and prov not in (None, "exa"):
            report.append({"op": op, "status": "skip",
                           "detail": f"similar 仅 exa 支持，当前 --provider={prov}"})
            continue
        try:
            result = asyncio.run(_run(op, opts, prov))
            n = (len(result.payload) if isinstance(result.payload, list)
                 else len(getattr(result.payload, "text", "") or ""))
            report.append({"op": op, "status": "ok",
                           "provider": result.actual_provider, "count": n})
        except WRRError as e:
            overall_ok = False
            report.append({"op": op, "status": "fail",
                           "error": type(e).__name__, "detail": str(e)[:300]})
        except Exception as e:  # noqa: BLE001 — 冒烟测试要兜住一切
            overall_ok = False
            report.append({"op": op, "status": "fail",
                           "error": type(e).__name__, "detail": str(e)[:300]})

    if ns.json:
        _emit_json({"ok": overall_ok, "provider": prov, "cases": report})
    else:
        print("wrr-cli 冒烟测试" + (f"（provider={prov}）" if prov else "（默认 fallback 链）"))
        for r in report:
            mark = {"ok": "✓", "fail": "✗", "skip": "—"}[r["status"]]
            if r["status"] == "ok":
                print(f"  {mark} {r['op']:<8} provider={r['provider']} count={r['count']}")
            elif r["status"] == "skip":
                print(f"  {mark} {r['op']:<8} {r['detail']}")
            else:
                print(f"  {mark} {r['op']:<8} {r['error']}: {r['detail']}")
        print("结果：" + ("全部通过" if overall_ok else "存在失败"))
    return 0 if overall_ok else 1


def cmd_test_unit(ns) -> int:
    """运行 wrr 单元测试。"""
    import subprocess

    env = os.environ.copy()
    args = ["pytest", "tests/unit", "-q"]
    if ns.json:
        args = ["pytest", "tests/unit", "-q", "--json-report"]
    print("运行单元测试：" + " ".join(args))
    return subprocess.call(args, env=env)


def cmd_inventory(ns) -> int:
    """List sources using snapshot credentials and non-live registry health."""
    import os
    from wrr.core.contracts import Health, Provision
    from wrr.core.inventory import build_inventory, invalid_manifests
    from wrr.runtime.detect import detect_runtime
    from wrr.runtime.env import load_env as load_snapshot

    runtime = detect_runtime(explicit=ns.runtime)
    env = load_snapshot(
        runtime, overrides=os.environ, env_files=[ns.env] if ns.env else None,
        trust_project=ns.trust_project,
    )
    sources = build_inventory(runtime=runtime, env=env, trust_project=ns.trust_project)
    invalid = invalid_manifests(runtime=runtime, trust_project=ns.trust_project)
    if ns.json:
        _emit_json({
            "sources": [source.to_dict() for source in sources],
            "routable": [source.id for source in sources if source.routable],
            "invalid": invalid,
            "summary": {
                "provision": {
                    state.value: sum(source.provision == state for source in sources)
                    for state in Provision
                },
                "health": {
                    state.value: sum(source.health == state for source in sources)
                    for state in Health
                },
            },
        })
    else:
        print(f"{'Source':<24} {'Transport':<10} {'Provision':<12} {'Health':<10} Routable")
        print("-" * 70)
        for source in sources:
            print(f"{source.id:<24} {source.transport.value:<10} "
                  f"{source.provision.value:<12} {source.health.value:<10} "
                  f"{'yes' if source.routable else 'no'}")
        for item in invalid:
            reasons = ", ".join(item["errors"] + item["blocked"]) or "blocked"
            print(f"! {item['path']}: {reasons}")
    return 0


def cmd_install(ns) -> int:
    """v6 install surface: report-only in P0."""
    from wrr.cli.install import install

    if not ns.dry_run:
        _eprint("✗ P0 install 仅支持 --dry-run，不写文件")
        return 2

    report = install(
        dry_run=True,
        runtime_hint=ns.runtime,
        trust_project=ns.trust_project,
        env_files=[ns.env] if ns.env else None,
        refresh_deps=ns.refresh_deps,
    )
    if ns.json:
        _emit_json(report.to_dict())
    else:
        payload = report.to_dict()
        print("WRR v6 install dry-run")
        print(f"  runtime: {payload['runtime']['name']}")
        print(f"  config target: {payload['config_target']}")
        print("  env candidates:")
        for candidate in payload["env_candidates"]:
            marker = "loaded" if candidate["loaded"] else candidate.get("reason") or "not_loaded"
            print(f"    - {candidate['path']} [{candidate['trust_level']}, {marker}]")
        print(f"  missing required env: {len(payload['missing_required_env'])}")
        print(f"  dependency updates: {len(payload['dependency_updates'])}")
        print("  writes performed: 0")
    return 0


def cmd_update(ns) -> int:
    """v6 dependency update surface."""
    from wrr.cli.update import update

    report = update(
        dry_run=ns.dry_run,
        trust_project=ns.trust_project,
        owned_paths=ns.owned_repo,
        candidate_root=ns.candidate_root,
    )
    payload = report.to_dict()
    if ns.json:
        _emit_json(payload)
    else:
        summary = payload["summary"]
        print("WRR dependency candidates (no activation)")
        print(f"  repos: {summary['repos']}")
        print(f"  planned: {summary['planned']}")
        print(f"  downloaded: {summary['downloaded']}")
        print(f"  refused: {summary['refused']}")
        print(f"  failed: {summary['failed']}")
        for item in payload["repos"]:
            print(f"  {item['path']}: {item['status']} ({item['message']})")
            if item["candidate_path"]:
                print(f"    candidate: {item['candidate_path']}")
                print(f"    revision: {item['candidate_revision']}")
                print("    activated: false")
        for error in payload["errors"]:
            print(f"  refused: {error}")
    return 1 if payload["summary"]["status"] == "fail" else 0


def cmd_doctor(ns) -> int:
    """Doctor: 检查引擎健康状况和本地依赖。"""
    # --env-report is a separate diagnostic that does not run doctor checks.
    if getattr(ns, "env_report", False):
        return _cmd_env_report(ns)

    # Validate --profile-matrix constraints
    if getattr(ns, "profile_matrix", False):
        if not getattr(ns, "v6", False):
            _eprint("✗ --profile-matrix requires --v6")
            return 2
        if ns.deep:
            _eprint("✗ --profile-matrix does not support --deep")
            return 2
        if not ns.json:
            _eprint("✗ --profile-matrix requires --json")
            return 2
        if ns.engine:
            _eprint("✗ --profile-matrix does not support --engine")
            return 2
        if ns.tier is not None:
            _eprint("✗ --profile-matrix does not support --tier")
            return 2

    if getattr(ns, "v6", False):
        # Handle --profile-matrix
        if getattr(ns, "profile_matrix", False):
            from wrr.doctor import doctor_profile_matrix

            try:
                report = doctor_profile_matrix(
                    json=ns.json,
                    deep=False,
                    trust_project=ns.trust_project,
                    runtime_hint=ns.runtime,
                    env_files=[ns.env] if ns.env else None,
                )
            except Exception as e:
                _eprint(f"✗ v6 Profile Matrix 失败: {type(e).__name__}: {e}")
                return 1
            _emit_json(report.to_dict())
            status = report.summary.get("status")
            return 1 if status == "fail" else 0

        # Regular --v6 doctor
        from wrr.doctor import doctor_v6

        try:
            report = doctor_v6(
                json=ns.json,
                deep=ns.deep,
                trust_project=ns.trust_project,
                runtime_hint=ns.runtime,
                env_files=[ns.env] if ns.env else None,
            )
        except Exception as e:
            _eprint(f"✗ v6 Doctor 失败: {type(e).__name__}: {e}")
            return 1
        if ns.json:
            _emit_json(report.to_dict())
        else:
            payload = report.to_dict()
            summary = payload["summary"]
            print("WRR v6 doctor")
            print(f"  runtime: {payload['runtime']['name']}")
            print(
                "  engines: "
                f"discovered={summary['discovered']} "
                f"resolved={summary['resolved']} "
                f"routable={summary['routable']}"
            )
            print(
                "  health: "
                f"healthy={summary['healthy']} "
                f"degraded={summary['degraded']} "
                f"unhealthy={summary['unhealthy']}"
            )
        status = report.to_dict()["summary"].get("status")
        return 1 if status == "fail" or (ns.strict and status == "warn") else 0

    from wrr.registry import get_registry
    from wrr.doctor import run_doctor, summarize_checks, doctor_exit_code, run_deps_doctor, summarize_deps
    from wrr.formatters import format_doctor_report

    registry = get_registry()

    # 验证 --engine 参数（如果指定）
    if ns.engine:
        if ns.engine not in registry.names():
            _eprint(f"✗ 未知引擎: {ns.engine}")
            _eprint(f"  可用引擎: {', '.join(registry.names())}")
            return 2

    # 运行 doctor
    try:
        results = asyncio.run(run_doctor(
            registry,
            engine=ns.engine,
            tier=ns.tier,
            deep=ns.deep,
        ))
        # v5.5: 同时检查外部依赖
        deps_results = []
        if not ns.engine:  # 只在全量检查时跑 deps
            deps_results = asyncio.run(run_deps_doctor(deep=ns.deep))
    except ValueError as e:
        _eprint(f"✗ {e}")
        return 2
    except Exception as e:
        _eprint(f"✗ Doctor 失败: {type(e).__name__}: {e}")
        return 1

    # 输出结果
    if ns.json:
        summary = summarize_checks(results)
        payload = {
            "ok": summary["status"] == "ok",
            "status": summary["status"],
            "summary": {k: summary[k] for k in ("ok", "warn", "fail", "skip")},
            "engines": [r.to_dict() for r in results],
        }
        if deps_results:
            deps_summary = summarize_deps(deps_results)
            payload["deps"] = deps_results
            payload["deps_summary"] = deps_summary
            if deps_summary["status"] == "fail":
                payload["ok"] = False
        _emit_json(payload)
    else:
        print(format_doctor_report(results))
        if deps_results:
            _print_deps_report(deps_results)

    # 返回退出码
    return doctor_exit_code(results, strict=ns.strict)


def _cmd_env_report(ns) -> int:
    """Print a profile-aware environment report for the current WRR runtime."""
    import os
    from wrr import config

    rows = [
        ("EXA_API_KEY", "exa, brave fallback", "web search"),
        ("BRAVE_API_KEY", "brave", "web search"),
        ("GITHUB_TOKEN", "github", "GitHub repo/issue search"),
        ("SEARXNG_URL", "searxng", "SearXNG self-hosted / recovery"),
        ("OPENROUTER_API_KEY", "(not directly used by WRR)", "Hermes/OpenRouter only"),
    ]
    report = []
    for env_name, engines, impact in rows:
        value = os.environ.get(env_name)
        present = value is not None and value != ""
        status = "ok" if present else "missing"
        report.append({
            "env": env_name,
            "loaded": present,
            "engines": engines,
            "impact": impact,
            "status": status,
        })

    if ns.json:
        _emit_json({
            "profile": "current shell",
            "loaded_env": config.get_env.__module__,
            "variables": report,
        })
        return 0

    print("WRR environment report")
    print(f"{'Variable':<20} {'Status':<8} {'Engines':<22} {'Impact'}")
    print("-" * 70)
    mark = {"ok": "✅", "missing": "❌"}
    for r in report:
        print(f"{r['env']:<20} {mark.get(r['status'], r['status']):<8} {r['engines']:<22} {r['impact']}")
    print()
    print("Notes:")
    print("  • GITHUB_TOKEN absence makes github engine unavailable.")
    print("  • SEARXNG_URL absence uses built-in defaults or disables recovery mode.")
    print("  • OPENROUTER_API_KEY is not consumed by WRR itself; use it in Hermes/OpenRouter.")
    return 0


# ── argparse 组装 ────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wrr-cli.py",
        description="Web Research Router CLI —— 独立于 Hermes 的 search/fetch/similar。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  wrr-cli.py search \"claude opus 4.8\" --exa-mode deep --count 5\n"
               "  wrr-cli.py fetch https://exa.ai --max-chars 2000 --json\n"
               "  wrr-cli.py similar https://exa.ai\n"
               "  wrr-cli.py test --provider exa",
    )
    # 全局选项（放父 parser，子命令继承）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    common.add_argument("--env", metavar="PATH", help="指定 .env 路径（查询默认 $WRR_ENV 或 ~/.config/wrr/env.d/default.env）")
    common.add_argument("-q", "--quiet", action="store_true", help="不打印 provider 等元信息")
    common.add_argument("--provider", choices=["exa", "deepseek", "brave", "searxng", "github", "community", "academic", "skill",
                                               "local_supermemory", "local_session", "local_qmd", "local_obsidian"],
                        help="强制单引擎（禁用 fallback）")

    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory", help="发现本机搜索来源（仅 light 探测与健康缓存）")
    inv.add_argument("--json", action="store_true", help="输出来源、可路由 ID 与状态计数")
    inv.add_argument("--env", metavar="PATH", help="指定凭据环境文件")
    inv.add_argument("--runtime", choices=["hermes", "claude_code", "codex", "omp", "standalone", "unknown"],
                     help="显式指定 runtime")
    inv.add_argument("--trust-project", action="store_true", help="显式信任项目级来源与凭据")
    inv.set_defaults(func=cmd_inventory, quiet=True)

    sp = sub.add_parser("search", parents=[common], help="自动选源、融合与质量判断")
    sp.add_argument("query", nargs="?")
    sp.add_argument("--rpc", action="store_true", help="单次搜索的 stdin/stdout NDJSON 宿主桥")
    sp.add_argument("--count", type=int, default=8, help="交付结果数（默认 8）")
    sp.add_argument("--sources", help="显式来源 ID，逗号分隔（最多 4 个）")
    sp.add_argument("--runtime", choices=["hermes", "claude_code", "codex", "omp", "standalone", "unknown"])
    sp.add_argument("--trust-project", action="store_true", help="显式信任项目级来源与凭据")
    sp.add_argument("--route-mode", default=None, help="已移除；使用自动选源或 --sources")
    sp.add_argument(
        "--exa-mode",
        choices=["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"],
        default=None,
        help="Exa API search type",
    )
    sp.add_argument(
        "--mode",
        choices=["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"],
        default=None,
        help="已移除；请改用 --exa-mode",
    )
    sp.set_defaults(func=cmd_search)

    fp = sub.add_parser("fetch", parents=[common], help="抓取 URL 正文")
    fp.add_argument("url")
    fp.add_argument("--max-chars", type=int, default=5000, dest="max_chars",
                    help="正文截断上限（默认 5000）")
    fp.add_argument("--allow-private-origin", metavar="ORIGIN",
                    help="本次已获用户授权的精确 scheme/host/port；须显式 --provider brave，不持久保存")
    fp.add_argument(
        "--host-fallback", choices=["request"],
        help="显式请求宿主备用判定元数据；不调用或授权宿主工具",
    )
    fp.set_defaults(func=cmd_fetch)

    mp = sub.add_parser("similar", parents=[common], help="查找相似页面（仅 Exa）")
    mp.add_argument("url")
    mp.add_argument("--count", type=int, default=10, help="结果数（默认 10）")
    mp.set_defaults(func=cmd_similar)

    tp = sub.add_parser("test", parents=[common], help="冒烟测试 search/fetch/similar 或运行单元测试")
    tp.add_argument("what", nargs="?", default="smoke", choices=["smoke", "unit"],
                    help="测试类型：smoke（默认）或 unit")
    tp.set_defaults(func=cmd_test)


    hp = sub.add_parser(
        "host-fetch-result",
        help="从 stdin 校验一次已接受的宿主原生 fetch 回传；不调用 provider 或宿主工具",
    )
    hp.set_defaults(func=cmd_host_fetch_result, quiet=True, json=True, env=None)
    hep = sub.add_parser(
        "host-fetch-eligibility",
        help="判定 URL 是否可交给宿主原生 public fetch（需要 DNS 安全检查）；不加载 provider 或凭据配置",
    )
    hep.add_argument("url")
    hep.set_defaults(func=cmd_host_fetch_eligibility, quiet=True, json=True, env=None)


    ip = sub.add_parser(
        "install",
        help="生成 v6 install 报告；当前必须显式 --dry-run，默认不写文件",
        description=(
            "生成 v6 install 迁移报告。当前迁移窗口内 install 是 opt-in dry-run "
            "surface；不会创建配置或写入依赖。"
        ),
    )
    ip.add_argument(
        "--dry-run",
        action="store_true",
        help="必填：只输出 v6 install 计划，不写配置、不克隆 repo、不修改依赖",
    )
    ip.add_argument("--json", action="store_true", help="输出 JSON 格式")
    ip.add_argument("--env", metavar="PATH", help="指定 v6 env 文件")
    ip.add_argument("--runtime", choices=["hermes", "claude_code", "codex", "omp", "standalone", "unknown"],
                    help="显式指定 v6 runtime")
    ip.add_argument(
        "--trust-project",
        action="store_true",
        help="显式信任项目级插件、adapter 和项目 .env secret；默认不信任 project scope",
    )
    ip.add_argument(
        "--refresh-deps",
        action="store_true",
        help="把 v6 repo 依赖刷新计划并入 install 报告；配合 --dry-run 时只列计划",
    )
    ip.add_argument("-q", "--quiet", action="store_true", help="不打印元信息")
    ip.set_defaults(func=cmd_install)

    up = sub.add_parser(
        "update",
        help="规划或隔离下载依赖候选；不改运行目标，默认 dry-run",
        description=(
            "为 manifest 依赖准备固定提交候选，不 checkout 现有目标。"
            "执行需要 --apply、精确 --owned-repo 与既有 --candidate-root；不会激活。"
        ),
    )
    up.add_argument("--dry-run", action="store_true", default=True, help="默认行为：只输出计划，不执行 git 操作")
    up.add_argument("--apply", action="store_false", dest="dry_run", help="通过授权检查后隔离下载候选，不改运行目标")
    up.add_argument("--owned-repo", action="append", default=[], metavar="PATH",
                    help="选择并确认本次拥有维护权的精确 WRR 私有目标；可重复；不用于宿主或共享资源")
    up.add_argument("--candidate-root", metavar="PATH",
                    help="显式提供既有、独立、拥有写权的候选输出目录")
    up.add_argument("--json", action="store_true", help="输出 JSON 格式")
    up.add_argument(
        "--trust-project",
        action="store_true",
        help="信任 project-level manifest 来源；不代替目标所有权或切换授权",
    )
    up.add_argument("-q", "--quiet", action="store_true", help="不打印元信息")
    up.set_defaults(func=cmd_update)

    # doctor 子命令（v5.1，不继承 common 中的 --provider，因需独立 --engine）
    dp = sub.add_parser(
        "doctor",
        help="检查引擎健康状况和本地依赖；默认保持 legacy JSON",
        description=(
            "默认运行 legacy doctor，保持旧 JSON schema。传 --v6 才启用 v6 "
            "control-plane doctor。"
        ),
    )
    dp.add_argument("--engine", choices=["exa", "deepseek", "brave", "github", "skill", "searxng", "community", "academic",
                                         "local_supermemory", "local_session", "local_qmd", "local_obsidian"],
                    help="仅检查指定引擎")
    dp.add_argument("--tier", type=int, choices=[0, 1, 2], help="仅检查指定 tier")
    dp.add_argument("--json", action="store_true", help="输出 JSON 格式")
    dp.add_argument("--env", metavar="PATH", help="指定 .env 路径")
    dp.add_argument("-q", "--quiet", action="store_true", help="不打印元信息")
    dp.add_argument("--strict", action="store_true", help="严格模式：warn 也视为失败（退出码 1）")
    dp.add_argument("--deep", action="store_true", help="深度探测：执行命令/API 实际验证（较慢）")
    dp.add_argument(
        "--env-report",
        action="store_true",
        help="仅输出环境变量与引擎可用性报告，不运行 doctor 检查",
    )
    dp.add_argument(
        "--v6",
        action="store_true",
        help="opt-in 使用 v6 control-plane doctor；不改变默认 legacy doctor 输出",
    )
    dp.add_argument("--runtime", choices=["hermes", "claude_code", "codex", "omp", "standalone", "unknown"],
                    help="显式指定 v6 runtime")
    dp.add_argument(
        "--trust-project",
        action="store_true",
        help="仅配合 --v6：显式信任项目级插件、adapter 和项目 .env secret",
    )
    dp.add_argument(
        "--profile-matrix",
        action="store_true",
        help="仅配合 --v6：输出多 runtime/profile 的静态 light 诊断矩阵（需 --json）",
    )
    dp.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    if ns.cmd in {"host-fetch-result", "host-fetch-eligibility"}:
        return ns.func(ns)

    use_v6_env = ns.cmd in {"install", "inventory"} or (ns.cmd == "doctor" and getattr(ns, "v6", False))
    try:
        loaded = None if use_v6_env else load_env(getattr(ns, "env", None))
    except (OSError, UnicodeError) as exc:
        detail = f"WRR env 加载失败 ({type(exc).__name__})；未尝试其他凭据文件。"
        if getattr(ns, "json", False):
            from wrr.formatters import failed_quality_payload
            _emit_json({
                "operation": "extract" if ns.cmd == "fetch" else ns.cmd,
                "ok": False, "error": "configuration_error", "detail": detail,
                "quality": failed_quality_payload(),
            })
        else:
            _eprint(detail)
        return 2
    if not ns.quiet and not getattr(ns, "json", False) and not getattr(ns, "rpc", False):
        if loaded:
            _eprint(f"· 已加载 env：{loaded}")
        else:
            _eprint("· 未找到 .env（将仅依赖现有环境变量）")

    return ns.func(ns)


def _print_deps_report(deps_results: list) -> None:
    """打印全量依赖检查报告。"""
    print()
    print("── 全量依赖 ──")
    for d in deps_results:
        icon = {"ok": "✓", "degraded": "⚠", "missing": "✗"}.get(d["status"], "?")
        dep_type = d.get("type", "?")
        required = "" if d.get("required", True) else " (可选)"
        print(f"  {icon} {d['id']} [{dep_type}]{required} — {d['status']}")
        if d.get("source_url"):
            print(f"     source: {d['source_url']}")
        if d.get("version") and d["version"] != "unknown":
            print(f"     version: {d['version']}")
        if d.get("detail"):
            print(f"     {d['detail']}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _eprint("\n已中断")
        sys.exit(130)
