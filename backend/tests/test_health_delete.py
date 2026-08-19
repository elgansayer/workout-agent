from health_delete import HEALTH_USER_TABLES, deletion_statements


def test_health_deletion_is_explicit_and_tenant_scoped():
    statements = deletion_statements(7)
    assert len(statements) == len(HEALTH_USER_TABLES)
    assert all("where user_id = ?" in sql.lower() for sql, _ in statements)
    assert all(params == (7,) for _, params in statements)
