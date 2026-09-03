from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from app.migrations import _schema_migration_connection, ensure_compatible_schema
from app.models import EvidenceConfig, Workspace


def test_mysql_schema_migration_uses_short_metadata_lock_timeout() -> None:
    engine = MagicMock()
    engine.dialect.name = "mysql"
    connection = engine.begin.return_value.__enter__.return_value

    with _schema_migration_connection(engine):
        pass

    connection.exec_driver_sql.assert_called_once_with(
        "SET SESSION lock_wait_timeout = 5"
    )


def test_mysql_schema_migration_explains_metadata_lock_timeout() -> None:
    engine = MagicMock()
    engine.dialect.name = "mysql"
    driver_error = RuntimeError(1205, "Lock wait timeout exceeded")
    error = OperationalError("ALTER TABLE review_jobs", {}, driver_error)

    with pytest.raises(RuntimeError, match="Stop the existing ALM Review Web and Worker"):
        with _schema_migration_connection(engine):
            raise error


def test_image_evidence_capabilities_default_to_disabled() -> None:
    config = EvidenceConfig()

    assert not config.external_evidence_review_enabled


def test_workspace_equipment_review_defaults_to_enabled() -> None:
    workspace = Workspace(name="Project", slug="project")

    assert workspace.equipment_review_enabled is None or workspace.equipment_review_enabled


def test_workspace_queue_controls_are_added_to_existing_schema() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE workspaces ("
                "id INTEGER PRIMARY KEY, "
                "name VARCHAR(255) NOT NULL UNIQUE, "
                "slug VARCHAR(128) NOT NULL UNIQUE, "
                "equipment_review_enabled BOOLEAN NOT NULL DEFAULT 1, "
                "equipment_area_filter VARCHAR(255) NOT NULL DEFAULT '', "
                "legacy_policy_adopted BOOLEAN NOT NULL DEFAULT 0, "
                "archived BOOLEAN NOT NULL DEFAULT 0, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO workspaces "
                "(id, name, slug, created_at) "
                "VALUES (1, 'Project', 'project', CURRENT_TIMESTAMP)"
            )
        )

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("workspaces")}
    assert {
        "review_queue_paused",
        "sync_queue_paused",
        "queue_priority",
    } <= columns
    assert "specialist_reviews_enabled" not in columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT review_queue_paused, sync_queue_paused, queue_priority "
                "FROM workspaces WHERE id = 1"
            )
        ).one()
    assert tuple(row) == (0, 0, 0)


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
        "external_evidence_review_enabled",
        "automation_release_project_name",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT external_evidence_review_enabled, "
                "automation_release_project_name "
                "FROM evidence_configs WHERE id = 1"
            )
        ).one()
    assert tuple(row) == (0, "")


def test_legacy_evidence_flags_enable_combined_external_review() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE evidence_configs ("
                "id INTEGER PRIMARY KEY, "
                "network_evidence_enabled BOOLEAN NOT NULL DEFAULT 0, "
                "image_review_enabled BOOLEAN NOT NULL DEFAULT 0"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO evidence_configs "
                "(id, network_evidence_enabled, image_review_enabled) "
                "VALUES (1, 1, 0)"
            )
        )

    ensure_compatible_schema(engine)

    with engine.connect() as connection:
        enabled = connection.scalar(
            text(
                "SELECT external_evidence_review_enabled "
                "FROM evidence_configs WHERE id = 1"
            )
        )
    assert enabled == 1


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


def test_equipment_id_becomes_optional_and_revision_is_added() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE equipment_registry ("
                "id INTEGER PRIMARY KEY, "
                "equipment_id VARCHAR(128) NOT NULL UNIQUE, "
                "serial_number VARCHAR(255) NOT NULL DEFAULT ''"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO equipment_registry (id, equipment_id, serial_number) "
                "VALUES (1, 'EQ-1', 'SN-1')"
            )
        )

    ensure_compatible_schema(engine)

    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("equipment_registry")
    }
    assert columns["equipment_id"]["nullable"]
    assert "revision" in columns
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO equipment_registry (id, equipment_id, serial_number, revision) "
                "VALUES (2, NULL, 'SN-2', 'A'), (3, NULL, 'SN-3', 'B')"
            )
        )


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


def test_pipeline_trace_column_is_added_to_existing_review_results() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE review_results ("
                "id INTEGER PRIMARY KEY, "
                "workspace_id INTEGER NULL, "
                "review_policy_key VARCHAR(64) NOT NULL DEFAULT ''"
                ")"
            )
        )

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("review_results")}
    assert "pipeline_json" in columns


def test_ai_review_settings_are_added_to_existing_config() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ai_configs ("
                "id INTEGER PRIMARY KEY, "
                "enabled BOOLEAN NOT NULL DEFAULT 0"
                ")"
            )
        )
        connection.execute(text("INSERT INTO ai_configs (id) VALUES (1)"))

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("ai_configs")}
    assert {
        "api_key",
        "review_concurrency",
        "health_status",
        "consecutive_failures",
        "cooldown_until",
        "last_error",
        "last_success_at",
        "last_failure_at",
    } <= columns
    with engine.connect() as connection:
        values = connection.execute(
            text(
                "SELECT api_key, review_concurrency, health_status, "
                "consecutive_failures FROM ai_configs WHERE id = 1"
            )
        ).one()
    assert values == ("", 1, "healthy", 0)


def test_worker_singleton_lease_table_is_created_by_compatible_schema() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    ensure_compatible_schema(engine)

    assert "worker_lease" in inspect(engine).get_table_names()


def test_sync_progress_columns_are_added_to_existing_sync_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sync_jobs ("
                "id INTEGER PRIMARY KEY, "
                "active_key VARCHAR(64) UNIQUE, "
                "status VARCHAR(32) NOT NULL, "
                "created_at DATETIME NOT NULL"
                ")"
            )
        )

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("sync_jobs")}
    assert {
        "progress_stage",
        "progress_message",
        "folders_discovered",
        "folders_processed",
        "test_sets_discovered",
        "runs_discovered",
    } <= columns


def test_existing_runs_and_configs_are_assigned_to_default_workspace() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE alm_runs ("
                "run_id BIGINT PRIMARY KEY, source_hash VARCHAR(64) NOT NULL"
                ")"
            )
        )
        connection.execute(
            text("INSERT INTO alm_runs (run_id, source_hash) VALUES (152459, 'hash')")
        )
        connection.execute(
            text(
                "CREATE TABLE sync_configs ("
                "id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL"
                ")"
            )
        )
        connection.execute(text("INSERT INTO sync_configs (id, name) VALUES (1, 'Testing')"))

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("alm_runs")}
    assert "execution_location" in columns
    sync_columns = {
        column["name"] for column in inspect(engine).get_columns("sync_configs")
    }
    assert "auto_review_after_sync" in sync_columns

    with engine.connect() as connection:
        workspace_id = connection.execute(
            text("SELECT id FROM workspaces WHERE slug = 'testing'")
        ).scalar_one()
        run = connection.execute(
            text("SELECT workspace_id, alm_run_id FROM alm_runs WHERE run_id = 152459")
        ).one()
        config_workspace_id = connection.execute(
            text("SELECT workspace_id FROM sync_configs WHERE id = 1")
        ).scalar_one()
        auto_review_after_sync = connection.execute(
            text("SELECT auto_review_after_sync FROM sync_configs WHERE id = 1")
        ).scalar_one()

    assert tuple(run) == (workspace_id, 152459)
    assert config_workspace_id == workspace_id
    assert not auto_review_after_sync
