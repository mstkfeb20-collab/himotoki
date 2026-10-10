package com.example.inventory;

import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import javax.annotation.Resource;
import javax.ejb.Stateless;
import javax.jws.WebMethod;
import javax.jws.WebParam;
import javax.jws.WebService;
import javax.sql.DataSource;

/** 在庫サービス（SOAP）。受注サーバから引当を受け付ける。 */
@Stateless
@WebService(serviceName = "InventoryService", portName = "InventoryPort", name = "InventoryPortType",
        targetNamespace = "http://example.com/inventory")
public class InventoryEndpoint {

    @Resource(lookup = "jdbc/InvDS")
    private DataSource ds;

    @WebMethod(operationName = "reserveStock")
    public String reserveStock(@WebParam(name = "itemId") String itemId, @WebParam(name = "qty") int qty,
                               @WebParam(name = "orderId") String orderId) {
        try (Connection c = ds.getConnection();
             PreparedStatement ps = c.prepareStatement("UPDATE STOCK SET QTY = QTY - ?, RESERVED = RESERVED + ? WHERE ITEM_ID = ?");
             CallableStatement cs = c.prepareCall("{call PKG_STOCK.SYNC_TO_ORDER(?)}")) {
            ps.setInt(1, qty);
            ps.setInt(2, qty);
            ps.setString(3, itemId);
            ps.executeUpdate();
            cs.setString(1, itemId);
            cs.execute();
            return "OK";
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @WebMethod(operationName = "releaseStock")
    public void releaseStock(@WebParam(name = "orderId") String orderId) {
        // 取消時の戻し（省略）
    }
}
