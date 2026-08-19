from health_connector_config import ProviderAppConfig, UserConnectorCredentialRef


def test_provider_app_and_user_credentials_are_separate_models():
    app = ProviderAppConfig("oura", "client-id", "secret-store:oura")
    user = UserConnectorCredentialRef(7, "oura", "credential-123")
    assert app.provider == user.provider
    assert not hasattr(user, "client_secret")
