# Project Selene — Architecture & Design Writeup

**Neil Advani · Independent Infrastructure Resilience Assessment**

**A Healthy Status Page Is Not The Same Thing As A Resilient System.**

---

## The Two Decisions That Shaped Everything

### Decision 1 — The mapper owns every measurement. The LLM narrates.

The first question I asked was: what should the LLM never touch?

In production multi-agent pipelines, the hardest lesson was that LLMs are unreliable measurers but excellent narrators. They can produce fluent cascade explanations while miscounting paths, rounding blast-radius claims, or inferring relationships that are not in the graph. The fix was the same here: compute first, narrate second.

So `graph.py` owns every measurement without exception — blast radius, trust scores, edge classification, cycle detection, capacity conflicts, timeline extraction. The LLM receives pre-computed evidence and is explicitly told not to infer beyond it. The result is a report where every number is traceable to a deterministic computation, not a model inference.

This also means the mapper is fully reproducible. Run it ten times, get the same `map.json`. That matters for an infrastructure assessment tool — if the measurement layer is stochastic, you can't trust it.

### Decision 2 — Don't flatten the dependency model

A dependency graph can tell you where risk travels, but not whether the map is still true.

The obvious approach is to collect `/dependencies` and `/supplies`, build a graph, and call edges either "present" or "missing." I didn't do that because it collapses three fundamentally different conditions into one:

- **True stale route** — a declared dependency whose physical path was sealed or rerouted. The route no longer exists. Any blast-radius calculation that includes it will produce incorrect results.
- **Dependency concentration** — an active dependency that lost its backup path. The route still carries live traffic, but a failure now has no fallback. Calling this "stale" would misrepresent the risk: the route exists, the redundancy does not.
- **Removed backup path** — a formally retired fallback capability. The primary dependency still functions, but the safety net is gone.

These three conditions have completely different operational consequences. Flattening them into a single "mismatch" count obscures what matters: whether the risk is a phantom dependency poisoning your graph model, a live dependency with no redundancy, or a silent safety margin removal. The agent separates all three.

---

## Architecture

```
run_mapping.sh → mapper.py → graph.py → output/map.json
run_reporting.sh → reporter.py → output/report.md
```

**`mapper.py`** — Discovery and collection. BFS from the gateway traverses declared relationships to find reachable pods. A parallel DNS sweep of ports 3001–3012 finds any pods not reachable through declared dependencies. The delta between both sets is the first signal: infrastructure the documentation doesn't mention but the network exposes. All six endpoints are crawled concurrently per pod. No LLM calls.

**`graph.py`** — The analysis engine. Pure Python and NetworkX. Takes raw pod data, produces every derived measurement: declared/observed/stale/concentration/mismatch edges, blast radius with cascade simulation, articulation points, cycle detection, trust scores with penalty breakdown, capacity conflict detection (metadata vs. latest log), and colony-wide timeline extraction from logs and comms. No HTTP calls, no LLM calls.

**`reporter.py`** — Reads `map.json`, extracts pre-computed evidence, builds a structured prompt with specific findings already computed, and calls the LLM once. The LLM's job is to narrate the evidence clearly — not to discover it.

The separation is deliberate. `graph.py` can be tested independently. `mapper.py` can be re-run without touching the analysis. `reporter.py` can be re-run against the same `map.json` without re-crawling the colony. Each layer has one job.

The final report is organized around four layers: blast radius shows where failures propagate, edge taxonomy explains what kind of infrastructure truth changed, trust score compares claimed health against evidence, and stress signals show where remaining operational margin is thin. That structure is intentional — the graph tells us where risk travels, but the evidence taxonomy tells us what kind of fix is required.

---

## What the Agent Found

The measurements point to one pattern: the colony did not fail; it quietly lost its margin.

**The core finding is a zero-redundancy dependency cycle.**

Aquifer, Helios, and Terminus form a tightly coupled mutual dependency: Aquifer supplies coolant to Helios and slurry water to Terminus; Helios supplies power to both; Terminus supplies silicon to Helios and pump components to Aquifer. The cycle makes all three nodes top-tier single points of failure, even though their failure modes differ — removing any one destabilizes the other two.

This is the finding that degree-counting misses. Terminus has only 3 direct dependents, making it look minor on a raw in-degree ranking. But its blast radius is in the same top tier as Aquifer and Helios: 10 of 11 downstream pods, affecting 110–114 residents depending on the initiating failure. The cycle is why — and only cycle-aware cascade simulation reveals it.

**The status layer is structurally blind to this.**

Every pod reports nominal. The agent's trust score model — which penalizes missing backups, high utilization, warning-level log evidence, and dependency mismatches — shows Aquifer at 60/100 while it claims green. The `/status` endpoint captures uptime only. It cannot detect that backup systems were decommissioned, that utilization is above 90%, or that declared dependencies no longer reflect operational reality. A pod can lose all its redundancy and still report nominal until the moment it fails.

**The fragility was built deliberately, one directive at a time.**

Between May 2093 and February 2094, four independent directives (2093-089, 2093-P4, 2094-011, and an infrastructure simplification directive) each made a locally rational decision — reduce maintenance overhead, consolidate plumbing, reallocate reserve budget — and together removed every backup path in the colony's most critical failure domain. The log evidence is specific: dual-feed slurry to single Aquifer loop (2093-05-11), Prometheus direct water feed sealed (2093-09-30), Vault water reserve to maintenance status (2093-089), Helios backup coolant decommissioned (2094-02-14). Each entry looks like an efficiency gain. Sequenced chronologically across all pods, they tell the story of a colony that optimized itself into fragility.

**Remaining margin is thin.**

Helios logs show silicon feedstock consumption at 140% of quarterly forecast, increasing the colony's dependence on Terminus precisely as Terminus's single-loop slurry dependency on Aquifer leaves no fallback. Zephyr has 4 hours of backup power; Medica has 6 hours of oxygen reserve. A grid failure produces roughly a 2-hour intervention window before clinical oxygen supply begins to degrade.

---

## Why These Design Choices, Not Others

**Why pre-compute in graph.py instead of asking the LLM to analyze the raw map?**

Because the LLM is the wrong component to own exact graph measurements. It can miscount cascade paths, conflate declared and observed edges, and produce a report that sounds authoritative but is not verifiable. Pre-computing in deterministic code means every number in the report came from a function you can read, test, and re-run. The LLM adds narrative value; it adds no measurement authority.

**Why separate stale routes from dependency concentrations?**

Because they require different responses. A stale route means your graph model is wrong — fix the documentation before you do any impact analysis. A dependency concentration means your graph model is correct but the safety margin is gone — fix the infrastructure before you expand. Collapsing both into "mismatches" tells you something is wrong but not what to do about it.

**Why two discovery methods?**

BFS through declared relationships finds what the colony says exists. DNS sweep finds what the network actually exposes. The delta is what the colony has but doesn't document — shadow infrastructure, undeclared services, or pods that lost their documentation links. In this assessment the delta was empty, which is itself a finding: the risk is not hidden infrastructure, it's hidden fragility in documented infrastructure.

**Why a trust score instead of just flagging anomalies?**

Because a ranked score forces prioritization. Knowing that Aquifer has a warning log is less useful than knowing that Aquifer has the lowest trust score in the colony (60/100) with three compounding penalties — zero backups, above-threshold utilization, and dependency mismatches — while reporting nominal. The score makes the gap between claimed health and actual resilience explicit and comparable across pods.

---

## What I'd Do With More Time

The agent produces a static snapshot. Latent Defense's product is a live world model. The gap between them is the interesting engineering problem.

**Continuous delta detection.** Re-crawl on a schedule. Flag when declared dependencies change, when new mismatches appear, when trust scores degrade, or when the blast-radius ranking shifts. The redundancy erosion timeline in this assessment took 18 months of individually invisible decisions to accumulate. A live model would have flagged the first directive that removed a backup path before the pattern became a crisis.

**Confidence-weighted cascade simulation.** The current blast-radius model treats all declared dependencies as equally certain. In practice, high-criticality declared edges with corroborating log evidence should be weighted differently from low-criticality edges with no observed confirmation. A confidence layer on top of the graph model would produce more accurate cascade predictions and reduce false positives in the blast-radius ranking.

**Adversarial path enumeration.** Given the dependency graph and blast-radius rankings, enumerate the minimal set of nodes whose simultaneous failure would maximize colony fragmentation. This is the natural extension from "which single pod is most dangerous" to "what does a targeted multi-point failure look like" — which is the question a real red-team assessment would ask, and the question Latent Defense's autonomous red-teaming product would need to answer.

---

## A Note on the Assessment

The colony's /status endpoints are uniformly nominal. The safety review dated 2094-07-15 confirms all systems operational. The agent found no broken pods and no active failures.

What it found was a colony that has been systematically optimized for efficiency at the cost of resilience — and a monitoring layer that cannot tell the difference between a colony that is healthy and one that is one failure away from a 10-pod cascade. Phase 3 expansion would add new dependencies on top of an already zero-redundancy core.

The recommendation is not to fix a failure. It is to restore the margin before the failure that the current architecture makes inevitable.
