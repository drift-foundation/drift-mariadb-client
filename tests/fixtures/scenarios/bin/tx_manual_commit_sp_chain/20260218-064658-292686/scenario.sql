-- Scenario: tx_manual_commit_sp_chain
-- Run: 20260218-064658-292686
-- Extracted from COM_QUERY packets in fixture capture.

SET autocommit=0;
CALL sp_1();
CALL sp_2();
COMMIT;
SET autocommit=1;
