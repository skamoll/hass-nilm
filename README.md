# NILM Appliance Disaggregator for Home Assistant (`hass-nilm`)

[![CI](https://github.com/skamoll/hass-nilm/actions/workflows/ci.yml/badge.svg)](https://github.com/skamoll/hass-nilm/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A deterministic, zero-dependency Home Assistant custom integration (`hass_nilm`) for **Non-Intrusive Load Monitoring (NILM)**, hierarchical sub-meter subtraction, and multi-phase appliance cycle/program classification — all running natively inside Home Assistant with `O(1)` real-time overhead and zero external add-ons or ML containers.

---

## Architecture & Core Invariants

- **Pure Deterministic Engine (`custom_components/hass_nilm/engine/`):** Zero `homeassistant.*` imports, bounded `collections.deque` ring buffers, median edge-preserving filtering, and trapezoidal Riemann energy integration.
- **Hierarchical Residual Tree (`SourceLine`) with Skew Guard:** Subtracts known child sub-meters (`max(0, P_parent - ∑ P_children)`) while applying a configurable settling window (`skew_hold_s`) whenever a child load toggles, preventing phantom residual spikes caused by different sensor polling intervals.
- **Three Composable Appliance Archetypes:**
  1. **`step` (*NILM Blueprint* style):** Pairs rising and falling `ΔW` edges within `[min_power_w, max_power_w]` with `min_on_s` / `max_on_s` guards (resistive heaters, ovens, kettles, pumps).
  2. **`cycling` (Thermostatic / pulsed):** Groups repeated heating bursts separated by thermal off-gaps into a single active session while attributing power only during real active draws (ovens, hobs, fridges, irons).
  3. **`multiphase` (Finite-state cycle & program matcher):** Tracks ordered phases (e.g., `prewash` → `heating` → `wash_rinse` → `spin`) on shared circuits or dedicated smart plugs, closes cycles only after valid work + sustained idle, and matches labeled program fingerprints (returning `unknown_program` when ambiguous).
- **Power Conservation Ceiling:** Active inferred appliance powers on a `SourceLine` are proportionally or priority-clamped so `∑ P_inferred ≤ P_residual` at every instant.
- **On-Demand Recorder Scanner:** Service `hass_nilm.scan_recorder` inspects historical Recorder states on a background executor thread (`async_add_executor_job`) and suggests step signatures and recurring cycle profiles.

---

## Learning appliances (pattern discovery)

You do not need to know how an appliance consumes. Once a day (03:40, and 5 minutes after the first start) each line learns from the last 30 days of its power history in the Recorder:

1. **Discover** - steady-state edges are detected, ON/OFF edges are paired into runs, and runs are grouped by power and duration (density peaks on a log scale). Each group is a *pattern*: median power, run length, runs per day, periodicity and a hint (fridge compressor, No Frost defrost, kettle...).
2. **Ask** - every new pattern that no existing detector explains appears in **Settings > System > Repairs** as *"New load pattern on <line>: which appliance is it?"*. Open it and either **name** it or **ignore** it.
3. **Build** - naming a pattern creates its detector automatically, with power bands and timings taken from the observed spread. Patterns are matched across days, so each one is asked once.

Run it on demand with the `hass_nilm.discover_patterns` action (returns the learned patterns). History is read in 3-day chunks without attributes, off the event loop.

## Entities Created per Appliance

| Platform | Entity Key | Device / State Class | Unit | Purpose |
|---|---|---|---|---|
| `binary_sensor` | `running` | `running` | — | True while the appliance session/cycle is active |
| `sensor` | `phase` | — | — | `off`, `running`, `paused_thermal`, or active phase name |
| `sensor` | `power` | `power` / `measurement` | `W` | Instantaneous attributed power (conservation-bounded) |
| `sensor` | `energy` | `energy` / `total_increasing` | `kWh` | Cumulative energy (trapezoidal Riemann + `RestoreEntity`, Energy Dashboard ready) |
| `sensor` | `last_cycle` | — | — | Summary of last completed cycle (`matched_program`, `duration_min`, `energy_kwh`, `peak_w`, `high_blocks`, `confidence`) |

Plus diagnostic line sensors (`residual_power` in `W`, `unexplained_power` in `W`) and bus event `hass_nilm_cycle_finished` whenever an appliance completes a valid cycle.

---

## Installation

1. Install via **HACS → Integrations → Custom repositories** (`https://github.com/skamoll/hass-nilm`, category **Integration**) or copy `custom_components/hass_nilm` into `<config>/custom_components/hass_nilm`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration → NILM Appliance Disaggregator** to configure a monitored source line (`parent_power_entity` + optional `known_submeters`), then add appliances via **Configure** (`OptionsFlow`).
4. Optional: Run action **`hass_nilm.scan_recorder`** in **Developer Tools → Actions** against any power sensor to discover step sizes and recurring multi-block cycles from your Recorder history.

---

## License

MIT © Andres Bestard. See [LICENSE](LICENSE).
