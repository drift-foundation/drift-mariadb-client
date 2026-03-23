# mariadb-rpc: BINARY/VARBINARY result columns fail with UTF-8 validation error

## Summary

`rpc.next_event()` fails with `utf8-invalid-leading-byte` when a result set
contains BINARY or VARBINARY columns with non-UTF-8 byte sequences. The error
occurs during row decoding inside the RPC layer, before the caller has any
opportunity to read the data.

## Severity

High. Any stored procedure that returns BINARY/VARBINARY columns containing
raw byte data (UUIDs, hashes, keys, encrypted blobs) is unusable through
mariadb-rpc.

## Reproduction

### Prerequisites

- MariaDB on 127.0.0.1:13306 with root/scratchpad22
- A database with a stored procedure that returns BINARY columns

### Schema setup

```sql
CREATE DATABASE IF NOT EXISTS rpc_defect_test;
USE rpc_defect_test;

CREATE TABLE IF NOT EXISTS tb_binary_test (
    id INT PRIMARY KEY,
    raw_key BINARY(16) NOT NULL
);

INSERT IGNORE INTO tb_binary_test (id, raw_key)
VALUES (1, UNHEX('5D41402ABC4B3A76B9719D911017C592'));

DELIMITER $$
CREATE OR REPLACE PROCEDURE sp_get_binary(IN arg_id INT)
READS SQL DATA
BEGIN
    SELECT id, raw_key FROM tb_binary_test WHERE id = arg_id;
END $$
DELIMITER ;
```

### Drift test code

```drift
module repro;

import std.core as core;
import std.console as con;
import mariadb.rpc as rpc;

fn run() -> Int {
    var b = rpc.new_connection_config_builder();
    b.with_host("127.0.0.1");
    b.with_port(13306);
    b.with_user("root");
    b.with_password("scratchpad22");
    b.with_database("rpc_defect_test");
    b.with_connect_timeout_ms(3000);
    b.with_read_timeout_ms(3000);
    b.with_write_timeout_ms(3000);
    match rpc.build_connection_config(move b) {
        core.Result::Err(_) => { return 10; },
        core.Result::Ok(cfg) => {
            match rpc.connect(move cfg) {
                core.Result::Err(_) => { return 20; },
                core.Result::Ok(conn) => {
                    var c = move conn;
                    var args = rpc.new_args();
                    args.push(rpc.arg_int(1));
                    match c.call(&"sp_get_binary", &args) {
                        core.Result::Err(e) => {
                            con.println("call error");
                            return 30;
                        },
                        core.Result::Ok(stmt) => {
                            var s = move stmt;
                            match rpc.next_event(&mut s) {
                                core.Result::Err(e) => {
                                    // THIS IS THE BUG: fails here
                                    con.println("event error - expected row, got UTF-8 error");
                                    return 40;
                                },
                                core.Result::Ok(ev) => {
                                    con.println("got event ok");
                                }
                            }
                            match rpc.skip_remaining(&mut s) { default => {} }
                            match rpc.commit(&mut c) { default => {} }
                            val _ = rpc.close(&mut c);
                            return 0;
                        }
                    }
                }
            }
        }
    }
}

pub fn main() nothrow -> Int {
    return try run() catch { 99 };
}
```

### Expected behavior

`rpc.next_event()` returns `RpcEvent::Row` containing the result. The caller
then reads the BINARY column via an appropriate accessor.

### Actual behavior

`rpc.next_event()` returns `Result::Err` with message `utf8-invalid-leading-byte`.
The row is never delivered to the caller.

## Root cause (suspected)

The RPC layer (or the underlying wire-proto layer) decodes all result set cell
data as UTF-8 strings during row construction, regardless of column type. For
BINARY/VARBINARY columns, the server correctly sends raw bytes with
`character_set = 63` (binary collation) in the column metadata. The client
ignores this metadata and applies UTF-8 validation uniformly, which fails on
any byte sequence that isn't valid UTF-8.

## Impact on Singular port

The Singular idempotency library stores raw 16-byte UUIDs in BINARY(16) and
VARBINARY(32) columns. All 7 stored procedures (`sp_singular_try_claim`,
`sp_singular_reclaim`, `sp_singular_complete`, `sp_singular_fail`,
`sp_singular_renew`, `sp_singular_inspect`, `sp_singular_history`) return
BINARY columns in their result sets. The Singular Drift client is blocked
until this is fixed.

## Suggested fix

During row decoding, check the column metadata `character_set` field. If it is
63 (binary), store the cell data as raw bytes without UTF-8 validation. Provide
a `get_bytes(col)` accessor on `RpcRow` that returns `Result<Array<Byte>, RpcError>`
for reading binary column data. `get_string(col)` can continue to validate
UTF-8 for text columns.

## Workaround

None known. The error occurs inside `next_event()` before the caller can
access any data. Hex-encoding binary values before storage would work around
the read path but would break compatibility with existing data written by the
Java Singular client.

## Environment

- mariadb-rpc 0.1.3
- mariadb-wire-proto 0.1.3
- driftc 0.27.103+abi6
- MariaDB 11.8.x
