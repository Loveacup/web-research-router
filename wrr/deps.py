"""WRR 全量依赖声明 + 自检（v5.6）。

每种依赖都必须声明以下字段：
  - id: 稳定标识
  - type: env_var | git_repo | cli_tool | docker | python_pkg | hermes_tool
  - source_url: 仓库/来源链接
  - description: 一句话说明
  - health_check: 怎样验证它是健康的
  - install_guide: 缺失时怎样安装/配置
  - required: 是否必需（False = 可选，缺失不阻塞）

从 v5.5 的 4 个 git repo 扩展到全量 14 个依赖。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


# ══════════════════════════════════════════════════════════════════════
# 核心类型
# ══════════════════════════════════════════════════════════════════════

class DepType(str, Enum):
    ENV_VAR = "env_var"
    GIT_REPO = "git_repo"
    CLI_TOOL = "cli_tool"
    DOCKER = "docker"
    PYTHON_PKG = "python_pkg"
    HERMES_TOOL = "hermes_tool"


class CallingPattern(str, Enum):
    SUBPROCESS = "subprocess"
    HTTP = "http"
    MCP = "mcp"


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    MISSING = "missing"


@dataclass
class FailureObservability:
    exit_code: Optional[int] = None
    stderr: str = ""
    duration_ms: float = 0.0
    provenance: str = ""


@dataclass
class HealthResult:
    status: HealthStatus
    version: str = ""
    detail: str = ""
    failure: Optional[FailureObservability] = None


@dataclass
class CallResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float


# ══════════════════════════════════════════════════════════════════════
# 基类
# ══════════════════════════════════════════════════════════════════════

class BaseDep:
    """所有依赖的基类。discover/health 默认同步，子类可覆盖为 async。"""

    id: str
    dep_type: DepType
    source_url: str
    description: str = ""
    required: bool = True

    def discover(self) -> Optional[str]:
        raise NotImplementedError

    def health(self, deep: bool = False) -> HealthResult:
        raise NotImplementedError

    def version(self) -> str:
        return "unknown"

    @property
    def install_guide(self) -> List[str]:
        return []


# ══════════════════════════════════════════════════════════════════════
# 具体类型实现
# ══════════════════════════════════════════════════════════════════════

class EnvVarDep(BaseDep):
    """环境变量依赖。"""

    dep_type = DepType.ENV_VAR

    def __init__(
        self,
        dep_id: str,
        var_name: str,
        source_url: str,
        description: str,
        required: bool = True,
        install_guide: Optional[List[str]] = None,
    ) -> None:
        self.id = dep_id
        self._var_name = var_name
        self.source_url = source_url
        self.description = description
        self.required = required
        self._install_guide = install_guide or []

    async def discover(self) -> Optional[str]:
        val = os.environ.get(self._var_name)
        return f"[set, {len(val)} chars]" if val else None

    async def health(self, deep: bool = False) -> HealthResult:
        val = os.environ.get(self._var_name)
        if val:
            return HealthResult(
                status=HealthStatus.OK,
                detail=f"{self._var_name}: OK",
                version=f"{len(val)} chars",
            )
        return HealthResult(
            status=HealthStatus.MISSING if self.required else HealthStatus.DEGRADED,
            detail=f"{self._var_name}: not set",
            failure=FailureObservability(
                exit_code=-1, stderr="env var not set", provenance=f"${self._var_name}"
            ),
        )

    @property
    def install_guide(self) -> List[str]:
        return self._install_guide


class CliToolDep(BaseDep):
    """命令行工具依赖。"""

    dep_type = DepType.CLI_TOOL

    def __init__(
        self,
        dep_id: str,
        binary: str,
        version_flag: str,
        source_url: str,
        description: str,
        required: bool = True,
        install_guide: Optional[List[str]] = None,
        extra_paths: Optional[List[str]] = None,
    ) -> None:
        self.id = dep_id
        self._binary = binary
        self._version_flag = version_flag
        self.source_url = source_url
        self.description = description
        self.required = required
        self._install_guide = install_guide or []
        self._extra_paths = extra_paths or []
        self._cached_path: Optional[str] = None

    async def discover(self) -> Optional[str]:
        # PATH 搜索
        found = shutil.which(self._binary)
        if found:
            self._cached_path = found
            return found
        # 额外路径
        for p in self._extra_paths:
            full = os.path.expanduser(p)
            if os.path.exists(full):
                self._cached_path = full
                return full
        # env var 覆盖
        env_key = f"{self._binary.upper()}_PATH"
        env_val = os.environ.get(env_key)
        if env_val and os.path.exists(env_val):
            self._cached_path = env_val
            return env_val
        return None

    async def health(self, deep: bool = False) -> HealthResult:
        path = await self.discover()
        if not path:
            return HealthResult(
                status=HealthStatus.MISSING if self.required else HealthStatus.DEGRADED,
                detail=f"{self._binary}: not found",
                failure=FailureObservability(
                    exit_code=-1, stderr="binary not found", provenance="PATH"
                ),
            )
        if deep:
            rc, stdout, stderr, dur = await _run_subprocess(
                [path, self._version_flag], timeout=5.0
            )
            if rc != 0:
                return HealthResult(
                    status=HealthStatus.DEGRADED,
                    detail=f"{self._binary}: {self._version_flag} returned {rc}",
                    failure=FailureObservability(
                        exit_code=rc, stderr=stderr, duration_ms=dur, provenance=path
                    ),
                )
        return HealthResult(
            status=HealthStatus.OK,
            version=await self.version(),
            detail=f"{self._binary}: OK ({path})",
        )

    async def version(self) -> str:
        path = await self.discover()
        if not path:
            return "unknown"
        rc, stdout, _, _ = await _run_subprocess(
            [path, self._version_flag], timeout=5.0
        )
        return stdout.strip() if rc == 0 else "unknown"

    @property
    def install_guide(self) -> List[str]:
        return self._install_guide


class DockerDep(BaseDep):
    """Docker 容器依赖。"""

    dep_type = DepType.DOCKER

    def __init__(
        self,
        dep_id: str,
        container_name: str,
        health_url: str,
        source_url: str,
        description: str,
        required: bool = True,
        install_guide: Optional[List[str]] = None,
    ) -> None:
        self.id = dep_id
        self._container = container_name
        self._health_url = health_url
        self.source_url = source_url
        self.description = description
        self.required = required
        self._install_guide = install_guide or []

    async def discover(self) -> Optional[str]:
        return self._container

    async def health(self, deep: bool = False) -> HealthResult:
        # 检查容器是否运行
        rc, stdout, stderr, dur = await _run_subprocess(
            ["docker", "ps", "--filter", f"name={self._container}", "--format", "{{.Status}}"],
            timeout=5.0,
        )
        if rc != 0 or not stdout.strip():
            return HealthResult(
                status=HealthStatus.MISSING if self.required else HealthStatus.DEGRADED,
                detail=f"Docker: {self._container} not running",
                failure=FailureObservability(
                    exit_code=rc, stderr=stderr, duration_ms=dur, provenance=f"docker ps --filter name={self._container}"
                ),
            )
        if deep and self._health_url:
            import urllib.request, urllib.error
            try:
                urllib.request.urlopen(self._health_url, timeout=3.0)
            except Exception:
                return HealthResult(
                    status=HealthStatus.DEGRADED,
                    detail=f"{self._container}: running but {self._health_url} unreachable",
                )
        return HealthResult(
            status=HealthStatus.OK,
            version=stdout.strip(),
            detail=f"{self._container}: {stdout.strip()}",
        )

    @property
    def install_guide(self) -> List[str]:
        return self._install_guide


class HermesToolDep(BaseDep):
    """Hermes 内置工具依赖。无需安装，只需确认可用。"""

    dep_type = DepType.HERMES_TOOL

    def __init__(
        self,
        dep_id: str,
        tool_name: str,
        source_url: str = "https://hermes-agent.nousresearch.com/docs",
        description: str = "",
    ) -> None:
        self.id = dep_id
        self._tool = tool_name
        self.source_url = source_url
        self.description = description
        self.required = True

    async def discover(self) -> Optional[str]:
        return self._tool

    async def health(self, deep: bool = False) -> HealthResult:
        # Hermes 工具无法从代码侧探测，标记为 OK（由 Hermes runtime 保证）
        return HealthResult(
            status=HealthStatus.OK,
            detail=f"{self._tool}: built-in Hermes tool",
        )


class GitRepoDep(BaseDep):
    """外部 git 仓库依赖（保留 v5.5 的 ExternalDep 语义）。"""

    dep_type = DepType.GIT_REPO
    calling_pattern = CallingPattern.SUBPROCESS

    def __init__(
        self,
        dep_id: str,
        capability: str,
        source_url: str,
        description: str,
        required: bool = True,
        env_var: Optional[str] = None,
        fallback_path: Optional[str] = None,
        install_guide: Optional[List[str]] = None,
        calling_pattern: CallingPattern = CallingPattern.SUBPROCESS,
    ) -> None:
        self.id = dep_id
        self._capability = capability
        self.source_url = source_url
        self.description = description
        self.required = required
        self._env_var = env_var
        self._fallback = os.path.expanduser(fallback_path) if fallback_path else ""
        self._install_guide = install_guide or []
        self.calling_pattern = calling_pattern
        self._cached_path: Optional[str] = None

    @property
    def capability(self) -> str:
        return self._capability

    async def discover(self) -> Optional[str]:
        path = None
        if self._env_var:
            path = os.environ.get(self._env_var)
        if not path and self._fallback:
            if os.path.exists(self._fallback):
                path = self._fallback
        if path:
            self._cached_path = path
            return path
        return None

    async def health(self, deep: bool = False) -> HealthResult:
        path = await self.discover()
        if not path:
            return HealthResult(
                status=HealthStatus.MISSING if self.required else HealthStatus.DEGRADED,
                detail=f"{self.id}: not found",
                failure=FailureObservability(
                    exit_code=-1, stderr="path not found", provenance=self._fallback or ""
                ),
            )
        if deep and os.path.isfile(path) and path.endswith(".py"):
            rc, stdout, stderr, dur = await _run_subprocess(
                ["python3", path, "--help"], timeout=5.0
            )
            if rc != 0:
                return HealthResult(
                    status=HealthStatus.DEGRADED,
                    detail=f"{self.id}: --help returned {rc}",
                    failure=FailureObservability(
                        exit_code=rc, stderr=stderr, duration_ms=dur, provenance=path
                    ),
                )
        elif deep and self.calling_pattern == CallingPattern.HTTP:
            import urllib.request, urllib.error
            try:
                urllib.request.urlopen(f"{path}/health", timeout=3.0)
            except Exception as e:
                return HealthResult(
                    status=HealthStatus.DEGRADED,
                    detail=f"{self.id}: health endpoint failed: {e}",
                )
        version = ""
        try:
            mtime = os.path.getmtime(path)
            import datetime
            version = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except OSError:
            version = "unknown"
        return HealthResult(
            status=HealthStatus.OK,
            version=version,
            detail=f"{self.id}: OK ({path})",
        )

    async def version(self) -> str:
        path = await self.discover()
        if not path:
            return "unknown"
        try:
            mtime = os.path.getmtime(path)
            import datetime
            return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except OSError:
            return "unknown"

    @property
    def install_guide(self) -> List[str]:
        return self._install_guide


# ══════════════════════════════════════════════════════════════════════
# Subprocess 工具函数
# ══════════════════════════════════════════════════════════════════════

async def _run_subprocess(
    cli: List[str], timeout: float = 30.0, inject_local_bin: bool = True
) -> Tuple[int, str, str, float]:
    env = os.environ.copy()
    if inject_local_bin:
        local_bin = os.path.expanduser("~/.local/bin")
        parts = env.get("PATH", "").split(os.pathsep)
        if local_bin not in parts:
            env["PATH"] = os.pathsep.join([local_bin] + parts)

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cli,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, OSError) as e:
        return (-1, "", str(e), (time.monotonic() - t0) * 1000)

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return (-1, "", "timeout", (time.monotonic() - t0) * 1000)

    duration = (time.monotonic() - t0) * 1000
    return (
        proc.returncode or 0,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
        duration,
    )


# ══════════════════════════════════════════════════════════════════════
# 全量依赖清单（14 项）
# ══════════════════════════════════════════════════════════════════════

DEPENDENCY_MANIFEST: List[BaseDep] = [
    # ── 环境变量 ──
    EnvVarDep(
        dep_id="exa_api_key",
        var_name="EXA_API_KEY",
        source_url="https://exa.ai",
        description="Exa 语义搜索 API key",
        install_guide=[
            "注册 https://exa.ai → 获取 API key",
            "export EXA_API_KEY=<your_key>",
        ],
    ),
    EnvVarDep(
        dep_id="brave_api_key",
        var_name="BRAVE_API_KEY",
        source_url="https://brave.com/search/api/",
        description="Brave 搜索 API key",
        install_guide=[
            "注册 https://brave.com/search/api/ → 获取 API key",
            "export BRAVE_API_KEY=<your_key>",
        ],
    ),
    EnvVarDep(
        dep_id="github_token",
        var_name="GITHUB_TOKEN",
        source_url="https://github.com/settings/tokens",
        description="GitHub 个人访问令牌（代码搜索 + issue 搜索）",
        install_guide=[
            "https://github.com/settings/tokens → Generate new token (classic)",
            "权限: public_repo (不必选全部)",
            "export GITHUB_TOKEN=<your_token>",
        ],
    ),
    EnvVarDep(
        dep_id="searxng_url",
        var_name="SEARXNG_URL",
        source_url="https://github.com/searxng/searxng",
        description="SearXNG 实例 URL（恢复模式兜底引擎）",
        required=False,
        install_guide=[
            "docker run -d --name searxng -p 32080:8080 searxng/searxng",
            "export SEARXNG_URL=http://127.0.0.1:32080",
        ],
    ),

    # ── Git 仓库 ──
    GitRepoDep(
        dep_id="last30days_en",
        capability="recent_web_search",
        source_url="https://github.com/mvanhorn/last30days-skill",
        description="英文社区搜索（Reddit/X/YouTube）",
        env_var="WRR_LAST30DAYS_EN",
        fallback_path="~/code/last30days-skill/skills/last30days/scripts/last30days.py",
        install_guide=[
            "git clone https://github.com/mvanhorn/last30days-skill ~/code/last30days-skill",
            "export WRR_LAST30DAYS_EN=~/code/last30days-skill/skills/last30days/scripts/last30days.py",
        ],
    ),
    GitRepoDep(
        dep_id="last30days_cn",
        capability="recent_web_search",
        source_url="https://github.com/Jesseovo/last30days-skill-cn",
        description="中文社区搜索（微博/小红书/B站/知乎等）",
        env_var="WRR_LAST30DAYS_CN",
        fallback_path="~/code/last30days-skill-cn/skills/last30days/scripts/last30days.py",
        install_guide=[
            "git clone https://github.com/Jesseovo/last30days-skill-cn ~/code/last30days-skill-cn",
            "export WRR_LAST30DAYS_CN=~/code/last30days-skill-cn/skills/last30days/scripts/last30days.py",
        ],
    ),
    GitRepoDep(
        dep_id="paper_search_mcp",
        capability="academic_search",
        source_url="https://github.com/openags/paper-search-mcp",
        description="学术论文 MCP 搜索服务（可选增强）",
        required=False,
        calling_pattern=CallingPattern.HTTP,
        install_guide=[
            "git clone https://github.com/openags/paper-search-mcp",
            "cd paper-search-mcp && pip install -e .",
            "python -m paper_search_mcp  # 启动服务",
        ],
    ),
    GitRepoDep(
        dep_id="agent_reach",
        capability="internet_access",
        source_url="https://github.com/Panniantong/Agent-Reach",
        description="Agent-Reach 互联网接入（OpenCLI 底层渠道）",
        install_guide=[
            "git clone https://github.com/Panniantong/Agent-Reach",
            "cd Agent-Reach && pip install -e .",
        ],
    ),

    # ── CLI 工具 ──
    CliToolDep(
        dep_id="opencli",
        binary="opencli",
        version_flag="--version",
        source_url="https://github.com/Panniantong/Agent-Reach",
        description="OpenCLI 社区搜索 CLI（Agent-Reach 提供）",
        extra_paths=["~/.local/bin/opencli"],
        install_guide=[
            "brew install opencli  # macOS",
            "或 pip install opencli",
        ],
    ),
    CliToolDep(
        dep_id="qmd",
        binary="qmd",
        version_flag="--version",
        source_url="https://github.com/qmd/qmd",
        description="qmd 全文搜索引擎（Obsidian vault 索引）",
        extra_paths=["/opt/homebrew/bin/qmd"],
        install_guide=[
            "brew install qmd  # macOS",
            "或从 https://github.com/qmd/qmd 安装",
        ],
    ),

    # ── Docker 容器 ──
    DockerDep(
        dep_id="searxng",
        container_name="searxng",
        health_url="http://127.0.0.1:32080",
        source_url="https://github.com/searxng/searxng",
        description="SearXNG 无搜索引擎（恢复模式兜底）",
        required=False,
        install_guide=[
            "docker run -d --name searxng -p 32080:8080 searxng/searxng",
            "export SEARXNG_URL=http://127.0.0.1:32080",
        ],
    ),

    # ── Hermes 内置工具 ──
    HermesToolDep(
        dep_id="supermemory",
        tool_name="supermemory",
        description="云端长期记忆检索",
    ),
    HermesToolDep(
        dep_id="session_search",
        tool_name="session_search",
        description="本地历史对话检索",
    ),
]


# ══════════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════════

class DepRegistry:
    """全量依赖注册表（从 DEPENDENCY_MANIFEST 加载）。"""

    _instance: Optional[DepRegistry] = None

    def __init__(self) -> None:
        self._deps: Dict[str, BaseDep] = {}
        self._load()

    @classmethod
    def get(cls) -> DepRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load(self) -> None:
        for dep in DEPENDENCY_MANIFEST:
            self._deps[dep.id] = dep

    @property
    def all(self) -> Dict[str, BaseDep]:
        return self._deps

    def get_dep(self, dep_id: str) -> Optional[BaseDep]:
        return self._deps.get(dep_id)

    def by_type(self, dep_type: DepType) -> List[BaseDep]:
        return [d for d in self._deps.values() if d.dep_type == dep_type]


# ══════════════════════════════════════════════════════════════════════
# v6 manifest -> v5 dependency bridge
# ══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ManifestDepsBridgeReport:
    """Comparison report for legacy deps and manifest-derived legacy deps."""

    legacy_ids: Tuple[str, ...]
    manifest_ids: Tuple[str, ...]
    required_missing_ids: Tuple[str, ...]
    type_alignment: Dict[str, Dict[str, Tuple[str, ...]]]
    intentional_type_gaps: Dict[str, str] = field(default_factory=dict)

    @property
    def no_required_dependency_disappears(self) -> bool:
        return not self.required_missing_ids

    def to_dict(self) -> Dict[str, Any]:
        return {
            "no_required_dependency_disappears": self.no_required_dependency_disappears,
            "legacy_ids": list(self.legacy_ids),
            "manifest_ids": list(self.manifest_ids),
            "required_missing_ids": list(self.required_missing_ids),
            "type_alignment": {
                dep_type: {
                    "legacy": list(values["legacy"]),
                    "manifest": list(values["manifest"]),
                    "missing": list(values["missing"]),
                    "extra": list(values["extra"]),
                }
                for dep_type, values in self.type_alignment.items()
            },
            "intentional_type_gaps": dict(self.intentional_type_gaps),
        }


def builtin_manifest_legacy_deps() -> List[BaseDep]:
    """Return legacy dependency objects generated from builtin v6 manifests.

    This is an additive bridge for parity tests and migration tooling. The
    default ``DepRegistry`` still reads the static ``DEPENDENCY_MANIFEST``.
    """

    from wrr.engines.loader import discover_engine_plugins

    deps: Dict[str, BaseDep] = {}
    legacy = _legacy_deps_by_id()
    for discovery in discover_engine_plugins(include_builtin=True):
        if not discovery.valid or discovery.manifest is None:
            continue
        for dep in manifest_to_legacy_deps(discovery.manifest, legacy_deps=legacy):
            existing = deps.get(dep.id)
            if existing is None:
                deps[dep.id] = dep
                continue
            existing.required = existing.required or dep.required
    return list(deps.values())


def manifest_to_legacy_deps(
    manifest: Any,
    *,
    legacy_deps: Mapping[str, BaseDep] | None = None,
) -> List[BaseDep]:
    """Convert one v6 manifest requirements block into v5 dependency objects."""

    requirements = getattr(manifest, "requirements", {}) or {}
    if not isinstance(requirements, Mapping):
        return []

    legacy_deps = legacy_deps or _legacy_deps_by_id()
    deps: List[BaseDep] = []
    deps.extend(_env_deps_from_manifest(requirements, legacy_deps))
    deps.extend(_binary_deps_from_manifest(requirements, legacy_deps))
    deps.extend(_repo_deps_from_manifest(requirements, legacy_deps))
    deps.extend(_docker_deps_from_manifest(requirements, legacy_deps))
    deps.extend(_hermes_tool_deps_from_manifest(requirements, legacy_deps))
    return deps


def compare_manifest_bridge_to_legacy(
    *,
    legacy_deps: Iterable[BaseDep] = DEPENDENCY_MANIFEST,
    manifest_deps: Iterable[BaseDep] | None = None,
) -> ManifestDepsBridgeReport:
    """Compare static v5 deps with the builtin manifest-derived deps view."""

    legacy_list = list(legacy_deps)
    manifest_list = list(manifest_deps) if manifest_deps is not None else builtin_manifest_legacy_deps()
    legacy_by_id = {dep.id: dep for dep in legacy_list}
    manifest_by_id = {dep.id: dep for dep in manifest_list}

    required_missing = tuple(
        sorted(
            dep_id
            for dep_id, dep in legacy_by_id.items()
            if dep.required and dep_id not in manifest_by_id
        )
    )

    type_alignment: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    intentional_type_gaps: Dict[str, str] = {}
    for dep_type in DepType:
        legacy_ids = {dep.id for dep in legacy_list if dep.dep_type == dep_type}
        manifest_ids = {dep.id for dep in manifest_list if dep.dep_type == dep_type}
        missing = legacy_ids - manifest_ids
        extra = manifest_ids - legacy_ids
        type_alignment[dep_type.value] = {
            "legacy": tuple(sorted(legacy_ids)),
            "manifest": tuple(sorted(manifest_ids)),
            "missing": tuple(sorted(missing)),
            "extra": tuple(sorted(extra)),
        }
        if dep_type == DepType.PYTHON_PKG and not legacy_ids and not manifest_ids:
            intentional_type_gaps[dep_type.value] = (
                "legacy enum exists, but v5 DEPENDENCY_MANIFEST declares no python_pkg deps"
            )

    return ManifestDepsBridgeReport(
        legacy_ids=tuple(dep.id for dep in legacy_list),
        manifest_ids=tuple(dep.id for dep in manifest_list),
        required_missing_ids=required_missing,
        type_alignment=type_alignment,
        intentional_type_gaps=intentional_type_gaps,
    )


def _legacy_deps_by_id() -> Dict[str, BaseDep]:
    return {dep.id: dep for dep in DEPENDENCY_MANIFEST}


def _env_deps_from_manifest(
    requirements: Mapping[str, Any],
    legacy_deps: Mapping[str, BaseDep],
) -> List[BaseDep]:
    out: List[BaseDep] = []
    for item in _requirement_items(requirements, "env"):
        var_name = str(item.get("env") or item.get("name") or "")
        if not var_name:
            continue
        dep_id = _dep_id(item, _env_dep_id(var_name))
        legacy = legacy_deps.get(dep_id)
        out.append(
            EnvVarDep(
                dep_id=dep_id,
                var_name=var_name,
                source_url=_source_url(item, legacy, f"env:{var_name}"),
                description=_description(item, legacy, f"{var_name} environment variable"),
                required=_required(item, legacy),
                install_guide=_install_guide(item, legacy),
            )
        )
    return out


def _binary_deps_from_manifest(
    requirements: Mapping[str, Any],
    legacy_deps: Mapping[str, BaseDep],
) -> List[BaseDep]:
    out: List[BaseDep] = []
    for item in _requirement_items(requirements, "binaries"):
        binary = str(item.get("binary") or item.get("name") or "")
        if not binary:
            continue
        dep_id = _dep_id(item, binary)
        legacy = legacy_deps.get(dep_id)
        out.append(
            CliToolDep(
                dep_id=dep_id,
                binary=binary,
                version_flag=str(item.get("version_flag") or "--version"),
                source_url=_source_url(item, legacy, f"binary:{binary}"),
                description=_description(item, legacy, f"{binary} command"),
                required=_required(item, legacy),
                install_guide=_install_guide(item, legacy),
                extra_paths=[
                    str(path)
                    for path in item.get("extra_paths", [])
                    if isinstance(path, str)
                ],
            )
        )
    return out


def _repo_deps_from_manifest(
    requirements: Mapping[str, Any],
    legacy_deps: Mapping[str, BaseDep],
) -> List[BaseDep]:
    out: List[BaseDep] = []
    for item in _requirement_items(requirements, "repos"):
        name = str(item.get("name") or item.get("repo") or "")
        if not name:
            continue
        dep_id = _dep_id(item, name)
        legacy = legacy_deps.get(dep_id)
        calling_pattern = CallingPattern(str(item.get("calling_pattern") or "subprocess"))
        out.append(
            GitRepoDep(
                dep_id=dep_id,
                capability=str(item.get("capability") or name),
                source_url=_source_url(item, legacy, str(item.get("remote") or f"repo:{name}")),
                description=_description(item, legacy, f"{name} repository"),
                required=_required(item, legacy),
                env_var=str(item["env"]) if item.get("env") else None,
                fallback_path=str(item["default_path"]) if item.get("default_path") else None,
                install_guide=_install_guide(item, legacy),
                calling_pattern=calling_pattern,
            )
        )
    return out


def _docker_deps_from_manifest(
    requirements: Mapping[str, Any],
    legacy_deps: Mapping[str, BaseDep],
) -> List[BaseDep]:
    out: List[BaseDep] = []
    for item in _requirement_items(requirements, "docker"):
        container = str(item.get("container") or item.get("name") or "")
        if not container:
            continue
        dep_id = _dep_id(item, container)
        legacy = legacy_deps.get(dep_id)
        out.append(
            DockerDep(
                dep_id=dep_id,
                container_name=container,
                health_url=str(item.get("health_url") or ""),
                source_url=_source_url(item, legacy, f"docker:{container}"),
                description=_description(item, legacy, f"{container} container"),
                required=_required(item, legacy),
                install_guide=_install_guide(item, legacy),
            )
        )
    return out


def _hermes_tool_deps_from_manifest(
    requirements: Mapping[str, Any],
    legacy_deps: Mapping[str, BaseDep],
) -> List[BaseDep]:
    out: List[BaseDep] = []
    for item in _requirement_items(requirements, "hermes_tools"):
        tool_name = str(item.get("tool") or item.get("name") or "")
        if not tool_name:
            continue
        dep_id = _dep_id(item, tool_name)
        legacy = legacy_deps.get(dep_id)
        out.append(
            HermesToolDep(
                dep_id=dep_id,
                tool_name=tool_name,
                source_url=_source_url(item, legacy, "https://hermes-agent.nousresearch.com/docs"),
                description=_description(item, legacy, f"{tool_name} Hermes tool"),
            )
        )
        out[-1].required = _required(item, legacy)
    return out


def _requirement_items(requirements: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = requirements.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _dep_id(item: Mapping[str, Any], fallback: str) -> str:
    return str(item.get("dep_id") or item.get("id") or fallback)


def _env_dep_id(var_name: str) -> str:
    return {
        "EXA_API_KEY": "exa_api_key",
        "BRAVE_API_KEY": "brave_api_key",
        "GITHUB_TOKEN": "github_token",
        "SEARXNG_URL": "searxng_url",
    }.get(var_name, var_name.lower())


def _required(item: Mapping[str, Any], legacy: BaseDep | None) -> bool:
    if "required" in item:
        return bool(item["required"])
    if legacy is not None:
        return bool(legacy.required)
    return True


def _source_url(item: Mapping[str, Any], legacy: BaseDep | None, fallback: str) -> str:
    value = item.get("source_url") or item.get("remote")
    if value:
        return str(value)
    if legacy is not None:
        return legacy.source_url
    return fallback


def _description(item: Mapping[str, Any], legacy: BaseDep | None, fallback: str) -> str:
    value = item.get("description")
    if value:
        return str(value)
    if legacy is not None:
        return legacy.description
    return fallback


def _install_guide(item: Mapping[str, Any], legacy: BaseDep | None) -> List[str]:
    value = item.get("install_guide")
    if isinstance(value, list):
        return [str(step) for step in value]
    if legacy is not None:
        return list(legacy.install_guide)
    return []
