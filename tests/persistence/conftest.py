from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.domain import (
    Evidence,
    EvidenceType,
    Person,
    Requirement,
    RequirementProvenance,
    Workflow,
    WorkflowType,
)
from app.persistence import (
    Base,
    WorkflowRepository,
    create_database_engine,
    create_session_factory,
)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'commitmentops-test.db'}"


@pytest.fixture
def repository(database_url: str) -> Iterator[WorkflowRepository]:
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    yield WorkflowRepository(create_session_factory(engine))
    engine.dispose()


@pytest.fixture
def persisted_workflow() -> Workflow:
    owner = Person.create(
        name="Alex Rivera",
        email="alex.rivera@example.org",
        role="Program Manager",
    )
    supervisor = Person.create(
        name="Jordan Lee",
        email="jordan.lee@example.org",
        role="Director",
    )
    reviewer = Person.create(
        name="Sam Okonkwo",
        email="sam.okonkwo@example.org",
        role="Compliance Officer",
    )
    requirement = Requirement.create(
        title="Submit quarterly progress report",
        description="File the Q3 narrative and budget-to-actuals.",
        owner=owner,
        supervisor=supervisor,
        evidence_expectation="Signed report and participant records",
        provenance=RequirementProvenance.AI_INFERRED,
        due_date=date(2026, 12, 15),
        target="50 participants",
    )
    requirement.approve(
        reviewer=reviewer,
        note="Confirmed against the reporting clause.",
    )
    requirement.start_work()
    requirement.add_evidence(
        Evidence.create(
            description="Q3 participant register",
            evidence_type=EvidenceType.DOCUMENT,
            submitted_by=owner,
            reference="evidence/q3-participants.pdf",
            submitted_at=datetime(2026, 10, 9, 14, 0, tzinfo=UTC),
        )
    )

    workflow = Workflow.create(
        name="Synthetic Community Grant",
        workflow_type=WorkflowType.GRANT,
    )
    workflow.add_requirement(requirement)
    workflow.activate()
    return workflow
