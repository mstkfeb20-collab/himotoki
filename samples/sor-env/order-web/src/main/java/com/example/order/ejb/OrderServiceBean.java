package com.example.order.ejb;

import javax.ejb.EJB;
import javax.ejb.Stateless;
import javax.ejb.TransactionAttribute;
import javax.ejb.TransactionAttributeType;
import javax.inject.Inject;
import com.example.order.client.InventoryPortType;
import com.example.order.client.ShippingClient;
import com.example.order.dao.OrderDao;

/** 受注の確定。コンテナ管理トランザクション（既定 REQUIRED）。 */
@Stateless
@TransactionAttribute(TransactionAttributeType.REQUIRED)
public class OrderServiceBean implements OrderService {

    @Inject
    private OrderDao orderDao;

    @Inject
    private InventoryPortType inventoryPort;

    @Inject
    private ShippingClient shippingClient;

    @EJB
    private AuditLogBean auditLog;

    @Override
    public String placeOrder(String customerId, String itemId, int qty, String note) {
        String orderId = orderDao.nextOrderId();
        orderDao.insertOrder(orderId, customerId, note);
        orderDao.insertItem(orderId, itemId, qty);
        // 在庫サーバに引当を依頼（SOAP）。このトランザクションが後でロールバックしても取り消されない
        inventoryPort.reserveStock(itemId, qty, orderId);
        // 監査ログは独立したトランザクション（REQUIRES_NEW）
        auditLog.write(orderId, "PLACED");
        // 確定処理はストアド（内部で COMMIT している）
        orderDao.confirmOrder(orderId);
        // 出荷サーバへ出荷依頼（REST）
        shippingClient.requestShipment(orderId, itemId, qty);
        return orderId;
    }

    @Override
    public void cancelOrder(String orderId) {
        orderDao.lockOrder(orderId);
        orderDao.updateStatus(orderId, "CANCELLED");
        auditLog.write(orderId, "CANCELLED");
    }
}
