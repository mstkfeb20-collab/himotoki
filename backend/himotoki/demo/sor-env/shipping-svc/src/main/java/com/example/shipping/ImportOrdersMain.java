package com.example.shipping;

import java.io.BufferedReader;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;

/** 受注サーバから届いた受注CSVを取り込む（ファイル監視ジョブの後続） */
public class ImportOrdersMain {

    private static final String INPUT = "/recv/orders.csv";

    public static void main(String[] args) throws Exception {
        try (BufferedReader r = Files.newBufferedReader(Paths.get(INPUT));
             Connection c = DriverManager.getConnection(System.getenv("SHIP_DB_URL"));
             PreparedStatement ps = c.prepareStatement(
                     "INSERT INTO SHIPMENT_REQ (ORDER_ID, CUSTOMER_ID, NOTE) VALUES (?, ?, ?)")) {
            String line;
            while ((line = r.readLine()) != null) {
                String[] f = line.split(",", -1);
                ps.setString(1, f[0]);
                ps.setString(2, f[1]);
                ps.setString(3, f[2]);
                ps.addBatch();
            }
            ps.executeBatch();
        }
    }
}
