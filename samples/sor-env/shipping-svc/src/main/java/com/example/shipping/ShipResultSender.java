package com.example.shipping;

import javax.annotation.Resource;
import javax.jms.JMSContext;
import javax.jms.Queue;
import javax.inject.Inject;

/** 出荷結果を受注サーバのキューへ送る（JMS）。あわせて結果ファイルも出力する。 */
public class ShipResultSender {

    @Inject
    private JMSContext jms;

    @Resource(lookup = "jms/ShipResultQueue")
    private Queue shipResultQueue;

    public void send(String orderId, String status) throws Exception {
        jms.createProducer().send(shipResultQueue, orderId + "," + status);
        java.nio.file.Files.write(java.nio.file.Paths.get("/send/ship_result.csv"),
                (orderId + "," + status + "\n").getBytes(), java.nio.file.StandardOpenOption.APPEND,
                java.nio.file.StandardOpenOption.CREATE);
    }
}
