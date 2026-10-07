import pytest

from app.domain import DomainValidationError, Person


def test_person_requires_name_and_email() -> None:
    with pytest.raises(DomainValidationError, match="name is required"):
        Person.create(name=" ", email="valid@example.org")

    with pytest.raises(DomainValidationError, match="email must be a valid address"):
        Person.create(name="Taylor Chen", email="not-an-email")
