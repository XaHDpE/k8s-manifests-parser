# k8s-config-parser

Parse Kubernetes and Istio YAML manifests and output network flow interactions: inbound (outside → app), outbound (app → outside), and internal (app → app).

## Usage

```bash
# JSON output (default)
python main.py data/in

# Human-readable text
python main.py data/in -f text

# Compact outbound grouping
python main.py data/in -f text --compact

# Full config-kind trace (Gateway/VirtualService/ServiceEntry)
python main.py data/in -f text --compact --trace-config

# Multiple input directories
python main.py data/in path/to/other/manifests
```

## Output

- **inbound**: External traffic into the app (Ingress → Gateway → VirtualService → Service → Workload)
- **outbound**: App traffic to external services (`Application` → egress `Service` → `Gateway` → `VirtualService` → `ServiceEntry` → external). Source is reported at app level unless manifests explicitly identify a workload.
- **internal**: Strictly explicit internal calls only. Current sample manifests do not provide enough data to attribute internal sources, so this section may be empty.
- **compact mode** (`--compact`): groups outbound flows with same destination/route and lists all sources in one record
- **default `via`**: infrastructure pods/workloads that process traffic
- **`--trace-config`**: full trace by config kinds (`Service`, `Gateway`, `VirtualService`, `ServiceEntry`, etc.)
- Ports are shown only at hop level: inline in text output and as `hop_details` / `via_details` in JSON. Client ephemeral source ports are not inferred.
