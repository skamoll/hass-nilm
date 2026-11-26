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
import random
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
    PatternCandidate,
    coincidences,
    covered_by_profile,
    discover_patterns,
    flow_share,
    profile_from_candidate,
    residual_series,
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
# Bump when discovery/clue logic changes: the next start re-learns at once.
DISCOVERY_VERSION = 4  # v0.2.4: learns from the residual (sub-meters removed)
CLUE_WINDOW_S = 90
RANDOM_WINDOWS = 40
CLUE_DOMAINS = {
    "binary_sensor",
    "switch",
    "light",
    "cover",
    "media_player",
    "input_boolean",
    "lock",
    "person",
    "device_tracker",
    "climate",
    "fan",
    "vacuum",
    "remote",
    "select",
    "input_select",
}
FLOW_UNITS = {"L/min", "l/min", "L/h", "m³/h"}
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
NONE_TEXT = "—"


def issue_id_for(entry_id: str, candidate_id: str) -> str:
    return f"{ISSUE_PREFIX}{entry_id}_{candidate_id}"


class PatternDiscovery:
    """Per-line pattern learner backed by a Store and Repairs issues."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        power_entity: str,
        submeter_entities: tuple[str, ...] = (),
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.power_entity = power_entity
        self.submeter_entities = tuple(submeter_entities)
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
        # A detector may have been added since the last run (entry reloads on
        # every options change): close the questions it now answers.
        if self._reconcile():
            self._store.async_delay_save(lambda: self.data, 1)
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
        if not self.data.get("last_run") or self.data.get("discovery_version") != DISCOVERY_VERSION:
            self._unsubs.append(async_call_later(self.hass, STARTUP_DELAY_S, self._scheduled_run))

    async def async_shutdown(self) -> None:
        """Stop timers and flush the store: a reload right after a change (e.g. a
        detector added) must not read a store whose delayed save hasn't landed."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self.data)

    async def _scheduled_run(self, _now: datetime | None = None) -> None:
        try:
            await self.async_run()
        except Exception:  # noqa: BLE001 - a failed learning run must not break the line
            _LOGGER.exception("hass_nilm pattern discovery failed for %s", self.entry.title)

    # ------------------------------------------------------------------ run

    async def _async_load_samples(self, days: int) -> list[tuple[datetime, float]]:
        """The line's residual history: parent meter minus metered children."""
        if "recorder" not in self.hass.config.components:
            return []
        parent = await self._async_load_series(self.power_entity, days)
        subs = [await self._async_load_series(e, days) for e in self.submeter_entities]
        if not subs:
            return parent
        return await self.hass.async_add_executor_job(residual_series, parent, subs)

    async def _async_load_series(self, entity: str, days: int) -> list[tuple[datetime, float]]:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder import history as recorder_history

        end = dt_util.utcnow()
        start = end - timedelta(days=days)

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
                        out.append((dt_util.as_local(st.last_updated), w))
                del states
                first = False
                t0 = t1
            return out

        return await get_instance(self.hass).async_add_executor_job(_job)

    async def async_run(self, days: int = HISTORY_DAYS) -> dict[str, Any]:
        """Learn from history, update the store and the Repairs issues."""
        if self._running:
            return {"skipped": "already running"}
        self._running = True
        try:
            samples = await self._async_load_samples(days)
            found: list[PatternCandidate] = await self.hass.async_add_executor_job(
                lambda: discover_patterns(samples, DiscoveryConfig())
            )
            summary = self.process_candidates([to_dict(c) for c in found])
            by_cid = {c.candidate_id: c for c in found}
            targets = {
                pid: by_cid[cid]
                for pid, cid in summary.pop("seen").items()
                if pid in self._asked() and cid in by_cid
            }
            clues = await self._async_clues(targets, days)
            for pid, clue in clues.items():
                self.data["patterns"][pid]["clues"] = clue
            self.data["discovery_version"] = DISCOVERY_VERSION
            self._store.async_delay_save(lambda: self.data, 1)
            self._sync_issues()
            summary["samples"] = len(samples)
            summary["clues_for"] = sorted(clues)
            return summary
        finally:
            self._running = False

    @callback
    def process_candidates(self, cands: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge a discovery result into the store (pure bookkeeping, testable)."""
        patterns: dict[str, dict[str, Any]] = self.data.setdefault("patterns", {})
        profiles = list(self.entry.options.get(CONF_APPLIANCES, []))
        new, updated, known = [], [], []
        seen: dict[str, str] = {}
        for cand in cands:
            match_id = next(
                (pid for pid, p in patterns.items() if same_pattern(p["stats"], cand)), None
            )
            if match_id is not None:
                patterns[match_id]["stats"] = cand
                patterns[match_id]["last_seen_run"] = dt_util.utcnow().isoformat()
                updated.append(match_id)
                seen[match_id] = cand["candidate_id"]
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
            seen[pid] = cand["candidate_id"]
        retired = self._reconcile(seen=set(seen))
        self.data["last_run"] = dt_util.utcnow().isoformat()
        self._store.async_delay_save(lambda: self.data, 1)
        self._sync_issues()
        return {"new": new, "updated": updated, "known": known, "retired": retired, "seen": seen}

    @callback
    def _reconcile(self, seen: set[str] | None = None) -> list[str]:
        """Close pending questions that no longer apply.

        - a detector now explains it (added by hand or by naming another pattern)
          -> ``known``;
        - the latest run no longer finds it (e.g. a sub-meter now removes that
          load) -> forgotten.
        """
        patterns: dict[str, dict[str, Any]] = self.data.setdefault("patterns", {})
        profiles = list(self.entry.options.get(CONF_APPLIANCES, []))
        changed: list[str] = []
        for pid in list(patterns):
            pat = patterns[pid]
            if pat["status"] != "pending":
                continue
            covered = covered_by_profile(pat["stats"], profiles)
            if covered:
                pat["status"], pat["appliance_id"] = "known", covered
                changed.append(pid)
            elif seen is not None and pid not in seen:
                del patterns[pid]
                ir.async_delete_issue(self.hass, DOMAIN, issue_id_for(self.entry.entry_id, pid))
                changed.append(pid)
        return changed

    @callback
    def _asked(self) -> set[str]:
        """The pending patterns that get a Repairs question (biggest energy first)."""
        patterns = self.data.get("patterns", {})
        pending = sorted(
            (pid for pid, p in patterns.items() if p["status"] == "pending"),
            key=lambda pid: patterns[pid]["stats"].get("energy_kwh_per_day", 0),
            reverse=True,
        )
        return set(pending[:MAX_PENDING_ISSUES])

    # ---------------------------------------------------------------- clues

    async def _async_clues(
        self, targets: dict[str, PatternCandidate], days: int
    ) -> dict[str, dict[str, str]]:
        """What else happens in the house when each pattern runs.

        For the last runs of each asked pattern: which entities change state within
        +-90 s (versus random instants: the lift), and whether the house water
        meter shows flow. Read-only Recorder queries, off the event loop.
        """
        if not targets or "recorder" not in self.hass.config.components:
            return {}
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder import history as recorder_history
        from homeassistant.helpers import entity_registry as er

        own = {
            e.entity_id
            for e in er.async_entries_for_config_entry(er.async_get(self.hass), self.entry.entry_id)
        }
        own.add(self.power_entity)
        own.update(self.submeter_entities)
        ents = [
            st.entity_id
            for st in self.hass.states.async_all()
            if st.domain in CLUE_DOMAINS and st.entity_id not in own
        ]
        water = next(
            (
                st.entity_id
                for st in self.hass.states.async_all("sensor")
                if st.attributes.get("unit_of_measurement") in FLOW_UNITS
            ),
            None,
        )
        names = {e: (self.hass.states.get(e).name if self.hass.states.get(e) else e) for e in ents}
        hass = self.hass
        win = timedelta(seconds=CLUE_WINDOW_S)

        def job() -> dict[str, dict[str, str]]:
            def changed(t: datetime) -> set[str]:
                if not ents:
                    return set()
                res = recorder_history.get_significant_states(
                    hass,
                    t - win,
                    t + win,
                    ents,
                    include_start_time_state=False,
                    significant_changes_only=False,
                    minimal_response=True,
                    no_attributes=True,
                )
                return {e for e, rows in res.items() if rows}

            now = dt_util.utcnow()
            randoms = [
                now - timedelta(seconds=random.uniform(0, days * 86400))
                for _ in range(RANDOM_WINDOWS)
            ]
            base = [changed(t) for t in randoms]
            flow: list[tuple[datetime, float]] = []
            if water:
                for st in recorder_history.state_changes_during_period(
                    hass, now - timedelta(days=days), now, water, no_attributes=True
                ).get(water, []):
                    try:
                        flow.append((st.last_updated, float(st.state)))
                    except (TypeError, ValueError):
                        continue
            out: dict[str, dict[str, str]] = {}
            for pid, cand in targets.items():
                runs = list(cand.recent_runs)
                per_run = [changed(a) | changed(b) for a, b in runs]
                found = coincidences(per_run, base)
                coincide = " · ".join(f"{names.get(e, e)} ({rate:.0%})" for e, rate, _ in found)
                water_txt = "sin caudalímetro"
                if water:
                    share = flow_share(runs, flow)
                    span = runs[0][1] - runs[0][0] if runs else timedelta(minutes=5)
                    base_share = flow_share([(t, t + span) for t in randoms], flow)
                    if share is not None:
                        water_txt = (
                            f"agua en el {share:.0%} de los usos (al azar: {base_share or 0:.0%})"
                        )
                out[pid] = {"coincide": coincide or "nada destacable", "water": water_txt}
            return out

        try:
            return await get_instance(self.hass).async_add_executor_job(job)
        except Exception:  # noqa: BLE001 - clues are a nicety; never break learning
            _LOGGER.exception("hass_nilm clue lookup failed for %s", self.entry.title)
            return {}

    # --------------------------------------------------------------- issues

    @callback
    def _sync_issues(self) -> None:
        """One Repairs issue per pending pattern (biggest energy first, capped)."""
        patterns = self.data.get("patterns", {})
        ask = self._asked()
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
                    **_time_clues(s),
                    "coincide": pat.get("clues", {}).get("coincide", NONE_TEXT),
                    "water": pat.get("clues", {}).get("water", NONE_TEXT),
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


def _time_clues(stats: dict[str, Any]) -> dict[str, str]:
    """Peak hours, busiest weekdays and last runs, in the user's words."""
    hours = stats.get("hours_profile") or []
    top_h = [h for h in sorted(range(len(hours)), key=lambda h: -hours[h]) if hours[h]][:3]
    days = stats.get("weekday_profile") or []
    top_d = [d for d in sorted(range(len(days)), key=lambda d: -days[d]) if days[d]][:3]
    last = []
    for ex in (stats.get("examples") or [])[-3:]:
        try:
            when = dt_util.as_local(datetime.fromisoformat(ex["start"]))
        except (KeyError, TypeError, ValueError):
            continue
        last.append(
            f"{WEEKDAYS[when.weekday()]} {when:%d %H:%M} ({ex.get('duration_min', 0):.0f} min)"
        )
    return {
        "hours": " · ".join(f"{h:02d}h" for h in top_h) or NONE_TEXT,
        "days": " · ".join(WEEKDAYS[d] for d in top_d) or NONE_TEXT,
        "last": " · ".join(reversed(last)) or NONE_TEXT,
    }
