#!/bin/sh
# 出荷サーバから出荷結果を取得
ftp -n ship-svc <<EOF
user shipuser ${SHIP_PW}
binary
get /send/ship_result.csv /data/in/ship_result.csv
bye
EOF
