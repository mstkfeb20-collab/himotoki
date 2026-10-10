#!/bin/sh
# 当日予約を連携ファイル（CSV）に出力する
OUT_DIR=/data/tour/out
psql -h dbhost -U tour -d tour -A -F, -f /opt/tour/batch/sql/export_reserve.sql > ${OUT_DIR}/reserve_export.csv
exit $?
