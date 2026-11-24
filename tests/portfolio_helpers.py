"""Explicit singleton helpers for pre-portfolio regression scenarios only."""
def single_phv(state):
    values = state.get("hypothesis_verifications", ())
    assert len(values) <= 1, "test requires an explicitly single-HS scenario"
    return values[0] if values else None


def subject(state):
    value = single_phv(state)
    return value.hypothesis_spec if value else None
