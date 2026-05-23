"""
mapper.py — Deterministic data collection pipeline. No LLM calls.
Discovers all pods, crawls their endpoints, runs graph analysis, writes map.json.
"""

import asyncio
import json
import os
import sys

import httpx

from graph import compute_all_derived

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:3000")
OUTPUT_PATH = "/rover/output/map.json"

# Known pod hostnames and ports
KNOWN_PODS = {
    "helios":       3001,
    "artemis":      3002,
    "hydroponics":  3003,
    "aquifer":      3004,
    "zephyr":       3005,
    "prometheus":   3006,
    "medica":       3007,
    "terminus":     3008,
    "nexus":        3009,
    "forge":        3010,
    "vault":        3011,
    "sentinel":     3012,
}

ENDPOINTS = ["/info", "/status", "/dependencies", "/supplies", "/logs", "/comms"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

async def discover_via_bfs(client: httpx.AsyncClient) -> set:
    """
    BFS starting from gateway. Gateway returns a pointer to Artemis.
    From each pod follow /dependencies and /supplies to find connected pods.
    """
    discovered = set()
    queue = []

    # Hit the gateway first
    try:
        resp = await client.get(GATEWAY_URL, timeout=10)
        resp.raise_for_status()
        data = resp.text
        print(f"[gateway] {data[:120]}")
        # Gateway typically returns JSON with a pointer to artemis
        try:
            gw_json = json.loads(data)
            # Look for any value that mentions a pod hostname
            for val in str(gw_json).lower().split():
                for pod_name in KNOWN_PODS:
                    if pod_name in val:
                        queue.append(pod_name)
        except json.JSONDecodeError:
            # Plain text — scan for pod names
            data_lower = data.lower()
            for pod_name in KNOWN_PODS:
                if pod_name in data_lower:
                    queue.append(pod_name)
        if not queue:
            queue.append("artemis")  # default starting point
    except Exception as e:
        print(f"[gateway] error: {e} — starting BFS from artemis", flush=True)
        queue.append("artemis")

    visited = set()
    while queue:
        pod_name = queue.pop(0)
        if pod_name in visited:
            continue
        visited.add(pod_name)
        if pod_name not in KNOWN_PODS:
            continue

        port = KNOWN_PODS[pod_name]
        base = f"http://{pod_name}:{port}"
        discovered.add(pod_name)

        for endpoint in ["/dependencies", "/supplies"]:
            try:
                resp = await client.get(f"{base}{endpoint}", timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    key = endpoint.lstrip("/")
                    # Unwrap wrapper object if present
                    items = data[key] if isinstance(data, dict) and key in data else data
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                ref = item.get("pod_id")
                                if ref and ref in KNOWN_PODS and ref not in visited:
                                    queue.append(ref)
            except Exception:
                pass

    return discovered


async def discover_via_dns(client: httpx.AsyncClient) -> set:
    """Try all known pod hostnames. Any that respond to /info are reachable."""
    discovered = set()

    async def probe(pod_name, port):
        try:
            resp = await client.get(f"http://{pod_name}:{port}/info", timeout=5)
            if resp.status_code == 200:
                discovered.add(pod_name)
                print(f"[dns] {pod_name}:{port} reachable", flush=True)
        except Exception:
            pass

    await asyncio.gather(*[probe(name, port) for name, port in KNOWN_PODS.items()])
    return discovered


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

async def crawl_pod(client: httpx.AsyncClient, pod_name: str, port: int) -> dict:
    """Fetch all endpoints for a single pod concurrently."""
    base = f"http://{pod_name}:{port}"

    async def fetch(endpoint):
        try:
            resp = await client.get(f"{base}{endpoint}", timeout=15)
            if resp.status_code == 404:
                return endpoint, None
            resp.raise_for_status()
            return endpoint, resp.json()
        except Exception as e:
            print(f"[{pod_name}] {endpoint} error: {e}", flush=True)
            return endpoint, None

    results = await asyncio.gather(*[fetch(ep) for ep in ENDPOINTS])
    raw = {}
    # Each endpoint returns a wrapper object e.g. {"id":..., "dependencies":[...]}
    # or {"id":..., "messages":[...]} for comms, or {"error":...} for no-comms pods.
    # Unwrap to the inner list/value; treat error responses as None.
    unwrap_keys = {
        "info": "info",          # /info has no single inner key — keep whole object
        "status": "status",      # keep whole object (has .status, .alerts, etc.)
        "dependencies": "dependencies",
        "supplies": "supplies",
        "logs": "logs",
        "comms": "messages",     # /comms wraps as {"messages": [...]}
    }
    for ep, data in results:
        key = ep.lstrip("/")
        if data is None:
            raw[key] = None
        elif isinstance(data, dict) and "error" in data and len(data) == 1:
            raw[key] = None  # e.g. {"error": "No comms channel configured"}
        elif key in ("info", "status"):
            raw[key] = data  # keep whole object
        else:
            inner_key = unwrap_keys.get(key, key)
            raw[key] = data.get(inner_key) if isinstance(data, dict) else data
    print(f"[{pod_name}] crawled — info:{raw['info'] is not None} deps:{raw['dependencies'] is not None}", flush=True)
    return raw


async def crawl_all_pods(pods: set) -> dict:
    """Crawl all discovered pods concurrently."""
    async with httpx.AsyncClient() as client:
        tasks = {
            pod_name: crawl_pod(client, pod_name, KNOWN_PODS[pod_name])
            for pod_name in pods
            if pod_name in KNOWN_PODS
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        crawled = {}
        for pod_name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                print(f"[{pod_name}] crawl failed: {result}", flush=True)
                crawled[pod_name] = {}
            else:
                crawled[pod_name] = result
    return crawled


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("[mapper] starting discovery", flush=True)

    async with httpx.AsyncClient() as client:
        bfs_task = asyncio.create_task(discover_via_bfs(client))
        dns_task = asyncio.create_task(discover_via_dns(client))
        bfs_pods, dns_pods = await asyncio.gather(bfs_task, dns_task)

    all_pods = bfs_pods | dns_pods
    delta = sorted(dns_pods - bfs_pods)

    print(f"[mapper] BFS found: {sorted(bfs_pods)}", flush=True)
    print(f"[mapper] DNS found: {sorted(dns_pods)}", flush=True)
    print(f"[mapper] delta (DNS only): {delta}", flush=True)

    print(f"[mapper] crawling {len(all_pods)} pods", flush=True)
    crawled = await crawl_all_pods(all_pods)

    # Build pods_data structure: {pod_id: {raw: {...}}}
    pods_data = {pod_id: {"raw": raw} for pod_id, raw in crawled.items()}

    print("[mapper] running graph analysis", flush=True)
    derived = compute_all_derived(pods_data)

    # Merge derived per-pod data into pods_data
    for pod_id, pod_derived in derived["per_pod_derived"].items():
        if pod_id in pods_data:
            pods_data[pod_id]["derived"] = pod_derived

    map_json = {
        "discovery": {
            "bfs_reachable": sorted(bfs_pods),
            "dns_discovered": sorted(dns_pods),
            "all_pods": sorted(all_pods),
            "delta": delta,
        },
        "pods": pods_data,
        "edges": derived["edges"],
        "graph": derived["graph"],
        "timeline": derived["timeline"],
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(map_json, f, indent=2)

    print(f"[mapper] wrote {OUTPUT_PATH} ({os.path.getsize(OUTPUT_PATH)} bytes)", flush=True)
    print(f"[mapper] articulation points: {derived['graph']['articulation_points']}", flush=True)
    print(f"[mapper] cycles: {derived['graph']['cycles']}", flush=True)
    print(f"[mapper] timeline events: {len(derived['timeline'])}", flush=True)
    print(f"[mapper] stale edges: {len(derived['edges']['stale'])}", flush=True)
    print(f"[mapper] mismatched edges: {len(derived['edges']['mismatched'])}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
