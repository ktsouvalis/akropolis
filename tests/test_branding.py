"""Tests for the email-logo bind-mount derived from authentik.branding.logo."""

from __future__ import annotations

from akropolis import remote
from akropolis.phases.authentik_phase import EMAIL_LOGO_PATH, email_logo_volume


class RecordingCtx:
    def __init__(self):
        self.records = []

    def record(self, node, label, ok, detail="", warn=False):
        self.records.append((node, label, ok, detail, warn))


def test_email_logo_path_matches_authentik():
    # authentik/stages/email/utils.py:logo_data(), relative to WORKDIR /
    assert EMAIL_LOGO_PATH == "/web/dist/assets/icons/icon_left_brand.png"


def test_png_logo_is_mounted_read_only_over_email_logo():
    ctx = RecordingCtx()
    vols = email_logo_volume(ctx, "node1", "/opt/authentik/branding/icons/Logo.PNG")
    assert vols == ["/opt/authentik/branding/icons/Logo.PNG:"
                    "/web/dist/assets/icons/icon_left_brand.png:ro"]
    assert ctx.records == []


def test_non_png_logo_is_skipped_with_warning():
    ctx = RecordingCtx()
    assert email_logo_volume(ctx, "node1", "/opt/authentik/branding/icons/logo.svg") == []
    (node, _label, ok, detail, warn), = ctx.records
    assert (node, ok, warn) == ("node1", False, True)
    assert "logo.svg" in detail


def test_worker_volumes_rendered_in_both_compose_templates():
    vol = f"/opt/authentik/branding/icons/logo.png:{EMAIL_LOGO_PATH}:ro"
    ha = remote.render("authentik-compose.yml.j2",
                       extra_server_volumes=[], extra_worker_volumes=[vol])
    single = remote.render("authentik-single-compose.yml.j2",
                           extra_server_volumes=[], extra_worker_volumes=[vol],
                           publish_pg_port=False, pg_loopback=False, pg_port=5432)
    for out in (ha, single):
        worker = out.split("\n  worker:\n", 1)[1]
        assert f"      - {vol}\n" in worker
        assert vol not in out.split("\n  worker:\n", 1)[0]
