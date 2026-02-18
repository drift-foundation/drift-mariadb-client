-- Scenario: tx_error_then_rollback
-- Run: 20260218-071006-294852
-- Extracted from COM_QUERY packets in fixture capture.

show databases;
show tables;
select @@version_comment limit 1;
SET autocommit=0;
CALL sp_1();
CALL sp_error();
ROLLBACK;
SET autocommit=1;
