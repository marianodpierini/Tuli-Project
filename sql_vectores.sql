CREATE EXTENSION IF NOT EXISTS vector;


ALTER TABLE aptour.suggested_questions
ADD COLUMN embedding vector(1024);


CREATE INDEX idx_suggested_questions_embedding
ON aptour.suggested_questions
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);