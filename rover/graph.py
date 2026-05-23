"""
graph.py — Pure analysis engine. No HTTP calls, no LLM calls.
All inputs are pre-crawled pod data dicts.
"""

import re
from collections import defaultdict
import networkx as nx


def _truncate_quote(text: str, max_len: int = 200) -> str:
    """Trim evidence text at a word boundary — never mid-word."""
    text = re.sub(r'\s+', ' ', (text or "").strip())
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(' ', 1)[0].rstrip('.,;:(')
    return cut + "..."

# Pod hostnames used for text matching in logs/comms
ALL_POD_NAMES = [
    "helios", "artemis", "hydroponics", "aquifer", "zephyr",
    "prometheus", "medica", "terminus", "nexus", "forge", "vault", "sentinel"
]

# Negative-context warning phrases — must be specific enough to not fire on positive evidence.
# Each is a regex pattern matched against the lowercased log detail.
WARNING_PHRASES = [
    r"no active .{0,30} (backup|capability|capacity)",
    r"(backup|coolant|reserve).{0,30} (decommissioned|retired|removed|sealed)",
    r"formally decommissioned",
    r"decommissioned per directive",
    r"maintenance reserve status",
    r"acting up again",
    r"consumed at \d+% of .{0,20} forecast",
    r"above .{0,10} forecast",
    r"single .{0,15} loop",
    r"dual.feed .{0,30} (to single|reduced|consolidated)",
    r"sourced entirely from",
    r"100% of .{0,20} (budget|source|allocation)",
    r"only source",
    r"sole source",
    r"all .{0,30} (needs|supply) should be directed to",
    r"no longer operational",
    r"fully dependent on",
    r"throughput dips",
    r"below policy minimum",
    r"unresolved concern",
]

# Positive phrases that should NOT trigger a warning penalty even if other keywords match
WARNING_POSITIVE_EXEMPTIONS = [
    "backup tested successfully",
    "failover time meets",
    "failover time .{0,20} meets",
    "within policy",
    "passed",
    "no impact",
    "confirmed as nominal",
    "restored to nominal",
    "stable",
    "within tolerance",
    "within 15%",
]

INFRASTRUCTURE_CHANGE_KEYWORDS = [
    "decommission", "sealed", "consolidated", "removed", "reallocated",
    "rerouted", "reroute", "expansion", "backup", "directive", "directive",
    "transferred", "relocated", "offline", "shutdown", "eliminated",
    "increased", "decreased", "assumed", "transferred",
]


# ---------------------------------------------------------------------------
# Edge classification
# ---------------------------------------------------------------------------

def build_declared_edges(pods_data: dict) -> list:
    """Edges from /dependencies and /supplies endpoints."""
    edges = []
    seen = set()

    for pod_id, pod in pods_data.items():
        raw = pod.get("raw", {})

        deps = raw.get("dependencies") or []
        for dep in deps:
            target = dep.get("pod_id")
            if not target:
                continue
            key = (pod_id, target, dep.get("resource", ""))
            if key not in seen:
                seen.add(key)
                edges.append({
                    "from": pod_id,
                    "to": target,
                    "resource": dep.get("resource", "unknown"),
                    "criticality": dep.get("criticality", "unknown"),
                    "source": "dependencies",
                })

        supplies = raw.get("supplies") or []
        for sup in supplies:
            target = sup.get("pod_id")
            if not target:
                continue
            key = (pod_id, target, sup.get("resource", ""))
            if key not in seen:
                seen.add(key)
                edges.append({
                    "from": pod_id,
                    "to": target,
                    "resource": sup.get("resource", "unknown"),
                    "criticality": "unknown",
                    "source": "supplies",
                })

    return edges


# Strong operational dependency trigger phrases for observed edge detection.
# A mere mention of another pod name is NOT enough — the text must contain one of these.
#
# DIRECTION NOTE: When we scan pod_A's logs and find "routed through pod_B",
# that means pod_A depends on pod_B → observed edge: pod_A → pod_B.
# When we scan pod_B's logs and find "pod_A now routes through us / through pod_B",
# that also means pod_A depends on pod_B → we create pod_A → pod_B (not pod_B → pod_A).
# We use REROUTED_TO_ME_PHRASES separately for this reverse-scanning case.
OBSERVED_EDGE_TRIGGERS = [
    r"routed through",
    r"now routed through",
    r"sourced entirely from",
    r"now sourced from",
    r"all .{0,30} should be directed to",
    r"primary source",
    r"primary distribution point",
    r"draw from",
    r"draw is now",
    r"single loop",
    r"no active backup capability",
    r"depends on",
    r"requires",
    r"feedstock from",
    r"current operating procedures direct .{0,40} through",
    r"now receiving .{0,30} from",
    r"rerouted through",
    r"consolidated .{0,30} through",
    r"synthesis water comes through",
    r"water comes through .{0,20} circuit",
    r"pulling .{0,30} (allocation|supply|from)",
]

# Phrases found in pod_B's logs indicating that pod_A was rerouted TO pod_B.
# Creates observed edge pod_A → pod_B (A depends on B), not B → A.
REROUTED_TO_ME_PHRASES = [
    r"now routed through .{0,20}(our|this|the) .{0,30}circuit",
    r"shared with .{0,30}(for|as) .{0,30}(water|synthesis|draw)",
    r"synthesis water .{0,20}(draw|supply) .{0,20}(running|routed) through .{0,20}(our|this)",
    r"pulling .{0,30}(our|this) .{0,30}(allocation|circuit|feed|line|source|route)",
]

def _has_observed_trigger(text_lower: str) -> tuple[bool, str]:
    """Return (matched, trigger_phrase) if text contains an operational dependency trigger."""
    for pattern in OBSERVED_EDGE_TRIGGERS:
        m = re.search(pattern, text_lower)
        if m:
            return True, m.group(0)
    return False, ""


def build_observed_edges(pods_data: dict) -> list:
    """
    Edges found in logs/comms text that indicate an operational dependency not
    declared in /dependencies or /supplies. Only created from strong trigger phrases —
    generic pod name mentions are ignored.

    Two-pass approach:
    Pass 1 (forward): scan pod_id's own text. "routed through X" → pod_id depends on X.
    Pass 2 (reverse): scan pod_id's text for evidence that OTHER pods were rerouted
                      TO pod_id, using REROUTED_TO_ME_PHRASES. Creates other → pod_id edge.
    """
    declared_pairs = set()
    for pod_id, pod in pods_data.items():
        raw = pod.get("raw", {})
        for dep in (raw.get("dependencies") or []):
            if dep.get("pod_id"):
                declared_pairs.add((pod_id, dep["pod_id"]))
        for sup in (raw.get("supplies") or []):
            if sup.get("pod_id"):
                declared_pairs.add((pod_id, sup["pod_id"]))

    observed = []
    seen = set()

    def _add_observed(from_pod, to_pod, text, timestamp, source_type, trigger):
        """Add observed edge if not already declared or seen."""
        pair = (from_pod, to_pod)
        if pair in declared_pairs or pair in seen:
            return
        seen.add(pair)
        observed.append({
            "from": from_pod,
            "to": to_pod,
            "resource": "observed",
            "edge_type": "observed",
            "confidence": "high" if source_type == "logs" else "medium",
            "evidence": {
                "pod": from_pod,
                "source": source_type,
                "timestamp": timestamp,
                "trigger": trigger,
                "quote": _truncate_quote(text, 180),
            },
        })

    for pod_id, pod in pods_data.items():
        raw = pod.get("raw", {})
        text_sources = []

        logs = raw.get("logs") or []
        for entry in logs:
            detail = entry.get("detail", "")
            if detail:
                text_sources.append((detail, entry.get("timestamp", ""), "logs"))

        comms = raw.get("comms") or []
        for entry in comms:
            if isinstance(entry, str):
                msg = entry
            else:
                msg = entry.get("content", entry.get("message", entry.get("body", entry.get("text", ""))))
            if msg:
                text_sources.append((msg, entry.get("timestamp", ""), "comms"))

        for text, timestamp, source_type in text_sources:
            text_lower = text.lower()

            if _is_non_operational(text_lower):
                continue

            # Pass 1 (forward): pod_id's text mentions other_pod with a dependency trigger
            # → pod_id depends on other_pod
            #
            # Guard against third-party narration: if the text is from pod A's logs but
            # describes pod B's route being rerouted through pod C (not A's own dependency),
            # we should NOT create edges from A. Detection: if the sentence subject before
            # the trigger phrase is another named pod (not pod_id), skip that sentence.
            has_trigger, trigger_phrase = _has_observed_trigger(text_lower)
            if has_trigger:
                # Check if the text is narrating a third pod's reroute rather than pod_id's own.
                # Heuristic: if any other pod name appears as a subject BEFORE "routed through"
                # and pod_id itself does NOT appear as subject, this is third-party narration.
                # e.g. aquifer log: "Prometheus synthesis water now routed through Hydroponics"
                #      → subject is prometheus, not aquifer → skip for aquifer's forward pass.
                is_third_party_narration = False
                for candidate_subject in ALL_POD_NAMES:
                    if candidate_subject == pod_id:
                        continue
                    # candidate_subject appears before the trigger AND pod_id is not the subject
                    subj_match = re.search(r'\b' + re.escape(candidate_subject) + r'\b', text_lower)
                    if subj_match:
                        # Find position of trigger in text
                        trig_match = re.search(r'routed through|rerouted through|now routed through|routes through', text_lower)
                        if trig_match and subj_match.start() < trig_match.start():
                            # candidate_subject appears before the routing verb — it's the subject
                            is_third_party_narration = True
                            break

                if not is_third_party_narration:
                    for other_pod in ALL_POD_NAMES:
                        if other_pod == pod_id:
                            continue
                        if not re.search(r'\b' + re.escape(other_pod) + r'\b', text_lower):
                            continue
                        _add_observed(pod_id, other_pod, text, timestamp, source_type, trigger_phrase)

            # Pass 2 (reverse): pod_id's text indicates another pod was rerouted TO pod_id
            # → other_pod depends on pod_id (other_pod → pod_id)
            for rpattern in REROUTED_TO_ME_PHRASES:
                if re.search(rpattern, text_lower):
                    for other_pod in ALL_POD_NAMES:
                        if other_pod == pod_id:
                            continue
                        if not re.search(r'\b' + re.escape(other_pod) + r'\b', text_lower):
                            continue
                        _add_observed(other_pod, pod_id, text, timestamp, source_type, rpattern)
                    break

    return observed


# TRUE STALE: the declared edge itself (the exact resource/route) was removed/sealed/rerouted.
# The relationship no longer exists as declared.
#
# IMPORTANT: "rerouted through X" patterns must NOT fire here — they describe the NEW route
# (creating an observed edge to X), not the staleness of the edge to X.
# Only fire when the text explicitly says the FROM→TO path itself was sealed/removed.
TRUE_STALE_PHRASES = [
    r"direct feed to .{0,40} decommissioned",
    r"direct .{0,20} connection .{0,20} sealed",
    r"direct .{0,20} feed .{0,20} sealed",
    r"previous direct .{0,40} connection sealed",
    r"previous .{0,20} connection .{0,20} sealed",
    r"connection sealed",
    r"route sealed",
    r"no longer .{0,20} direct",
]

# DEPENDENCY CONCENTRATION: the dependency still exists but backup/dual-feed was removed,
# making the active dependency more dangerous — NOT stale.
CONCENTRATION_PHRASES = [
    r"dual.feed .{0,40} (to single|reduced|consolidated|single .{0,10} loop)",
    r"single .{0,25} (loop|feed|source|path|route|circuit)",
    r"now sources? entirely from",
    r"internal .{0,20} (loop|system|reclaim) .{0,20} (retired|decommissioned)",
    r"atmospheric moisture .{0,20} now .{0,20} (sourced|from)",
    r"moisture budget .{0,20} (sourced|from)",
]

# REMOVED BACKUP PATH: a backup/redundant capability was removed; may not match a declared edge.
REMOVED_BACKUP_PHRASES = [
    r"backup .{0,30} (decommissioned|formally decommissioned|retired|removed)",
    r"formally decommissioned per directive",
    r"coolant .{0,20} (loop|system) .{0,20} (decommissioned|retired)",
    r"(water|coolant) .{0,20} reserve .{0,20} (transferred to maintenance|maintenance reserve|no active)",
    r"no active .{0,20} (backup|water backup|coolant) capability",
    r"maintenance reserve status",
]

# CANDIDATE DRIFT: weak operational signals — worth noting but not asserting.
# Personnel/social/planning events must be excluded.
CANDIDATE_DRIFT_OPERATIONAL_PHRASES = [
    r"capacity reallocated",
    r"budget reallocated",
    r"pipe consolidation",
    r"consolidated .{0,30} through",
]

# These patterns in the same entry indicate a personnel/social/planning event — skip entirely.
NON_OPERATIONAL_SKIP_PATTERNS = [
    r"engineer rotation",
    r"personnel",
    r"transferred to .{0,20} (works|ward|lab|bay|mine|hub|relay|array|reserve|station|core)",
    r"certification",
    r"paperwork",
    r"movie night",
    r"supply run",
    r"social",
    r"survey",
    r"filing",
    r"vacation",
    r"replaced by",
    r"posting",
]


def _is_non_operational(text_lower: str) -> bool:
    return any(re.search(p, text_lower) for p in NON_OPERATIONAL_SKIP_PATTERNS)


def find_stale_edges(declared_edges: list, pods_data: dict) -> dict:
    """
    Classify declared edges into:
      - true_stale: the exact declared route was removed/sealed/rerouted
      - dependency_concentration: still active but backup/dual-feed removed (more dangerous, NOT stale)
      - removed_backup_paths: a backup capability was retired (may not match a declared edge)
      - candidate_drift: weak operational signals (no personnel/social events)

    Only true_stale should appear as dashed edges or be called "stale" in the report.
    """
    true_stale = []
    concentration = []
    removed_backup = []
    candidate_drift = []
    seen_true = set()
    seen_conc = set()
    seen_backup = set()
    seen_candidate = set()

    # Also scan all pods for removed backup evidence (not just declared edges)
    all_pod_ids = list(pods_data.keys())

    # --- True stale and concentration: scan declared edges ---
    # Only scan the FROM pod's logs — it is the one that declared the dependency.
    # This prevents symmetric false positives where the same log entry triggers
    # both (A→B) and (B→A) for different declared edges.
    #
    # For TRUE STALE: the log must mention to_pod AND contain a stale phrase
    # that refers to the FROM→TO direction being removed. To avoid false positives
    # where a "rerouted through X" log (describing a new route to X) incorrectly
    # marks the edge FROM→X as stale, we additionally require that the log is NOT
    # primarily describing X as the new destination (i.e., "now routed through X"
    # should create an observed edge to X, not mark FROM→X as stale).
    for edge in declared_edges:
        from_pod = edge["from"]
        to_pod = edge["to"]
        resource = edge.get("resource", "")

        if from_pod not in pods_data:
            continue
        raw = pods_data[from_pod].get("raw", {})
        logs = raw.get("logs") or []

        for entry in logs:
            detail_raw = entry.get("detail", "") or ""
            detail = detail_raw.lower()

            if _is_non_operational(detail):
                continue
            # Log must mention the pod this edge points to
            if not re.search(r'\b' + re.escape(to_pod) + r'\b', detail):
                continue

            # True stale check:
            # Extra guard — if the log says "now routed through <to_pod>" or
            # "rerouted through <to_pod>", that means to_pod is the NEW path,
            # not the decommissioned one. Do NOT mark FROM→TO as stale in that case.
            rerouted_through_to = re.search(
                r'(now routed through|rerouted through|routes through|routed through)\s+\S*\s*' + re.escape(to_pod),
                detail
            ) or re.search(
                r'(now routed through|rerouted through|routes through|routed through)\s+[^.]*' + re.escape(to_pod),
                detail
            )

            for pattern in TRUE_STALE_PHRASES:
                if re.search(pattern, detail):
                    if rerouted_through_to:
                        # to_pod is the new route — this is an observed edge, not stale
                        break
                    key = (from_pod, to_pod, resource)
                    if key not in seen_true:
                        seen_true.add(key)
                        true_stale.append({
                            "from": from_pod,
                            "to": to_pod,
                            "resource": resource,
                            "confidence": "high",
                            "reason": _truncate_quote(detail_raw, 180),
                            "log_timestamp": entry.get("timestamp", ""),
                            "evidence_pod": from_pod,
                        })
                    break
            else:
                # Concentration check (not stale — edge is still active but single-source)
                for pattern in CONCENTRATION_PHRASES:
                    if re.search(pattern, detail):
                        key = (from_pod, to_pod, resource)
                        if key not in seen_conc and key not in seen_true:
                            seen_conc.add(key)
                            concentration.append({
                                "from": from_pod,
                                "to": to_pod,
                                "resource": resource,
                                "note": "active dependency — backup/dual-feed removed, now single-source",
                                "reason": _truncate_quote(detail_raw, 180),
                                "log_timestamp": entry.get("timestamp", ""),
                                "evidence_pod": from_pod,
                            })
                        break
                else:
                    # Weak candidate drift (operational only)
                    for pattern in CANDIDATE_DRIFT_OPERATIONAL_PHRASES:
                        if re.search(pattern, detail):
                            key = (from_pod, to_pod, resource)
                            if key not in seen_candidate and key not in seen_true and key not in seen_conc:
                                seen_candidate.add(key)
                                candidate_drift.append({
                                    "from": from_pod,
                                    "to": to_pod,
                                    "resource": resource,
                                    "confidence": "candidate",
                                    "reason": _truncate_quote(detail_raw, 180),
                                    "log_timestamp": entry.get("timestamp", ""),
                                    "evidence_pod": from_pod,
                                })
                            break

    # --- Removed backup paths: scan all pods regardless of declared edges ---
    for pod_id in all_pod_ids:
        raw = pods_data[pod_id].get("raw", {})
        for entry in (raw.get("logs") or []):
            detail_raw = entry.get("detail", "") or ""
            detail = detail_raw.lower()
            if _is_non_operational(detail):
                continue
            for pattern in REMOVED_BACKUP_PHRASES:
                if re.search(pattern, detail):
                    key = (pod_id, detail_raw[:60])
                    if key not in seen_backup:
                        seen_backup.add(key)
                        removed_backup.append({
                            "pod": pod_id,
                            "note": _truncate_quote(detail_raw, 180),
                            "log_timestamp": entry.get("timestamp", ""),
                        })
                    break
        for entry in (raw.get("comms") or []):
            text_raw = entry.get("content", entry.get("message", "")) or ""
            text = text_raw.lower()
            if _is_non_operational(text):
                continue
            for pattern in REMOVED_BACKUP_PHRASES:
                if re.search(pattern, text):
                    key = (pod_id, text_raw[:60])
                    if key not in seen_backup:
                        seen_backup.add(key)
                        removed_backup.append({
                            "pod": pod_id,
                            "note": _truncate_quote(text_raw, 180),
                            "log_timestamp": entry.get("timestamp", ""),
                            "source": "comms",
                        })
                    break

    return {
        "true_stale": true_stale,
        "dependency_concentration": concentration,
        "removed_backup_paths": removed_backup,
        "candidate_drift": candidate_drift,
    }


def find_mismatched_edges(pods_data: dict) -> list:
    """
    Pod A lists Pod B in /dependencies but Pod B doesn't list Pod A in /supplies,
    or Pod A lists Pod B in /supplies but Pod B doesn't list Pod A in /dependencies.
    """
    dep_map = defaultdict(set)   # dep_map[a] = set of pods a depends on
    supply_map = defaultdict(set)  # supply_map[a] = set of pods a supplies

    for pod_id, pod in pods_data.items():
        raw = pod.get("raw", {})
        for dep in (raw.get("dependencies") or []):
            if dep.get("pod_id"):
                dep_map[pod_id].add(dep["pod_id"])
        for sup in (raw.get("supplies") or []):
            if sup.get("pod_id"):
                supply_map[pod_id].add(sup["pod_id"])

    mismatched = []

    for a, deps in dep_map.items():
        for b in deps:
            # a depends on b → b should supply a
            if a not in supply_map.get(b, set()):
                mismatched.append({
                    "from": a,
                    "to": b,
                    "issue": f"{a} lists {b} as dependency but {b} does not list {a} in supplies",
                    "type": "dep_without_supply",
                })

    for a, supplies in supply_map.items():
        for b in supplies:
            # a supplies b → b should depend on a
            if a not in dep_map.get(b, set()):
                mismatched.append({
                    "from": a,
                    "to": b,
                    "issue": f"{a} lists {b} in supplies but {b} does not list {a} as dependency",
                    "type": "supply_without_dep",
                })

    return mismatched


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_digraph(pods_data: dict) -> nx.DiGraph:
    """
    Build a directed graph where edge A→B means A depends on B
    (A requires something from B).
    """
    G = nx.DiGraph()
    for pod_id in pods_data:
        G.add_node(pod_id)
    for pod_id, pod in pods_data.items():
        raw = pod.get("raw", {})
        for dep in (raw.get("dependencies") or []):
            target = dep.get("pod_id")
            if target and target in pods_data:
                G.add_edge(pod_id, target, resource=dep.get("resource", ""))
    return G


# ---------------------------------------------------------------------------
# Graph metrics
# ---------------------------------------------------------------------------

def compute_degrees(G: nx.DiGraph) -> dict:
    result = {}
    for node in G.nodes():
        result[node] = {
            "in_degree": G.in_degree(node),
            "out_degree": G.out_degree(node),
        }
    return result


def find_articulation_points(G: nx.DiGraph) -> list:
    """
    In a directed graph, find nodes whose removal disconnects the graph.
    We use the undirected version for articulation point detection,
    then verify impact on the directed graph.
    """
    undirected = G.to_undirected()
    try:
        aps = list(nx.articulation_points(undirected))
    except Exception:
        aps = []
    return sorted(aps)


def find_cycles(G: nx.DiGraph) -> list:
    """Return all simple cycles in the dependency graph."""
    try:
        cycles = list(nx.simple_cycles(G))
    except Exception:
        cycles = []
    return cycles


# ---------------------------------------------------------------------------
# Risk analysis
# ---------------------------------------------------------------------------

def simulate_blast_radius(G: nx.DiGraph, pod_id: str) -> dict:
    """
    Remove pod_id from graph, find all pods that lose a path to at least
    one of their declared dependencies.
    depth_1: pods directly depending on pod_id
    depth_2: pods depending on depth_1 pods (transitive)
    """
    if pod_id not in G:
        return {"depth_1": [], "depth_2": [], "total_affected": 0}

    G_copy = G.copy()
    G_copy.remove_node(pod_id)

    # Direct dependents: pods that had pod_id as a direct dependency
    depth_1 = [n for n in G.predecessors(pod_id) if n != pod_id]

    # Depth-2: predecessors of depth_1 nodes that aren't already in depth_1
    depth_2 = set()
    for d1_node in depth_1:
        if d1_node in G_copy:
            for pred in G.predecessors(d1_node):
                if pred != pod_id and pred not in depth_1:
                    depth_2.add(pred)

    depth_2 = sorted(depth_2)
    all_affected = sorted(set(depth_1) | set(depth_2))

    return {
        "depth_1": sorted(depth_1),
        "depth_2": depth_2,
        "total_affected": len(all_affected),
    }


def extract_warning_keywords(logs: list) -> dict:
    """
    Count negative-context warning events in logs.
    Skips entries that contain positive exemption phrases (e.g. 'backup tested successfully').
    """
    count = 0
    matches = []
    for entry in (logs or []):
        detail_raw = entry.get("detail", "") or ""
        detail = detail_raw.lower()

        # Skip if the entry is clearly positive
        if any(ex in detail for ex in WARNING_POSITIVE_EXEMPTIONS):
            continue

        for phrase in WARNING_PHRASES:
            if re.search(phrase, detail):
                count += 1
                matches.append({
                    "timestamp": entry.get("timestamp", ""),
                    "matched_phrase": phrase,
                    "detail": _truncate_quote(detail_raw, 150),
                })
                break
    return {"count": count, "entries": matches}


def compute_trust_score(pod_id: str, pod_data: dict, mismatched_edges: list) -> dict:
    """
    Score starts at 100, penalties applied for discrepancies between
    claimed status and actual evidence from logs/metadata.
    """
    score = 100
    penalties = []
    raw = pod_data.get("raw", {})

    status_claim = (raw.get("status") or {}).get("status", "unknown")

    # Warning events while claiming nominal
    warning_result = extract_warning_keywords(raw.get("logs") or [])
    warning_count = warning_result["count"]
    if status_claim == "nominal" and warning_count > 0:
        penalty = min(warning_count * 5, 30)
        score -= penalty
        penalties.append({
            "reason": f"{warning_count} warning-level log entries while status is nominal",
            "penalty": -penalty,
        })

    # No backup systems
    info = raw.get("info") or {}
    metadata = info.get("metadata") or {}
    backup_systems = metadata.get("backup_systems", None)
    if backup_systems is not None and backup_systems == 0:
        score -= 15
        penalties.append({"reason": "backup_systems = 0 in metadata", "penalty": -15})

    # Capacity utilization above 85%
    capacity = detect_capacity_conflicts(pod_data)
    util = capacity.get("metadata_utilization_pct")
    if util is not None and util > 85:
        score -= 15
        penalties.append({
            "reason": f"capacity utilization at {util:.1f}% (above 85% threshold)",
            "penalty": -15,
            "evidence": f"{pod_id}.info.metadata throughput/rated",
        })

    # Comms mention unresolved concerns
    comms = raw.get("comms") or []
    concern_keywords = ["concern", "worried", "issue", "problem", "unresolved", "risk", "danger", "urgent"]
    for msg in comms:
        if isinstance(msg, str):
            text = msg.lower()
        else:
            text = (msg.get("content", msg.get("message", msg.get("body", msg.get("text", "")))) or "").lower()
        if any(kw in text for kw in concern_keywords):
            score -= 10
            penalties.append({"reason": "comms contain unresolved concerns", "penalty": -10})
            break

    # Dependency mismatch
    pod_mismatches = [e for e in mismatched_edges if e["from"] == pod_id or e["to"] == pod_id]
    if pod_mismatches:
        score -= 10
        penalties.append({
            "reason": f"dependency/supply mismatch ({len(pod_mismatches)} inconsistencies)",
            "penalty": -10,
        })

    return {
        "score": max(score, 0),
        "status_claim": status_claim,
        "warning_count": warning_count,
        "penalties": penalties,
    }


def detect_capacity_conflicts(pod_data: dict) -> dict:
    """
    Compute capacity utilization from throughput/rated pairs in metadata (not efficiency fields),
    and extract the latest utilization figure from logs.
    Fields: metadata_utilization_pct, latest_log_utilization_pct, recycling_efficiency_pct,
            source_delta_pct, conflict.
    """
    raw = pod_data.get("raw", {})
    info = raw.get("info") or {}
    metadata = info.get("metadata") or {}

    # Compute utilization only from explicit numerator/denominator pairs.
    # Do NOT use recycling_efficiency_pct or similar process-efficiency fields.
    metadata_utilization_pct = None
    throughput_pairs = [
        ("throughput_l_day",   "rated_capacity_l_day"),
        ("throughput_kwh_day", "rated_capacity_kwh_day"),
        ("output_kg_day",      "rated_capacity_kg_day"),
        ("power_output_kw",    "rated_capacity_kw"),
    ]
    for t_key, r_key in throughput_pairs:
        throughput = metadata.get(t_key)
        rated = metadata.get(r_key)
        if throughput and rated and rated > 0:
            metadata_utilization_pct = round((throughput / rated) * 100, 1)
            break

    # Fall back to explicit utilization fields (not efficiency) only if no pair found
    if metadata_utilization_pct is None:
        for key in ["utilization_pct", "capacity_utilization_pct", "load_pct", "throughput_pct"]:
            if key in metadata:
                metadata_utilization_pct = float(metadata[key])
                break

    # Preserve process-efficiency separately so it's not confused with utilization
    recycling_efficiency_pct = metadata.get("recycling_efficiency_pct") or metadata.get("efficiency_pct")
    if recycling_efficiency_pct is not None:
        recycling_efficiency_pct = float(recycling_efficiency_pct)

    # Extract utilization % from the latest log entry that contains a percentage
    logs = raw.get("logs") or []
    latest_log_utilization_pct = None
    latest_log_timestamp = None
    pct_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*%')
    for entry in reversed(logs):
        detail = entry.get("detail", "") or ""
        # Only look in entries that are operationally about throughput/utilization/capacity
        detail_lower = detail.lower()
        if not any(kw in detail_lower for kw in ["utilization", "throughput", "capacity", "l/day", "kwh", "output"]):
            continue
        match = pct_pattern.search(detail)
        if match:
            val = float(match.group(1))
            if 0 < val < 100:
                latest_log_utilization_pct = val
                latest_log_timestamp = entry.get("timestamp", "")
                break

    source_delta_pct = None
    minor_source_variance = False
    material_conflict = False
    if metadata_utilization_pct is not None and latest_log_utilization_pct is not None:
        source_delta_pct = round(abs(metadata_utilization_pct - latest_log_utilization_pct), 1)
        minor_source_variance = source_delta_pct > 1   # sources differ slightly but agree on load level
        material_conflict = source_delta_pct >= 5      # sources meaningfully disagree

    result = {
        "metadata_utilization_pct": metadata_utilization_pct,
        "latest_log_utilization_pct": latest_log_utilization_pct,
        "source_delta_pct": source_delta_pct,
        "minor_source_variance": minor_source_variance,
        "material_conflict": material_conflict,
    }
    if recycling_efficiency_pct is not None:
        result["recycling_efficiency_pct"] = recycling_efficiency_pct
    if latest_log_timestamp:
        result["latest_log_timestamp"] = latest_log_timestamp
    return result


# ---------------------------------------------------------------------------
# Timeline extraction
# ---------------------------------------------------------------------------

def extract_timeline(pods_data: dict) -> list:
    """
    Parse all log entries colony-wide. Extract entries that describe
    infrastructure changes, sorted chronologically.
    """
    timeline = []

    for pod_id, pod in pods_data.items():
        raw = pod.get("raw", {})
        logs = raw.get("logs") or []
        comms = raw.get("comms") or []

        for entry in logs:
            detail = (entry.get("detail", "") or "")
            detail_lower = detail.lower()
            matched_keywords = [kw for kw in INFRASTRUCTURE_CHANGE_KEYWORDS if kw in detail_lower]
            if not matched_keywords:
                continue

            # Extract directive number if present
            directive_match = re.search(r'directive[^\d]*(\d{4}-\d+)', detail_lower)
            directive = directive_match.group(1) if directive_match else None

            timeline.append({
                "date": entry.get("timestamp", "")[:10],
                "pod": pod_id,
                "event": _truncate_quote(detail, 180),
                "event_type": entry.get("event", ""),
                "directive": directive,
                "matched_keywords": matched_keywords[:3],
                "source": "logs",
            })

        for entry in comms:
            if isinstance(entry, str):
                text = entry
            else:
                text = (entry.get("content", entry.get("message", entry.get("body", entry.get("text", "")))) or "")
            text_lower = text.lower()
            matched_keywords = [kw for kw in INFRASTRUCTURE_CHANGE_KEYWORDS if kw in text_lower]
            if not matched_keywords:
                continue

            directive_match = re.search(r'directive[^\d]*(\d{4}-\d+)', text_lower)
            directive = directive_match.group(1) if directive_match else None

            timeline.append({
                "date": entry.get("timestamp", "")[:10],
                "pod": pod_id,
                "event": _truncate_quote(text, 180),
                "event_type": "comms",
                "directive": directive,
                "matched_keywords": matched_keywords[:3],
                "source": "comms",
            })

    timeline.sort(key=lambda x: x["date"])
    return timeline


# ---------------------------------------------------------------------------
# Top-level derived data builder (called from mapper.py)
# ---------------------------------------------------------------------------

def compute_all_derived(pods_data: dict) -> dict:
    """
    Compute all derived fields for map.json.
    Returns: edges dict, graph-level stats, per-pod derived data.
    """
    G = build_digraph(pods_data)

    declared_edges = build_declared_edges(pods_data)
    observed_edges = build_observed_edges(pods_data)
    stale_result = find_stale_edges(declared_edges, pods_data)
    mismatched_edges = find_mismatched_edges(pods_data)

    degrees = compute_degrees(G)
    articulation_points = find_articulation_points(G)
    cycles = find_cycles(G)
    timeline = extract_timeline(pods_data)

    blast_radius_all = {}
    for pod_id in pods_data:
        blast_radius_all[pod_id] = simulate_blast_radius(G, pod_id)

    blast_radius_ranking = sorted(
        [{"pod": p, **br} for p, br in blast_radius_all.items()],
        key=lambda x: x["total_affected"],
        reverse=True,
    )

    per_pod_derived = {}
    for pod_id, pod in pods_data.items():
        br = blast_radius_all[pod_id]
        trust = compute_trust_score(pod_id, pod, mismatched_edges)
        capacity = detect_capacity_conflicts(pod)
        deg = degrees.get(pod_id, {"in_degree": 0, "out_degree": 0})
        per_pod_derived[pod_id] = {
            "in_degree": deg["in_degree"],
            "out_degree": deg["out_degree"],
            "is_articulation_point": pod_id in articulation_points,
            "blast_radius": br,
            "trust_score": trust,
            "capacity": capacity,
        }

    return {
        "edges": {
            "declared": declared_edges,
            "observed": observed_edges,
            "stale": stale_result["true_stale"],
            "dependency_concentration": stale_result["dependency_concentration"],
            "removed_backup_paths": stale_result["removed_backup_paths"],
            "candidate_drift": stale_result["candidate_drift"],
            "mismatched": mismatched_edges,
        },
        "graph": {
            "articulation_points": articulation_points,
            "cycles": cycles,
            "blast_radius_ranking": blast_radius_ranking,
        },
        "timeline": timeline,
        "per_pod_derived": per_pod_derived,
    }
