-- Recreate appdb from scratch for local fixture/capture runs.
-- Usage:
--   mariadb -h 127.0.0.1 -P 34114 -u root -prootpw < tests/fixtures/appdb_schema.sql

DROP DATABASE IF EXISTS appdb;
CREATE DATABASE appdb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE appdb;

DELIMITER //

CREATE PROCEDURE sp_ping()
BEGIN
  SELECT 42 AS v;
END//

CREATE PROCEDURE sp_rows()
BEGIN
  SELECT 1 AS a
  UNION ALL
  SELECT 2 AS a;
END//

CREATE PROCEDURE sp_multi_rs()
BEGIN
  SELECT 1 AS a;
  SELECT 2 AS b;
END//

CREATE PROCEDURE sp_add(IN a INT, IN b INT)
BEGIN
  SELECT a + b AS s;
END//

-- Transaction scenario helpers:
-- sp_1/sp_2 mutate tx_demo, sp_error forces a deterministic SQL exception.
CREATE PROCEDURE sp_1()
BEGIN
  CREATE TABLE IF NOT EXISTS tx_demo (
    id INT AUTO_INCREMENT PRIMARY KEY,
    note VARCHAR(64) NOT NULL
  );
  INSERT INTO tx_demo(note) VALUES ('sp_1');
END//

CREATE PROCEDURE sp_2()
BEGIN
  INSERT INTO tx_demo(note) VALUES ('sp_2');
END//

CREATE PROCEDURE sp_error()
BEGIN
  SIGNAL SQLSTATE '45000'
    SET MYSQL_ERRNO = 1644,
        MESSAGE_TEXT = 'sp_error forced failure';
END//

-- Binary column regression test (mariadb-rpc-binary-column-utf8 defect).
CREATE TABLE tb_binary_test (
  id INT PRIMARY KEY,
  raw_key BINARY(16) NOT NULL
)//

INSERT INTO tb_binary_test (id, raw_key)
VALUES (1, UNHEX('5D41402ABC4B2A76B9719D911017C592'))//

CREATE PROCEDURE sp_get_binary(IN arg_id INT)
READS SQL DATA
BEGIN
  SELECT id, raw_key FROM tb_binary_test WHERE id = arg_id;
END//

DELIMITER ;
