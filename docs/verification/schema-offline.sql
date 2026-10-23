BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001

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
);;

INSERT INTO alembic_version (version_num) VALUES ('0001') RETURNING alembic_version.version_num;

COMMIT;

BEGIN;

-- Running upgrade 0001 -> 0002

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
    );;

UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001';

COMMIT;

BEGIN;

-- Running upgrade 0002 -> 0003

CREATE FUNCTION check_task_finance() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE tid uuid; t tasks%ROWTYPE; charges bigint; refunds bigint; invalid bigint;
    BEGIN
        IF TG_TABLE_NAME='tasks' THEN tid=NEW.id; ELSE tid=NEW.task_id; END IF;
        IF tid IS NULL THEN RETURN NULL; END IF;
        SELECT * INTO t FROM tasks WHERE id=tid;
        IF NOT FOUND THEN RETURN NULL; END IF;
        SELECT count(*) FILTER(WHERE kind='charge'), count(*) FILTER(WHERE kind='refund'),
               count(*) FILTER(WHERE user_id<>t.user_id OR
                   (kind='charge' AND delta<>-t.price) OR (kind='refund' AND delta<>t.price))
        INTO charges,refunds,invalid FROM ledger WHERE task_id=tid;
        IF charges<>1 OR invalid<>0 OR
           (t.status='failed' AND refunds<>1) OR (t.status<>'failed' AND refunds<>0) THEN
            RAISE EXCEPTION 'Task/ledger invariant violated for %',tid;
        END IF;
        RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER task_finance AFTER INSERT OR UPDATE ON tasks
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_task_finance();
    CREATE CONSTRAINT TRIGGER ledger_task_finance AFTER INSERT ON ledger
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_task_finance();
    CREATE FUNCTION check_payment_finance() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE pid text; p payments%ROWTYPE; credits bigint; invalid bigint;
    BEGIN
        pid=NEW.payment_id;
        IF pid IS NULL THEN RETURN NULL; END IF;
        SELECT * INTO p FROM payments WHERE payment_id=pid;
        IF NOT FOUND THEN RETURN NULL; END IF;
        SELECT count(*),count(*) FILTER(WHERE user_id<>p.user_id OR delta<>p.tokens)
            INTO credits,invalid FROM ledger WHERE payment_id=pid;
        IF invalid<>0 OR (p.status='succeeded' AND credits<>1) OR
            (p.status<>'succeeded' AND credits<>0) THEN
            RAISE EXCEPTION 'Payment/ledger invariant violated for %',pid;
        END IF;
        RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER payment_finance AFTER INSERT OR UPDATE ON payments
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_payment_finance();
    CREATE CONSTRAINT TRIGGER ledger_payment_finance AFTER INSERT ON ledger
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_payment_finance();;

UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002';

COMMIT;

