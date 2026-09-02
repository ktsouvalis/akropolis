"""etcd — guide Step 2: 3-node RAFT cluster in Docker with network_mode: host.

The initial-cluster token is generated once and pinned in the state file, so a
re-run can never re-bootstrap a formed cluster. Config changes are detected by
checksum and applied with `down && up -d` (never `restart` — bind-mount inode
trap, guide learnings). All three nodes are started back-to-back, well inside
the ~30-second window the guide requires.
"""

from __future__ import annotations

import secrets

from ..remote import push_file, render, wait_for
from .base import Phase, PhaseContext


class EtcdPhase(Phase):
    name = "etcd"

    def _token(self, ctx: PhaseContext) -> str:
        return ctx.state.get_or_generate(
            "etcd_initial_cluster_token",
            lambda: f"{ctx.cfg.name}-etcd-{secrets.token_hex(4)}",
        )

    def plan(self, ctx: PhaseContext) -> list[str]:
        return [
            "create /opt/etcd/{data,config} (data chmod 700) on all nodes",
            "render /opt/etcd/docker-compose.yml per node "
            f"(image etcd v3.5.30, cluster token pinned in state: {self._token(ctx)!r})",
            "docker compose up -d on all 3 nodes back-to-back "
            "(down && up when the rendered file changed)",
            "verify: endpoint health OK on all 3 client URLs, member list has 3 members",
        ]

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        token = self._token(ctx)

        for conn in ctx.fleet:
            node = conn.node.name
            r = conn.run("mkdir -p /opt/etcd/data /opt/etcd/config && chmod 700 /opt/etcd/data")
            ctx.record(node, "directories", r.ok, r.err if not r.ok else "")

            content = render("etcd-compose.yml.j2",
                             node=conn.node, nodes=cfg.nodes,
                             cluster_token=token, cluster_state="new")
            changed = push_file(conn, content, "/opt/etcd/docker-compose.yml")
            running = conn.run("docker ps --format '{{.Names}}' | grep -qx etcd").ok
            if changed and running:
                r = conn.run("cd /opt/etcd && docker compose down && docker compose up -d",
                             timeout=300)
                ctx.record(node, "compose down && up (config changed)", r.ok,
                           r.err if not r.ok else "")
            else:
                r = conn.run("cd /opt/etcd && docker compose up -d", timeout=300)
                ctx.record(node, "compose up -d", r.ok, r.err if not r.ok else "")

    def verify(self, ctx: PhaseContext) -> bool:
        cfg = ctx.cfg
        endpoints = ",".join(f"http://{n.ip}:2379" for n in cfg.nodes)
        first = ctx.fleet.conns[0]

        healthy = wait_for(
            first,
            f"docker exec etcd etcdctl --endpoints={endpoints} endpoint health 2>&1",
            timeout=90, interval=5,
        )
        detail = ""
        if healthy:
            r = first.run(f"docker exec etcd etcdctl --endpoints={endpoints} endpoint health 2>&1")
            unhealthy = [ln for ln in r.out.splitlines() if "is healthy" not in ln]
            healthy = not unhealthy
            detail = "; ".join(unhealthy) if unhealthy else "all 3 endpoints healthy"
        ctx.record("cluster", "etcd endpoint health", healthy, detail or "timed out waiting")

        r = first.run(f"docker exec etcd etcdctl --endpoints={endpoints} member list 2>&1")
        members = len([ln for ln in r.out.splitlines() if "started" in ln])
        ctx.record("cluster", "etcd members started", members == 3, f"{members}/3")

        return healthy and members == 3
