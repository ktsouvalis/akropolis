"""ldap-reconcile — repoint Authentik LDAP source identifiers after an
out-of-band entryUUID change.

Authentik's LDAP source sync keys every user off a per-source
`object_uniqueness_field` (typically `entryUUID`), stored as `identifier` on
`UserLDAPSourceConnection`. If something outside Authentik reissues that
value for an existing account (same `uid`, new `entryUUID`) while the
username stays put, Authentik's own unique-username constraint blocks it
from re-linking automatically: the sync for that entry errors or skips every
cycle, and the account's group memberships/attributes silently freeze at
whatever they were before the change.

The fix is narrow and reversible: leave the LDAP entry and the Authentik
User alone, and just update the stored `identifier` to match what's live in
LDAP, so the next sync recognizes the account again. This command finds
every drifted user across however many LDAP sources are configured (not a
hand-maintained list) and applies confirmed fixes one user at a time.

Runs entirely inside the `worker` container via `ak shell`, the same way
authentik_phase.py's `remint_bootstrap_token` re-mints the bootstrap token:
a plain Python script, values embedded with `repr()`, executed through
`docker compose exec -T worker ak shell -c ...`. This deliberately never
binds to LDAP from the workstation and never prompts for an LDAP password —
`source.connection()` inside Authentik reuses the bind credentials Authentik
already has stored for that source, the same shortcut a manual `ak shell`
fix would use.

Two correlating assumptions, worth knowing if this ever needs adjusting for
a different environment: usernames are correlated via the LDAP `uid`
attribute (matching Authentik's default LDAP username mapping), and the
uniqueness attribute is treated as a plain string — a source whose
`object_uniqueness_field` is byte-valued (e.g. Active Directory's
`ms-DS-ConsistencyGuid`) isn't handled specially here.
"""

from __future__ import annotations

import json
import shlex

from rich.console import Console
from rich.table import Table

from .base import PhaseContext

console = Console()

_MARKER = "AKROPOLIS_JSON:"


def _dry_run_script(source_slug: str | None, usernames: list[str] | None = None) -> str:
    """Read-only: for each LDAP source, one live LDAP search (uid + the
    source's own object_uniqueness_field) compared against every
    UserLDAPSourceConnection row already on file for that source."""
    return (
        "import json\n"
        "from authentik.sources.ldap.models import LDAPSource, UserLDAPSourceConnection\n"
        f"SOURCE_SLUG = {source_slug!r}\n"
        f"USERNAMES = {usernames!r}\n"
        "rows = []\n"
        "errors = []\n"
        "sources = LDAPSource.objects.all()\n"
        "if SOURCE_SLUG:\n"
        "    sources = sources.filter(slug=SOURCE_SLUG)\n"
        "for source in sources:\n"
        "    try:\n"
        "        uniq_field = source.object_uniqueness_field\n"
        "        base = source.base_dn\n"
        "        if getattr(source, 'additional_user_dn', ''):\n"
        "            base = source.additional_user_dn + ',' + base\n"
        "        filt = source.user_object_filter or '(objectClass=person)'\n"
        "        if not filt.startswith('('):\n"
        "            filt = '(' + filt + ')'\n"
        "        search_filter = '(&' + filt + '(uid=*))'\n"
        "        conn = source.connection()\n"
        "        conn.search(base, search_filter, attributes=['uid', uniq_field])\n"
        "        live = {}\n"
        "        for entry in conn.entries:\n"
        "            attrs = entry.entry_attributes_as_dict\n"
        "            uids = attrs.get('uid') or []\n"
        "            uniqs = attrs.get(uniq_field) or []\n"
        "            if uids and uniqs:\n"
        "                live[str(uids[0])] = str(uniqs[0])\n"
        "    except Exception as e:\n"
        "        errors.append({'source': source.slug, 'detail': str(e)})\n"
        "        continue\n"
        "    conns = (UserLDAPSourceConnection.objects.filter(source=source)\n"
        "             .select_related('user'))\n"
        "    if USERNAMES:\n"
        "        conns = conns.filter(user__username__in=USERNAMES)\n"
        "    for lsc in conns:\n"
        "        uname = lsc.user.username\n"
        "        stored = lsc.identifier\n"
        "        live_val = live.get(uname)\n"
        "        if live_val is None:\n"
        "            status = 'NOT_FOUND_IN_LDAP'\n"
        "        elif live_val == stored:\n"
        "            status = 'SAME'\n"
        "        else:\n"
        "            status = 'DIFFERENT'\n"
        "        rows.append({'source': source.slug, 'username': uname, 'stored': stored,\n"
        "                     'live': live_val, 'status': status})\n"
        f"print({_MARKER!r} + json.dumps({{'rows': rows, 'errors': errors}}))\n"
    )


def _apply_script(changes: list[dict]) -> str:
    """Compare-and-swap: only overwrites `identifier` if it still equals what
    the plan step last saw, so a real sync or a concurrent fix landing
    between plan and apply is skipped rather than clobbered."""
    payload = [(c["source"], c["username"], c["stored"], c["live"]) for c in changes]
    return (
        "import json\n"
        "from authentik.sources.ldap.models import UserLDAPSourceConnection\n"
        f"CHANGES = {payload!r}\n"
        "results = []\n"
        "for source_slug, username, old, new in CHANGES:\n"
        "    lsc = UserLDAPSourceConnection.objects.filter(\n"
        "        source__slug=source_slug, user__username=username).first()\n"
        "    if lsc is None:\n"
        "        results.append({'source': source_slug, 'username': username,\n"
        "                        'status': 'SKIPPED_NO_ROW'})\n"
        "        continue\n"
        "    if lsc.identifier != old:\n"
        "        results.append({'source': source_slug, 'username': username,\n"
        "                        'status': 'SKIPPED_CHANGED', 'expected': old,\n"
        "                        'found': lsc.identifier})\n"
        "        continue\n"
        "    lsc.identifier = new\n"
        "    lsc.save()\n"
        "    results.append({'source': source_slug, 'username': username,\n"
        "                    'status': 'UPDATED', 'old': old, 'new': new})\n"
        f"print({_MARKER!r} + json.dumps(results))\n"
    )


def _run_script(ctx: PhaseContext, conn, py: str, label: str, timeout: int = 120):
    ctx.begin(conn.node.name, label, "ak shell")
    r = conn.run(
        f"cd /opt/authentik && docker compose exec -T worker ak shell -c {shlex.quote(py)}",
        timeout=timeout,
    )
    ctx.end_status()
    if not r.ok:
        raise RuntimeError(f"{label} failed on {conn.node.name}: {r.err or r.out}")
    line = next((l for l in r.out.splitlines() if l.startswith(_MARKER)), None)
    if line is None:
        raise RuntimeError(
            f"{label}: no {_MARKER} output from {conn.node.name} "
            f"(ak shell output: {r.out[-2000:]!r})"
        )
    return json.loads(line[len(_MARKER):])


def collect_drift(ctx: PhaseContext, conn, source_slug: str | None = None,
                  usernames: list[str] | None = None) -> list[dict]:
    data = _run_script(ctx, conn, _dry_run_script(source_slug, usernames),
                       "reading LDAP source drift")
    for err in data.get("errors", []):
        console.print(f"[yellow]⚠ source {err['source']}: {err['detail']}[/yellow]")
    return data.get("rows", [])


def apply_changes(ctx: PhaseContext, conn, changes: list[dict]) -> list[dict]:
    if not changes:
        return []
    return _run_script(ctx, conn, _apply_script(changes), "repointing confirmed identifiers")


_STATUS_STYLE = {"SAME": "dim", "DIFFERENT": "yellow", "NOT_FOUND_IN_LDAP": "red"}


def _print_table(rows: list[dict]) -> None:
    table = Table(title="LDAP identifier check")
    for col in ("source", "username", "stored", "live", "status"):
        table.add_column(col)
    for row in rows:
        style = _STATUS_STYLE.get(row["status"])
        status = f"[{style}]{row['status']}[/{style}]" if style else row["status"]
        table.add_row(row["source"], row["username"], row["stored"] or "",
                     row["live"] or "", status)
    console.print(table)


def run(ctx: PhaseContext, source: str | None = None) -> int:
    conn = next((c for c in ctx.fleet if c.node.name == ctx.cfg.bootstrap_leader.name), None)
    if conn is None:
        console.print("[red]bootstrap leader not found in fleet[/red]")
        return 2

    rows = collect_drift(ctx, conn, source)
    if not rows:
        console.print("[green]no LDAP-linked users found — nothing to check.[/green]")
        return 0

    same = sum(1 for r in rows if r["status"] == "SAME")
    flagged = [r for r in rows if r["status"] != "SAME"]
    console.print(f"[dim]{len(rows)} checked, {same} SAME (not shown)[/dim]")

    if not flagged:
        console.print("[green]nothing to reconcile.[/green]")
        return 0

    _print_table(flagged)

    drifted = [r for r in flagged if r["status"] == "DIFFERENT"]
    if not drifted:
        console.print("[yellow]nothing DIFFERENT to reconcile — the row(s) above are "
                      "NOT_FOUND_IN_LDAP, which this command doesn't act on "
                      "(deprovisioning is a separate decision).[/yellow]")
        return 0

    confirmed = []
    for row in drifted:
        console.print(f"\n[bold]{row['username']}[/bold] (source: {row['source']})")
        console.print(f"  stored: {row['stored']}")
        console.print(f"  live:   {row['live']}")
        answer = input("  repoint this user's identifier? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            confirmed.append(row)

    if not confirmed:
        console.print("[yellow]nothing confirmed — no changes made.[/yellow]")
        return 0

    results = apply_changes(ctx, conn, confirmed)
    ok = True
    for res in results:
        good = res["status"] == "UPDATED"
        detail = "" if good else json.dumps(res)
        ctx.record(conn.node.name, f"{res['username']}: {res['status']}", good, detail)
        ok = ok and good

    usernames = [r["username"] for r in confirmed]
    verify_rows = collect_drift(ctx, conn, source, usernames=usernames)
    for row in verify_rows:
        good = row["status"] == "SAME"
        ctx.record(conn.node.name, f"verify: {row['username']}", good, row["status"])
        ok = ok and good

    return 0 if ok else 1
