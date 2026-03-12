"""Resolvers for inbound, outbound, and internal network flows."""

from __future__ import annotations

from dataclasses import dataclass, field

from .index import ResourceIndex, _labels_match
from .models import DestinationRule, Flow, Gateway, Service, VirtualService, VirtualServiceRoute, Workload


@dataclass
class FlowReport:
    """Report of all resolved flows."""

    inbound: list[Flow] = field(default_factory=list)
    outbound: list[Flow] = field(default_factory=list)
    internal: list[Flow] = field(default_factory=list)


def resolve_all_flows(index: ResourceIndex) -> FlowReport:
    """Resolve inbound, outbound, and internal flows from the resource index."""
    report = FlowReport()
    report.inbound = _resolve_inbound(index)
    report.outbound = _resolve_outbound(index)
    report.internal = _resolve_internal(index)
    return report


def _deduplicate_flows(flows: list[Flow], key_fn=None) -> list[Flow]:
    """Deduplicate flows while preserving first occurrence."""
    seen: set = set()
    result: list[Flow] = []
    for f in flows:
        key = key_fn(f) if key_fn else (f.source, f.destination, f.protocol, f.port, tuple(f.hops))
        if key in seen:
            continue
        seen.add(key)
        result.append(f)
    return result


def _application_ref(namespace: str) -> str:
    return f"Application/{namespace}/app"


def _iter_vs_routes(vs: VirtualService):
    for route_list in vs.http_routes:
        for route in route_list:
            yield "http", route
    for route_list in vs.tcp_routes:
        for route in route_list:
            yield "tcp", route
    for route_list in vs.tls_routes:
        for route in route_list:
            yield "tls", route


def _route_matches_gateway(route: VirtualServiceRoute, gateway_name: str) -> bool:
    # Empty match means route applies to all gateways from spec.gateways.
    if not route.gateways:
        return True
    return gateway_name in route.gateways


def _host_matches(vs_host: str, host: str) -> bool:
    if not host:
        return True
    if vs_host == host:
        return True
    if vs_host.startswith("*.") and ("." + host).endswith("." + vs_host.removeprefix("*.")):
        return True
    if vs_host.endswith("*") and host.startswith(vs_host.removesuffix("*")):
        return True
    return False


def _service_from_host(index: ResourceIndex, host: str, namespace: str) -> Service | None:
    if not host:
        return None
    svc = index.service_by_dns(host)
    if svc:
        return svc
    short = host.split(".")[0] if "." in host else host
    return index.service_by_name(short, namespace)


def _gateway_protocol_for_host(gw: Gateway, host: str | None = None) -> str:
    fallback = ""
    for server in gw.servers:
        if not isinstance(server, dict):
            continue
        hosts = server.get("hosts") or []
        if not isinstance(hosts, list):
            hosts = [hosts]
        port_cfg = server.get("port") or {}
        proto = (port_cfg.get("protocol") if isinstance(port_cfg, dict) else "") or ""
        if proto and not fallback:
            fallback = str(proto).lower()
        if host:
            matched = any(_host_matches(str(h), host) for h in hosts if isinstance(h, str))
            if not matched:
                continue
        if proto:
            return str(proto).lower()
    return fallback


def _services_for_gateway_workload(index: ResourceIndex, gw: Gateway) -> list[Service]:
    w = index.workload_for_gateway(gw)
    if not w:
        return []
    return [
        s
        for s in index.services
        if s.namespace == w.namespace and _labels_match(s.selector, w.labels)
    ]


def _service_by_port(candidates: list[Service], port: int | None) -> Service | None:
    if port is None:
        return None
    for svc in candidates:
        for p in svc.ports:
            if isinstance(p, dict) and p.get("port") == port:
                return svc
    return None


def _service_port_spec(service: Service, port: int | None) -> dict | None:
    if port is None:
        return None
    for spec in service.ports:
        if isinstance(spec, dict) and spec.get("port") == port:
            return spec
    return None


def _resolve_workload_port(service: Service, service_port: int | None, workloads: list[Workload]) -> int | None:
    port_spec = _service_port_spec(service, service_port)
    if not port_spec:
        return None

    target_port = port_spec.get("targetPort")
    if isinstance(target_port, int):
        return target_port
    if isinstance(target_port, str):
        for workload in workloads:
            if target_port in workload.named_ports:
                return workload.named_ports[target_port]
        return None

    port = port_spec.get("port")
    return port if isinstance(port, int) else None


def _gateway_server_port(gw: Gateway, host: str | None = None) -> int | None:
    fallback: int | None = None
    for server in gw.servers:
        if not isinstance(server, dict):
            continue
        hosts = server.get("hosts") or []
        if not isinstance(hosts, list):
            hosts = [hosts]
        port_cfg = server.get("port") or {}
        port = port_cfg.get("number") if isinstance(port_cfg, dict) else None
        if isinstance(port, int) and fallback is None:
            fallback = port
        if host:
            matched = any(_host_matches(str(h), host) for h in hosts if isinstance(h, str))
            if not matched:
                continue
        if isinstance(port, int):
            return port
    return fallback


def _gateway_service_port(vs: VirtualService, gw: Gateway, route: VirtualServiceRoute) -> int | None:
    for _, mesh_route in _iter_vs_routes(vs):
        if mesh_route.gateways and "mesh" not in mesh_route.gateways:
            continue
        if not mesh_route.host or mesh_route.port is None:
            continue
        return mesh_route.port
    return _gateway_server_port(gw, route.host)


def _resolve_gateway_service(
    index: ResourceIndex,
    vs: VirtualService,
    gw: Gateway,
    route: VirtualServiceRoute,
) -> Service | None:
    candidates = _services_for_gateway_workload(index, gw)
    if not candidates:
        return None

    # 1) Preferred: mesh leg of the same VS usually points to egress service.
    mesh_services: list[Service] = []
    for _, mesh_route in _iter_vs_routes(vs):
        if mesh_route.gateways and "mesh" not in mesh_route.gateways:
            continue
        if not mesh_route.host or index.is_external_host(mesh_route.host):
            continue
        svc = _service_from_host(index, mesh_route.host, vs.namespace)
        if svc and svc in candidates and svc not in mesh_services:
            mesh_services.append(svc)
    if len(mesh_services) == 1:
        return mesh_services[0]

    # 2) Some VS use internal service in spec.hosts directly.
    host_services: list[Service] = []
    for h in vs.hosts:
        if index.is_external_host(h):
            continue
        svc = _service_from_host(index, h, vs.namespace)
        if svc and svc in candidates and svc not in host_services:
            host_services.append(svc)
    if len(host_services) == 1:
        return host_services[0]

    # 3) Try gateway server port and route destination port.
    by_route_port = _service_by_port(candidates, route.port)
    if by_route_port:
        return by_route_port

    for server in gw.servers:
        if not isinstance(server, dict):
            continue
        port_cfg = server.get("port") or {}
        server_port = port_cfg.get("number") if isinstance(port_cfg, dict) else None
        by_server_port = _service_by_port(candidates, server_port)
        if by_server_port:
            return by_server_port

    # 4) Deterministic fallback.
    return sorted(candidates, key=lambda s: s.name)[0]


def _find_destination_rule(index: ResourceIndex, host: str, namespace: str) -> DestinationRule | None:
    """Find the best matching DestinationRule for a host in a namespace."""
    for dr in index.destination_rules:
        if dr.namespace != namespace or not dr.host:
            continue
        if dr.host == host:
            return dr
    for dr in index.destination_rules:
        if dr.namespace != namespace or not dr.host:
            continue
        if _host_matches(dr.host, host):
            return dr
    return None


def _outbound_destination_rules(
    index: ResourceIndex,
    egress_svc: Service,
    external_host: str,
    namespace: str,
) -> tuple[DestinationRule | None, DestinationRule | None]:
    """Find the pair of DRs for an outbound egress flow.

    Returns (dr_to_egress, dr_to_external):
      - dr_to_egress:  DR for app→egress service (host = egress svc dns)
      - dr_to_external: DR for egress→external host (host = external host)
    """
    dr_to_egress = _find_destination_rule(index, egress_svc.dns_name(), namespace)
    dr_to_external = _find_destination_rule(index, external_host, namespace)
    return dr_to_egress, dr_to_external


def _resolve_inbound(index: ResourceIndex) -> list[Flow]:
    """Resolve flows from outside into the application."""
    flows: list[Flow] = []
    seen: set[tuple[str, ...]] = set()
    gateways_handled_by_ingress: set[tuple[str, str]] = set()

    for ingress in index.ingresses:
        for rule in ingress.rules:
            host = rule.get("host", "")
            for path_cfg in rule.get("http", {}).get("paths", []) or []:
                backend = path_cfg.get("backend", {})
                svc_ref = backend.get("service", {}) or backend
                svc_name = svc_ref.get("name") if isinstance(svc_ref, dict) else None
                if not svc_name:
                    continue
                ingress_svc = index.service_by_name(svc_name, ingress.namespace)
                if not ingress_svc:
                    continue
                gw_workload = next(
                    (w for w in index.workloads_for_service(ingress_svc) if "ingress" in w.name.lower()),
                    None,
                )
                if not gw_workload:
                    gateway_workloads = index.workloads_for_service(ingress_svc)
                    gw_workload = gateway_workloads[0] if gateway_workloads else None
                gateways = [g for g in index.gateways if index.workload_for_gateway(g) == gw_workload]
                for gw in gateways:
                    for vs in index.virtual_services_for_gateway(gw.name, gw.namespace):
                        if host and not any(_host_matches(vh, host) for vh in vs.hosts):
                            continue
                        for protocol, route in _iter_vs_routes(vs):
                            if not _route_matches_gateway(route, gw.name):
                                continue
                            if not route.host:
                                continue
                            dest_svc = _service_from_host(index, route.host, vs.namespace)
                            if not dest_svc:
                                continue
                            for dw in index.workloads_for_service(dest_svc):
                                if "ingress" in dw.name.lower() or "egress" in dw.name.lower():
                                    continue
                                hops = [
                                    f"Ingress/{ingress.namespace}/{ingress.name}",
                                    f"Service/{ingress_svc.namespace}/{ingress_svc.name}",
                                    f"Gateway/{gw.namespace}/{gw.name}",
                                    f"VirtualService/{vs.namespace}/{vs.name}",
                                    f"Service/{dest_svc.namespace}/{dest_svc.name}",
                                    f"{dw.kind}/{dw.namespace}/{dw.name}",
                                ]
                                key = (ingress.name, gw.name, vs.name, dest_svc.name, dw.name, route.host)
                                if key in seen:
                                    continue
                                seen.add(key)
                                gateways_handled_by_ingress.add((gw.namespace, gw.name))
                                ingress_protocol = (
                                    "https"
                                    if host and host in ingress.tls_hosts
                                    else (_gateway_protocol_for_host(gw, host) or protocol)
                                )
                                listener_port = _gateway_server_port(gw, host)
                                ingress_gateway_workloads = index.workloads_for_service(ingress_svc)
                                dest_workload_port = _resolve_workload_port(
                                    dest_svc,
                                    route.port,
                                    index.workloads_for_service(dest_svc),
                                )
                                ports: dict[str, int] = {}
                                if listener_port is not None:
                                    ports["gateway_listener"] = listener_port
                                    ports["gateway_service"] = listener_port
                                    ingress_gateway_port = _resolve_workload_port(
                                        ingress_svc,
                                        listener_port,
                                        ingress_gateway_workloads,
                                    )
                                    if ingress_gateway_port is not None:
                                        ports["gateway_workload"] = ingress_gateway_port
                                if route.port is not None:
                                    ports["destination_service"] = route.port
                                if dest_workload_port is not None:
                                    ports["destination_workload"] = dest_workload_port
                                flows.append(
                                    Flow(
                                        source=f"external:{host or '*'}" if host else "external",
                                        destination=f"{dw.kind}/{dw.namespace}/{dw.name}",
                                        hops=hops,
                                        protocol=ingress_protocol,
                                        port=route.port,
                                        ports=ports,
                                    )
                                )

    for gw in index.gateways:
        if (gw.namespace, gw.name) in gateways_handled_by_ingress:
            continue
        gw_workload = index.workload_for_gateway(gw)
        if not gw_workload or "ingress" not in gw_workload.name.lower():
            continue
        gw_svc = index.service_for_gateway_workload(gw)
        if not gw_svc:
            continue
        for vs in index.virtual_services_for_gateway(gw.name, gw.namespace):
            for host in vs.hosts:
                if host.endswith(".svc.cluster.local"):
                    continue
                for protocol, route in _iter_vs_routes(vs):
                    if not _route_matches_gateway(route, gw.name):
                        continue
                    if not route.host:
                        continue
                    dest_svc = _service_from_host(index, route.host, vs.namespace)
                    if not dest_svc:
                        continue
                    for dw in index.workloads_for_service(dest_svc):
                        if "ingress" in dw.name.lower() or "egress" in dw.name.lower():
                            continue
                        listener_port = _gateway_server_port(gw, host)
                        gateway_workloads = index.workloads_for_service(gw_svc)
                        dest_workloads = index.workloads_for_service(dest_svc)
                        destination_workload_port = _resolve_workload_port(
                            dest_svc,
                            route.port,
                            dest_workloads,
                        )
                        gateway_workload_port = _resolve_workload_port(
                            gw_svc,
                            listener_port,
                            gateway_workloads,
                        )
                        hops = [
                            f"Gateway/{gw.namespace}/{gw.name}",
                            f"Service/{gw_svc.namespace}/{gw_svc.name}",
                            f"VirtualService/{vs.namespace}/{vs.name}",
                            f"Service/{dest_svc.namespace}/{dest_svc.name}",
                            f"{dw.kind}/{dw.namespace}/{dw.name}",
                        ]
                        key = (gw.name, vs.name, dest_svc.name, dw.name, route.host)
                        if key in seen:
                            continue
                        seen.add(key)
                        flows.append(
                            Flow(
                                source=f"external:{host}",
                                destination=f"{dw.kind}/{dw.namespace}/{dw.name}",
                                hops=hops,
                                protocol=_gateway_protocol_for_host(gw, host) or protocol,
                                port=route.port,
                                ports={
                                    **({"gateway_listener": listener_port} if listener_port is not None else {}),
                                    **({"gateway_service": listener_port} if listener_port is not None else {}),
                                    **({"gateway_workload": gateway_workload_port} if gateway_workload_port is not None else {}),
                                    **({"destination_service": route.port} if route.port is not None else {}),
                                    **(
                                        {"destination_workload": destination_workload_port}
                                        if destination_workload_port is not None
                                        else {}
                                    ),
                                },
                            )
                        )

    return _deduplicate_flows(flows)


def _resolve_outbound(index: ResourceIndex) -> list[Flow]:
    """Resolve flows from the application to external services."""
    flows: list[Flow] = []
    seen: set[tuple[str, ...]] = set()

    for vs in index.virtual_services:
        non_mesh_gateways = [g for g in vs.gateways if g != "mesh"]
        if not non_mesh_gateways:
            continue
        for gw_name in non_mesh_gateways:
            gw = index.gateway_by_name(gw_name, vs.namespace)
            if not gw:
                continue
            gw_workload = index.workload_for_gateway(gw)
            if not gw_workload or "egress" not in gw_workload.name.lower():
                continue
            for protocol, route in _iter_vs_routes(vs):
                if not _route_matches_gateway(route, gw_name):
                    continue
                if not route.host or not index.is_external_host(route.host):
                    continue

                gw_svc = _resolve_gateway_service(index, vs, gw, route)
                if not gw_svc:
                    continue
                se = index.service_entry_for_host(route.host)
                source = _application_ref(vs.namespace)
                dr_to_egress, dr_to_external = _outbound_destination_rules(
                    index, gw_svc, route.host, vs.namespace,
                )
                service_port = _gateway_service_port(vs, gw, route)
                gateway_workloads = index.workloads_for_service(gw_svc)
                workload_port = _resolve_workload_port(gw_svc, service_port, gateway_workloads)
                listener_port = _gateway_server_port(gw, route.host)
                hops = [source]
                if dr_to_egress:
                    hops.append(f"DestinationRule/{dr_to_egress.namespace}/{dr_to_egress.name}")
                hops.append(f"Service/{gw_svc.namespace}/{gw_svc.name}")
                hops.append(f"Gateway/{gw.namespace}/{gw.name}")
                hops.append(f"VirtualService/{vs.namespace}/{vs.name}")
                if dr_to_external:
                    hops.append(f"DestinationRule/{dr_to_external.namespace}/{dr_to_external.name}")
                if se:
                    hops.append(f"ServiceEntry/{se.namespace}/{se.name}")
                hops.append(f"external:{route.host}")

                key = (vs.namespace, gw_svc.name, gw.name, vs.name, route.host, route.port or -1)
                if key in seen:
                    continue
                seen.add(key)
                flows.append(
                    Flow(
                        source=source,
                        destination=f"external:{route.host}",
                        hops=hops,
                        protocol=_gateway_protocol_for_host(gw, route.host) or protocol,
                        port=route.port,
                        ports={
                            **({"egress_service": service_port} if service_port is not None else {}),
                            **({"egress_workload": workload_port} if workload_port is not None else {}),
                            **({"gateway_listener": listener_port} if listener_port is not None else {}),
                            **({"external_destination": route.port} if route.port is not None else {}),
                        },
                    )
                )

    return _deduplicate_flows(flows)


def _resolve_internal(index: ResourceIndex) -> list[Flow]:
    """Resolve internal app-to-app flows.

    Kubernetes/Istio manifests in this parser scope do not provide a reliable
    source workload for in-mesh calls. To avoid fabricated dependencies, this
    resolver reports no internal flows for the current manifest model.
    """
    _ = index
    return []
