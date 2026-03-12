"""Load and parse Kubernetes/Istio YAML manifests into the resource model."""

from pathlib import Path

import yaml

from .index import ResourceIndex
from .models import (
    DestinationRule,
    Gateway,
    Ingress,
    Service,
    ServiceEntry,
    VirtualService,
    VirtualServiceRoute,
    Workload,
)


def _safe_get(data: dict, *keys: str, default=None):
    d = data
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def _parse_workload(kind: str, doc: dict) -> Workload | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name") or meta.get("generateName", "")
    namespace = meta.get("namespace", "default")
    labels = dict(meta.get("labels") or {})
    template = spec.get("template") or {}
    template_meta = template.get("metadata") or {}
    template_labels = dict(template_meta.get("labels") or {})
    template_spec = template.get("spec") or {}
    if template_labels:
        labels = {**labels, **template_labels}
    selector = dict(spec.get("selector") or {})
    if isinstance(selector.get("matchLabels"), dict):
        selector = dict(selector["matchLabels"])
    container_ports: list[int] = []
    named_ports: dict[str, int] = {}
    for container in template_spec.get("containers") or []:
        if not isinstance(container, dict):
            continue
        for port_cfg in container.get("ports") or []:
            if not isinstance(port_cfg, dict):
                continue
            port = port_cfg.get("containerPort")
            if isinstance(port, int) and port not in container_ports:
                container_ports.append(port)
            port_name = port_cfg.get("name")
            if isinstance(port_name, str) and port_name and isinstance(port, int):
                named_ports[port_name] = port
    if not name:
        return None
    return Workload(
        kind=kind,
        name=name,
        namespace=namespace,
        labels=labels,
        selector=selector,
        container_ports=container_ports,
        named_ports=named_ports,
    )


def _parse_service(doc: dict) -> Service | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name")
    namespace = meta.get("namespace", "default")
    if not name:
        return None
    selector = dict(spec.get("selector") or {})
    ports = list(spec.get("ports") or [])
    return Service(name=name, namespace=namespace, selector=selector, ports=ports)


def _parse_ingress(doc: dict) -> Ingress | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name")
    namespace = meta.get("namespace", "default")
    if not name:
        return None
    rules = list(spec.get("rules") or [])
    backend = spec.get("backend")
    tls_hosts: list[str] = []
    for tls_item in spec.get("tls") or []:
        if not isinstance(tls_item, dict):
            continue
        for h in tls_item.get("hosts") or []:
            if isinstance(h, str) and h:
                tls_hosts.append(h)
    return Ingress(name=name, namespace=namespace, rules=rules, backend=backend, tls_hosts=tls_hosts)


def _parse_gateway(doc: dict) -> Gateway | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name")
    namespace = meta.get("namespace", "default")
    if not name:
        return None
    selector = dict(spec.get("selector") or {})
    servers = list(spec.get("servers") or [])
    return Gateway(name=name, namespace=namespace, selector=selector, servers=servers)


def _extract_match_gateways(route_rule: dict) -> list[str]:
    gateways: list[str] = []
    matches = route_rule.get("match") or []
    if not isinstance(matches, list):
        matches = [matches]
    for m in matches:
        if not isinstance(m, dict):
            continue
        for gw in m.get("gateways") or []:
            if isinstance(gw, str) and gw and gw not in gateways:
                gateways.append(gw)
    return gateways


def _extract_routes_from_http(routes: list) -> list[list[VirtualServiceRoute]]:
    result: list[list[VirtualServiceRoute]] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        match_gateways = _extract_match_gateways(r)
        dests = r.get("route") or []
        if not isinstance(dests, list):
            dests = [dests] if dests else []
        path_routes: list[VirtualServiceRoute] = []
        for d in dests:
            dest = (d.get("destination") if isinstance(d, dict) else None) or d
            if not isinstance(dest, dict):
                continue
            host = dest.get("host", "")
            port_spec = dest.get("port") or {}
            port = port_spec.get("number") if isinstance(port_spec, dict) else None
            subset = dest.get("subset")
            path_routes.append(
                VirtualServiceRoute(
                    host=host,
                    port=port,
                    subset=subset,
                    gateways=match_gateways.copy(),
                )
            )
        if path_routes:
            result.append(path_routes)
    return result


def _extract_routes_from_tcp(routes: list) -> list[list[VirtualServiceRoute]]:
    result: list[list[VirtualServiceRoute]] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        match_gateways = _extract_match_gateways(r)
        dests = r.get("route") or []
        if not isinstance(dests, list):
            dests = [dests] if dests else []
        path_routes: list[VirtualServiceRoute] = []
        for d in dests:
            if not isinstance(d, dict):
                continue
            dest = d.get("destination") or d
            if not isinstance(dest, dict):
                continue
            host = dest.get("host", "")
            port_spec = dest.get("port") or {}
            port = port_spec.get("number") if isinstance(port_spec, dict) else None
            path_routes.append(
                VirtualServiceRoute(host=host, port=port, subset=None, gateways=match_gateways.copy())
            )
        if path_routes:
            result.append(path_routes)
    return result


def _extract_routes_from_tls(routes: list) -> list[list[VirtualServiceRoute]]:
    result: list[list[VirtualServiceRoute]] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        match_gateways = _extract_match_gateways(r)
        dests = r.get("route") or []
        if not isinstance(dests, list):
            dests = [dests] if dests else []
        path_routes: list[VirtualServiceRoute] = []
        for d in dests:
            if not isinstance(d, dict):
                continue
            dest = d.get("destination") or d
            if not isinstance(dest, dict):
                continue
            host = dest.get("host", "")
            port_spec = dest.get("port") or {}
            port = port_spec.get("number") if isinstance(port_spec, dict) else None
            path_routes.append(
                VirtualServiceRoute(host=host, port=port, subset=None, gateways=match_gateways.copy())
            )
        if path_routes:
            result.append(path_routes)
    return result


def _parse_virtual_service(doc: dict) -> VirtualService | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name")
    namespace = meta.get("namespace", "default")
    if not name:
        return None
    hosts = list(spec.get("hosts") or [])
    gateways = list(spec.get("gateways") or [])
    http_routes: list[list[VirtualServiceRoute]] = []
    tcp_routes: list[list[VirtualServiceRoute]] = []
    tls_routes: list[list[VirtualServiceRoute]] = []
    for h in spec.get("http") or []:
        http_routes.extend(_extract_routes_from_http([h]))
    for t in spec.get("tcp") or []:
        tcp_routes.extend(_extract_routes_from_tcp([t]))
    for t in spec.get("tls") or []:
        tls_routes.extend(_extract_routes_from_tls([t]))
    return VirtualService(
        name=name,
        namespace=namespace,
        hosts=hosts,
        gateways=gateways,
        http_routes=http_routes,
        tcp_routes=tcp_routes,
        tls_routes=tls_routes,
    )


def _parse_service_entry(doc: dict) -> ServiceEntry | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name")
    namespace = meta.get("namespace", "default")
    if not name:
        return None
    hosts = list(spec.get("hosts") or [])
    location = spec.get("location", "MESH_EXTERNAL")
    ports = list(spec.get("ports") or [])
    endpoints = list(spec.get("endpoints") or [])
    return ServiceEntry(
        name=name,
        namespace=namespace,
        hosts=hosts,
        location=location,
        ports=ports,
        endpoints=endpoints,
    )


def _parse_destination_rule(doc: dict) -> DestinationRule | None:
    meta = _safe_get(doc, "metadata") or {}
    spec = _safe_get(doc, "spec") or {}
    name = meta.get("name")
    namespace = meta.get("namespace", "default")
    if not name:
        return None
    host = spec.get("host", "")
    ws = spec.get("workloadSelector") or {}
    workload_selector = dict(ws.get("matchLabels") or {})
    return DestinationRule(
        name=name,
        namespace=namespace,
        host=host,
        workload_selector=workload_selector,
    )


def load_yaml_docs(path: Path) -> list[dict]:
    """Load all YAML documents from a file (supports multi-doc)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        docs = list(yaml.safe_load_all(text))
        return [d for d in docs if isinstance(d, dict)]
    except Exception:
        return []


def load_directories(dirs: list[Path]) -> ResourceIndex:
    """Load all YAML manifests from given directories into a ResourceIndex."""
    index = ResourceIndex()
    seen: set[str] = set()

    for base in dirs:
        if not base.is_dir():
            continue
        for path in base.rglob("*.yaml"):
            _load_file(path, index, seen)
        for path in base.rglob("*.yml"):
            _load_file(path, index, seen)

    return index


def _load_file(path: Path, index: ResourceIndex, seen: set[str]) -> None:
    for doc in load_yaml_docs(path):
        kind = (doc.get("kind") or "").strip()
        api = (doc.get("apiVersion") or "").lower()
        if not kind:
            continue
        meta = doc.get("metadata") or {}
        namespace = meta.get("namespace", "default")
        name = meta.get("name") or ""
        uid = f"{kind}/{namespace}/{name}"
        if uid in seen:
            continue
        seen.add(uid)

        if kind == "Deployment":
            w = _parse_workload("Deployment", doc)
            if w:
                index.add_workload(w)
        elif kind == "StatefulSet":
            w = _parse_workload("StatefulSet", doc)
            if w:
                index.add_workload(w)
        elif kind == "DaemonSet":
            w = _parse_workload("DaemonSet", doc)
            if w:
                index.add_workload(w)
        elif kind == "Service":
            s = _parse_service(doc)
            if s:
                index.add_service(s)
        elif kind == "Ingress":
            i = _parse_ingress(doc)
            if i:
                index.add_ingress(i)
        elif kind == "Gateway" and "istio" in api:
            g = _parse_gateway(doc)
            if g:
                index.add_gateway(g)
        elif kind == "VirtualService" and "istio" in api:
            vs = _parse_virtual_service(doc)
            if vs:
                index.add_virtual_service(vs)
        elif kind == "ServiceEntry" and "istio" in api:
            se = _parse_service_entry(doc)
            if se:
                index.add_service_entry(se)
        elif kind == "DestinationRule" and "istio" in api:
            dr = _parse_destination_rule(doc)
            if dr:
                index.add_destination_rule(dr)
