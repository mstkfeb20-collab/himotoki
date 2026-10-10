package com.example.shipping;

import java.sql.Connection;
import java.sql.PreparedStatement;
import javax.annotation.Resource;
import javax.ws.rs.Consumes;
import javax.ws.rs.POST;
import javax.ws.rs.Path;
import javax.sql.DataSource;

/** 出荷依頼の受付（REST） */
@Path("/shipments")
public class ShipmentResource {

    @Resource(lookup = "jdbc/ShipDS")
    private DataSource ds;

    @POST
    @Consumes("application/json")
    public void create(ShipmentRequest req) throws Exception {
        try (Connection c = ds.getConnection();
             PreparedStatement ps = c.prepareStatement(
                     "INSERT INTO SHIPMENT (ORDER_ID, ITEM_ID, QTY, STATUS) VALUES (?, ?, ?, 'REQUESTED')")) {
            ps.setString(1, req.orderId);
            ps.setString(2, req.itemId);
            ps.setInt(3, req.qty);
            ps.executeUpdate();
        }
    }
}
