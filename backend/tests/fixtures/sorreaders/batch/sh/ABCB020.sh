#!/bin/ksh
recv_file SHOHIN_${YMD}.dat HOSTA
sqlldr userid=$DBUSER control=/app/ctl/shohin.ctl
