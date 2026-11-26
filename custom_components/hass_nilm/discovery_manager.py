"""Learn recurring load patterns on a line and ask the user what they are.

Once a day (and shortly after start-up) the manager reads the line's power
history from the Recorder, runs the pure discovery engine off the event loop,
and keeps a small store of patterns with their status:

- ``pending``  - new: a Repairs issue asks "which appliance is this?"
- ``named``    - the user named it and a detector profile was created
- ``ignored``  - the user said it is not worth tracking
- ``known``    - an existing detector already explains it (never asked)

Patterns are matched across runs by power and duration, so each load is asked
about once, and statistics are refreshed with every run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import CONF_APPLIANCE_ID, CONF_APPLIANCES, DOMAIN
from .coordinator import state_to_watts
from .engine.discovery import (
    DiscoveryConfig,
    covered_by_profile,
    discover_patterns,
    profile_from_candidate,
    same_pattern,
    to_dict,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
HISTORY_DAYS = 30
CHUNK_DAYS = 3
STARTUP_DELAY_S = 300
DAILY_RUN = (3, 40, 0)
MAX_PENDING_ISSUES = 5
ISSUE_PREFIX = "pattern_"


def issue_id_for(entry_id: str, candidate_id: str) -> str:
    return f"{ISSUE_PREFIX}{entry_id}_{candidate_id}"


class PatternDiscovery:
    """Per-line pattern learner backed by a Store and Repairs issues."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, power_entity: str) -> None:
        self.hass = hass
        self.entry = entry
        self.power_entity = power_entity
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.patterns.{entry.entry_id}"
        )
        self.data: dict[str, Any] = {"patterns": {}, "last_run": None}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._running = False

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        if stored:
            self.data = stored
        self._sync_issues()
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._scheduled_run,
                hour=DAILY_RUN[0],
                minute=DAILY_RUN[1],
                second=DAILY_RUN[2],
            )
        )
        if not self.data.get("last_run"):
            self._unsubs.append(async_call_later(self.hass, STARTUP_DELAY_S, self._scheduled_run))

    @callback
    def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    async def _scheduled_run(self, _now: datetime | None = None) -> None:
        try:
            await self.async_run()
        except Exception:  # noqa: BLE001 - a failed learning run must not break the line
            _LOGGER.exception("hass_nilm pattern discovery failed for %s", self.entry.title)

    # ------------------------------------------------------------------ run

    async def _async_load_samples(self, days: int) -> list[tuple[datetime, float]]:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder import history as recorder_history

        end = dt_util.utcnow()
        start = end - timedelta(days=days)
        entity = self.power_entity

        def _job() -> list[tuple[datetime, float]]:
            out: list[tuple[datetime, float]] = []
            t0 = start
            first = True
            while t0 < end:
                t1 = min(end, t0 + timedelta(days=CHUNK_DAYS))
                states = recorder_history.state_changes_during_period(
                    self.hass,
                    t0,
                    t1,
                    entity,
                    no_attributes=True,
                    include_start_time_state=first,
                ).get(entity, [])
                for st in states:
                    w = state_to_watts(st)
                    if w is not None:
                        out.append((st.last_updated, w))
                del states
                first = False
                t0 = t1
            return out

        if "recorder" not in self.hass.config.components:
            return []
        return await get_instance(self.hass).async_add_executor_job(_job)

    async def async_run(self, days: int = HISTORY_DAYS) -> dict[str, Any]:
        """Learn from history, update the store and the Repairs issues."""
        if self._running:
            return {"skipped": "already running"}
        self._running = True
        try:
            samples = await self._async_load_samples(days)
            cands = await self.hass.async_add_executor_job(
                lambda: [to_dict(c) for c in discover_patterns(samples, DiscoveryConfig())]
            )
            summary = self.process_candidates(cands)
            summary["samples"] = len(samples)
            return summary
        finally:
            self._running = False

    @callback
    def process_candidates(self, cands: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge a discovery result into the store (pure bookkeeping, testable)."""
        patterns: dict[str, dict[str, Any]] = self.data.setdefault("patterns", {})
        profiles = list(self.entry.options.get(CONF_APPLIANCES, []))
        new, updated, known = [], [], []
        for cand in cands:
            match_id = next(
                (pid for pid, p in patterns.items() if same_pattern(p["stats"], cand)), None
            )
            if match_id is not None:
                patterns[match_id]["stats"] = cand
                patterns[match_id]["last_seen_run"] = dt_util.utcnow().isoformat()
                updated.append(match_id)
                continue
            pid = slugify(cand["candidate_id"])
            covered = covered_by_profile(cand, profiles)
            patterns[pid] = {
                "stats": cand,
                "status": "known" if covered else "pending",
                "appliance_id": covered,
                "first_found": dt_util.utcnow().isoformat(),
                "last_seen_run": dt_util.utcnow().isoformat(),
            }
            (known if covered else new).append(pid)
        self.data["last_run"] = dt_util.utcnow().isoformat()
        self._store.async_delay_save(lambda: self.data, 1)
        self._sync_issues()
        return {"new": new, "updated": updated, "known": known}

    # --------------------------------------------------------------- issues

    @callback
    def _sync_issues(self) -> None:
        """One Repairs issue per pending pattern (biggest energy first, capped)."""
        patterns = self.data.get("patterns", {})
        pending = sorted(
            (pid for pid, p in patterns.items() if p["status"] == "pending"),
            key=lambda pid: patterns[pid]["stats"].get("energy_kwh_per_day", 0),
            reverse=True,
        )
        ask = set(pending[:MAX_PENDING_ISSUES])
        for pid, pat in patterns.items():
            iid = issue_id_for(self.entry.entry_id, pid)
            if pid not in ask:
                ir.async_delete_issue(self.hass, DOMAIN, iid)
                continue
            s = pat["stats"]
            per = s.get("period_min")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                iid,
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="new_pattern",
                translation_placeholders={
                    "line": self.entry.title,
                    "power": f"{s['power_w']:.0f}",
                    "duration": f"{s['duration_min']:.0f}",
                    "per_day": f"{s['events_per_day']:.1f}",
                    "period": f"{per:.0f} min" if per and s.get("periodic") else "irregular",
                    "kwh_day": f"{s['energy_kwh_per_day']:.2f}",
                    "hint": s.get("hint", ""),
                },
                data={"entry_id": self.entry.entry_id, "pattern_id": pid},
            )

    async def async_resolve(self, pattern_id: str, name: str | None) -> str | None:
        """Name a pattern (creating its detector) or ignore it. Returns appliance_id."""
        pat = self.data.get("patterns", {}).get(pattern_id)
        if pat is None:
            return None
        appliance_id: str | None = None
        if name and name.strip():
            profile = profile_from_candidate(pat["stats"], name)
            appliance_id = slugify(profile["name"])
            profile[CONF_APPLIANCE_ID] = appliance_id
            apps = [
                a
                for a in self.entry.options.get(CONF_APPLIANCES, [])
                if a.get(CONF_APPLIANCE_ID) != appliance_id
            ]
            apps.append(profile)
            pat["status"] = "named"
            pat["appliance_id"] = appliance_id
        else:
            pat["status"] = "ignored"
        pat["resolved_at"] = dt_util.utcnow().isoformat()
        await self._store.async_save(self.data)
        ir.async_delete_issue(self.hass, DOMAIN, issue_id_for(self.entry.entry_id, pattern_id))
        if appliance_id:
            # Updating options reloads the entry; do it last.
            self.hass.config_entries.async_update_entry(
                self.entry, options={**self.entry.options, CONF_APPLIANCES: apps}
            )
        else:
            self._sync_issues()
        return appliance_id

    def as_dict(self) -> dict[str, Any]:
        return self.data
