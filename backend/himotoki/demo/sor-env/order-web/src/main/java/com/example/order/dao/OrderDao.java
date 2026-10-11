package com.example.order.dao;

import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.ArrayList;
import java.util.List;
import javax.annotation.Resource;
import javax.sql.DataSource;

/** 受注DBアクセス（JDBC直書き） */
public class OrderDao {

    private static final String INSERT_ORDER =
            "INSERT INTO ORDERS (ORDER_ID, CUSTOMER_ID, STATUS, NOTE, ORDERED_AT) VALUES (?, ?, 'NEW', ?, SYSDATE)";
    private static final String INSERT_ITEM =
            "INSERT INTO ORDER_ITEMS (ORDER_ID, ITEM_ID, QTY) VALUES (?, ?, ?)";
    private static final String UPDATE_STATUS = "UPDATE ORDERS SET STATUS = ? WHERE ORDER_ID = ?";
    private static final String UPDATE_SHIP_STATUS = "UPDATE ORDERS SET SHIP_STATUS = ? WHERE ORDER_ID = ?";
    private static final String LOCK_ORDER = "SELECT STATUS FROM ORDERS WHERE ORDER_ID = ? FOR UPDATE";
    private static final String INSERT_AUDIT = "INSERT INTO AUDIT_LOG (ORDER_ID, EVENT, LOGGED_AT) VALUES (?, ?, SYSDATE)";
    private static final String FIND_OPEN = "SELECT ORDER_ID FROM ORDERS WHERE STATUS = 'CONFIRMED' AND TRUNC(ORDERED_AT) = TO_DATE(?, 'YYYYMMDD')";

    @Resource(lookup = "jdbc/OrderDS")
    private DataSource ds;

    public String nextOrderId() {
        return query("SELECT ORDER_SEQ.NEXTVAL FROM DUAL");
    }

    public void insertOrder(String orderId, String customerId, String note) {
        update(INSERT_ORDER, orderId, customerId, note);
    }

    public void insertItem(String orderId, String itemId, int qty) {
        update(INSERT_ITEM, orderId, itemId, qty);
    }

    public void updateStatus(String orderId, String status) {
        update(UPDATE_STATUS, status, orderId);
    }

    public void updateShipStatus(String orderId, String status) {
        update(UPDATE_SHIP_STATUS, status, orderId);
    }

    public void lockOrder(String orderId) {
        query(LOCK_ORDER, orderId);
    }

    public void insertAudit(String orderId, String event) {
        update(INSERT_AUDIT, orderId, event);
    }

    public List<String> findOpenOrders(String businessDate) {
        List<String> out = new ArrayList<>();
        out.add(query(FIND_OPEN, businessDate));
        return out;
    }

    /** 受注確定（ストアド） */
    public void confirmOrder(String orderId) {
        try (Connection c = ds.getConnection();
             CallableStatement cs = c.prepareCall("{call PKG_ORDER.CONFIRM_ORDER(?)}")) {
            cs.setString(1, orderId);
            cs.execute();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private void update(String sql, Object... args) {
        try (Connection c = ds.getConnection(); PreparedStatement ps = c.prepareStatement(sql)) {
            for (int i = 0; i < args.length; i++) ps.setObject(i + 1, args[i]);
            ps.executeUpdate();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private String query(String sql, Object... args) {
        try (Connection c = ds.getConnection(); PreparedStatement ps = c.prepareStatement(sql)) {
            for (int i = 0; i < args.length; i++) ps.setObject(i + 1, args[i]);
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() ? rs.getString(1) : null;
            }
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
