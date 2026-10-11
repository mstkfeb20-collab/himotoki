package com.example.order.client;

import javax.ws.rs.client.Client;
import javax.ws.rs.client.ClientBuilder;
import javax.ws.rs.client.Entity;
import javax.ws.rs.core.MediaType;

/** 出荷サーバの REST API を呼ぶ */
public class ShippingClient {

    private final String baseUrl = Config.get("shipping.api.url");

    public void requestShipment(String orderId, String itemId, int qty) {
        Client client = ClientBuilder.newClient();
        client.target(baseUrl).path("/shipments")
              .request(MediaType.APPLICATION_JSON)
              .post(Entity.json("{\"orderId\":\"" + orderId + "\",\"itemId\":\"" + itemId + "\",\"qty\":" + qty + "}"));
    }
}
