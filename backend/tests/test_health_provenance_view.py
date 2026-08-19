from health_models import SourceProvenance
from health_provenance_view import provenance_view


def test_provenance_view_exposes_source_not_connection_identity():
    view = provenance_view(SourceProvenance("health_connect", "secret-connection-id", source_app="Garmin Connect", data_origin="com.garmin.android.apps.connectmobile"))
    assert view.provider == "health_connect"
    assert view.source_app == "Garmin Connect"
    assert not hasattr(view, "connection_id")
