"""Keep the ledger, task state and durable delivery in one PostgreSQL transaction."""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = depends_on = None

DDL = r"""
CREATE TABLE users (
    id bigint PRIMARY KEY CHECK (id > 0),
    balance bigint NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE models (slug text PRIMARY KEY, price bigint NOT NULL CHECK (price > 0));
CREATE TABLE legs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_slug text NOT NULL REFERENCES models(slug),
    provider text NOT NULL CHECK(provider IN ('alpha','beta')),
    priority integer NOT NULL CHECK(priority >= 0),
    max_ref_images integer NOT NULL CHECK(max_ref_images >= 0),
    skip_on_moderation boolean NOT NULL DEFAULT false,
    UNIQUE(model_slug, priority)
);
CREATE TABLE tasks (
    id uuid PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    model_slug text NOT NULL REFERENCES models(slug),
    price bigint NOT NULL CHECK(price > 0),
    prompt text NOT NULL,
    ref_images jsonb NOT NULL,
    legs jsonb NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','failed')),
    current_index integer NOT NULL DEFAULT -1,
    result_url text,
    error_type text CHECK(error_type IN ('technical','moderation','payload')),
    request_key text,
    request_hash text NOT NULL,
    request_id text,
    scenario_key text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE(user_id, request_key),
    CHECK((status = 'completed') = (result_url IS NOT NULL)),
    CHECK((status = 'active') = (finished_at IS NULL))
);
CREATE INDEX tasks_active_user ON tasks(user_id) WHERE status='active';
CREATE TABLE attempts (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES tasks(id),
    leg_index integer NOT NULL,
    provider text NOT NULL CHECK(provider IN ('alpha','beta')),
    status text NOT NULL CHECK(status IN ('queued','sending','waiting','completed','failed','timed_out')),
    external_id text,
    error_type text CHECK(error_type IN ('technical','moderation','payload')),
    error_detail text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    deadline timestamptz,
    finished_at timestamptz,
    UNIQUE(task_id, leg_index),
    UNIQUE(provider, external_id)
);
CREATE UNIQUE INDEX one_live_attempt ON attempts(task_id)
    WHERE status IN ('queued','sending','waiting');
CREATE INDEX attempts_expired ON attempts(deadline) WHERE status IN ('sending','waiting');
CREATE TABLE outbox (
    attempt_id uuid PRIMARY KEY REFERENCES attempts(id),
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done')),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    lease_token uuid,
    deliveries integer NOT NULL DEFAULT 0
);
CREATE INDEX outbox_pending ON outbox(available_at, lease_until) WHERE state='pending';
CREATE TABLE payments (
    payment_id text PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    tokens bigint NOT NULL CHECK(tokens > 0),
    status text NOT NULL CHECK(status IN ('pending','failed','succeeded')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE ledger (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    delta bigint NOT NULL CHECK(delta <> 0),
    kind text NOT NULL CHECK(kind IN ('seed','payment','charge','refund')),
    operation_key text NOT NULL UNIQUE,
    task_id uuid REFERENCES tasks(id),
    payment_id text REFERENCES payments(payment_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((kind='charge' AND delta<0) OR (kind<>'charge' AND delta>0)),
    CHECK ((kind IN ('charge','refund')) = (task_id IS NOT NULL)),
    CHECK ((kind='payment') = (payment_id IS NOT NULL))
);
CREATE UNIQUE INDEX ledger_task_once ON ledger(task_id,kind) WHERE task_id IS NOT NULL;
CREATE UNIQUE INDEX ledger_payment_once ON ledger(payment_id) WHERE payment_id IS NOT NULL;
CREATE INDEX ledger_user ON ledger(user_id,id DESC);
CREATE FUNCTION apply_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE users SET balance=balance+NEW.delta WHERE id=NEW.user_id;
    RETURN NEW;
END $$;
-- AFTER, not BEFORE: a conflicting insert must not change the balance.
CREATE TRIGGER ledger_apply AFTER INSERT ON ledger FOR EACH ROW EXECUTE FUNCTION apply_ledger();
CREATE FUNCTION guard_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.balance<>0 THEN RAISE EXCEPTION 'Initial balance must enter through ledger'; END IF;
    ELSIF NEW.balance IS DISTINCT FROM OLD.balance AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'Balance may only be changed by the ledger';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER users_balance_guard BEFORE INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION guard_balance();
CREATE FUNCTION immutable_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Ledger is append-only';
END $$;
CREATE TRIGGER ledger_immutable BEFORE UPDATE OR DELETE ON ledger
    FOR EACH ROW EXECUTE FUNCTION immutable_ledger();
CREATE FUNCTION check_balance_sum() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE uid bigint; actual bigint; expected numeric;
BEGIN
    IF TG_TABLE_NAME='users' THEN uid=NEW.id; ELSE uid=NEW.user_id; END IF;
    SELECT balance INTO actual FROM users WHERE id=uid;
    SELECT COALESCE(SUM(delta),0) INTO expected FROM ledger WHERE user_id=uid;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'Ledger/balance mismatch for user %',uid;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER users_balance_sum AFTER INSERT OR UPDATE ON users
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_balance_sum();
CREATE CONSTRAINT TRIGGER ledger_balance_sum AFTER INSERT ON ledger
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_balance_sum();
CREATE TABLE task_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES tasks(id),
    attempt_id uuid REFERENCES attempts(id),
    kind text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX events_task ON task_events(task_id,id);
CREATE TABLE webhook_receipts (
    provider text NOT NULL,
    body_hash text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES attempts(id),
    disposition text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(provider,body_hash)
);
"""

def upgrade():
    op.execute(DDL)


def downgrade():
    op.execute("""
    DROP TABLE webhook_receipts, task_events, ledger, payments, outbox, attempts, tasks, legs, models, users CASCADE;
    DROP FUNCTION apply_ledger(),guard_balance(),immutable_ledger(),check_balance_sum();
    """)
