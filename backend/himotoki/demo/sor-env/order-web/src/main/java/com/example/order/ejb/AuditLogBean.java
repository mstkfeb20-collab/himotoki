package com.example.order.ejb;

import javax.ejb.Stateless;
import javax.ejb.TransactionAttribute;
import javax.ejb.TransactionAttributeType;
import javax.inject.Inject;
import com.example.order.dao.OrderDao;

/** 監査ログ。呼び出し元が失敗しても記録を残すため独立トランザクション。 */
@Stateless
public class AuditLogBean {

    @Inject
    private OrderDao orderDao;

    @TransactionAttribute(TransactionAttributeType.REQUIRES_NEW)
    public void write(String orderId, String event) {
        orderDao.insertAudit(orderId, event);
    }
}
