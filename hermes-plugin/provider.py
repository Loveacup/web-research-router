"""Search-only Hermes provider backed by WRR's shared Evidence pipeline."""
from __future__ import annotations

import os

from plugins.web._common import BaseWebSearchProvider, search_fail, search_ok, web_hit


class WrrWebSearchProvider(BaseWebSearchProvider):
    NAME = "wrr"
    DISPLAY_NAME = "WRR"
    EXTRACT = False
    PRIMARY_KEY_ENVS = ("EXA_API_KEY", "DEEPSEEK_API_KEY")

    def is_available(self) -> bool:
        # Local import/key checks only. Never perform discovery probes or HTTP.
        try:
            from wrr.core.hermes_bridge import search_sync
        except ImportError:
            return False
        return callable(search_sync) and any(
            os.environ.get(key, "").strip() for key in self.PRIMARY_KEY_ENVS
        )

    def search(self, query: str, limit: int = 8) -> dict:
        try:
            from wrr.core.hermes_bridge import search_sync

            evidence = search_sync(query, count=limit, runtime="hermes")
            response = search_ok([
                web_hit(item["url"], item["title"], item["snippet"], position)
                for position, item in enumerate(evidence["results"], 1)
            ])
            response["data"]["wrr"] = {
                key: evidence[key] for key in ("plan", "quality", "diagnostics")
            }
            executed = evidence["quality"]["executed"]
            if executed in {"failed", "cancelled"}:
                response.update(search_fail(f"WRR {executed}"))
            return response
        except ImportError:
            return search_fail("WRR dependency_unavailable")
        except TimeoutError:
            return search_fail("WRR timeout")
        except (ValueError, OSError):
            return search_fail("WRR configuration_error")
        except Exception:
            # Provider exceptions can contain URLs, request bodies and secrets.
            return search_fail("WRR search_failed")
