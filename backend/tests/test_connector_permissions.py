from connectors.permissions import explain_permissions


def test_permissions_are_explained_by_product_purpose():
    purposes = explain_permissions(frozenset({"sleep", "heart_rate", "unknown"}))
    assert [item.capability for item in purposes] == ["heart_rate", "sleep"]
    assert all(item.explanation for item in purposes)
