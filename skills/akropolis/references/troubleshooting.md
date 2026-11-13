# akropolis troubleshooting

Start every investigation with:
1. `akro status CONFIG` to find the failed phase and its recorded error.
2. The newest `<site>.*.transcript.log` next to the state file.
3. On HA, `akro logs config.<site>.monitor.yml --last 2 --level warning`.
4. When a health gate expires, akropolis prints the container log tail. Read
   it before anything else.

| Symptom | Cause | Fix |
|---|---|---|
| `load()` refuses the config with a version mismatch | `site.config_version` differs from what the release expects | Read CHANGELOG.md for the schema change, update the file, and bump the version. Don't just edit the number. |
| Patroni never promotes; the phase stops before replicas | Leader can't initdb or reach etcd | Check etcd health (`etcdctl endpoint health`), pg_hba and the patroni journal on the leader. Fix, then `--replay patroni`. |
| Replication stalls or inter-node ping fails at MTU | VXLAN overhead | Set `network.expected_mtu` to the real value (1400 on VXLAN) and fix the interface MTU. |
| HAProxy crash-loops with "Permission denied" | Config pushed as 0600 by an old version | Any current run repairs the mode; `--replay haproxy`. |
| Every Authentik node looks alive but 9000/9443 don't answer | Default listen ports clash with HAProxy, or the worker squats the server's ports | Current templates move them. If someone edited `.env`, re-render with `--replay authentik`. |
| Login UI broken under `tls.provider: none` | Expected limits: no secure context, so no WebAuthn | Use `none` only for local testing; production refuses it. |
| ACME cert still "STAGING" after setting `staging: false` | certbot didn't reissue a still-valid lineage | Current releases detect the issuer mismatch and force renewal. Otherwise set `acme.force_renewal: true` and replay the TLS/nginx phase. |
| ACME issuance fails | DNS doesn't reach the VIP or node, or port 80 is blocked upstream | The placeholder cert stays in place. Fix DNS, NAT or firewall, then replay. akropolis can't see NAT hops. |
| Restore aborts before DROP with GUC errors | Dump from a newer `pg_dump` | Known lines are stripped automatically; anything else means using a matching `pg_dump` version. |
| `docker compose up` fails "dependency failed to start" after a restore | Worker migrating real data outlasted compose's wait | The phase starts the worker alone. Raise `restore.migration_timeout` for very large databases. |
| Worker crash-loops `permission denied for table ...` after a restore | Objects owned by postgres, not the app role | Fixed by current releases. Re-run the restore with a current akro. |
| Branding files deployed but the login page is unchanged | Brand record not pointing at assets | akropolis PATCHes the default brand. If that warned, set it in System → Brands. Email logo needs a PNG. |
| Monitor shows nginx down or keepalived FAULT on a healthy cluster | `verify_tls: true` in the monitor config, a missing UFW rule for `:8080` from the monitor host, or a scheme mismatch under `tls: none` | Set `verify_tls: false`, `--replay base` with `monitor.ips`, and match the scheme to `tls.provider`. From the monitor host: `curl -s --max-time 5 http://<node>:8080/nginx_status` (rc 28 means a firewall drop, rc 7 means nothing is listening). |
| Monitor shows `_` instead of `●` | Terminal without a UTF-8 locale | Set `unicode_bullets: false` in the monitor config. |
| `akro logs`: `cannot read logs: permission denied ... docker.sock` | The SSH user in the monitor config can't reach Docker on that node | Add it to the `docker` group (`sudo usermod -aG docker <user>`, reconnect). That group is root-equivalent, so the operator decides. Releases before this fix showed such a service as **clean**: on an older `akro`, a clean result proves nothing until `--level debug` shows real log lines. |
| `akro logs`: `cannot read logs: no such object` | Container name in the monitor config's `services:` doesn't exist on the node | Compare with `docker ps --format '{{.Names}}'` on the node and fix the monitor config. |
| `akro logs` at warning level shows `"level": "info"` lines from `gunicorn.error` | The level filter matches words anywhere in the line, including logger names | Noise, not warnings: gunicorn recycling its workers. Read the JSON `level` field. |
| Sudo fails mid-phase | Password prompt or timeout | Use passwordless sudo or `auth: agent`. Re-run; phases are resumable. |
| LDAP users' groups are frozen; sync errors on existing usernames | entryUUID reissued outside Authentik | `akro ldap-reconcile CONFIG`. |
| Embedded outpost restart loop on HA after an upgrade | Known multi-node bug in some Authentik versions | Roll back to the pinned tag from a backup, and check NOTES.md before upgrading HA. |
