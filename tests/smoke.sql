-- Smoke test: the SQL subset, vector TopK, transactions and persistence.
CREATE TABLE docs (id BIGINT PRIMARY KEY, title VARCHAR(64), category VARCHAR(16), embedding VECTOR(4));
INSERT INTO docs VALUES (1,'alpha','a','[1,0,0,0]'),(2,'beta','a','[0,1,0,0]'),(3,'gamma','b','[0,0,1,0]');
SELECT * FROM docs;
SELECT id, title FROM docs WHERE category = 'a' ORDER BY id;
SELECT id, L2_DISTANCE(embedding,'[1,0,0,0]') AS d FROM docs ORDER BY L2_DISTANCE(embedding,'[1,0,0,0]') ASC LIMIT 2;
SELECT id FROM docs WHERE category = 'a' ORDER BY L2_DISTANCE(embedding,'[1,0,0,0]') LIMIT 10;
BEGIN;
INSERT INTO docs VALUES (4,'delta','b','[0,0,0,1]');
SELECT id FROM docs ORDER BY id;
ROLLBACK;
SELECT id FROM docs ORDER BY id;
UPDATE docs SET title = 'ALPHA' WHERE id = 1;
DELETE FROM docs WHERE id = 3;
SELECT id, title, category FROM docs ORDER BY id;
CREATE VECTOR INDEX idx_emb ON docs(embedding);
SELECT id FROM docs ORDER BY L2_DISTANCE(embedding,'[1,0,0,0]') LIMIT 3;
