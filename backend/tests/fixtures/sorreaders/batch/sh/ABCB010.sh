#!/bin/ksh
. /app/batch/sh/common_func.sh
SEND_DIR=/data/send
OUT_FILE=${SEND_DIR}/SHOHIN_${YMD}.dat
java -cp $CP jp.co.acme.batch.ShohinExport
send_file $OUT_FILE HOSTB
