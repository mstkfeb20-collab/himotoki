#!/bin/sh
# 連携ファイルを読み、出発前日の予約者へ案内メールを送る
IN_FILE=/data/tour/out/reserve_export.csv
java -cp /opt/tour/lib/* org.terasoluna.tourreservation.batch.ReminderMailMain ${IN_FILE}
