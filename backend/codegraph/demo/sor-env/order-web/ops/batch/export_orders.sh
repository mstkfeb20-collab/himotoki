#!/bin/sh
# 確定受注をCSVに出力
OUT=/data/out/orders.csv
sqlplus -s order_app/${ORDER_PW}@ORDERDB @/app/batch/sql/export_orders.sql
cp /data/out/orders_work.csv /data/out/orders.csv
