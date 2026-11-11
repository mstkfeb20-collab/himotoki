send_file() {
  ftp -n $2 <<END
put $1
END
}
