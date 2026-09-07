"""Marker file — do not delete.

Without it, `akropolis/templates/` is an implicit namespace package (PEP 420),
and `importlib.resources.files("akropolis.templates")` resolves to a
MultiplexedPath. That works when the package is an ordinary directory on disk,
but raises

    NotADirectoryError: MultiplexedPath only supports directories

as soon as the package lives inside a zip — which is exactly what the
single-file build in tools/build_pyz.sh produces. Every template read goes
through resources.files() (remote.py, haproxy_phase, nginx_keepalived_phase,
patroni_phase), so the failure is at import time and total.

Making it a regular package gives it a real loader with a resource reader that
zipimport implements.
"""
