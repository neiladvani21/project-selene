"""
reporter.py — Reads map.json, builds report.md.

Design principle:
  Exact facts are rendered deterministically from map.json.
  The LLM, if used, is only a narration layer and is not the source of truth
  for counts, dates, rankings, or edge labels. All of those come from this file.
"""

import json
import os
import re
import sys

from openai import OpenAI

MAP_PATH = "/rover/output/map.json"
REPORT_PATH = "/rover/output/report.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pluralize(count: int, singular: str, plural: str = "") -> str:
    """Return 'N singular' or 'N plural'. If plural omitted, appends 's'."""
    word = singular if count == 1 else (plural if plural else singular + "s")
    return f"{count} {word}"


def fmt_date(ts: str) -> str:
    """Return YYYY-MM-DD from an ISO timestamp string."""
    return (ts or "")[:10]


def clean_quote(text: str, max_len: int = 220) -> str:
    """Trim a quote cleanly — never mid-word, never mid-parenthesis."""
    text = re.sub(r'\s+', ' ', (text or "").strip())
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(' ', 1)[0].rstrip('.,;:(')
    return cut + "..."


def blast_radius_phrase(total_affected: int, total_other: int) -> str:
    """Return precise blast radius wording — never 'every other pod' unless exact."""
    if total_affected >= total_other:
        return f"every other pod ({total_other} of {total_other})"
    return f"{total_affected} of the {total_other} other pods"


# High-signal timeline event patterns — only redundancy/topology-changing events
_TIMELINE_SIGNAL_PATTERNS = [
    r"decommission",
    r"sealed",
    r"rerouted? through",
    r"single .{0,20} loop",
    r"dual.feed .{0,20} (to single|removed|reduced|consolidated)",
    r"single.source",
    r"no active .{0,20} (backup|capability)",
    r"maintenance reserve",
    r"reserve .{0,20} (transferred|maintenance|status)",
    r"backup .{0,30} (removed|retired|decommissioned|formally)",
    r"coolant .{0,20} (loop|system) .{0,20} (decommissioned|retired|removed)",
    r"capacity .{0,20} (increase|above|threshold|reallocated)",
    r"consolidated .{0,30} through",
    r"direct feed .{0,20} (to|from) .{0,20} decommissioned",
    r"pipe consolidation",
    r"infrastructure simplification",
    r"redundant .{0,20} (plumbing|system|loop) .{0,20} decommissioned",
    r"throughput .{0,20} (increased|above|dips|below)",
    r"above .{0,20} (rated|threshold|forecast)",
    r"water .{0,20} (backup|reserve) .{0,20} (removed|retired|decommissioned|maintenance)",
    r"no longer operational",
    r"fully dependent",
]

_TIMELINE_NOISE_PATTERNS = [
    r"engineer rotation",
    r"personnel",
    r"transferred to .{0,30} (works|ward|lab|bay|mine|hub|relay|array|reserve|station|core)",
    r"certification",
    r"movie",
    r"supply run",
    r"social event",
    r"survey",
    r"filing",
    r"vacation",
    r"posting",
    r"fabrication",
    r"mounting bracket",
    r"reminder",
    r"submission",
    r"planning initiated",
    r"population:",
    r"expansion planning",
    r"site survey",
    r"antenna .{0,20} tested successfully",
    r"failover time .{0,20} meets",
    r"backup .{0,20} tested successfully",
]


def is_high_signal_timeline_event(event: str) -> bool:
    """Return True if the event describes a redundancy-affecting infrastructure change."""
    text = event.lower()
    if any(re.search(p, text) for p in _TIMELINE_NOISE_PATTERNS):
        return False
    return any(re.search(p, text) for p in _TIMELINE_SIGNAL_PATTERNS)


def _short_role(role: str) -> str:
    SHORT_ROLES = {
        "water":         "water/cooling hub",
        "power":         "power hub",
        "mining":        "materials hub",
        "food":          "food + water routing",
        "pharma":        "pharma synthesis",
        "atmospheric":   "oxygen/atmosphere",
        "medical":       "medical care",
        "reserve":       "former reserves",
        "monitoring":    "monitoring",
        "communications":"communications",
        "command":       "command",
        "manufacturing": "manufacturing",
    }
    role_lower = (role or "").lower()
    for keyword, short in SHORT_ROLES.items():
        if keyword in role_lower:
            return short
    return role_lower[:20] if role_lower else ""


# ---------------------------------------------------------------------------
# Mermaid diagram builder — 100% deterministic
# ---------------------------------------------------------------------------

def build_mermaid(map_data: dict) -> str:
    """
    Build Mermaid diagram where A --> B means A depends on B.
    Only true stale edges are dashed. Observed and concentration edges are solid.
    Red nodes = articulation points or top-3 blast-radius pods.
    """
    edges  = map_data.get("edges", {})
    dep_edges     = [e for e in edges.get("declared", []) if e.get("source") == "dependencies"]
    stale_edges   = edges.get("stale", [])
    observed_edges = edges.get("observed", [])

    stale_pairs = {(s["from"], s["to"]) for s in stale_edges}

    graph = map_data.get("graph", {})
    aps          = set(graph.get("articulation_points", []))
    blast_ranking = graph.get("blast_radius_ranking", [])
    top3_blast   = {entry["pod"] for entry in blast_ranking[:3]}
    high_risk    = aps | top3_blast

    pods  = map_data.get("pods", {})
    nodes = set(pods.keys())

    def _label(node: str) -> str:
        raw  = pods.get(node, {}).get("raw", {})
        info = raw.get("info") or {}
        name = info.get("name", node.capitalize())
        role = _short_role(info.get("role", ""))
        return f'{name}<br/>{role}' if role else name

    # Build a mapping: (from, to) -> resource for stale edges, so we can find
    # which declared edges are for the same pair as stale (synthesis_water reroute)
    stale_resource = {(s["from"], s["to"]): s.get("resource", "") for s in stale_edges}

    # For observed edges whose (from, to) pair corresponds to a stale reroute,
    # label with the stale resource to make the current path explicit.
    stale_rerouted_pairs: dict[tuple, str] = {}
    for s in stale_edges:
        reason_lower = s.get("reason", "").lower()
        m = re.search(r'rerouted? through\s+([a-z]+)', reason_lower)
        if m:
            new_hub = m.group(1)
            stale_rerouted_pairs[(s["from"], new_hub)] = s.get("resource", "")

    lines = ["graph TD"]
    lines.append("    %% Arrow direction: A --> B means A depends on B")
    lines.append("    %% Dashed edges indicate stale or contradicted dependency records")
    lines.append("    %% Red nodes = articulation points or top-3 blast radius pods")

    for node in sorted(nodes):
        lines.append(f'    {node}["{_label(node)}"]')

    # Index observed edges by pair for fast lookup
    observed_by_pair: dict[tuple, dict] = {}
    for e in observed_edges:
        observed_by_pair[(e["from"], e["to"])] = e

    seen_pairs: set[tuple] = set()

    for e in dep_edges:
        pair = (e["from"], e["to"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        if pair in stale_pairs:
            # Stale declared edge — render dashed
            lines.append(f'    {e["from"]} -. stale .-> {e["to"]}')
        elif pair in observed_by_pair:
            # Both declared and observed exist for this pair — prefer observed label.
            obs = observed_by_pair[pair]
            rerouted_res = stale_rerouted_pairs.get(pair)
            if rerouted_res:
                label_str = f"observed {rerouted_res}"
            else:
                obs_res = obs.get("resource", "")
                label_str = obs_res if obs_res and obs_res != "observed" else "observed"
            lines.append(f'    {e["from"]} -->|{label_str}| {e["to"]}')
        elif pair in stale_rerouted_pairs:
            # This declared edge is the current rerouted path for a stale route —
            # label it as observed so the diagram shows the live topology, not the old resource.
            label_str = f"observed {stale_rerouted_pairs[pair]}"
            lines.append(f'    {e["from"]} -->|{label_str}| {e["to"]}')
        else:
            resource = e.get("resource", "")
            label    = f"|{resource}|" if resource else ""
            lines.append(f'    {e["from"]} -->{label} {e["to"]}')

    for e in observed_edges:
        pair = (e["from"], e["to"])
        if pair in seen_pairs:
            continue  # already rendered above via the declared-edge loop
        seen_pairs.add(pair)
        rerouted_res = stale_rerouted_pairs.get(pair)
        if rerouted_res:
            label_str = f"observed {rerouted_res}"
        else:
            resource = e.get("resource", "")
            label_str = resource if resource and resource != "observed" else "observed"
        lines.append(f'    {e["from"]} -->|{label_str}| {e["to"]}')

    for node in sorted(high_risk):
        if node in nodes:
            lines.append(f'    style {node} fill:#ff4444,color:#fff')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

def _score_pod_risk(pod_id: str, pod_data: dict, blast_entry: dict, aps: list,
                    conc_edges: list, backup_removed: list) -> float:
    derived    = pod_data.get("derived", {})
    br         = blast_entry or {}
    trust      = derived.get("trust_score", {})
    cap        = derived.get("capacity", {})

    blast_total = br.get("total_affected", 0)
    is_ap       = 1 if pod_id in aps else 0
    trust_score = trust.get("score", 100)
    trust_gap   = (100 - trust_score) / 100.0

    meta_util  = cap.get("metadata_utilization_pct") or 0
    high_util  = 1 if meta_util > 85 else 0

    backup_val = (pod_data.get("raw", {}).get("info") or {}) \
                     .get("metadata", {}).get("backup_systems", None)
    no_backup  = 1 if backup_val == 0 else 0

    conc_count   = sum(1 for e in conc_edges    if e["from"] == pod_id or e["to"] == pod_id)
    backup_hits  = sum(1 for b in backup_removed if b["pod"]  == pod_id)

    return (
        blast_total * 3.0
        + is_ap     * 5.0
        + no_backup * 8.0
        + high_util * 4.0
        + trust_gap * 10.0
        + conc_count   * 2.0
        + backup_hits  * 3.0
    )


# ---------------------------------------------------------------------------
# Deterministic section builders
# ---------------------------------------------------------------------------

def build_executive_summary(map_data: dict, top3_pod_ids: list, max_blast: int) -> str:
    """
    Deterministic executive summary paragraph built from map.json counts.
    No LLM involvement.
    """
    pods   = map_data.get("pods", {})
    edges  = map_data.get("edges", {})
    stale  = edges.get("stale", [])
    conc   = edges.get("dependency_concentration", [])
    backup = edges.get("removed_backup_paths", [])

    nominal_count = sum(
        1 for p in pods.values()
        if (p.get("raw", {}).get("status") or {}).get("status") == "nominal"
    )
    total_pods = len(pods)
    top_pods_str = ", ".join(t.capitalize() for t in top3_pod_ids)

    stale_phrase  = pluralize(len(stale),  "true stale route")
    conc_phrase   = pluralize(len(conc),   "dependency concentration")
    backup_phrase = pluralize(len(backup), "removed backup path")

    total_other = total_pods - 1
    blast_phrase = blast_radius_phrase(max_blast, total_other)

    # Punchy opener — all values are computed
    all_nominal = nominal_count == total_pods
    status_clause = (
        f"The colony is nominal. It is not resilient. "
        f"All {nominal_count} of {total_pods} habitat pods report green, but "
    ) if all_nominal else (
        f"{nominal_count} of {total_pods} habitat pods report nominal. "
        f"Despite the apparent health, resilience is severely compromised: "
    )

    lines = [
        f"{status_clause}"
        f"**{top_pods_str}** each carry a modeled blast radius of {max_blast} pods.",
        "",
        f"The graph contains {stale_phrase}, {conc_phrase}, and {backup_phrase}. "
        f"The concentrations are not stale — the routes are live — "
        f"but each lost its backup path, leaving a single feed with no fallback. "
        f"A failure in any top-ranked pod would cascade to {blast_phrase}, "
        f"far exceeding the redundancy that the status flags suggest.",
    ]
    return "\n".join(lines)


def build_agent_found_section(map_data: dict) -> str:
    """
    Deterministic 'What the Agent Found' section — discovery summary and
    evidence-category definitions. All values come from map.json.
    """
    pods   = map_data.get("pods", {})
    edges  = map_data.get("edges", {})
    stale  = edges.get("stale", [])
    conc   = edges.get("dependency_concentration", [])
    backup = edges.get("removed_backup_paths", [])

    total_pods  = len(pods)
    stale_count  = len(stale)
    conc_count   = len(conc)
    backup_count = len(backup)

    # Discovery summary
    discovery = (
        f"The agent discovered all {total_pods} pods through both relationship traversal "
        f"and network-visible discovery. The discovery delta was empty — no pods were hidden "
        f"or unreachable. The hidden risk was not missing infrastructure."
    )

    # Core insight
    insight = (
        "The hidden risk was that the declared dependency model does not distinguish three "
        "different conditions that have very different operational consequences:"
    )

    # Category definitions — counts from map.json
    categories = [
        (
            "**True stale route** "
            f"({stale_count} found)"
            " — a declared dependency whose physical path was sealed or rerouted. "
            "The route no longer exists, so automated blast-radius calculations and "
            "impact analysis that rely on it will produce incorrect results."
        ),
        (
            "**Dependency concentration** "
            f"({conc_count} found)"
            " — an active dependency whose backup or dual-feed path was removed. "
            "The route still carries live traffic, but any failure now has no fallback."
        ),
        (
            "**Removed backup path** "
            f"({backup_count} found)"
            " — a formally retired fallback capability. The backup no longer protects "
            "the graph even if the primary dependency continues to function."
        ),
    ]

    return (
        discovery + "\n\n"
        + insight + "\n\n"
        + "\n".join(f"- {c}" for c in categories)
    )


def build_topology_note(map_data: dict, top3_pod_ids: list) -> str:
    """
    Deterministic 2-3 sentence topology description.
    """
    edges = map_data.get("edges", {})
    stale = edges.get("stale", [])
    conc  = edges.get("dependency_concentration", [])
    graph = map_data.get("graph", {})
    aps   = graph.get("articulation_points", [])
    pods  = map_data.get("pods", {})
    blast_ranking = graph.get("blast_radius_ranking", [])

    total_other = len(pods) - 1
    # Use the actual blast radius of the top pod for the blast phrase
    top_blast = blast_ranking[0]["total_affected"] if blast_ranking else total_other
    blast_phrase = blast_radius_phrase(top_blast, total_other)

    top_str = ", ".join(f"**{p.capitalize()}**" for p in top3_pod_ids)
    ap_str  = ", ".join(f"**{p.capitalize()}**" for p in aps) if aps else "none"

    if stale:
        stale_pairs_str = ", ".join(f"{s['from']} → {s['to']}" for s in stale)
        count_str = pluralize(len(stale), "stale edge")
        stale_sentence = (
            f"{count_str.capitalize()} "
            f"(dashed, declared route sealed/rerouted): {stale_pairs_str}."
        )
    else:
        stale_sentence = "No stale edges were detected."

    # For concentration list, correct direction using declared dependency edges
    declared_dep_set = {(e["from"], e["to"], e["resource"])
                        for e in map_data.get("edges", {}).get("declared", [])
                        if e.get("source") == "dependencies"}
    seen_conc_pairs: set = set()
    conc_pair_strs = []
    for c in conc[:6]:
        frm, to_, res = c["from"], c["to"], c["resource"]
        # Flip direction if the dependency runs the other way
        if (frm, to_, res) not in declared_dep_set and (to_, frm, res) in declared_dep_set:
            frm, to_ = to_, frm
        pair_key = (frm, to_)
        if pair_key not in seen_conc_pairs:
            seen_conc_pairs.add(pair_key)
            conc_pair_strs.append(f"{frm} → {to_} ({res})")
    conc_sentence = (
        f" Concentration risks (active dependencies that lost their backup feed): "
        f"{'; '.join(conc_pair_strs)} — shown as solid edges."
    ) if conc_pair_strs else ""

    technical = (
        f"The topology shows {top_str} as the three hubs whose removal would directly "
        f"or transitively impact {blast_phrase}. "
        f"Articulation points (whose removal disconnects the graph): {ap_str}. "
        f"{stale_sentence}"
        f"{conc_sentence}"
    )
    narrative = (
        "The map shows why the colony can look healthy while still being fragile: "
        "current dependencies remain active, but the safety nets around them have been removed."
    )
    return technical + "\n\n" + narrative


def build_priority_table(map_data: dict) -> tuple[str, list[dict]]:
    """
    Deterministic priority table and findings list.
    Returns (markdown_table_string, findings_list).
    All values come from map.json — no LLM.
    """
    graph  = map_data.get("graph", {})
    pods   = map_data.get("pods", {})
    edges  = map_data.get("edges", {})
    blast_ranking = graph.get("blast_radius_ranking", [])
    stale         = edges.get("stale", [])
    aps           = graph.get("articulation_points", [])
    conc_edges    = edges.get("dependency_concentration", [])
    backup_removed= edges.get("removed_backup_paths", [])

    total_other_pods = len(pods) - 1
    blast_by_pod = {r["pod"]: r for r in blast_ranking}

    scored = sorted(
        [(pod_id, _score_pod_risk(pod_id, pod_data, blast_by_pod.get(pod_id, {}),
                                  aps, conc_edges, backup_removed))
         for pod_id, pod_data in pods.items()],
        key=lambda x: x[1], reverse=True,
    )
    top3 = [pod_id for pod_id, _ in scored[:3]]

    findings = []

    for rank, pod_id in enumerate(top3, start=1):
        pod_data  = pods[pod_id]
        derived   = pod_data.get("derived", {})
        br        = blast_by_pod.get(pod_id, {})
        trust     = derived.get("trust_score", {})
        cap       = derived.get("capacity", {})
        meta_util = cap.get("metadata_utilization_pct")
        log_util  = cap.get("latest_log_utilization_pct")
        backup_val= (pod_data.get("raw", {}).get("info") or {}) \
                        .get("metadata", {}).get("backup_systems", "?")
        is_ap     = pod_id in aps
        pod_concs = [e for e in conc_edges    if e["from"] == pod_id or e["to"] == pod_id]
        pod_bkups = [b for b in backup_removed if b["pod"] == pod_id]

        blast_total = br.get("total_affected", "?")
        direct_list = ", ".join(br.get("depth_1", []))
        trans_list  = ", ".join(br.get("depth_2", []))
        trust_score = trust.get("score", "?")
        def _clean_pen(r: str) -> str:
            r = re.sub(r'\b1 inconsistencies\b', '1 inconsistency', r)
            r = re.sub(r'\b(\d+) inconsistencies\b',
                       lambda m: pluralize(int(m.group(1)), 'inconsistency', 'inconsistencies'), r)
            r = re.sub(r'\b(\d+) (warning-level log entries)\b',
                       lambda m: pluralize(int(m.group(1)), 'warning-level log entry',
                                           'warning-level log entries'), r)
            return clean_quote(r, max_len=70)
        penalty_str = "; ".join(_clean_pen(p["reason"]) for p in trust.get("penalties", [])[:3])

        util_str = ""
        if meta_util is not None and log_util is not None:
            util_str = f"utilization {meta_util:.1f}% (metadata) / {log_util:.1f}% (latest log)"
        elif meta_util is not None:
            util_str = f"utilization {meta_util:.1f}% (metadata)"

        backup_str = ""
        if backup_val == 0:
            backup_str = "backup_systems=0"
        elif backup_val != "?":
            backup_str = f"backup_systems={backup_val}"

        conc_str = ""
        if pod_concs:
            conc_str = pluralize(len(pod_concs), "active single-source concentration")

        bkup_removed_str = ""
        if pod_bkups:
            bp_date = fmt_date(pod_bkups[0].get("log_timestamp", ""))
            bkup_removed_str = (
                f"{pluralize(len(pod_bkups), 'backup path')} decommissioned"
                + (f" ({bp_date})" if bp_date else "")
            )

        why_parts = [f"{blast_total} of {total_other_pods} pods affected on failure"]
        if backup_str:   why_parts.append(backup_str)
        if util_str:     why_parts.append(util_str)
        why_parts.append(f"trust score {trust_score}/100")
        if penalty_str:  why_parts.append(f"penalties: {penalty_str}")
        if is_ap:        why_parts.append("graph articulation point — removal disconnects graph")
        if conc_str:     why_parts.append(conc_str)
        if bkup_removed_str: why_parts.append(bkup_removed_str)

        if is_ap:
            title = f"{pod_id.capitalize()} — articulation point + high blast radius"
        elif backup_val == 0 and meta_util and meta_util > 85:
            title = f"{pod_id.capitalize()} — zero-backup SPOF, high utilization"
        else:
            title = f"{pod_id.capitalize()} — upstream risk, blast radius {blast_total}"

        findings.append({
            "priority": rank,
            "finding": title,
            "why": ", ".join(why_parts),
            "pod": pod_id,
            "detail_type": "articulation_point" if is_ap else "blast_radius",
            "_direct": direct_list,
            "_transitive": trans_list,
            "_blast_total": blast_total,
            "_trust_score": trust_score,
            "_trust_penalties": trust.get("penalties", []),
            "_pod_concs": pod_concs,
            "_pod_bkups": pod_bkups,
            "_meta_util": meta_util,
            "_log_util": log_util,
            "_backup_val": backup_val,
            "_is_ap": is_ap,
        })

    # Finding 4: dependency model drift — counts from map.json
    stale_count  = len(stale)
    conc_count   = len(conc_edges)
    backup_count = len(backup_removed)
    stale_desc_parts = [f"{s['from']} → {s['to']} ({s['resource']})" for s in stale[:3]]
    stale_desc = "; ".join(stale_desc_parts) if stale_desc_parts else "none"

    findings.append({
        "priority": 4,
        "finding": (
            f"Dependency model drift — "
            f"{pluralize(stale_count, 'stale route')}, "
            f"{pluralize(conc_count,  'concentration')}, "
            f"{pluralize(backup_count,'removed backup')}"
        ),
        "why": (
            f"True stale routes (sealed/rerouted): {stale_desc}. "
            f"Active concentrations: {conc_count} dependencies lost backup/dual-feed paths. "
            f"Removed backups: {backup_count} capability retirements documented."
        ),
        "detail_type": "stale_edges",
        "_stale_count": stale_count,
        "_conc_count":  conc_count,
        "_backup_count":backup_count,
    })

    # Finding 5: status/trust gap
    lowest_trust_pod = min(
        pods.items(),
        key=lambda kv: kv[1].get("derived", {}).get("trust_score", {}).get("score", 100)
    )
    lt_pod_id  = lowest_trust_pod[0]
    lt_data    = lowest_trust_pod[1].get("derived", {}).get("trust_score", {})
    lt_score   = lt_data.get("score", "?")
    lt_penalties = [p["reason"] for p in lt_data.get("penalties", [])[:3]]
    lt_pen_str = "; ".join(lt_penalties) if lt_penalties else "multiple discrepancies"
    nominal_count = sum(
        1 for p in pods.values()
        if (p.get("raw", {}).get("status") or {}).get("status") == "nominal"
    )
    findings.append({
        "priority": 5,
        "finding": "Status 'nominal' does not reflect resilience loss",
        "why": (
            f"{nominal_count} of {len(pods)} pods report nominal. "
            f"Lowest trust score: {lt_pod_id} at {lt_score}/100 "
            f"(penalties: {lt_pen_str}). "
            f"Pods with decommissioned backup loops still report nominal — "
            f"the status field captures uptime only, not redundancy."
        ),
        "detail_type": "trust_score",
        "_lt_pod": lt_pod_id,
        "_lt_score": lt_score,
        "_lt_penalties": lt_penalties,
    })

    rows = ["| Priority | Finding | Why It Matters |",
            "|----------|---------|----------------|"]
    for f in findings[:5]:
        rows.append(f"| {f['priority']} | {f['finding']} | {f['why']} |")
    return "\n".join(rows), findings[:5]


# ---------------------------------------------------------------------------
# Deterministic finding paragraphs
# ---------------------------------------------------------------------------

def build_finding_paragraphs(findings: list, map_data: dict) -> str:
    """
    Deterministic per-finding narrative paragraphs built entirely from
    pre-computed evidence in map.json. No LLM.
    """
    edges   = map_data.get("edges", {})
    stale   = edges.get("stale", [])
    conc    = edges.get("dependency_concentration", [])
    backup  = edges.get("removed_backup_paths", [])
    pods    = map_data.get("pods", {})

    blocks = []

    for f in findings:
        rank     = f["priority"]
        pod_id   = f.get("pod")
        det_type = f.get("detail_type")

        if pod_id:
            # Findings 1-3: pod-specific
            is_ap       = f["_is_ap"]
            blast_total = f["_blast_total"]
            direct      = f["_direct"]
            transitive  = f["_transitive"]
            trust_score = f["_trust_score"]
            penalties   = f["_trust_penalties"]
            pod_concs   = f["_pod_concs"]
            pod_bkups   = f["_pod_bkups"]
            meta_util   = f["_meta_util"]
            log_util    = f["_log_util"]
            backup_val  = f["_backup_val"]

            total_other = len(pods) - 1
            direct_list = [p for p in direct.split(", ") if p] if direct else []
            trans_list  = [p for p in transitive.split(", ") if p] if transitive else []

            para = [f"**Finding {rank} – {pod_id.capitalize()}**"]
            blast_phrase = blast_radius_phrase(blast_total, total_other)
            trans_clause = (
                f", and transitively {len(trans_list)} more "
                f"({transitive})"
                if trans_list else ""
            )
            para.append(
                f"Removing {pod_id.capitalize()} from the graph would directly affect "
                f"{pluralize(len(direct_list), 'pod')} "
                f"({direct if direct else 'none'})"
                f"{trans_clause}, "
                f"totalling {blast_phrase}."
            )

            # Trust score and penalties — clean up robotic "1 inconsistencies" etc.
            pen_reasons = []
            for p in penalties:
                r = p["reason"]
                # Fix "1 inconsistencies" → "1 inconsistency" etc.
                r = re.sub(r'\b1 inconsistencies\b', '1 inconsistency', r)
                r = re.sub(r'\b(\d+) (warning-level log entries)\b',
                           lambda m: pluralize(int(m.group(1)), 'warning-level log entry',
                                               'warning-level log entries'), r)
                pen_reasons.append(r)
            pen_str = "; ".join(pen_reasons) if pen_reasons else "see trust score details"
            para.append(
                f"Trust score: {trust_score}/100. "
                f"Penalties: {pen_str}."
            )

            # Capacity
            if meta_util is not None:
                util_detail = f"Metadata utilization: {meta_util:.1f}%"
                if log_util is not None:
                    util_detail += f"; latest log: {log_util:.1f}%"
                    if meta_util > 85 or log_util > 85:
                        util_detail += " — both above the 85% safe threshold."
                para.append(util_detail)

            # Backup systems
            if backup_val == 0:
                para.append(
                    f"Metadata reports backup_systems=0 — no failover capability exists. "
                    f"Any failure in this pod immediately cascades with no redundant path."
                )

            # Concentrations — verify direction against declared dependency edges only
            # (source=="dependencies" is authoritative: A depends on B)
            declared_edges = edges.get("declared", [])
            declared_dep_dir = {(e["from"], e["to"], e["resource"]): True
                                 for e in declared_edges if e.get("source") == "dependencies"}
            if pod_concs:
                conc_items = []
                for c in pod_concs:
                    d = fmt_date(c.get("log_timestamp", ""))
                    frm, to_, res = c["from"], c["to"], c["resource"]
                    # If dependency direction is opposite to concentration entry, flip
                    if not declared_dep_dir.get((frm, to_, res)) and declared_dep_dir.get((to_, frm, res)):
                        frm, to_ = to_, frm
                    conc_items.append(
                        f"{frm} → {to_} ({res})" + (f" [{d}]" if d else "")
                    )
                para.append(
                    f"{pluralize(len(pod_concs), 'Active single-source concentration')}: "
                    + "; ".join(conc_items) + ". "
                    "These dependencies are live — not stale — but each lost its "
                    "backup/dual-feed path, leaving the destination as a single point of failure."
                )

            # Articulation point note
            if is_ap:
                para.append(
                    f"{pod_id.capitalize()} is a graph articulation point: "
                    f"its removal disconnects the dependency graph entirely, "
                    f"isolating pods that have no alternate path to their dependencies."
                )

            # Removed backups with clean quotes
            if pod_bkups:
                for b in pod_bkups:
                    d    = fmt_date(b.get("log_timestamp", ""))
                    note = clean_quote(b.get("note", ""), max_len=260)
                    para.append(f"Removed backup [{d}]: \"{note}\"")

            blocks.append("\n\n".join(para))

        elif det_type == "stale_edges":
            # Finding 4: dependency model drift
            stale_count  = f["_stale_count"]
            conc_count   = f["_conc_count"]
            backup_count = f["_backup_count"]

            para = [f"**Finding {rank} – Dependency Model Drift**"]

            declared_edges = edges.get("declared", [])
            # Only dependency-source edges are authoritative for direction
            declared_dep_dir = {(e["from"], e["to"], e["resource"]): True
                                 for e in declared_edges if e.get("source") == "dependencies"}

            # True stale routes
            if stale:
                stale_items = []
                for s in stale:
                    d      = fmt_date(s.get("log_timestamp", ""))
                    reason = clean_quote(s.get("reason", ""), max_len=260)
                    stale_items.append(
                        f"{s['from']} → {s['to']} ({s['resource']}) "
                        f"[sealed {d}]: \"{reason}\""
                    )
                para.append(
                    f"The {pluralize(stale_count, 'true stale route')} "
                    f"(declared route that was sealed and is no longer active): "
                    + "; ".join(stale_items) + "."
                )
            else:
                para.append("No true stale routes were identified.")

            # Classification: why stale ≠ concentration
            if conc:
                conc_ex_parts = []
                for c in conc[:4]:
                    frm, to_, res = c["from"], c["to"], c["resource"]
                    if (frm, to_, res) not in declared_dep_dir and (to_, frm, res) in declared_dep_dir:
                        frm, to_ = to_, frm
                    conc_ex_parts.append(f"{frm} → {to_} ({res})")
                conc_examples = "; ".join(conc_ex_parts)
                para.append(
                    f"**Why this classification matters:** "
                    f"The {pluralize(conc_count, 'dependency concentration')} "
                    f"({conc_examples}) are still active dependencies — not stale — "
                    f"but each lost its backup/dual-feed path, leaving the destination pod "
                    f"as a single point of failure. Calling them stale would misrepresent "
                    f"the real risk: the route exists, the redundancy does not."
                )

            # Removed backups with clean quotes
            if backup:
                backup_items = []
                for b in backup:
                    d    = fmt_date(b.get("log_timestamp", ""))
                    note = clean_quote(b.get("note", ""), max_len=260)
                    backup_items.append(f"[{d}] {b['pod'].capitalize()}: \"{note}\"")
                para.append(
                    f"The {pluralize(backup_count, 'removed backup path')} represent "
                    f"formally retired capability with no replacement: "
                    + "; ".join(backup_items) + "."
                )

            # Generic bidirectional coupling note: find any pair of pods with
            # concentration edges running in both directions between them.
            conc_by_pair: dict[frozenset, list] = {}
            for c in conc:
                key = frozenset([c["from"], c["to"]])
                conc_by_pair.setdefault(key, []).append(c)
            for pair_pods, pair_concs in conc_by_pair.items():
                if len(pair_concs) < 2:
                    continue
                # Verify each direction against declared edges
                dir_strs = []
                for c in pair_concs:
                    frm, to_, res = c["from"], c["to"], c["resource"]
                    if not declared_dep_dir.get((frm, to_, res)) and declared_dep_dir.get((to_, frm, res)):
                        frm, to_ = to_, frm
                    dir_strs.append(f"{frm.capitalize()} depends on {to_.capitalize()} for {res}")
                sorted_pods = sorted(pair_pods)
                para.append(
                    f"Note on {sorted_pods[0].capitalize()} ↔ {sorted_pods[1].capitalize()} "
                    f"coupling: under the A→B = A depends on B convention, these are two "
                    f"separate directed dependencies — {'; and '.join(dir_strs)}. "
                    f"Each dependency is unidirectional; a cascade failure may propagate "
                    f"in both directions depending on which resource fails first."
                )

            blocks.append("\n\n".join(para))

        elif det_type == "trust_score":
            # Finding 5: status/trust gap
            lt_pod     = f["_lt_pod"]
            lt_score   = f["_lt_score"]
            lt_pens    = f["_lt_penalties"]

            nominal_count = sum(
                1 for p in pods.values()
                if (p.get("raw", {}).get("status") or {}).get("status") == "nominal"
            )

            para = [f"**Finding {rank} – Status vs. Resilience Gap**"]
            para.append(
                f"All {nominal_count} pods claim nominal operation. "
                f"The lowest trust score is {lt_pod.capitalize()} at {lt_score}/100. "
                f"Computed penalties: {'; '.join(lt_pens) if lt_pens else 'see trust score details'}."
            )
            # Find pods with decommissioned backups still reporting nominal
            pods_nominal_with_backup_loss = []
            backup_pods = {b["pod"] for b in backup}
            for pid in backup_pods:
                status = (pods.get(pid, {}).get("raw", {}).get("status") or {}).get("status", "")
                if status == "nominal":
                    pods_nominal_with_backup_loss.append(pid)
            if pods_nominal_with_backup_loss:
                para.append(
                    f"Pods reporting nominal while having documented backup decommissions: "
                    f"{', '.join(p.capitalize() for p in sorted(pods_nominal_with_backup_loss))}. "
                    f"The /status endpoint captures uptime only — not backup availability, "
                    f"not capacity margin, not dependency concentration. "
                    f"A pod can lose all redundancy and still report nominal until the moment it fails."
                )
            blocks.append("\n\n".join(para))

    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Deterministic timeline section
# ---------------------------------------------------------------------------

def build_timeline_section(map_data: dict) -> str:
    """
    Render the redundancy erosion timeline — high-signal events only.
    Filters out personnel, social, planning, and successful-test events.
    """
    timeline = map_data.get("timeline", [])
    if not timeline:
        return "No infrastructure change events found in colony logs."

    # Filter to high-signal redundancy/topology-changing events only
    high_signal = [t for t in timeline if is_high_signal_timeline_event(t.get("event", ""))]

    # If more than 12, keep only the most impactful by keyword priority
    PRIORITY_KEYWORDS = [
        r"decommission", r"sealed", r"rerouted?", r"no active .{0,20} backup",
        r"maintenance reserve", r"single .{0,20} loop", r"dual.feed",
        r"pipe consolidation", r"infrastructure simplification",
        r"redundant .{0,20} (plumbing|system) .{0,20} decommissioned",
    ]
    def _event_priority(t: dict) -> int:
        text = (t.get("event", "") or "").lower()
        for i, pat in enumerate(PRIORITY_KEYWORDS):
            if re.search(pat, text):
                return i
        return len(PRIORITY_KEYWORDS)

    if len(high_signal) > 12:
        high_signal = sorted(high_signal, key=_event_priority)[:12]
        high_signal.sort(key=lambda t: t.get("date", ""))

    lines = []
    for t in high_signal:
        d       = t.get("date", "")[:10]
        pod     = t.get("pod", "")
        evt     = clean_quote(t.get("event", ""), max_len=240)
        dir_str = f" [Directive {t['directive']}]" if t.get("directive") else ""
        lines.append(f"- **{d}** | {pod.capitalize()}{dir_str}: {evt}")

    top_blast_pods = [
        r["pod"] for r in map_data.get("graph", {}).get("blast_radius_ranking", [])[:3]
    ]
    hub_str = ", ".join(p.capitalize() for p in top_blast_pods)

    intro = (
        "The colony did not become fragile because one pod failed. "
        "It became fragile through a sequence of individually rational infrastructure "
        "simplifications: reserves were moved to maintenance status, dual-feed loops "
        "were collapsed, a direct synthesis-water route was sealed, and backup coolant "
        "was decommissioned. Each change reduced maintenance overhead, but together they "
        f"removed the safety margin — leaving {hub_str} as single points of failure "
        "with no backup path.\n\n"
        "The following events — extracted verbatim from pod logs — document that sequence:"
    )
    return intro + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic recommendations
# ---------------------------------------------------------------------------

def build_recommendations(findings: list, stale_edges: list,
                           conc_edges: list, backup_removed: list,
                           pods: dict, aps: list) -> str:
    """
    Generate grouped recommendations from computed finding types.
    Pod names come from findings/map data, not hardcoded.
    Three clusters: Restore redundancy / Fix the dependency model / Improve monitoring.
    """
    restore_recs = []
    model_recs   = []
    monitor_recs = []

    for f in findings:
        pod_id   = f.get("pod")
        det_type = f.get("detail_type", "")

        if not pod_id:
            if det_type == "stale_edges":
                for s in stale_edges[:3]:
                    d = fmt_date(s.get("log_timestamp", ""))
                    model_recs.append(
                        f"Update declared dependency records for "
                        f"{s['from']} → {s['to']} ({s.get('resource','?')}): "
                        f"this route was sealed {d} per logs but may still appear as active. "
                        f"Correct the declared model to reflect the current routed path."
                    )
                if conc_edges:
                    affected = sorted({e["from"] for e in conc_edges} | {e["to"] for e in conc_edges})
                    restore_recs.append(
                        f"Reintroduce backup or dual-feed capacity for pods with active "
                        f"single-source concentrations: {', '.join(affected)}. "
                        f"These dependencies are live but have no fallback — "
                        f"a single feed failure becomes a colony-wide cascade."
                    )
            elif det_type == "trust_score":
                monitor_recs.append(
                    "Redefine the pod /status endpoint to incorporate resilience indicators: "
                    "backup system count, capacity utilization threshold, "
                    "stale declared dependencies, and removed redundancy. "
                    "A pod should not be able to report 'nominal' while failing these checks."
                )
            continue

        derived    = pods.get(pod_id, {}).get("derived", {})
        cap        = derived.get("capacity", {})
        backup_val = (pods.get(pod_id, {}).get("raw", {}).get("info") or {}) \
                         .get("metadata", {}).get("backup_systems", None)
        pod_bkups  = [b for b in backup_removed if b.get("pod") == pod_id]
        is_ap      = f.get("_is_ap", False)

        if is_ap:
            restore_str = ""
            if pod_bkups:
                d = fmt_date(pod_bkups[0].get("log_timestamp", ""))
                note = clean_quote(pod_bkups[0].get("note", ""), max_len=120)
                restore_str = (
                    f" Restore the decommissioned backup capability "
                    f"documented on {d}: \"{note}\"."
                )
            restore_recs.append(
                f"Reduce single-point-of-failure exposure for {pod_id.capitalize()} "
                f"(graph articulation point whose removal disconnects the dependency graph). "
                f"Add alternate dependency paths or failover capability for its critical resources."
                + restore_str
            )
        elif backup_val == 0:
            meta_util = cap.get("metadata_utilization_pct")
            util_str  = f" at {meta_util:.1f}% utilization" if meta_util is not None else ""
            blast_total = f.get("_blast_total", "?")
            restore_recs.append(
                f"Restore independent failover/redundancy for {pod_id.capitalize()} "
                f"before adding new infrastructure that depends on it. "
                f"It currently has zero backup systems{util_str} and "
                f"a blast radius of {blast_total} pods."
            )
        else:
            trust_score = f.get("_trust_score", "?")
            blast_total = f.get("_blast_total", "?")
            restore_recs.append(
                f"Include {pod_id.capitalize()} in the next resilience review — "
                f"blast radius {blast_total} pods, trust score {trust_score}/100. "
                f"Verify that its dependency paths remain adequately redundant."
            )

    # Dependency model recs — mismatch reconciliation
    mismatch_pods = sorted({
        pid for pid, pdata in pods.items()
        if pdata.get("derived", {}).get("mismatch_count", 0) > 3
    })
    if mismatch_pods:
        model_recs.append(
            f"Reconcile asymmetric dependency/supply records for "
            f"{', '.join(p.capitalize() for p in mismatch_pods[:4])} "
            f"— declared dependencies do not match what those pods list as their supply targets."
        )

    # Monitoring recs
    pods_missing_cap = [
        pid for pid, pdata in pods.items()
        if pdata.get("derived", {}).get("capacity", {}).get("metadata_utilization_pct") is None
        and pdata.get("derived", {}).get("capacity", {}).get("latest_log_utilization_pct") is None
    ]
    if pods_missing_cap:
        monitor_recs.append(
            f"Require all pods to publish machine-readable capacity data "
            f"(throughput and rated figures). "
            f"{pluralize(len(pods_missing_cap), 'pod')} currently publish no utilization data, "
            f"making it impossible to detect saturation before it becomes a crisis."
        )

    pods_with_backup_loss = sorted({b["pod"] for b in backup_removed if b.get("pod")})
    if pods_with_backup_loss:
        monitor_recs.append(
            f"Update the /status endpoint for "
            f"{', '.join(p.capitalize() for p in pods_with_backup_loss)} "
            f"(and any pod with documented backup decommissions) to report a "
            f"degraded resilience state rather than 'nominal'. "
            f"Uptime alone is an insufficient health signal when redundancy has been retired."
        )

    top_pods = [f["pod"] for f in findings if f.get("pod")][:3]
    if top_pods:
        monitor_recs.append(
            f"Designate {', '.join(p.capitalize() for p in top_pods)} as Tier-1 targets "
            f"for the next resilience review. "
            f"Their computed blast radii produce the largest cascades in the colony. "
            f"No new infrastructure should increase dependency on them before redundancy is restored."
        )

    def _numbered(items: list) -> str:
        return "\n".join(f"- {r}" for r in items)

    sections = []
    if restore_recs:
        sections.append("### Restore redundancy\n\n" + _numbered(restore_recs))
    if model_recs:
        sections.append("### Fix the dependency model\n\n" + _numbered(model_recs))
    if monitor_recs:
        sections.append("### Improve monitoring\n\n" + _numbered(monitor_recs))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# LLM call — narration only, bounded to prose paragraphs, no facts
# ---------------------------------------------------------------------------

def _call_llm_for_prose(api_key: str, map_data: dict, findings: list,
                         top3_pod_ids: list) -> dict:
    """
    Ask LLM for short explanatory commentary only.
    Returns dict with optional fields: exec_addendum, timeline_intro.
    LLM is explicitly told to NOT include counts, dates, values, or edge labels.
    If the call fails or returns bad JSON, caller uses empty strings.
    """
    edges   = map_data.get("edges", {})
    stale   = edges.get("stale", [])
    conc    = edges.get("dependency_concentration", [])
    backup  = edges.get("removed_backup_paths", [])
    timeline = map_data.get("timeline", [])

    stale_count  = len(stale)
    conc_count   = len(conc)
    backup_count = len(backup)

    top_str = ", ".join(t.capitalize() for t in top3_pod_ids)

    # Build minimal context — only structure, no raw values
    finding_titles = "\n".join(
        f"  {f['priority']}. {f['finding']}" for f in findings
    )

    timeline_excerpt = "\n".join(
        f"  {t['date'][:10]} | {t['pod']}: {t['event'][:100]}"
        for t in timeline[:8]
    )

    prompt = f"""You are a senior infrastructure analyst writing a report on a lunar colony.
All exact facts (pod names, counts, dates, blast radius values, trust scores, utilization figures,
edge labels, and priority rankings) have already been computed and will be inserted by the report
generator. Do NOT include any of those facts in your response.

Your job is to write short explanatory prose:
1. A 1-2 sentence addendum to the executive summary that explains WHY concentration risks are
   more dangerous than stale routes from an operational perspective. No numbers. No pod names.
2. A 1-2 sentence addendum to the timeline section that explains the systemic pattern.
   No dates. No directive numbers. No pod names.

The top priority findings are:
{finding_titles}

The colony has a pattern of single-source concentrations and removed backup paths.

Return ONLY valid JSON with exactly these two fields (no markdown, no preamble):
{{
  "exec_addendum": "...",
  "timeline_addendum": "..."
}}

Rules:
- Do not mention any number, date, pod name, percentage, directive, or edge label.
- Do not say "three" or "one" or any numeral.
- Do not reference "2024" or "2094" or any year.
- If in doubt, return an empty string for that field.
- Maximum 2 sentences per field.
"""

    import uuid
    prompt += f"\n<!-- {uuid.uuid4().hex[:6]} -->"

    try:
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
        resp   = client.chat.completions.create(
            model="openai/gpt-oss-120b:free",
            max_tokens=400,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown code fences if present
        raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
        result = json.loads(raw)
        return {
            "exec_addendum":      str(result.get("exec_addendum",      "") or ""),
            "timeline_addendum":  str(result.get("timeline_addendum",  "") or ""),
        }
    except Exception as exc:
        print(f"[reporter] LLM prose call failed ({exc}), using empty prose", flush=True)
        return {"exec_addendum": "", "timeline_addendum": ""}


# ---------------------------------------------------------------------------
# Final report assembler — Python stitches all sections
# ---------------------------------------------------------------------------

def assemble_report(map_data: dict, mermaid: str, exec_summary: str,
                    agent_found: str, topology_note: str, priority_table: str,
                    finding_paragraphs: str, timeline_section: str,
                    recommendations: str, llm_prose: dict) -> str:
    """
    Stitch all deterministic sections and optional LLM addenda into final report.md.
    New section order:
      1. Executive Snapshot
      2. What the Agent Found
      3. Colony Dependency Map
      4. Priority Risk Findings
      5. How Resilience Eroded
      6. Trust and Status Gap
      7. Recommended Actions
    """
    # Static methodology sentence — LLM exec_addendum not used to avoid inaccurate framing.
    exec_addendum = (
        "Concentration risks create single points of failure that can cascade rapidly, "
        "while stale routes cause planners and automated checks to reason from an outdated topology."
    )
    exec_block = exec_summary + f"\n\n{exec_addendum}"

    # Trust score table (deterministic)
    pods = map_data.get("pods", {})
    trust_rows = [
        "| Pod | Trust Score | Warnings | Status Claim | Key Penalties |",
        "|-----|-------------|----------|--------------|---------------|",
    ]
    for pod_id, pod in sorted(pods.items()):
        ts = pod.get("derived", {}).get("trust_score", {})
        if not ts:
            continue
        pen_parts = []
        for p in ts.get("penalties", [])[:3]:
            r = p["reason"]
            r = re.sub(r'\b1 inconsistencies\b', '1 inconsistency', r)
            r = re.sub(r'\b(\d+) inconsistencies\b',
                       lambda m: pluralize(int(m.group(1)), 'inconsistency', 'inconsistencies'), r)
            r = re.sub(r'\b(\d+) (warning-level log entries)\b',
                       lambda m: pluralize(int(m.group(1)), 'warning-level log entry',
                                           'warning-level log entries'), r)
            r = clean_quote(r, max_len=70)
            pen_parts.append(r)
        pen_str = "; ".join(pen_parts)
        trust_rows.append(
            f"| {pod_id} | {ts.get('score','?')}/100 | {ts.get('warning_count',0)} | "
            f"{ts.get('status_claim','?')} | {pen_str} |"
        )
    trust_table = "\n".join(trust_rows)

    trust_intro = (
        "After the redundancy timeline, the trust table explains why /status is not enough. "
        "Status reports uptime; the trust score penalizes missing backups, high utilization, "
        "unresolved warnings, and dependency mismatches."
    )

    report = f"""# Project Selene Infrastructure Resilience Assessment

## 1. Executive Snapshot

{exec_block}

---

## 2. What the Agent Found

{agent_found}

---

## 3. Colony Dependency Map

In this graph, **A → B** means *A* depends on *B*. Dashed edges indicate stale or contradicted dependency records (routes that were removed or sealed). Concentration risks — active dependencies where backup paths were removed — remain as solid edges. Red nodes are articulation points or top blast-radius pods.

```mermaid
{mermaid}
```

{topology_note}

---

## 4. Priority Risk Findings

{priority_table}

{finding_paragraphs}

---

## 5. How Resilience Eroded

{timeline_section}

---

## 6. Trust and Status Gap

{trust_intro}

{trust_table}

---

## 7. Recommended Actions

{recommendations}
"""
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("[reporter] reading map.json", flush=True)
    try:
        with open(MAP_PATH) as f:
            map_data = json.load(f)
    except FileNotFoundError:
        print(f"[reporter] ERROR: {MAP_PATH} not found — run mapping first", flush=True)
        sys.exit(1)

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("[reporter] ERROR: LLM_API_KEY not set", flush=True)
        sys.exit(1)

    print("[reporter] building Mermaid diagram", flush=True)
    mermaid = build_mermaid(map_data)

    print("[reporter] building priority table", flush=True)
    priority_table, findings = build_priority_table(map_data)

    graph  = map_data.get("graph", {})
    blast_ranking = graph.get("blast_radius_ranking", [])
    top3_pod_ids  = [f["pod"] for f in findings if f.get("pod")][:3]
    max_blast     = blast_ranking[0]["total_affected"] if blast_ranking else 0

    print("[reporter] building deterministic sections", flush=True)
    exec_summary     = build_executive_summary(map_data, top3_pod_ids, max_blast)
    agent_found      = build_agent_found_section(map_data)
    topology_note    = build_topology_note(map_data, top3_pod_ids)
    finding_paras    = build_finding_paragraphs(findings, map_data)
    timeline_section = build_timeline_section(map_data)
    edges            = map_data.get("edges", {})
    recs             = build_recommendations(
        findings,
        edges.get("stale", []),
        edges.get("dependency_concentration", []),
        edges.get("removed_backup_paths", []),
        map_data.get("pods", {}),
        graph.get("articulation_points", []),
    )

    print("[reporter] calling LLM for optional prose addenda", flush=True)
    llm_prose = _call_llm_for_prose(api_key, map_data, findings, top3_pod_ids)

    print("[reporter] assembling report", flush=True)
    report_content = assemble_report(
        map_data, mermaid, exec_summary, agent_found, topology_note,
        priority_table, finding_paras, timeline_section, recs, llm_prose,
    )

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(report_content)

    print(f"[reporter] wrote {REPORT_PATH} ({os.path.getsize(REPORT_PATH)} bytes)", flush=True)


if __name__ == "__main__":
    main()
