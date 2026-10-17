"""Behavioral contract for isolated, externally authorized WRR release selection."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import wrr.cli.release_supervisor as release_supervisor

from wrr.cli.release_supervisor import (
    SupervisorRefusal,
    execute,
    release_identity,
)


PRECHECKS = (
    "candidate_git_safety",
    "targeted_tests",
    "full_unit_suite",
    "finite_local_git_smoke",
    "independent_review",
    "controlled_omp_consumer",
    "controlled_hermes_consumer",
)
POSTCHECKS = ("post_switch_omp", "post_switch_hermes")
MISSING_EVIDENCE_KIND = object()


def git(*args: object) -> str:
    return subprocess.run(
        ["git", *map(str, args)], check=True, capture_output=True, text=True,
    ).stdout.strip()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def make_release_source(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "origin"
    git("init", source)
    git("-C", source, "config", "user.email", "fixture@example.invalid")
    git("-C", source, "config", "user.name", "Fixture")
    (source / "pyproject.toml").write_text('[project]\nname="fixture-wrr"\nversion="1.0.0"\n')
    (source / "payload.txt").write_text("old\n")
    git("-C", source, "add", ".")
    git("-C", source, "commit", "-m", "old")
    old_revision = git("-C", source, "rev-parse", "HEAD")
    (source / "pyproject.toml").write_text('[project]\nname="fixture-wrr"\nversion="2.0.0"\n')
    (source / "payload.txt").write_text("new\n")
    git("-C", source, "commit", "-am", "new")
    return source, old_revision, git("-C", source, "rev-parse", "HEAD")


def evidence_receipt(
    inputs: Path,
    identity: dict,
    *,
    post: bool = False,
    activation: dict | None = None,
    evidence_kind: str = "isolated_fixture",
) -> tuple[Path, list[Path]]:
    check_ids = POSTCHECKS if post else PRECHECKS
    artifacts = []
    checks = []
    for check_id in check_ids:
        artifact = inputs / f"{check_id}.json"
        write_json(artifact, {"check": check_id, "verdict": "PASS"})
        artifacts.append(artifact)
        checks.append({
            "id": check_id,
            "verdict": "PASS",
            "evidence_path": str(artifact),
            "evidence_sha256": sha256_file(artifact),
        })
    receipt = {
        "schema_version": 1,
        "evidence_kind": evidence_kind,
        "producer": "fixture_harness" if evidence_kind == "isolated_fixture" else "real_host_matrix",
        "candidate": identity,
        "checks": checks,
        "consumer_verdicts": {"omp": "PASS", "hermes": "PASS"},
    }
    if activation is not None:
        receipt["activation_transaction_id"] = activation["transaction_id"]
        receipt["activation_generation"] = activation["generation_after"]
    path = inputs / ("post-verification.json" if post else "verification.json")
    write_json(path, receipt)
    return path, artifacts


def impact_receipt(inputs: Path, identity: dict) -> tuple[Path, Path]:
    evidence = inputs / "impact-evidence.json"
    write_json(evidence, {"review": "no migration or shared-state impact"})
    path = inputs / "impact.json"
    write_json(path, {
        "schema_version": 1,
        "producer": "release_impact_review",
        "evidence_kind": "isolated_fixture",
        "candidate_revision": identity["revision"],
        "candidate_tree_sha256": identity["tree_sha256"],
        "config_schema_change": False,
        "state_schema_change": False,
        "shared_resource_change": False,
        "requires_migration": False,
        "rollback_compatible": True,
        "evidence": [{"path": str(evidence), "sha256": sha256_file(evidence)}],
    })
    return path, evidence


def operation_paths(root: Path, transaction_id: str, output: Path) -> dict[str, str]:
    return {
        "active_link": str(root / "active"),
        "releases_root": str(root / "releases"),
        "lock": str(root / ".wrr-release.lock"),
        "generation": str(root / ".wrr-release-generation.json"),
        "generation_tmp": str(root / f".wrr-release-generation.{transaction_id}.tmp"),
        "journal": str(root / ".wrr-release-journal.json"),
        "journal_guard": str(root / ".wrr-release-journal.guard.json"),
        "journal_tmp": str(root / f".wrr-release-journal.{transaction_id}.tmp"),
        "active_tmp": str(root / f".active.{transaction_id}.tmp"),
        "output_receipt": str(output),
    }


def allowed_writes(
    paths: dict[str, str], *, transactional: bool, cleanup_paths: list[str] | None = None,
) -> list[str]:
    names = ["output_receipt"]
    if transactional:
        names += [
            "active_link", "active_tmp", "journal", "journal_guard",
            "journal_tmp", "generation", "generation_tmp",
        ]
    return sorted({*(paths[name] for name in names), *(cleanup_paths or [])})


def authorization(
    case: dict,
    plan_path: Path,
    plan: dict,
    *,
    inputs: list[Path],
    transactional: bool,
) -> tuple[Path, str]:
    auth_path = case["inputs"] / f"authorization-{plan['transaction_id']}.json"
    all_inputs = [plan_path, *inputs]
    auth = {
        "schema_version": 1,
        "authorization_id": plan["authorization_id"],
        "transaction_id": plan["transaction_id"],
        "action": plan["operation"],
        "evidence_kind": plan.get("evidence_kind", "isolated_fixture"),
        "expires_at": "2999-01-01T00:00:00Z",
        "deployment_root": case["root_identity"],
        "paths": {
            "releases_root": str(case["releases"]),
            "active_link": str(case["active"]),
            "lock": str(case["lock"]),
            "generation": str(case["state"]),
            "journal": str(case["journal"]),
            "journal_guard": str(case["guard"]),
        },
        "expected_generation": plan["expected_generation"],
        "before": plan["before"],
        "after": plan["after"],
        "inputs": [{"path": str(path), "sha256": sha256_file(path)} for path in all_inputs],
        "allowed_writes": allowed_writes(
            plan["paths"],
            transactional=transactional,
            cleanup_paths=plan.get("cleanup_paths"),
        ),
    }
    write_json(auth_path, auth)
    return auth_path, sha256_file(auth_path)


@pytest.fixture
def deployment(tmp_path: Path) -> dict:
    source, old_revision, new_revision = make_release_source(tmp_path)
    root = tmp_path / "deployment"
    releases = root / "releases"
    inputs = tmp_path / "inputs"
    releases.mkdir(parents=True)
    root = root.resolve()
    releases = root / "releases"
    inputs.mkdir()
    os.chmod(root, 0o700)
    os.chmod(releases, 0o700)
    old = releases / "old"
    new = releases / "new"
    git("clone", source, old)
    git("-C", old, "checkout", "--detach", old_revision)
    git("clone", source, new)
    git("-C", new, "checkout", "--detach", new_revision)
    active = root / "active"
    active.symlink_to(old, target_is_directory=True)
    lock = root / ".wrr-release.lock"
    lock.touch(mode=0o600)
    state = root / ".wrr-release-generation.json"
    write_json(state, {
        "schema_version": 1,
        "generation": 0,
        "last_transaction_id": None,
        "active_target": str(old),
        "committed_operation": None,
        "committed_receipt_path": None,
        "committed_receipt_sha256": None,
    })
    os.chmod(state, 0o600)
    config = tmp_path / "config.json"
    write_json(config, {"provider": "controlled"})
    root_stat = root.stat()
    origin = str(source.resolve())
    case = {
        "root": root,
        "root_identity": {"path": str(root), "st_dev": root_stat.st_dev, "st_ino": root_stat.st_ino},
        "releases": releases,
        "inputs": inputs,
        "active": active,
        "lock": lock,
        "state": state,
        "journal": root / ".wrr-release-journal.json",
        "guard": root / ".wrr-release-journal.guard.json",
        "old": old,
        "new": new,
        "old_identity": release_identity(old, expected_origin=origin),
        "new_identity": release_identity(new, expected_origin=origin),
        "config": config,
        "config_sha256": sha256_file(config),
    }
    return case


def activation_bundle(case: dict, transaction_id: str = "activate-1") -> tuple[Path, Path, str]:
    verification, verification_artifacts = evidence_receipt(case["inputs"], case["new_identity"])
    impact, impact_evidence = impact_receipt(case["inputs"], case["new_identity"])
    output = case["root"] / f"activation-{transaction_id}.json"
    paths = operation_paths(case["root"], transaction_id, output)
    plan = {
        "schema_version": 1,
        "operation": "activate",
        "transaction_id": transaction_id,
        "authorization_id": f"auth-{transaction_id}",
        "evidence_kind": "isolated_fixture",
        "deployment_root": case["root_identity"],
        "paths": paths,
        "expected_generation": read_json(case["state"])["generation"],
        "before": case["old_identity"],
        "after": case["new_identity"],
        "verification": {"path": str(verification), "sha256": sha256_file(verification)},
        "impact": {"path": str(impact), "sha256": sha256_file(impact)},
        "configs": [{"path": str(case["config"]), "sha256": case["config_sha256"]}],
        "output_receipt": str(output),
        "rollback_supported": True,
    }
    plan_path = case["inputs"] / f"plan-{transaction_id}.json"
    write_json(plan_path, plan)
    auth_path, auth_digest = authorization(
        case,
        plan_path,
        plan,
        inputs=[verification, impact, case["config"], impact_evidence, *verification_artifacts],
        transactional=True,
    )
    return plan_path, auth_path, auth_digest


def simple_bundle(
    case: dict,
    *,
    operation: str,
    transaction_id: str,
    before: dict,
    after: dict,
    expected_generation: int,
    activation_receipt: Path | None = None,
    verification: Path | None = None,
) -> tuple[Path, Path, str]:
    output = case["root"] / f"{operation}-{transaction_id}.json"
    paths = operation_paths(case["root"], transaction_id, output)
    plan = {
        "schema_version": 1,
        "operation": operation,
        "transaction_id": transaction_id,
        "authorization_id": f"auth-{transaction_id}",
        "evidence_kind": "isolated_fixture",
        "deployment_root": case["root_identity"],
        "paths": paths,
        "expected_generation": expected_generation,
        "before": before,
        "after": after,
        "configs": [{"path": str(case["config"]), "sha256": case["config_sha256"]}],
        "output_receipt": str(output),
    }
    extra_inputs = [case["config"]]
    if activation_receipt is not None:
        plan["activation_receipt"] = {
            "path": str(activation_receipt),
            "sha256": sha256_file(activation_receipt),
        }
        extra_inputs.append(activation_receipt)
    if verification is not None:
        plan["verification"] = {"path": str(verification), "sha256": sha256_file(verification)}
        extra_inputs.append(verification)
        for check in read_json(verification)["checks"]:
            extra_inputs.append(Path(check["evidence_path"]))
    if operation == "recover":
        record = case["journal"] if case["journal"].exists() else case["guard"]
        journal_value = read_json(record)
        plan["journal"] = {
            "path": str(record),
            "sha256": sha256_file(record),
        }
        plan["cleanup_paths"] = [
            journal_value["paths"][name]
            for name in ("active_tmp", "journal_tmp", "generation_tmp")
        ]
        interrupted_receipt = Path(journal_value["receipt_path"])
        if interrupted_receipt.exists():
            plan["interrupted_receipt"] = {
                "path": str(interrupted_receipt),
                "sha256": sha256_file(interrupted_receipt),
            }
            if interrupted_receipt not in extra_inputs:
                extra_inputs.append(interrupted_receipt)
        extra_inputs.append(record)
    plan_path = case["inputs"] / f"plan-{transaction_id}.json"
    write_json(plan_path, plan)
    auth_path, auth_digest = authorization(
        case,
        plan_path,
        plan,
        inputs=extra_inputs,
        transactional=operation in {"rollback", "recover"},
    )
    return plan_path, auth_path, auth_digest


def invoke(bundle: tuple[Path, Path, str], operation: str, **kwargs) -> dict:
    plan, auth, digest = bundle
    return execute(
        operation,
        plan,
        authorization_path=auth,
        authorization_sha256=digest,
        apply=True,
        **kwargs,
    )


def test_activate_finalize_and_rollback_are_distinct_observable_states(deployment):
    activation = invoke(activation_bundle(deployment), "activate")
    assert activation["status"] == "provisional"
    assert activation["fixture_only"] is True
    assert activation["real_host_verified"] is False
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 1
    assert sha256_file(deployment["config"]) == deployment["config_sha256"]

    activation_path = Path(activation["receipt_path"])
    post, _ = evidence_receipt(
        deployment["inputs"], deployment["new_identity"], post=True, activation=activation,
    )
    final_bundle = simple_bundle(
        deployment,
        operation="finalize",
        transaction_id="finalize-1",
        before=deployment["new_identity"],
        after=deployment["new_identity"],
        expected_generation=1,
        activation_receipt=activation_path,
        verification=post,
    )
    finalized = invoke(final_bundle, "finalize")
    assert finalized["status"] == "finalized_fixture_only"
    assert finalized["real_host_verified"] is False

    rollback_bundle = simple_bundle(
        deployment,
        operation="rollback",
        transaction_id="rollback-1",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=activation_path,
    )
    rolled_back = invoke(rollback_bundle, "rollback")
    assert rolled_back["status"] == "rolled_back"
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 2
    assert sha256_file(deployment["config"]) == deployment["config_sha256"]


def test_authorization_is_acyclic_and_any_plan_mutation_refuses_without_writes(deployment):
    plan, auth, digest = activation_bundle(deployment)
    value = read_json(plan)
    value["after"]["version"] = "forged"
    write_json(plan, value)
    with pytest.raises(SupervisorRefusal, match="authorized_input_digest_mismatch"):
        invoke((plan, auth, digest), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda receipt: receipt.update(checks=[]), "verification_checks_mismatch"),
        (lambda receipt: receipt.update(evidence_kind="real_host"), "evidence_kind_mismatch"),
    ],
)
def test_empty_or_kind_confused_verification_never_switches(deployment, mutation, code):
    plan, _, _ = activation_bundle(deployment)
    plan_value = read_json(plan)
    verification = Path(plan_value["verification"]["path"])
    receipt = read_json(verification)
    mutation(receipt)
    write_json(verification, receipt)
    plan_value["verification"]["sha256"] = sha256_file(verification)
    write_json(plan, plan_value)
    auth, digest = authorization(
        deployment,
        plan,
        plan_value,
        inputs=[verification, Path(plan_value["impact"]["path"]), deployment["config"]],
        transactional=True,
    )
    with pytest.raises(SupervisorRefusal, match=code):
        invoke((plan, auth, digest), "activate")
    assert deployment["active"].resolve() == deployment["old"]


def test_migration_or_unknown_impact_is_a_pre_switch_refusal(deployment):
    plan, _, _ = activation_bundle(deployment)
    plan_value = read_json(plan)
    impact = Path(plan_value["impact"]["path"])
    impact_value = read_json(impact)
    impact_value["requires_migration"] = True
    write_json(impact, impact_value)
    plan_value["impact"]["sha256"] = sha256_file(impact)
    write_json(plan, plan_value)
    verification = Path(plan_value["verification"]["path"])
    evidence_inputs = [
        Path(item["evidence_path"]) for item in read_json(verification)["checks"]
    ]
    impact_inputs = [Path(item["path"]) for item in impact_value["evidence"]]
    auth, digest = authorization(
        deployment,
        plan,
        plan_value,
        inputs=[impact, verification, deployment["config"], *evidence_inputs, *impact_inputs],
        transactional=True,
    )
    with pytest.raises(SupervisorRefusal, match="candidate_requires_migration"):
        invoke((plan, auth, digest), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0


def test_unknown_impact_field_is_a_pre_switch_refusal(deployment):
    plan_path, _, _ = activation_bundle(deployment, "unknown-impact")
    plan = read_json(plan_path)
    impact = Path(plan["impact"]["path"])
    receipt = read_json(impact)
    receipt["filesystem_layout_change"] = True
    write_json(impact, receipt)
    plan["impact"]["sha256"] = sha256_file(impact)
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match="impact_receipt_invalid"):
        invoke(reauthorize(deployment, plan_path, transactional=True), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0


def test_malformed_verification_check_id_is_stable_refusal(deployment):
    plan_path, _, _ = activation_bundle(deployment, "malformed-check-id")
    plan = read_json(plan_path)
    verification = Path(plan["verification"]["path"])
    receipt = read_json(verification)
    receipt["checks"][0]["id"] = {}
    write_json(verification, receipt)
    plan["verification"]["sha256"] = sha256_file(verification)
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match="verification_checks_mismatch"):
        invoke(reauthorize(deployment, plan_path, transactional=True), "activate")
    assert deployment["active"].resolve() == deployment["old"]


@pytest.mark.parametrize("unsafe", ["skip-worktree", "filter", "gitlink"])
def test_existing_git_safety_rejections_are_inherited(deployment, unsafe):
    candidate = deployment["new"]
    marker = deployment["root"].parent / "filter-ran"
    if unsafe == "skip-worktree":
        git("-C", candidate, "update-index", "--skip-worktree", "payload.txt")
    elif unsafe == "filter":
        included = candidate / ".git" / "filter-config"
        git("config", "--file", included, "filter.probe.clean", f"touch '{marker}'; cat")
        git("-C", candidate, "config", "include.path", str(included))
    else:
        git("-C", candidate, "update-index", "--add", "--cacheinfo", "160000", deployment["new_identity"]["revision"], "nested")
    bundle = activation_bundle(deployment)
    with pytest.raises(SupervisorRefusal, match="release_git_admission_failed"):
        invoke(bundle, "activate")
    assert not marker.exists()
    assert deployment["active"].resolve() == deployment["old"]


def test_lock_conflict_is_fail_fast_and_write_free(deployment):
    bundle = activation_bundle(deployment)
    with deployment["lock"].open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SupervisorRefusal, match="supervisor_lock_busy"):
            invoke(bundle, "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert not deployment["journal"].exists()

@pytest.mark.parametrize(
    ("boundary", "expected_status", "expected_generation", "expected_target"),
    [
        ("journal_prepared", "aborted", 1, "old"),
        ("temporary_link_persisted", "aborted", 1, "old"),
        ("active_replaced", "recovered", 1, "old"),
        ("journal_switched", "recovered", 1, "old"),
        ("receipt_persisted", "recovered", 1, "old"),
        ("generation_persisted", "committed_cleanup", 1, "new"),
        ("journal_committed", "committed_cleanup", 1, "new"),
    ],
)
def test_every_persistence_boundary_has_deterministic_recovery(
    deployment, boundary, expected_status, expected_generation, expected_target,
):
    activation = activation_bundle(deployment, f"interrupt-{boundary}")

    def crash(observed: str) -> None:
        if observed == boundary:
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit, match="simulated crash"):
        invoke(activation, "activate", _fault_hook=crash)
    assert deployment["journal"].exists()
    interrupted = read_json(deployment["journal"])
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id=f"recover-{boundary}",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=read_json(deployment["state"])["generation"],
        activation_receipt=(
            Path(interrupted["receipt_path"])
            if Path(interrupted["receipt_path"]).exists()
            else None
        ),
    )
    result = invoke(recover, "recover")
    assert result["status"] == expected_status
    assert read_json(deployment["state"])["generation"] == expected_generation
    assert deployment["active"].resolve() == deployment[expected_target]
    assert not deployment["journal"].exists()
    assert not any(
        os.path.lexists(interrupted["paths"][name])
        for name in ("active_tmp", "journal_tmp", "generation_tmp")
    )


def test_receipt_before_state_crash_recovers_and_old_receipt_stays_stale(deployment):
    first = activation_bundle(deployment, "activate-crash")

    def crash(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit, match="simulated crash"):
        invoke(first, "activate", _fault_hook=crash)
    old_receipt = deployment["root"] / "activation-activate-crash.json"
    assert old_receipt.exists()
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()

    recover_bundle = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-crash",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=old_receipt,
    )
    recovered = invoke(recover_bundle, "recover")
    assert recovered["status"] == "recovered"
    assert deployment["active"].resolve() == deployment["old"]
    assert not deployment["journal"].exists()

    second = invoke(activation_bundle(deployment, "activate-2"), "activate")
    assert read_json(deployment["state"])["last_transaction_id"] == second["transaction_id"]
    post, _ = evidence_receipt(
        deployment["inputs"], deployment["new_identity"], post=True,
        activation=read_json(old_receipt),
    )
    stale_finalize = simple_bundle(
        deployment,
        operation="finalize",
        transaction_id="finalize-stale",
        before=deployment["new_identity"],
        after=deployment["new_identity"],
        expected_generation=read_json(deployment["state"])["generation"],
        activation_receipt=old_receipt,
        verification=post,
    )
    with pytest.raises(SupervisorRefusal, match=r"activation_(generation|transaction)_not_current"):
        invoke(stale_finalize, "finalize")


def test_aba_reuse_of_first_activation_receipt_refuses(deployment):
    first = invoke(activation_bundle(deployment, "activate-first"), "activate")
    first_receipt = Path(first["receipt_path"])
    rollback = simple_bundle(
        deployment,
        operation="rollback",
        transaction_id="rollback-first",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=first_receipt,
    )
    invoke(rollback, "rollback")
    invoke(activation_bundle(deployment, "activate-second"), "activate")
    stale = simple_bundle(
        deployment,
        operation="rollback",
        transaction_id="rollback-stale",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=3,
        activation_receipt=first_receipt,
    )
    with pytest.raises(SupervisorRefusal, match=r"activation_(generation|transaction)_not_current"):
        invoke(stale, "rollback")
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 3


def operation_inputs(plan: dict) -> list[Path]:
    paths = [Path(item["path"]) for item in plan.get("configs", [])]
    for role in ("activation_receipt", "interrupted_receipt", "verification", "impact", "journal"):
        reference = plan.get(role)
        if reference is not None:
            paths.append(Path(reference["path"]))
    verification = plan.get("verification")
    if verification is not None:
        for check in read_json(Path(verification["path"]))["checks"]:
            paths.append(Path(check["evidence_path"]))
    impact = plan.get("impact")
    if impact is not None:
        for item in read_json(Path(impact["path"]))["evidence"]:
            paths.append(Path(item["path"]))
    return list(dict.fromkeys(paths))


def reauthorize(case: dict, plan_path: Path, *, transactional: bool) -> tuple[Path, Path, str]:
    plan = read_json(plan_path)
    auth, digest = authorization(
        case,
        plan_path,
        plan,
        inputs=operation_inputs(plan),
        transactional=transactional,
    )
    return plan_path, auth, digest


def test_cross_role_output_alias_refuses_before_switch(deployment):
    plan_path, _, _ = activation_bundle(deployment, "alias-output")
    plan = read_json(plan_path)
    plan["output_receipt"] = plan["paths"]["journal_tmp"]
    plan["paths"]["output_receipt"] = plan["paths"]["journal_tmp"]
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match="path_role_alias"):
        invoke(reauthorize(deployment, plan_path, transactional=True), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()


def test_config_cannot_alias_generation_state(deployment):
    plan_path, _, _ = activation_bundle(deployment, "alias-config")
    plan = read_json(plan_path)
    plan["configs"] = [{
        "path": str(deployment["state"]),
        "sha256": sha256_file(deployment["state"]),
    }]
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match="input_write_role_alias"):
        invoke(reauthorize(deployment, plan_path, transactional=True), "activate")
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["active"].resolve() == deployment["old"]



def test_config_cannot_resolve_through_active_release(deployment):
    plan_path, _, _ = activation_bundle(deployment, "active-config")
    plan = read_json(plan_path)
    active_config = deployment["active"] / ".git" / "config"
    plan["configs"] = [{
        "path": str(active_config),
        "sha256": sha256_file(active_config),
    }]
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match="input_inside_release"):
        invoke(reauthorize(deployment, plan_path, transactional=True), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0


def test_non_config_input_cannot_resolve_through_active_release(deployment):
    plan_path, _, _ = activation_bundle(deployment, "active-evidence")
    plan = read_json(plan_path)
    verification = Path(plan["verification"]["path"])
    receipt = read_json(verification)
    active_evidence = deployment["active"] / ".git" / "config"
    receipt["checks"][0]["evidence_path"] = str(active_evidence)
    receipt["checks"][0]["evidence_sha256"] = sha256_file(active_evidence)
    write_json(verification, receipt)
    plan["verification"]["sha256"] = sha256_file(verification)
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match="input_inside_release"):
        invoke(reauthorize(deployment, plan_path, transactional=True), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0

def test_active_drift_after_receipt_never_commits_success(deployment):
    bundle = activation_bundle(deployment, "late-active-drift")

    def drift(boundary: str) -> None:
        if boundary == "receipt_persisted":
            deployment["active"].unlink()
            deployment["active"].symlink_to(deployment["old"], target_is_directory=True)

    with pytest.raises(SupervisorRefusal, match="deployment_drift"):
        invoke(bundle, "activate", _fault_hook=drift)
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()


def test_prior_release_drift_after_switch_blocks_commit(deployment):
    bundle = activation_bundle(deployment, "late-before-drift")

    def drift(boundary: str) -> None:
        if boundary == "active_replaced":
            (deployment["old"] / "payload.txt").write_text("rollback target drifted\\n")

    with pytest.raises(SupervisorRefusal, match="release_identity_drift"):
        invoke(bundle, "activate", _fault_hook=drift)
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()


def test_root_rebind_cannot_redirect_transaction_writes(deployment):
    bundle = activation_bundle(deployment, "root-rebind")
    original_root = deployment["root"]
    moved_root = original_root.with_name("deployment-moved")

    def rebind(boundary: str) -> None:
        if boundary == "journal_prepared":
            original_root.rename(moved_root)
            original_root.mkdir(mode=0o700)

    with pytest.raises(SupervisorRefusal, match="deployment_root_identity_mismatch"):
        invoke(bundle, "activate", _fault_hook=rebind)
    assert list(original_root.iterdir()) == []
    assert (moved_root / ".wrr-release-journal.json").exists()


def test_recovery_refuses_missing_prior_release(deployment):
    bundle = activation_bundle(deployment, "missing-prior")

    def crash(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit):
        invoke(bundle, "activate", _fault_hook=crash)
    receipt = deployment["root"] / "activation-missing-prior.json"
    shutil.rmtree(deployment["old"])
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-missing-prior",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=receipt,
    )
    with pytest.raises(SupervisorRefusal, match="recovery_release_invalid"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()


def test_committed_recovery_requires_matching_receipt(deployment):
    bundle = activation_bundle(deployment, "invalid-committed-receipt")

    def crash(boundary: str) -> None:
        if boundary == "generation_persisted":
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit):
        invoke(bundle, "activate", _fault_hook=crash)
    receipt = deployment["root"] / "activation-invalid-committed-receipt.json"
    write_json(receipt, {"unrelated": True})
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-invalid-receipt",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=receipt,
    )
    with pytest.raises(SupervisorRefusal, match="committed_receipt_invalid"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()


def test_reused_interrupted_transaction_id_cannot_revive_orphan_receipt(deployment):
    transaction_id = "reused-interrupted"
    first = activation_bundle(deployment, transaction_id)

    def crash(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit):
        invoke(first, "activate", _fault_hook=crash)
    orphan = deployment["root"] / f"activation-{transaction_id}.json"
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-reused",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=orphan,
    )
    invoke(recover, "recover")

    second_plan, _, _ = activation_bundle(deployment, transaction_id)
    second = read_json(second_plan)
    second_output = deployment["root"] / "activation-reused-second.json"
    second["output_receipt"] = str(second_output)
    second["paths"]["output_receipt"] = str(second_output)
    write_json(second_plan, second)
    current = invoke(reauthorize(deployment, second_plan, transactional=True), "activate")
    assert current["transaction_id"] == transaction_id

    post, _ = evidence_receipt(
        deployment["inputs"],
        deployment["new_identity"],
        post=True,
        activation=read_json(orphan),
    )
    stale = simple_bundle(
        deployment,
        operation="finalize",
        transaction_id="finalize-reused-orphan",
        before=deployment["new_identity"],
        after=deployment["new_identity"],
        expected_generation=read_json(deployment["state"])["generation"],
        activation_receipt=orphan,
        verification=post,
    )
    with pytest.raises(SupervisorRefusal, match=r"activation_(generation|transaction)_not_current"):
        invoke(stale, "finalize")


def test_recovery_receipt_is_written_only_after_recovery_state(deployment):
    activation = activation_bundle(deployment, "activation-receipt-order")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit, match="activation interrupted"):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-receipt-order",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-receipt-order.json",
    )

    def interrupt_recovery(boundary: str) -> None:
        if boundary == "recovery_state_persisted":
            raise SystemExit("recovery interrupted")

    with pytest.raises(SystemExit, match="recovery interrupted"):
        invoke(recover, "recover", _fault_hook=interrupt_recovery)
    recovery_output = deployment["root"] / "recover-recover-receipt-order.json"
    assert not recovery_output.exists()
    assert deployment["journal"].exists()
    state = read_json(deployment["state"])
    assert state["generation"] == 1
    assert state["last_transaction_id"] == "recover-receipt-order"


def test_committed_recovery_requires_exact_recovery_receipt(deployment):
    activation = activation_bundle(deployment, "activation-receipt-check")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-receipt-check",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-receipt-check.json",
    )

    def interrupt_recovery(boundary: str) -> None:
        if boundary == "recovery_receipt_persisted":
            raise SystemExit("recovery interrupted")

    with pytest.raises(SystemExit, match="recovery interrupted"):
        invoke(recover, "recover", _fault_hook=interrupt_recovery)
    recovery_output = deployment["root"] / "recover-recover-receipt-check.json"
    write_json(recovery_output, {"forged": True})
    cleanup = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-receipt-cleanup",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=deployment["root"] / "activation-activation-receipt-check.json",
    )
    with pytest.raises(SupervisorRefusal, match="recovery_receipt_invalid"):
        invoke(cleanup, "recover")
    assert deployment["journal"].exists()


def test_recovery_receipt_write_failure_preserves_blocking_journal(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-receipt-failure")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-receipt-failure",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-receipt-failure.json",
    )
    recovery_output = deployment["root"] / "recover-recover-receipt-failure.json"
    original = release_supervisor._write_exclusive_json_at

    def fail_recovery_receipt(root_fd, root, path, value):
        if Path(path) == recovery_output:
            raise SupervisorRefusal("injected_recovery_receipt_failure")
        return original(root_fd, root, path, value)

    monkeypatch.setattr(release_supervisor, "_write_exclusive_json_at", fail_recovery_receipt)
    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()
    assert not recovery_output.exists()


def test_recovery_journal_update_failure_preserves_blocking_journal(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-journal-failure")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-journal-failure",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-journal-failure.json",
    )

    def fail_journal_update(*args, **kwargs):
        raise SupervisorRefusal("injected_recovery_journal_failure")

    monkeypatch.setattr(release_supervisor, "_journal_update", fail_journal_update)
    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()


def test_recovery_state_write_failure_preserves_blocking_journal(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-state-failure")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-state-failure",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-state-failure.json",
    )
    original = release_supervisor._write_atomic_json_at

    def fail_recovery_state(root_fd, root, path, temporary, value):
        if Path(path) == deployment["state"]:
            raise SupervisorRefusal("injected_recovery_state_failure")
        return original(root_fd, root, path, temporary, value)

    monkeypatch.setattr(release_supervisor, "_write_atomic_json_at", fail_recovery_state)
    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()
    assert read_json(deployment["state"])["generation"] == 0


def test_recovery_cleanup_failure_preserves_blocking_journal(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-cleanup-failure")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "temporary_link_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-cleanup-failure",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
    )
    interrupted = read_json(deployment["journal"])
    temporary_name = Path(interrupted["paths"]["active_tmp"]).name
    original = release_supervisor.os.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path == temporary_name and kwargs.get("dir_fd") is not None:
            raise OSError("injected recovery cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(release_supervisor.os, "unlink", fail_cleanup)
    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()


def test_malformed_recovery_cleanup_path_is_stable_refusal(deployment):
    activation = activation_bundle(deployment, "activation-malformed-cleanup")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "journal_prepared":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    plan_path, auth_path, _ = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-malformed-cleanup",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
    )
    plan = read_json(plan_path)
    plan["cleanup_paths"] = [{}]
    write_json(plan_path, plan)
    authorization_value = read_json(auth_path)
    for entry in authorization_value["inputs"]:
        if entry["path"] == str(plan_path):
            entry["sha256"] = sha256_file(plan_path)
    write_json(auth_path, authorization_value)
    with pytest.raises(SupervisorRefusal, match="recovery_cleanup_paths_invalid"):
        invoke((plan_path, auth_path, sha256_file(auth_path)), "recover")
    assert deployment["journal"].exists()


def test_recovery_restore_failure_preserves_blocking_journal(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-restore-failure")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-restore-failure",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-restore-failure.json",
    )
    original = release_supervisor.os.replace

    def fail_restore(source, destination, *args, **kwargs):
        if destination == deployment["active"].name:
            raise OSError("injected recovery restore failure")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(release_supervisor.os, "replace", fail_restore)
    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 0


def test_post_receipt_journal_failure_leaves_only_conditional_receipt(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-post-receipt-journal")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-post-receipt-journal",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-post-receipt-journal.json",
    )
    original = release_supervisor._journal_update

    def fail_committed_update(root_fd, root, paths, journal, phase):
        if phase == "recovery_committed":
            raise SupervisorRefusal("injected_post_receipt_journal_failure")
        return original(root_fd, root, paths, journal, phase)

    monkeypatch.setattr(release_supervisor, "_journal_update", fail_committed_update)
    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    receipt = read_json(deployment["root"] / "recover-recover-post-receipt-journal.json")
    assert receipt["status"] == "conditional"
    assert receipt["committed"] is False
    assert receipt["valid_when"]["transaction_records_absent"] is True
    assert deployment["journal"].exists()


def test_guard_unlink_failure_leaves_only_conditional_receipt(deployment, monkeypatch):
    activation = activation_bundle(deployment, "activation-guard-unlink")

    def interrupt_activation(boundary: str) -> None:
        if boundary == "receipt_persisted":
            raise SystemExit("activation interrupted")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=interrupt_activation)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-guard-unlink",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=deployment["root"] / "activation-activation-guard-unlink.json",
    )
    original = release_supervisor.os.unlink

    def fail_guard_unlink(path, *args, **kwargs):
        if path == deployment["guard"].name and kwargs.get("dir_fd") is not None:
            raise OSError("injected guard unlink failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(release_supervisor.os, "unlink", fail_guard_unlink)
    with pytest.raises(
        SupervisorRefusal, match="transaction_committed_recovery_required",
    ):
        invoke(recover, "recover")
    receipt = read_json(deployment["root"] / "recover-recover-guard-unlink.json")
    assert receipt["status"] == "conditional"
    assert receipt["committed"] is False
    assert not deployment["journal"].exists()
    assert deployment["guard"].exists()


@pytest.mark.parametrize(
    ("evidence_kind", "reason"),
    [
        ("real_host", "finalize_evidence_kind_mismatch"),
        ("invented_kind", "evidence_kind_invalid"),
    ],
)
def test_fixture_activation_cannot_be_promoted_by_evidence_kind(
    deployment, evidence_kind, reason,
):
    activation = invoke(activation_bundle(deployment, f"fixture-{evidence_kind}"), "activate")
    activation_path = Path(activation["receipt_path"])
    post, _ = evidence_receipt(
        deployment["inputs"],
        deployment["new_identity"],
        post=True,
        activation=activation,
        evidence_kind=evidence_kind,
    )
    plan_path, _, _ = simple_bundle(
        deployment,
        operation="finalize",
        transaction_id=f"final-{evidence_kind}",
        before=deployment["new_identity"],
        after=deployment["new_identity"],
        expected_generation=1,
        activation_receipt=activation_path,
        verification=post,
    )
    plan = read_json(plan_path)
    plan["evidence_kind"] = evidence_kind
    write_json(plan_path, plan)
    with pytest.raises(SupervisorRefusal, match=reason):
        invoke(reauthorize(deployment, plan_path, transactional=False), "finalize")


def test_script_ignores_ambient_wrr_package(deployment, tmp_path):
    fake = tmp_path / "ambient"
    fake_module = fake / "wrr" / "cli"
    fake_module.mkdir(parents=True)
    (fake / "wrr" / "__init__.py").write_text("")
    (fake_module / "__init__.py").write_text("")
    marker = tmp_path / "ambient-imported"
    (fake_module / "release_supervisor.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "def main(): return 0\n"
    )
    plan, auth, digest = activation_bundle(deployment, "trusted-wrapper")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            os.fspath(root / "scripts" / "wrr_release_supervisor"),
            "activate",
            str(plan),
            "--authorization",
            str(auth),
            "--authorization-sha256",
            digest,
        ],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=str(fake)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["reason"] == "explicit_apply_required"
    assert not marker.exists()


def test_script_ignores_ambient_sitecustomize(deployment, tmp_path):
    fake = tmp_path / "ambient-startup"
    fake.mkdir()
    marker = tmp_path / "sitecustomize-executed"
    (fake / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    )
    path_marker = tmp_path / "path-python-executed"
    fake_python = fake / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf executed > {str(path_marker)!r}\n"
        "exit 99\n"
    )
    fake_python.chmod(0o700)
    plan, auth, digest = activation_bundle(deployment, "trusted-startup")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            os.fspath(root / "scripts" / "wrr_release_supervisor"),
            "activate",
            str(plan),
            "--authorization",
            str(auth),
            "--authorization-sha256",
            digest,
        ],
        cwd=root,
        env=dict(
            os.environ,
            PYTHONPATH=str(fake),
            PATH=f"{fake}:{os.environ.get('PATH', '')}",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["reason"] == "explicit_apply_required"
    assert not marker.exists()
    assert not path_marker.exists()


def test_malformed_signed_authorization_is_stable_cli_refusal(deployment):
    plan, auth, _ = activation_bundle(deployment, "malformed-auth")
    value = read_json(auth)
    value["deployment_root"] = None
    write_json(auth, value)
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            os.fspath(root / "scripts" / "wrr_release_supervisor"),
            "activate",
            str(plan),
            "--authorization",
            str(auth),
            "--authorization-sha256",
            sha256_file(auth),
            "--apply",
        ],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=str(root)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "reason": "deployment_root_invalid",
        "status": "refused",
    }
    assert completed.stderr == ""


def test_malformed_allowed_writes_is_stable_cli_refusal(deployment):
    plan, auth, _ = activation_bundle(deployment, "malformed-writes")
    value = read_json(auth)
    value["allowed_writes"] = [{}]
    write_json(auth, value)
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            os.fspath(root / "scripts" / "wrr_release_supervisor"),
            "activate",
            str(plan),
            "--authorization",
            str(auth),
            "--authorization-sha256",
            sha256_file(auth),
            "--apply",
        ],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=str(root)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "reason": "allowed_writes_mismatch",
        "status": "refused",
    }
    assert completed.stderr == ""


def actual_cli(operation: str, bundle: tuple[Path, Path, str]) -> subprocess.CompletedProcess[str]:
    plan, authorization_path, authorization_sha256 = bundle
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [
            os.fspath(root / "scripts" / "wrr_release_supervisor"),
            operation,
            str(plan),
            "--authorization",
            str(authorization_path),
            "--authorization-sha256",
            authorization_sha256,
            "--apply",
        ],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=str(root)),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )




def test_authorization_cannot_resolve_through_active_release(deployment):
    plan, authorization_path, _ = activation_bundle(deployment, "active-authorization")
    active_authorization = deployment["active"] / ".git" / "attempt2-authorization.json"
    write_json(active_authorization, read_json(authorization_path))
    with pytest.raises(SupervisorRefusal, match="input_inside_release"):
        invoke(
            (plan, active_authorization, sha256_file(active_authorization)),
            "activate",
        )
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()


@pytest.mark.parametrize(
    "malformed_kind",
    [{}, [], 1, True, None, "invented_kind", MISSING_EVIDENCE_KIND],
    ids=["object", "list", "number", "boolean", "null", "unknown", "missing"],
)
def test_malformed_activate_evidence_kind_is_stable_cli_refusal(
    deployment, malformed_kind,
):
    plan_path, authorization_path, _ = activation_bundle(
        deployment, f"malformed-activate-{type(malformed_kind).__name__}",
    )
    plan = read_json(plan_path)
    if malformed_kind is MISSING_EVIDENCE_KIND:
        plan.pop("evidence_kind")
    else:
        plan["evidence_kind"] = malformed_kind
    write_json(plan_path, plan)
    authorization_value = read_json(authorization_path)
    if malformed_kind is MISSING_EVIDENCE_KIND:
        authorization_value.pop("evidence_kind")
    else:
        authorization_value["evidence_kind"] = malformed_kind
    for entry in authorization_value["inputs"]:
        if entry["path"] == str(plan_path):
            entry["sha256"] = sha256_file(plan_path)
    write_json(authorization_path, authorization_value)

    completed = actual_cli(
        "activate",
        (plan_path, authorization_path, sha256_file(authorization_path)),
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "reason": "evidence_kind_invalid",
        "status": "refused",
    }
    assert completed.stderr == ""
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()


@pytest.mark.parametrize(
    "malformed_kind",
    [{}, [], 1, True, None, "invented_kind", MISSING_EVIDENCE_KIND],
    ids=["object", "list", "number", "boolean", "null", "unknown", "missing"],
)
def test_malformed_finalize_evidence_kind_is_stable_cli_refusal(
    deployment, malformed_kind,
):
    activation = invoke(
        activation_bundle(
            deployment, f"valid-before-finalize-{type(malformed_kind).__name__}",
        ),
        "activate",
    )
    activation_path = Path(activation["receipt_path"])
    post, _ = evidence_receipt(
        deployment["inputs"],
        deployment["new_identity"],
        post=True,
        activation=activation,
    )
    plan_path, authorization_path, _ = simple_bundle(
        deployment,
        operation="finalize",
        transaction_id=f"malformed-finalize-{type(malformed_kind).__name__}",
        before=deployment["new_identity"],
        after=deployment["new_identity"],
        expected_generation=1,
        activation_receipt=activation_path,
        verification=post,
    )
    plan = read_json(plan_path)
    if malformed_kind is MISSING_EVIDENCE_KIND:
        plan.pop("evidence_kind")
    else:
        plan["evidence_kind"] = malformed_kind
    write_json(plan_path, plan)
    authorization_value = read_json(authorization_path)
    if malformed_kind is MISSING_EVIDENCE_KIND:
        authorization_value.pop("evidence_kind")
    else:
        authorization_value["evidence_kind"] = malformed_kind
    for entry in authorization_value["inputs"]:
        if entry["path"] == str(plan_path):
            entry["sha256"] = sha256_file(plan_path)
    write_json(authorization_path, authorization_value)

    completed = actual_cli(
        "finalize",
        (plan_path, authorization_path, sha256_file(authorization_path)),
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "reason": "evidence_kind_invalid",
        "status": "refused",
    }
    assert completed.stderr == ""
    assert read_json(deployment["state"])["generation"] == 1
    assert not Path(plan["output_receipt"]).exists()
    assert not deployment["journal"].exists()


def test_first_guard_directory_fsync_failure_keeps_blocking_record(
    deployment, monkeypatch,
):
    bundle = activation_bundle(deployment, "attempt2-first-guard-fsync")
    guard = deployment["root"] / ".wrr-release-journal.guard.json"
    original_replace = release_supervisor.os.replace
    original_fsync = release_supervisor.os.fsync
    guard_renamed = False

    def track_guard_rename(source, destination, *args, **kwargs):
        nonlocal guard_renamed
        result = original_replace(source, destination, *args, **kwargs)
        if source == deployment["journal"].name and destination == guard.name:
            guard_renamed = True
        return result

    def fail_first_guard_directory_fsync(descriptor):
        nonlocal guard_renamed
        if guard_renamed:
            guard_renamed = False
            raise OSError("injected first guard directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(release_supervisor.os, "replace", track_guard_rename)
    monkeypatch.setattr(
        release_supervisor.os, "fsync", fail_first_guard_directory_fsync,
    )
    with pytest.raises(
        SupervisorRefusal, match="transaction_committed_recovery_required",
    ):
        invoke(bundle, "activate")
    assert not deployment["journal"].exists()
    assert guard.exists()

    next_bundle = activation_bundle(deployment, "attempt2-blocked-by-guard")
    with pytest.raises(SupervisorRefusal, match="recovery_required"):
        invoke(next_bundle, "activate")


def test_trailing_guard_fsync_failure_is_explicit_committed_uncertainty(
    deployment, monkeypatch,
):
    bundle = activation_bundle(deployment, "attempt2-trailing-guard-fsync")
    guard = deployment["root"] / ".wrr-release-journal.guard.json"
    original_unlink = release_supervisor.os.unlink
    original_fsync = release_supervisor.os.fsync
    guard_unlinked = False
    guard_payload = b""

    def track_guard_unlink(path, *args, **kwargs):
        nonlocal guard_payload, guard_unlinked
        if path == guard.name and kwargs.get("dir_fd") is not None:
            guard_payload = guard.read_bytes()
        result = original_unlink(path, *args, **kwargs)
        if path == guard.name and kwargs.get("dir_fd") is not None:
            guard_unlinked = True
        return result

    def fail_trailing_guard_directory_fsync(descriptor):
        nonlocal guard_unlinked
        if guard_unlinked:
            guard_unlinked = False
            raise OSError("injected trailing guard directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(release_supervisor.os, "unlink", track_guard_unlink)
    monkeypatch.setattr(
        release_supervisor.os, "fsync", fail_trailing_guard_directory_fsync,
    )
    result = invoke(bundle, "activate")
    assert result["status"] == "provisional"
    assert result["cleanup_status"] == "durability_uncertain"
    assert result["cleanup_durable"] is False
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 1
    assert not deployment["journal"].exists()
    assert not guard.exists()
    guard.write_bytes(guard_payload)
    restored_guard_bundle = activation_bundle(
        deployment, "attempt2-restored-guard",
    )
    with pytest.raises(SupervisorRefusal, match="recovery_required"):
        invoke(restored_guard_bundle, "activate")


def test_guard_only_restart_recovers_committed_transaction(deployment):
    activation = activation_bundle(deployment, "guard-only-restart")

    def crash_after_guard_persisted(boundary: str) -> None:
        if boundary == "journal_guard_persisted":
            raise SystemExit("simulated crash after guard persistence")

    with pytest.raises(SystemExit, match="simulated crash"):
        invoke(
            activation,
            "activate",
            _fault_hook=crash_after_guard_persisted,
        )
    assert not deployment["journal"].exists()
    assert deployment["guard"].exists()
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 1

    result = invoke(
        simple_bundle(
            deployment,
            operation="recover",
            transaction_id="recover-guard-only-restart",
            before=deployment["new_identity"],
            after=deployment["old_identity"],
            expected_generation=1,
            activation_receipt=deployment["root"] / "activation-guard-only-restart.json",
        ),
        "recover",
    )

    assert result["status"] == "committed_cleanup"
    assert result["cleanup_status"] == "durable"
    assert result["cleanup_durable"] is True
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 1
    assert not deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_both_transaction_records_require_manual_recovery(deployment):
    activation = activation_bundle(deployment, "both-records")

    def crash_after_guard_persisted(boundary: str) -> None:
        if boundary == "journal_guard_persisted":
            raise SystemExit("simulated crash after guard persistence")

    with pytest.raises(SystemExit):
        invoke(
            activation,
            "activate",
            _fault_hook=crash_after_guard_persisted,
        )
    shutil.copy2(deployment["guard"], deployment["journal"])
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-both-records",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=deployment["root"] / "activation-both-records.json",
    )

    with pytest.raises(SupervisorRefusal, match="manual_recovery_required"):
        invoke(recover, "recover")
    assert deployment["journal"].exists()
    assert deployment["guard"].exists()


def test_same_digest_authorization_inode_replacement_blocks_commit(
    deployment, tmp_path,
):
    plan, authorization_path, digest = activation_bundle(
        deployment, "authorization-inode-drift",
    )

    def replace_authorization(boundary: str) -> None:
        if boundary == "active_replaced":
            replacement = tmp_path / "replacement-authorization.json"
            replacement.write_bytes(authorization_path.read_bytes())
            os.replace(replacement, authorization_path)

    with pytest.raises(SupervisorRefusal, match="authorization_identity_changed"):
        invoke(
            (plan, authorization_path, digest),
            "activate",
            _fault_hook=replace_authorization,
        )
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_trailing_guard_fsync_failure_has_stable_cli_success_envelope(
    deployment, monkeypatch, capsys,
):
    plan, authorization_path, authorization_sha256 = activation_bundle(
        deployment, "attempt2-cli-trailing-fsync",
    )
    guard = deployment["guard"]
    original_unlink = release_supervisor.os.unlink
    original_fsync = release_supervisor.os.fsync
    guard_unlinked = False

    def track_guard_unlink(path, *args, **kwargs):
        nonlocal guard_unlinked
        result = original_unlink(path, *args, **kwargs)
        if path == guard.name and kwargs.get("dir_fd") is not None:
            guard_unlinked = True
        return result

    def fail_trailing_guard_directory_fsync(descriptor):
        nonlocal guard_unlinked
        if guard_unlinked:
            guard_unlinked = False
            raise OSError("injected trailing guard directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(release_supervisor.os, "unlink", track_guard_unlink)
    monkeypatch.setattr(
        release_supervisor.os, "fsync", fail_trailing_guard_directory_fsync,
    )
    exit_code = release_supervisor.main([
        "activate",
        str(plan),
        "--authorization",
        str(authorization_path),
        "--authorization-sha256",
        authorization_sha256,
        "--apply",
    ])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert result["status"] == "provisional"
    assert result["cleanup_status"] == "durability_uncertain"
    assert result["cleanup_durable"] is False


def fabricated_genesis_activation_receipt(deployment, path: Path) -> Path:
    write_json(path, {
        "schema_version": 1,
        "operation": "activate",
        "transaction_id": None,
        "plan_sha256": "0" * 64,
        "authorization_sha256": "1" * 64,
        "generation_before": 0,
        "generation_after": 0,
        "before": deployment["new_identity"],
        "after": deployment["old_identity"],
        "config_hashes": {str(deployment["config"]): deployment["config_sha256"]},
        "journal_outcome": "receipt_persisted",
        "status": "provisional",
        "provisional": True,
        "fixture_only": True,
        "real_host_verified": False,
        "evidence_kind": "isolated_fixture",
        "verification_sha256": "2" * 64,
        "impact_sha256": "3" * 64,
        "rollback_supported": True,
    })
    return path


def test_fabricated_genesis_activation_receipt_cannot_drive_rollback(deployment):
    forged = fabricated_genesis_activation_receipt(
        deployment, deployment["inputs"] / "forged-genesis-activation.json",
    )
    rollback = simple_bundle(
        deployment,
        operation="rollback",
        transaction_id="rollback-forged-genesis",
        before=deployment["old_identity"],
        after=deployment["new_identity"],
        expected_generation=0,
        activation_receipt=forged,
    )

    with pytest.raises(
        SupervisorRefusal, match="activation_receipt_not_state_bound",
    ):
        invoke(rollback, "rollback")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_fabricated_genesis_activation_receipt_cannot_finalize(deployment):
    forged = fabricated_genesis_activation_receipt(
        deployment, deployment["inputs"] / "forged-genesis-finalize.json",
    )
    forged_value = read_json(forged)
    post, _ = evidence_receipt(
        deployment["inputs"],
        deployment["old_identity"],
        post=True,
        activation=forged_value,
    )
    plan_path, authorization_path, digest = simple_bundle(
        deployment,
        operation="finalize",
        transaction_id="finalize-forged-genesis",
        before=deployment["old_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
        activation_receipt=forged,
        verification=post,
    )

    with pytest.raises(
        SupervisorRefusal, match="activation_receipt_not_state_bound",
    ):
        invoke((plan_path, authorization_path, digest), "finalize")
    assert not Path(read_json(plan_path)["output_receipt"]).exists()
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0


def test_duplicate_current_activation_receipt_path_cannot_drive_rollback(deployment):
    activation = invoke(
        activation_bundle(deployment, "state-bound-activation"),
        "activate",
    )
    genuine = Path(activation["receipt_path"])
    duplicate = deployment["inputs"] / "duplicate-current-activation.json"
    duplicate.write_bytes(genuine.read_bytes())
    rollback = simple_bundle(
        deployment,
        operation="rollback",
        transaction_id="rollback-duplicate-current",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=duplicate,
    )

    with pytest.raises(
        SupervisorRefusal, match="activation_receipt_not_state_bound",
    ):
        invoke(rollback, "rollback")
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 1
    assert not deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_authorization_with_release_hard_link_alias_refuses(deployment):
    plan, authorization_path, digest = activation_bundle(
        deployment, "hard-linked-authorization",
    )
    release_alias = deployment["old"] / ".git" / "authorization-hard-link.json"
    os.link(authorization_path, release_alias)

    with pytest.raises(SupervisorRefusal, match="authorization_hard_link_refused"):
        invoke((plan, authorization_path, digest), "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()


@pytest.mark.parametrize(
    ("boundary", "record_name"),
    [
        ("journal_prepared", "journal"),
        ("journal_guard_persisted", "guard"),
    ],
)
def test_hard_linked_recovery_record_refuses(
    deployment, boundary, record_name,
):
    activation = activation_bundle(
        deployment, f"hard-linked-{record_name}",
    )

    def crash(observed: str) -> None:
        if observed == boundary:
            raise SystemExit("simulated record boundary crash")

    with pytest.raises(SystemExit):
        invoke(activation, "activate", _fault_hook=crash)
    record = deployment[record_name]
    outside_alias = deployment["inputs"] / f"{record_name}-outside-alias.json"
    os.link(record, outside_alias)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id=f"recover-hard-linked-{record_name}",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=read_json(deployment["state"])["generation"],
        activation_receipt=(
            deployment["root"] / f"activation-hard-linked-{record_name}.json"
            if (deployment["root"] / f"activation-hard-linked-{record_name}.json").exists()
            else None
        ),
    )

    with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
        invoke(recover, "recover")
    assert record.exists()


@pytest.mark.parametrize(
    ("boundary", "active_key"),
    [
        ("journal_prepared", "old"),
        ("receipt_persisted", "new"),
    ],
)
def test_late_hard_link_refuses_at_next_transaction_recheck(
    deployment, boundary, active_key,
):
    bundle = activation_bundle(deployment, f"late-hard-link-{boundary}")
    alias = deployment["inputs"] / f"late-config-alias-{boundary}.json"

    def add_alias(observed: str) -> None:
        if observed == boundary:
            os.link(deployment["config"], alias)

    with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
        invoke(bundle, "activate", _fault_hook=add_alias)
    assert deployment["active"].resolve() == deployment[active_key]
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_hard_link_between_input_and_config_recheck_refuses(
    deployment, monkeypatch,
):
    bundle = activation_bundle(deployment, "between-input-and-config-recheck")
    alias = deployment["inputs"] / "between-loop-config-alias.json"
    original = release_supervisor._assert_inputs_unchanged
    calls = 0

    def add_alias_after_input_recheck(inputs):
        nonlocal calls
        original(inputs)
        calls += 1
        if calls == 2:
            os.link(deployment["config"], alias)

    monkeypatch.setattr(
        release_supervisor,
        "_assert_inputs_unchanged",
        add_alias_after_input_recheck,
    )
    with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
        invoke(bundle, "activate")
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()


@pytest.mark.parametrize("role", ["plan", "evidence", "config"])
def test_preexisting_protected_hard_link_refuses(
    deployment, role,
):
    bundle = activation_bundle(deployment, f"preexisting-{role}-hard-link")
    plan = read_json(bundle[0])
    target = {
        "plan": bundle[0],
        "evidence": Path(plan["verification"]["path"]),
        "config": deployment["config"],
    }[role]
    os.link(target, deployment["inputs"] / f"{role}-hard-link-alias.json")

    with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
        invoke(bundle, "activate")
    assert deployment["active"].resolve() == deployment["old"]
    assert read_json(deployment["state"])["generation"] == 0
    assert not deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_preexisting_protected_hard_link_activation_receipt_refuses(deployment):
    activation = invoke(
        activation_bundle(deployment, "preexisting-receipt-activation"),
        "activate",
    )
    receipt = Path(activation["receipt_path"])
    os.link(receipt, deployment["inputs"] / "activation-receipt-alias.json")
    rollback = simple_bundle(
        deployment,
        operation="rollback",
        transaction_id="preexisting-receipt-rollback",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=1,
        activation_receipt=receipt,
    )

    with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
        invoke(rollback, "rollback")
    assert deployment["active"].resolve() == deployment["new"]
    assert read_json(deployment["state"])["generation"] == 1
    assert not deployment["journal"].exists()
    assert not deployment["guard"].exists()


def test_root_local_single_link_helpers_succeed(deployment):
    root_fd = release_supervisor._open_root(
        deployment["root"], deployment["root_identity"],
    )
    expected = read_json(deployment["state"])
    try:
        assert release_supervisor._sha256_at(
            root_fd, deployment["root"], deployment["state"],
        ) == sha256_file(deployment["state"])
        assert release_supervisor._load_json_at(
            root_fd, deployment["root"], deployment["state"],
        ) == expected
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("helper", ["sha256", "json"])
def test_root_local_preexisting_hard_link_refuses(
    deployment, helper,
):
    os.link(
        deployment["state"],
        deployment["root"] / f"state-{helper}-hard-link-alias.json",
    )
    root_fd = release_supervisor._open_root(
        deployment["root"], deployment["root_identity"],
    )
    try:
        with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
            if helper == "sha256":
                release_supervisor._sha256_at(
                    root_fd, deployment["root"], deployment["state"],
                )
            else:
                release_supervisor._load_json_at(
                    root_fd, deployment["root"], deployment["state"],
                )
    finally:
        os.close(root_fd)


def test_root_local_name_replacement_during_sha256_refuses(
    deployment, monkeypatch,
):
    replacement = deployment["root"] / "replacement-state-for-hash.json"
    replacement.write_bytes(deployment["state"].read_bytes())
    original = release_supervisor.os.read
    replaced = False

    def replace_name(descriptor, size):
        nonlocal replaced
        chunk = original(descriptor, size)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, deployment["state"])
        return chunk

    root_fd = release_supervisor._open_root(
        deployment["root"], deployment["root_identity"],
    )
    monkeypatch.setattr(release_supervisor.os, "read", replace_name)
    try:
        with pytest.raises(SupervisorRefusal, match="input_identity_changed"):
            release_supervisor._sha256_at(
                root_fd, deployment["root"], deployment["state"],
            )
    finally:
        os.close(root_fd)


def test_root_local_name_replacement_during_json_read_refuses(
    deployment, monkeypatch,
):
    replacement = deployment["root"] / "replacement-state-for-load.json"
    replacement.write_bytes(deployment["state"].read_bytes())
    original = release_supervisor.os.read
    replaced = False

    def replace_name(descriptor, size):
        nonlocal replaced
        chunk = original(descriptor, size)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, deployment["state"])
        return chunk

    root_fd = release_supervisor._open_root(
        deployment["root"], deployment["root_identity"],
    )
    assert release_supervisor._sha256_at(
        root_fd, deployment["root"], deployment["state"],
    ) == sha256_file(deployment["state"])
    monkeypatch.setattr(release_supervisor.os, "read", replace_name)
    try:
        with pytest.raises(SupervisorRefusal, match="input_identity_changed"):
            release_supervisor._load_json_at(
                root_fd, deployment["root"], deployment["state"],
            )
    finally:
        os.close(root_fd)


def test_recovery_config_hard_link_keeps_blocker_and_stable_refusal(
    deployment, monkeypatch,
):
    activation = activation_bundle(deployment, "recovery-config-hard-link")

    def interrupt(observed: str) -> None:
        if observed == "journal_prepared":
            raise SystemExit("simulated recovery prerequisite")

    with pytest.raises(SystemExit, match="simulated recovery prerequisite"):
        invoke(activation, "activate", _fault_hook=interrupt)
    recover = simple_bundle(
        deployment,
        operation="recover",
        transaction_id="recover-config-hard-link",
        before=deployment["new_identity"],
        after=deployment["old_identity"],
        expected_generation=0,
    )
    alias = deployment["inputs"] / "recovery-config-alias.json"
    original = release_supervisor._validate_configs

    def add_alias_before_config_recheck(plan, inputs, releases_root):
        os.link(deployment["config"], alias)
        return original(plan, inputs, releases_root)

    monkeypatch.setattr(
        release_supervisor,
        "_validate_configs",
        add_alias_before_config_recheck,
    )
    with pytest.raises(SupervisorRefusal, match="hard_link_input_refused"):
        invoke(recover, "recover")
    assert read_json(deployment["state"])["generation"] == 0
    assert deployment["journal"].exists()
    assert not Path(read_json(recover[0])["output_receipt"]).exists()


def test_release_tree_digest_accepts_ordinary_hard_linked_content(tmp_path):
    release = tmp_path / "hard-linked-release-content"
    release.mkdir()
    original = release / "original.txt"
    alias = release / "alias.txt"
    original.write_text("same content\n")
    os.link(original, alias)
    hard_link_digest = release_supervisor.tree_digest(release)
    alias.unlink()
    alias.write_text("same content\n")
    assert release_supervisor.tree_digest(release) == hard_link_digest
