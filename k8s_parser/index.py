"""Resource index for linking Kubernetes and Istio resources."""

from __future__ import annotations

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


def _labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """Check if selector matches labels (selector is subset of labels)."""
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


class ResourceIndex:
    """Index of all parsed resources with lookup helpers."""

    def __init__(self) -> None:
        self.workloads: list[Workload] = []
        self.services: list[Service] = []
        self.ingresses: list[Ingress] = []
        self.gateways: list[Gateway] = []
        self.virtual_services: list[VirtualService] = []
        self.service_entries: list[ServiceEntry] = []
        self.destination_rules: list[DestinationRule] = []

    def add_workload(self, w: Workload) -> None:
        self.workloads.append(w)

    def add_service(self, s: Service) -> None:
        self.services.append(s)

    def add_ingress(self, i: Ingress) -> None:
        self.ingresses.append(i)

    def add_gateway(self, g: Gateway) -> None:
        self.gateways.append(g)

    def add_virtual_service(self, vs: VirtualService) -> None:
        self.virtual_services.append(vs)

    def add_service_entry(self, se: ServiceEntry) -> None:
        self.service_entries.append(se)

    def add_destination_rule(self, dr: DestinationRule) -> None:
        self.destination_rules.append(dr)

    def service_by_name(self, name: str, namespace: str) -> Service | None:
        for s in self.services:
            if s.name == name and s.namespace == namespace:
                return s
        return None

    def service_by_dns(self, dns: str) -> Service | None:
        for s in self.services:
            if s.dns_name() == dns:
                return s
        for s in self.services:
            short = f"{s.name}.{s.namespace}"
            if dns == short or dns.startswith(short + "."):
                return s
        return None

    def workloads_for_service(self, svc: Service) -> list[Workload]:
        if not svc.selector:
            return []
        return [w for w in self.workloads if _labels_match(svc.selector, w.labels)]

    def gateway_by_name(self, name: str, namespace: str) -> Gateway | None:
        for g in self.gateways:
            if g.name == name and g.namespace == namespace:
                return g
        return None

    def virtual_services_for_gateway(self, gateway_name: str, namespace: str) -> list[VirtualService]:
        result: list[VirtualService] = []
        for vs in self.virtual_services:
            for gw in vs.gateways:
                if gw == gateway_name:
                    if vs.namespace == namespace:
                        result.append(vs)
                        break
        return result

    def service_entry_for_host(self, host: str) -> ServiceEntry | None:
        for se in self.service_entries:
            if host in se.hosts:
                return se
        return None

    def service_for_workload(self, w: Workload) -> Service | None:
        for s in self.services:
            if _labels_match(s.selector, w.labels) and s.namespace == w.namespace:
                return s
        return None

    def workload_for_gateway(self, g: Gateway) -> Workload | None:
        for w in self.workloads:
            if _labels_match(g.selector, w.labels) and w.namespace == g.namespace:
                return w
        return None

    def service_for_gateway_workload(self, g: Gateway) -> Service | None:
        w = self.workload_for_gateway(g)
        if w:
            return self.service_for_workload(w)
        return None

    def is_external_host(self, host: str) -> bool:
        host_l = host.lower()
        if host_l == "localhost" or host_l.startswith("localhost.") or host_l.startswith("127."):
            return False
        if host.endswith(".svc.cluster.local"):
            return False
        if self.service_by_dns(host):
            return False
        se = self.service_entry_for_host(host)
        if se:
            return se.is_external()
        if "." in host and not host.endswith(".cluster.local"):
            return True
        return False
