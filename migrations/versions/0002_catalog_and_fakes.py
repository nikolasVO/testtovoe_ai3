"""Seed the required catalog; persist fake jobs to survive process restarts too."""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
    INSERT INTO models(slug,price) VALUES ('image-fast',2),('video-long',25);
    INSERT INTO legs(model_slug,provider,priority,max_ref_images,skip_on_moderation) VALUES
        ('image-fast','alpha',10,4,false), ('image-fast','beta',20,10,false),
        ('video-long','beta',10,2,false), ('video-long','alpha',20,1,true);
    INSERT INTO users(id) VALUES(1);
    INSERT INTO ledger(user_id,delta,kind,operation_key) VALUES(1,100,'seed','seed:demo-user-1');
    CREATE TABLE fake_configs (
        provider text NOT NULL,
        scope text NOT NULL DEFAULT 'default',
        config jsonb NOT NULL,
        PRIMARY KEY(provider,scope)
    );
    CREATE TABLE fake_jobs (
        provider text NOT NULL,
        idempotency_key text NOT NULL,
        external_id text NOT NULL,
        request_hash text NOT NULL,
        payload jsonb NOT NULL,
        behavior jsonb NOT NULL,
        callback jsonb NOT NULL,
        due_at timestamptz NOT NULL,
        delivered_at timestamptz,
        lease_until timestamptz,
        lease_token uuid,
        deliveries integer NOT NULL DEFAULT 0,
        PRIMARY KEY(provider,idempotency_key),
        UNIQUE(provider,external_id)
    );
    CREATE INDEX fake_jobs_due ON fake_jobs(provider,due_at) WHERE delivered_at IS NULL;
    CREATE TABLE demo_runs (
        id uuid PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES users(id),
        task_id uuid REFERENCES tasks(id),
        scenario text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    """)


def downgrade():
    # Financial rows cannot be deleted. Downgrade needs an empty/demo DB; do not rewrite history.
    raise RuntimeError("Seed migration is irreversible. Restore a backup or recreate the demo database.")
