"""A terminal state and its financial effect must commit together, even after a code regression."""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None


def upgrade():
    op.execute(r"""
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
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_payment_finance();
    """)


def downgrade():
    op.execute("""
    DROP TRIGGER task_finance ON tasks;
    DROP TRIGGER ledger_task_finance ON ledger;
    DROP TRIGGER payment_finance ON payments;
    DROP TRIGGER ledger_payment_finance ON ledger;
    DROP FUNCTION check_task_finance(),check_payment_finance();
    """)
