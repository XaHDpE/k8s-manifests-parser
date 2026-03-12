"""CLI entry point for k8s-config-parser."""

import argparse
import json
import sys
from pathlib import Path

from k8s_parser.loader import load_directories
from k8s_parser.resolvers import resolve_all_flows


def _compact_outbound(flows):
    """Group outbound flows by destination + shared config route."""
    grouped: dict[tuple, dict] = {}
    for f in flows:
        # First hop is the source reference in the current model.
        shared_hops = tuple(f.hops[1:]) if len(f.hops) > 1 else tuple(f.hops)
        key = (f.destination, shared_hops, f.protocol, f.port)
        if key not in grouped:
            grouped[key] = {
                "sources": [],
                "destination": f.destination,
                "hops": list(shared_hops),
                "protocol": f.protocol,
                "port": f.port,
                "ports": dict(f.ports),
            }
        if f.source not in grouped[key]["sources"]:
            grouped[key]["sources"].append(f.source)
    return list(grouped.values())


def _parse_ref(ref: str):
    parts = ref.split("/", 2)
    if len(parts) != 3:
        return None, None, None
    return parts[0], parts[1], parts[2]


def _workload_or_pod_ref(workload, index):
    pods = [p for p in getattr(index, "pods", []) if p.namespace == workload.namespace and all(p.labels.get(k) == v for k, v in workload.selector.items())]
    if pods:
        pods = sorted(pods, key=lambda p: p.name)
        return f"Pod/{pods[0].namespace}/{pods[0].name}"
    return f"{workload.kind}/{workload.namespace}/{workload.name}"


def _infra_via_from_hops(hops, index):
    """Convert config-kind hop list to infra pod/workload hop list."""
    via: list[str] = []
    seen = set()
    for h in hops:
        kind, ns, name = _parse_ref(h)
        if kind == "Gateway":
            gw = index.gateway_by_name(name, ns)
            if not gw:
                continue
            workload = index.workload_for_gateway(gw)
            if not workload:
                continue
            ref = _workload_or_pod_ref(workload, index)
            if ref not in seen:
                seen.add(ref)
                via.append(ref)
    return via


def _hop_port(ref, ports):
    kind, _, name = _parse_ref(ref)
    if ref.startswith("external:"):
        return ports.get("external_destination")
    if kind == "Gateway":
        return ports.get("gateway_listener")
    if kind == "ServiceEntry":
        return ports.get("external_destination")
    if kind == "Service":
        lowered = (name or "").lower()
        if "ingress" in lowered and "gateway_service" in ports:
            return ports.get("gateway_service")
        if "egress" in lowered and "egress_service" in ports:
            return ports.get("egress_service")
        return ports.get("destination_service")
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Pod"}:
        lowered = (name or "").lower()
        if "ingress" in lowered and "gateway_workload" in ports:
            return ports.get("gateway_workload")
        if "egress" in lowered and "egress_workload" in ports:
            return ports.get("egress_workload")
        return ports.get("destination_workload")
    return None


def _hop_details(hops, ports):
    details = []
    for ref in hops:
        item = {"ref": ref}
        port = _hop_port(ref, ports or {})
        if port is not None:
            item["port"] = port
        details.append(item)
    return details


def _format_hop(ref, ports):
    port = _hop_port(ref, ports or {})
    if port is None:
        return ref
    return f"{ref} [port={port}]"


def _flow_to_json(f, index, trace_config: bool) -> dict:
    hops = list(f.hops)
    via = hops if trace_config else _infra_via_from_hops(f.hops, index)
    return {
        "source": f.source,
        "destination": f.destination,
        "protocol": f.protocol,
        "port": f.port,
        "hops": hops,
        "hop_details": _hop_details(hops, f.ports),
        "via": via,
        "via_details": _hop_details(via, f.ports),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze Kubernetes/Istio manifests and output network flow interactions."
    )
    parser.add_argument(
        "dirs",
        nargs="+",
        type=Path,
        help="Directories containing YAML manifests (e.g. data/in)",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Compact outbound output by grouping same route/destination and listing sources",
    )
    parser.add_argument(
        "--trace-config",
        action="store_true",
        help="Show full config trace (Gateway/VirtualService/ServiceEntry...). By default only infra pods/workloads are shown in via.",
    )
    args = parser.parse_args()

    dirs = [p.resolve() for p in args.dirs]
    for d in dirs:
        if not d.is_dir():
            print(f"Error: not a directory: {d}", file=sys.stderr)
            return 1

    index = load_directories(dirs)
    flows = resolve_all_flows(index)
    compact_outbound = _compact_outbound(flows.outbound) if args.compact else None

    if args.format == "json":
        if args.compact:
            outbound = []
            for item in compact_outbound or []:
                hops = item["hops"]
                via = hops if args.trace_config else _infra_via_from_hops(hops, index)
                outbound.append({
                    "sources": item["sources"],
                    "destination": item["destination"],
                    "protocol": item["protocol"],
                    "port": item["port"],
                    "hops": hops,
                    "hop_details": _hop_details(hops, item["ports"]),
                    "via": via,
                    "via_details": _hop_details(via, item["ports"]),
                })
        else:
            outbound = []
            for f in flows.outbound:
                outbound.append(_flow_to_json(f, index, args.trace_config))

        inbound = []
        for f in flows.inbound:
            inbound.append(_flow_to_json(f, index, args.trace_config))

        internal = []
        for f in flows.internal:
            internal.append(_flow_to_json(f, index, args.trace_config))

        out = {
            "inbound": inbound,
            "outbound": outbound,
            "internal": internal,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        _print_text(flows, index, compact_outbound=compact_outbound, trace_config=args.trace_config)

    return 0


def _print_text(flows, index, compact_outbound=None, trace_config=False) -> None:
    for label, items in [
        ("=== INBOUND (outside -> app) ===", flows.inbound),
        ("=== OUTBOUND (app -> outside) ===", compact_outbound if compact_outbound is not None else flows.outbound),
        ("=== INTERNAL (app -> app) ===", flows.internal),
    ]:
        print(label)
        if not items:
            print("  (none)")
        is_compact_outbound = label.startswith("=== OUTBOUND") and compact_outbound is not None
        for f in items:
            if is_compact_outbound:
                print(f"  sources ({len(f['sources'])}) -> {f['destination']}")
                for s in f["sources"]:
                    print(f"    source: {s}")
                via_hops = f["hops"] if trace_config else _infra_via_from_hops(f["hops"], index)
                for h in via_hops:
                    print(f"    via: {_format_hop(h, f.get('ports'))}")
            else:
                print(f"  {f.source} -> {f.destination}")
                via_hops = f.hops if trace_config else _infra_via_from_hops(f.hops, index)
                for h in via_hops:
                    print(f"    via: {_format_hop(h, f.ports)}")
        print()


if __name__ == "__main__":
    sys.exit(main())
