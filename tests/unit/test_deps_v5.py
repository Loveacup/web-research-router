"""wrr/deps.py 全量依赖测试（v5.6）。"""
import asyncio
import pytest

from wrr.deps import (
    BaseDep,
    DepRegistry,
    DEPENDENCY_MANIFEST,
    DepType,
    HealthStatus,
    EnvVarDep,
    CliToolDep,
    DockerDep,
    GitRepoDep,
    HermesToolDep,
    CallingPattern,
)


class TestDepManifest:
    """全量依赖清单完整性测试。"""

    def test_manifest_is_non_empty(self):
        assert len(DEPENDENCY_MANIFEST) == 13

    def test_all_deps_have_source_url(self):
        for dep in DEPENDENCY_MANIFEST:
            assert dep.source_url, f"{dep.id} missing source_url"

    def test_all_deps_have_type(self):
        for dep in DEPENDENCY_MANIFEST:
            assert isinstance(dep.dep_type, DepType), f"{dep.id} bad type"

    def test_all_deps_have_install_guide(self):
        for dep in DEPENDENCY_MANIFEST:
            if dep.required:
                assert isinstance(dep.install_guide, list), f"{dep.id} bad install_guide"

    def test_required_deps_count(self):
        required = [d for d in DEPENDENCY_MANIFEST if d.required]
        assert len(required) == 10  # 13 total, 3 optional

    def test_optional_deps(self):
        optional = {d.id for d in DEPENDENCY_MANIFEST if not d.required}
        assert optional == {"searxng_url", "paper_search_mcp", "searxng"}


class TestDepRegistry:
    """注册表测试。"""

    def test_singleton(self):
        r1 = DepRegistry.get()
        r2 = DepRegistry.get()
        assert r1 is r2

    def test_get_existing(self):
        reg = DepRegistry.get()
        dep = reg.get_dep("exa_api_key")
        assert dep is not None
        assert dep.id == "exa_api_key"
        assert dep.dep_type == DepType.ENV_VAR

    def test_get_missing(self):
        reg = DepRegistry.get()
        assert reg.get_dep("nonexistent") is None

    def test_by_type(self):
        reg = DepRegistry.get()
        env_vars = reg.by_type(DepType.ENV_VAR)
        assert len(env_vars) == 4
        names = {d.id for d in env_vars}
        assert "exa_api_key" in names
        assert "brave_api_key" in names
        assert "github_token" in names
        assert "searxng_url" in names

    def test_by_type_git_repo(self):
        reg = DepRegistry.get()
        repos = reg.by_type(DepType.GIT_REPO)
        assert len(repos) == 4

    def test_by_type_cli_tool(self):
        reg = DepRegistry.get()
        tools = reg.by_type(DepType.CLI_TOOL)
        assert len(tools) == 2
        names = {t.id for t in tools}
        assert names == {"opencli", "qmd"}

    def test_all_deps_registered(self):
        reg = DepRegistry.get()
        assert len(reg.all) == 13


class TestEnvVarDep:
    """环境变量依赖测试。"""

    def test_discover_set_var(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "secret123")
        dep = EnvVarDep(
            dep_id="test_var",
            var_name="TEST_VAR",
            source_url="https://example.com",
            description="test",
        )
        assert "[set, 9 chars]" == asyncio.run(dep.discover())

    def test_discover_missing(self):
        dep = EnvVarDep(
            dep_id="test_var2",
            var_name="NONEXISTENT_VAR_12345",
            source_url="https://example.com",
            description="test",
        )
        assert asyncio.run(dep.discover()) is None

    def test_health_ok(self, monkeypatch):
        monkeypatch.setenv("HEALTH_VAR", "abc")
        dep = EnvVarDep("h", "HEALTH_VAR", "https://x.com", "test")
        result = asyncio.run(dep.health())
        assert result.status == HealthStatus.OK

    def test_health_missing_required(self):
        dep = EnvVarDep("h2", "NO_VAR_999", "https://x.com", "test", required=True)
        result = asyncio.run(dep.health())
        assert result.status == HealthStatus.MISSING

    def test_health_missing_optional(self):
        dep = EnvVarDep("h3", "NO_VAR_998", "https://x.com", "test", required=False)
        result = asyncio.run(dep.health())
        assert result.status == HealthStatus.DEGRADED


class TestCliToolDep:
    """CLI 工具依赖测试。"""

    def test_discover_python3(self):
        """python3 必然在 PATH 中。"""
        dep = CliToolDep(
            "python3_test", "python3", "--version",
            source_url="https://python.org",
            description="test",
        )
        path = asyncio.run(dep.discover())
        assert path is not None
        assert "python3" in path

    def test_discover_nonexistent(self):
        dep = CliToolDep(
            "no_such_tool", "no_such_binary_xyz_123", "--version",
            source_url="https://x.com",
            description="test",
        )
        assert asyncio.run(dep.discover()) is None

    def test_health_found(self):
        dep = CliToolDep(
            "python3_health", "python3", "--version",
            source_url="https://python.org",
            description="test",
        )
        result = asyncio.run(dep.health())
        assert result.status == HealthStatus.OK

    def test_health_not_found(self):
        dep = CliToolDep(
            "ghost_tool", "no_such_binary_abc_999", "--version",
            source_url="https://x.com",
            description="test",
            required=True,
        )
        result = asyncio.run(dep.health())
        assert result.status == HealthStatus.MISSING

    def test_install_guide(self):
        dep = CliToolDep(
            "test_install", "test_cmd", "--version",
            source_url="https://x.com",
            description="test",
            install_guide=["brew install test_cmd"],
        )
        assert dep.install_guide == ["brew install test_cmd"]


class TestHermesToolDep:
    """Hermes 内置工具依赖测试。"""

    def test_always_ok(self):
        dep = HermesToolDep(
            "supermemory", "supermemory",
            source_url="https://hermes-agent.nousresearch.com/docs",
            description="cloud memory",
        )
        result = asyncio.run(dep.health())
        assert result.status == HealthStatus.OK

    def test_required(self):
        dep = HermesToolDep("session", "session_search")
        assert dep.required is True


class TestDepTypeEnum:
    """枚举完整性。"""

    def test_all_types_covered(self):
        values = {t.value for t in DepType}
        assert values == {
            "env_var", "git_repo", "cli_tool", "docker",
            "python_pkg", "hermes_tool",
        }
