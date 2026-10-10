package com.example.order.ejb;

import java.util.List;
import javax.annotation.Resource;
import javax.ejb.Stateless;
import javax.ejb.TransactionManagement;
import javax.ejb.TransactionManagementType;
import javax.inject.Inject;
import javax.transaction.UserTransaction;
import com.example.order.dao.OrderDao;

/** 日次締め。Bean管理トランザクションで 100 件ごとにコミットする。 */
@Stateless
@TransactionManagement(TransactionManagementType.BEAN)
public class OrderBatchBean {

    private static final int COMMIT_INTERVAL = 100;

    @Resource
    private UserTransaction utx;

    @Inject
    private OrderDao orderDao;

    public void closeDay(String businessDate) throws Exception {
        List<String> ids = orderDao.findOpenOrders(businessDate);
        utx.begin();
        int n = 0;
        for (String id : ids) {
            orderDao.updateStatus(id, "CLOSED");
            if (++n % COMMIT_INTERVAL == 0) {
                utx.commit();
                utx.begin();
            }
        }
        utx.commit();
    }
}
