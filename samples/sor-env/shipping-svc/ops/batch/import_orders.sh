#!/bin/sh
java -cp /app/lib/shipping.jar com.example.shipping.ImportOrdersMain
mv /recv/orders.csv /recv/done/
