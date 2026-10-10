#!/bin/sh
# 取消済みの古い予約を削除する
psql -h dbhost -U tour -d tour <<EOF
DELETE FROM reserve WHERE transfer = '9' AND reserved_day < CURRENT_DATE - 365;
EOF
