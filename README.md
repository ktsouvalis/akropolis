# akropolis

Provision and monitor a 3-node Authentik HA cluster
(PostgreSQL/Patroni + etcd + HAProxy + nginx + keepalived), following the
UoP Digital Governance Unit implementation guide.

## Quick start

```bash
pip install -e .
akropolis init                        # wizard → config.<site>.yml
akropolis provision config.<site>.yml # resumable phase pipeline
akropolis provision config.<site>.yml --only preflight   # read-only, safe anywhere
```

## Status (v0.1)

| phase            | status |
|------------------|--------|
| preflight        | ✅ implemented (read-only) |
| base / etcd / patroni / haproxy | ✅ implemented (untested against real hosts yet) |
| tls (none / self_signed / acme-staging / import) | ✅ implemented, tested |
| acme finalization / nginx-keepalived / authentik / handoff | 🚧 stubs |

TLS providers planned: `none` (testing only, refused in production),
`self_signed`, `acme` (any directory URL — LE, HARICA ACME, ...), `import`
(externally issued, e.g. HARICA portal — validated, distributed, expiry handed
to the monitor).

Secrets are never stored in the site config. State lives in `.state/<site>.json`
(git-ignore it; contains pinned generated values).
