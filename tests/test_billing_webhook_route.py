import importlib
import os


def test_stripe_webhook_alias_route_registered():
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
    os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

    main_module = importlib.import_module("app.main")
    paths = {route.path for route in main_module.app.routes}

    assert "/api/webhooks/stripe" in paths
    assert "/billing/webhook" in paths
