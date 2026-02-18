-- Scenario: tx_manual_rollback_sp_chain
-- Run: 20260218-065505-293776
-- Extracted from COM_QUERY packets in fixture capture.

SET autocommit=0;
CALL sp_1();
CALL sp_2();
ROLLBACK;
SET autocommit=1;
