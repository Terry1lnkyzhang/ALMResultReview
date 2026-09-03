from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiConfig
from app.services.ai_transport import (
    ai_endpoint_available,
    ai_endpoint_health_status,
    record_ai_endpoint_failure,
    record_ai_endpoint_success,
)
from app.services.skill_runner import SkillFailure


def test_ai_endpoint_health_cools_down_and_recovers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        config = AiConfig(id=1, enabled=True)
        db.add(config)
        db.commit()

        for _ in range(3):
            record_ai_endpoint_failure(
                db,
                config.id,
                SkillFailure("HTTP 503", retryable=True),
            )

        assert config.health_status == "cooldown"
        assert config.cooldown_until is not None
        assert ai_endpoint_available(config) is False

        config.cooldown_until = config.last_failure_at
        assert ai_endpoint_available(config) is True
        assert ai_endpoint_health_status(config) == "recovering"

        record_ai_endpoint_success(db, config.id)

        assert config.health_status == "healthy"
        assert config.consecutive_failures == 0
        assert config.cooldown_until is None
        assert ai_endpoint_available(config) is True


def test_ai_endpoint_authentication_failure_blocks_immediately() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        config = AiConfig(id=1, enabled=True)
        db.add(config)
        db.commit()

        record_ai_endpoint_failure(
            db,
            config.id,
            SkillFailure("HTTP 401 Unauthorized", retryable=False),
        )

        assert config.health_status == "auth_error"
        assert ai_endpoint_available(config) is False


def test_internal_failure_does_not_change_endpoint_health() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        config = AiConfig(id=1, enabled=True)
        db.add(config)
        db.commit()

        record_ai_endpoint_failure(db, config.id, ValueError("internal validation"))

        assert config.health_status == "healthy"
        assert config.consecutive_failures == 0
