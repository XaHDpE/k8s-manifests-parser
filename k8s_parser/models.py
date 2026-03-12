"""Internal model of Kubernetes and Istio resources for flow analysis."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Workload:
    """Deployment, StatefulSet, DaemonSet, or Job/CronJob."""

    kind: str
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    selector: dict[str, str] = field(default_factory=dict)
    container_ports: list[int] = field(default_factory=list)
    named_ports: dict[str, int] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


@dataclass
class Service:
    """Kubernetes Service."""

    name: str
    namespace: str
    selector: dict[str, str] = field(default_factory=dict)
    ports: list[dict[str, Any]] = field(default_factory=list)

    def dns_name(self) -> str:
        return f"{self.name}.{self.namespace}.svc.cluster.local"

    def short_name(self) -> str:
        return f"{self.name}.{self.namespace}" if self.namespace else self.name

    def key(self) -> str:
        return f"Service/{self.namespace}/{self.name}"


@dataclass
class Ingress:
    """Kubernetes Ingress."""

    name: str
    namespace: str
    rules: list[dict[str, Any]] = field(default_factory=list)
    backend: dict[str, Any] | None = None
    tls_hosts: list[str] = field(default_factory=list)

    def key(self) -> str:
        return f"Ingress/{self.namespace}/{self.name}"


@dataclass
class Gateway:
    """Istio Gateway."""

    name: str
    namespace: str
    selector: dict[str, str] = field(default_factory=dict)
    servers: list[dict[str, Any]] = field(default_factory=list)

    def key(self) -> str:
        return f"Gateway/{self.namespace}/{self.name}"


@dataclass
class VirtualServiceRoute:
    """Single route destination in VirtualService."""

    host: str
    port: int | None
    subset: str | None = None
    gateways: list[str] = field(default_factory=list)


@dataclass
class VirtualService:
    """Istio VirtualService."""

    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    http_routes: list[list[VirtualServiceRoute]] = field(default_factory=list)
    tcp_routes: list[list[VirtualServiceRoute]] = field(default_factory=list)
    tls_routes: list[list[VirtualServiceRoute]] = field(default_factory=list)

    def key(self) -> str:
        return f"VirtualService/{self.namespace}/{self.name}"


@dataclass
class ServiceEntry:
    """Istio ServiceEntry."""

    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)
    location: str = "MESH_EXTERNAL"
    ports: list[dict[str, Any]] = field(default_factory=list)
    endpoints: list[dict[str, Any]] = field(default_factory=list)

    def is_external(self) -> bool:
        return self.location in ("MESH_EXTERNAL", "MESH_LOCAL")

    def key(self) -> str:
        return f"ServiceEntry/{self.namespace}/{self.name}"


@dataclass
class DestinationRule:
    """Istio DestinationRule."""

    name: str
    namespace: str
    host: str
    workload_selector: dict[str, str] = field(default_factory=dict)

    def key(self) -> str:
        return f"DestinationRule/{self.namespace}/{self.name}"


@dataclass
class Flow:
    """A single network flow with source, destination, and hop chain."""

    source: str
    destination: str
    hops: list[str]
    protocol: str = ""
    port: int | None = None
    ports: dict[str, int] = field(default_factory=dict)
