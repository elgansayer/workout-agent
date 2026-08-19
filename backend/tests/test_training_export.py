import pytest

from training_export import ExportedWorkout


def workout(user=1, approved=True):
    return ExportedWorkout(user, "garmin_training", "Pull day", "programme-1", approved)


def test_export_requires_same_authenticated_user():
    workout().validate(authenticated_user_id=1)
    with pytest.raises(ValueError, match="another user"):
        workout().validate(authenticated_user_id=2)


def test_export_requires_explicit_approval():
    with pytest.raises(ValueError, match="explicit"):
        workout(approved=False).validate(authenticated_user_id=1)
