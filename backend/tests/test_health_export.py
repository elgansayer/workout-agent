from datetime import datetime, timezone

import pytest

from health_export import HealthExport


def test_health_export_is_tenant_bound_and_versioned():
    export = HealthExport(1, datetime(2026, 8, 19, tzinfo=timezone.utc), 1, {"metrics": []})
    export.validate(authenticated_user_id=1)
    with pytest.raises(ValueError, match="another user"):
        export.validate(authenticated_user_id=2)
