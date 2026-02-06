CREATE OR REPLACE FUNCTION aptour.sync_suggested_questions_seq()
RETURNS trigger AS $$
BEGIN
    PERFORM setval(
        pg_get_serial_sequence('aptour.suggested_questions', 'id'),
        GREATEST(
            (SELECT COALESCE(MAX(id), 0) FROM aptour.suggested_questions),
            nextval(pg_get_serial_sequence('aptour.suggested_questions', 'id'))
        )
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


CREATE TRIGGER trg_sync_suggested_questions_seq
AFTER INSERT ON aptour.suggested_questions
FOR EACH STATEMENT
EXECUTE FUNCTION aptour.sync_suggested_questions_seq();