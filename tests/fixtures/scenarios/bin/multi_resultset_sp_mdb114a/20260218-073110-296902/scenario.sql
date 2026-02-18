-- Scenario: multi_resultset_sp_mdb114a
-- Run: 20260218-073110-296902
-- Extracted from COM_QUERY packets in fixture capture.

SET autocommit=0;
CALL sp_multi_rs();
SET autocommit=1;
