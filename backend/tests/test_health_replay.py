from health_replay import ReplayStore


def test_replay_store_claims_batch_only_once():
    store = ReplayStore()
    assert store.claim("1:pixel:batch")
    assert not store.claim("1:pixel:batch")
