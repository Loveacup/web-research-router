"""浏览器兜底源策略脚手架（Slice 1，禁用态）。

本模块只声明「浏览器兜底源（browser-harness fallback）」的策略开关与占位，用于
后续 v6.x 路线图接线前留出设计接缝（Seam）。

Slice 1 约束（须经 L1/L2 设计评审方可放宽）：
  - **默认禁用**：`enabled=False`，无任何启用路径。
  - **零浏览器依赖**：不导入任何浏览器自动化框架，不启动浏览器。
  - **不触碰登录材料**：不访问任何会话材料或密钥材料。
  - **非热路径**：不被 `wrr search`（community.py）导入或调用。

真实浏览器自动化属后续切片，本文件不得包含其实现。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserHarnessPolicy:
    """浏览器兜底策略（不可变）。当前切片仅承载禁用态开关。

    字段：
      enabled              — 是否启用浏览器兜底源（Slice 1 恒为 False）。
      allow_launch         — 是否允许启动浏览器进程（Slice 1 恒为 False）。
      allow_secret_access  — 是否允许访问会话材料/密钥材料（Slice 1 恒为 False）。
    """
    enabled: bool = False
    allow_launch: bool = False
    allow_secret_access: bool = False


# 进程级默认策略：全禁用。
DEFAULT_BROWSER_HARNESS_POLICY = BrowserHarnessPolicy()


def is_browser_harness_enabled(policy: BrowserHarnessPolicy | None = None) -> bool:
    """浏览器兜底源是否启用。缺省用 DEFAULT_BROWSER_HARNESS_POLICY（禁用）。"""
    return bool((policy or DEFAULT_BROWSER_HARNESS_POLICY).enabled)
