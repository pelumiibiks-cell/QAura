"""`qaura init --check`: does this config's invariants still apply to the live site?

Selectors rot. A redesign renames a class, a component library bumps a version, and an
invariant that used to guard the checkout total quietly stops matching anything. Nothing
breaks — extract_values() raises InvariantError, and core/invariants.check() correctly
treats that as inconclusive rather than a violation — which is exactly the problem: the
rule stops protecting you and the report looks identical to one where it passed.

This turns that silence into an answer. Read-only, no LLM, writes nothing, so it is cheap
enough to run in CI and notice the day a selector dies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from qaura.browser.driver import ContextSpec, Driver
from qaura.browser.readonly import ReadOnlyLedger, install_readonly_routes
from qaura.config import QAuraConfig
from qaura.core.guardrails import guard_goto
from qaura.core.invariants import InvariantError, evaluate_expression, extract_values
from qaura.init.limits import ReconLimits, recon_guardrails

CheckStatus = Literal["holds", "violated", "unmatched", "error"]


@dataclass
class InvariantCheck:
    name: str
    status: CheckStatus
    detail: str
    values: dict | None = None

    @property
    def healthy(self) -> bool:
        return self.status == "holds"


async def check_invariants(
    config: QAuraConfig,
    url: str,
    *,
    storage_state: str | None = None,
    headless: bool = True,
    limits: ReconLimits | None = None,
) -> list[InvariantCheck]:
    limits = limits or ReconLimits()
    cfg = recon_guardrails(url)
    results: list[InvariantCheck] = []
    ledger = ReadOnlyLedger()

    async with Driver(headless=headless) as driver:
        spec = ContextSpec(persona="init-check", role=None, storage_state_path=storage_state,
                           viewport=limits.viewports[0])
        async with driver.context(spec) as (context, page):
            await install_readonly_routes(context, cfg, ledger)
            page.set_default_navigation_timeout(limits.nav_timeout_ms)
            await guard_goto(page, url, cfg)
            try:
                await page.wait_for_load_state("networkidle", timeout=limits.settle_timeout_ms)
            except Exception:
                pass

            for invariant in config.invariants:
                try:
                    values = await extract_values(page, invariant)
                except InvariantError as e:
                    results.append(InvariantCheck(
                        invariant.name, "unmatched",
                        f"selectors did not resolve on this page: {e}",
                    ))
                    continue
                except Exception as e:
                    results.append(InvariantCheck(invariant.name, "error", str(e)))
                    continue

                try:
                    holds = evaluate_expression(invariant.expression, values)
                except InvariantError as e:
                    results.append(InvariantCheck(invariant.name, "error",
                                                  f"expression rejected: {e}", values))
                    continue

                results.append(InvariantCheck(
                    invariant.name,
                    "holds" if holds else "violated",
                    "held on this page" if holds else "expression evaluated False",
                    values,
                ))

    return results
