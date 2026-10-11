#!/bin/sh
# 出荷サーバへ受注CSVを送る
sftp -b - shipuser@ship-svc <<EOF
put /data/out/orders.csv /recv/orders.csv
bye
EOF
