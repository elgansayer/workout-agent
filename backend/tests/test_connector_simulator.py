import pytest

from connectors.simulator import SimulatedPage, SyncSimulator


def test_simulator_models_incremental_pagination():
    simulator = SyncSimulator(
        {
            None: SimulatedPage(({"id": "1"},), next_cursor="next"),
            "next": SimulatedPage(({"id": "2"},)),
        }
    )
    first = simulator.fetch()
    second = simulator.fetch(first.next_cursor)
    assert [record["id"] for record in (*first.records, *second.records)] == ["1", "2"]
    assert simulator.calls == [None, "next"]


def test_simulator_models_provider_failure():
    simulator = SyncSimulator({None: SimulatedPage((), error_code="rate_limited")})
    with pytest.raises(RuntimeError, match="rate_limited"):
        simulator.fetch()
