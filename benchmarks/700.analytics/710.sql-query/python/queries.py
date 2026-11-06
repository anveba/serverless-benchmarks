# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

# allowed table names, including intermediate results
INPUT_TABLES = {"customer", "orders", "lineitem", "supplier", "nation", "partials"}
PARTITION_KEY = "orderkey"

# tables used by each query
TABLES = {
    "q1": ["lineitem"],
    "q3": ["customer", "orders", "lineitem"],
    "q6": ["lineitem"],
    "q12": ["orders", "lineitem"],
    "q21": ["supplier", "lineitem", "orders", "nation"],
}

# TPC-H queries, default substitution parameters
QUERIES = {
    "q1": """
        SELECT
            l_returnflag,
            l_linestatus,
            sum(l_quantity) AS sum_qty,
            sum(l_extendedprice) AS sum_base_price,
            sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
            sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
            avg(l_quantity) AS avg_qty,
            avg(l_extendedprice) AS avg_price,
            avg(l_discount) AS avg_disc,
            count(*) AS count_order
        FROM lineitem
        WHERE l_shipdate <= CAST('1998-09-02' AS date)
        GROUP BY l_returnflag, l_linestatus
        ORDER BY l_returnflag, l_linestatus
    """,
    "q3": """
        SELECT
            l_orderkey,
            sum(l_extendedprice * (1 - l_discount)) AS revenue,
            o_orderdate,
            o_shippriority
        FROM customer, orders, lineitem
        WHERE c_mktsegment = 'BUILDING'
            AND c_custkey = o_custkey
            AND l_orderkey = o_orderkey
            AND o_orderdate < CAST('1995-03-15' AS date)
            AND l_shipdate > CAST('1995-03-15' AS date)
        GROUP BY l_orderkey, o_orderdate, o_shippriority
        ORDER BY revenue DESC, o_orderdate
        LIMIT 10
    """,
    "q6": """
        SELECT sum(l_extendedprice * l_discount) AS revenue
        FROM lineitem
        WHERE l_shipdate >= CAST('1994-01-01' AS date)
            AND l_shipdate < CAST('1995-01-01' AS date)
            AND l_discount BETWEEN 0.05 AND 0.07
            AND l_quantity < 24
    """,
    "q12": """
        SELECT
            l_shipmode,
            sum(CASE WHEN o_orderpriority = '1-URGENT' OR o_orderpriority = '2-HIGH'
                THEN 1 ELSE 0 END) AS high_line_count,
            sum(CASE WHEN o_orderpriority <> '1-URGENT' AND o_orderpriority <> '2-HIGH'
                THEN 1 ELSE 0 END) AS low_line_count
        FROM orders, lineitem
        WHERE o_orderkey = l_orderkey
            AND l_shipmode IN ('MAIL', 'SHIP')
            AND l_commitdate < l_receiptdate
            AND l_shipdate < l_commitdate
            AND l_receiptdate >= CAST('1994-01-01' AS date)
            AND l_receiptdate < CAST('1995-01-01' AS date)
        GROUP BY l_shipmode
        ORDER BY l_shipmode
    """,
    "q21": """
        SELECT
            s_name,
            count(*) AS numwait
        FROM supplier, lineitem l1, orders, nation
        WHERE s_suppkey = l1.l_suppkey
            AND o_orderkey = l1.l_orderkey
            AND o_orderstatus = 'F'
            AND l1.l_receiptdate > l1.l_commitdate
            AND EXISTS (
                SELECT *
                FROM lineitem l2
                WHERE l2.l_orderkey = l1.l_orderkey
                    AND l2.l_suppkey <> l1.l_suppkey)
            AND NOT EXISTS (
                SELECT *
                FROM lineitem l3
                WHERE l3.l_orderkey = l1.l_orderkey
                    AND l3.l_suppkey <> l1.l_suppkey
                    AND l3.l_receiptdate > l3.l_commitdate)
            AND s_nationkey = n_nationkey
            AND n_name = 'SAUDI ARABIA'
        GROUP BY s_name
        ORDER BY numwait DESC, s_name
        LIMIT 100
    """,
}

# stages of the parallel version, see scripts/sql_parallel.py
STAGES = {
    "q1.partial": """
        SELECT
            l_returnflag,
            l_linestatus,
            sum(l_quantity) AS sum_qty,
            sum(l_extendedprice) AS sum_base_price,
            sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
            sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
            sum(l_discount) AS sum_disc,
            count(*) AS count_order
        FROM lineitem
        WHERE l_shipdate <= CAST('1998-09-02' AS date)
        GROUP BY l_returnflag, l_linestatus
    """,
    "q1.final": """
        SELECT
            l_returnflag,
            l_linestatus,
            sum(sum_qty) AS sum_qty,
            sum(sum_base_price) AS sum_base_price,
            sum(sum_disc_price) AS sum_disc_price,
            sum(sum_charge) AS sum_charge,
            sum(sum_qty) / sum(count_order) AS avg_qty,
            sum(sum_base_price) / sum(count_order) AS avg_price,
            sum(sum_disc) / sum(count_order) AS avg_disc,
            sum(count_order) AS count_order
        FROM partials
        GROUP BY l_returnflag, l_linestatus
        ORDER BY l_returnflag, l_linestatus
    """,
    "q6.partial": QUERIES["q6"],
    "q6.final": "SELECT sum(revenue) AS revenue FROM partials",
    "q12.partition_lineitem": """
        SELECT l_orderkey::BIGINT AS orderkey, l_shipmode
        FROM lineitem
        WHERE l_shipmode IN ('MAIL', 'SHIP')
            AND l_commitdate < l_receiptdate
            AND l_shipdate < l_commitdate
            AND l_receiptdate >= CAST('1994-01-01' AS date)
            AND l_receiptdate < CAST('1995-01-01' AS date)
    """,
    "q12.partition_orders": """
        SELECT o_orderkey::BIGINT AS orderkey, o_orderpriority
        FROM orders
    """,
    "q12.join": """
        SELECT
            l_shipmode,
            sum(CASE WHEN o_orderpriority = '1-URGENT' OR o_orderpriority = '2-HIGH'
                THEN 1 ELSE 0 END) AS high_line_count,
            sum(CASE WHEN o_orderpriority <> '1-URGENT' AND o_orderpriority <> '2-HIGH'
                THEN 1 ELSE 0 END) AS low_line_count
        FROM orders JOIN lineitem USING (orderkey)
        GROUP BY l_shipmode
    """,
    "q12.final": """
        SELECT
            l_shipmode,
            sum(high_line_count) AS high_line_count,
            sum(low_line_count) AS low_line_count
        FROM partials
        GROUP BY l_shipmode
        ORDER BY l_shipmode
    """,
}
