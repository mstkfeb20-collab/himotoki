#!/bin/sh
sqlldr order_app/${ORDER_PW}@ORDERDB control=/app/batch/ctl/ship_result.ctl data=/data/in/ship_result.csv
