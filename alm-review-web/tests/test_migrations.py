from sqlalchemy import create_engine, inspect, text

from app.migrations import ensure_compatible_schema
from app.models import EvidenceConfig


def test_image_evidence_capabilities_default_to_disabled() -> None:
    config = EvidenceConfig()

    assert config.network_evidence_enabled is None or not config.network_evidence_enabled
    assert config.image_review_enabled is None or not config.image_review_enabled
    assert (
        config.allow_insecure_image_transport is None
        or not config.allow_insecure_image_transport
    )


def test_evidence_capability_flags_are_added_to_existing_schema() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE evidence_configs ("
                "id INTEGER PRIMARY KEY, "
                "allowed_network_root TEXT NOT NULL DEFAULT '', "
                "local_html_fallback_root TEXT NOT NULL DEFAULT ''"
                ")"
            )
        )
        connection.execute(text("INSERT INTO evidence_configs (id) VALUES (1)"))

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("evidence_configs")}
    assert {
        "network_evidence_enabled",
        "image_review_enabled",
        "allow_insecure_image_transport",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT network_evidence_enabled, image_review_enabled, "
                "allow_insecure_image_transport FROM evidence_configs WHERE id = 1"
            )
        ).one()
    assert tuple(row) == (0, 0, 0)


def test_equipment_serial_number_index_is_added_to_existing_schema() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE equipment_registry ("
                "id INTEGER PRIMARY KEY, "
                "serial_number VARCHAR(255) NOT NULL DEFAULT ''"
                ")"
            )
        )

    ensure_compatible_schema(engine)

    indexes = {index["name"] for index in inspect(engine).get_indexes("equipment_registry")}
    assert "ix_equipment_registry_serial_number" in indexes


def test_worker_claim_columns_are_added_to_existing_review_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE review_jobs ("
                "id INTEGER PRIMARY KEY, "
                "batch_id VARCHAR(36), "
                "status VARCHAR(32) NOT NULL, "
                "created_at DATETIME NOT NULL"
                ")"
            )
        )

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("review_jobs")}
    assert {"claimed_by", "lease_expires_at"} <= columns
    indexes = {index["name"] for index in inspect(engine).get_indexes("review_jobs")}
    assert {
        "ix_review_jobs_claimed_by",
        "ix_review_jobs_lease_expires_at",
    } <= indexes
