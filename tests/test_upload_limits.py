"""Every route that accepts a file is bounded by the upload-size middleware — found by
inspecting the routers, so a new upload route that is not added to
app/security/upload_limit.py fails here instead of shipping unbounded."""
import importlib
import inspect
import pkgutil
import re

from fastapi import UploadFile

import app.api.routes as routes_pkg
from app.security.upload_limit import limit_for


def _upload_routes():
    found = []
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        module = importlib.import_module(f"app.api.routes.{info.name}")
        router = getattr(module, "router", None)
        for route in getattr(router, "routes", []):
            params = inspect.signature(route.endpoint).parameters.values()
            if any(p.annotation in (UploadFile, "UploadFile") or "UploadFile" in str(p.annotation) for p in params):
                found.append((info.name, route.path))
    return found


def test_the_scan_finds_the_known_upload_routes():
    paths = {p for _, p in _upload_routes()}
    assert "/api/v1/associate/onboardings/{onboarding_id}/execution/files" in paths
    assert "/api/v1/dashboard/payments/{payment_id}/proof" in paths
    assert len(paths) >= 8


def test_every_upload_route_is_bounded():
    unbounded = []
    for module, path in _upload_routes():
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        if limit_for(concrete) is None:
            unbounded.append(f"{module}: {path}")
    assert unbounded == [], f"upload routes with no size bound: {unbounded}"


def test_other_routes_are_left_alone():
    assert limit_for("/api/v1/associate/onboardings/1/payments") is None
    assert limit_for("/api/v1/dashboard/payments/1/verify") is None
