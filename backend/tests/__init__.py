"""Backend tests."""
"""Backend test package configuration."""

import os

# Unit/API tests must never spend model allowance or depend on external network access.
os.environ.setdefault("WAYPOINT_PROVIDER_STARTUP_PROBE", "0")
